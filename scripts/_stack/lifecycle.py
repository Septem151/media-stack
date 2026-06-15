"""Lifecycle orchestration: up / down / restart / setup. `up` is the one-command
bring-up (run as your normal user); it auto-escalates host setup on first run, then
renders, brings the stack up, wires the apps, and seeds Jellyfin post-boot."""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

from . import host, lib, render, wiring

ENTRY = lib.REPO_ROOT / "scripts" / "stack"


def up(env: dict[str, str], assume_yes: bool = False) -> None:
    """Bring the stack up; auto-runs host setup on first run."""
    if os.geteuid() == 0:
        raise lib.StackError("run `stack up` as your normal user, not with sudo.")

    if not _host_ready():
        lib.step("first run: host setup needs sudo (users, ownership, firewall)")
        lib.run(["sudo", str(ENTRY), *(["--yes"] if assume_yes else []), "setup"])
        env = lib.load_env()

    render.fill_keys(env)
    env = lib.load_env()  # pick up freshly generated keys
    render.ensure_dirs(env)
    render.seed_configs(env)
    render.render_nginx(env)
    render.lan_selfsigned_cert()
    render.reclaim_certbot_tree()
    render.place_dummy_cert_if_missing(env)

    lib.step("bringing the stack up")
    lib.compose("up", "-d")

    _pihole_password()
    render.obtain_or_renew_cert(env, assume_yes)
    render.reload_nginx()
    wiring.wire(env)
    _jellyfin_seed(env)
    _final_banner(env)


def down(
    clean: bool = False,
    certs: bool = False,
    pihole: bool = False,
    assume_yes: bool = False,
) -> None:
    """Stop and remove containers; with `clean`, wipe regenerable state to re-seed."""
    lib.step("stopping and removing containers + network")
    lib.compose("down")
    if not clean:
        return

    # appdata holds service/root-owned files and certs holds root-owned certbot
    # output, so the wipe needs root.
    lib.require_root()
    extras = ["certs"] if certs else []
    extras += ["Pi-hole data"] if pihole else []
    scope = "appdata + rendered nginx" + ("".join(f" + {e}" for e in extras))
    if not lib.confirm(
        f"wipe regenerable state ({scope})? media + downloads are kept", assume_yes
    ):
        lib.warn("clean aborted.")
        return

    lib.info("wiping regenerable state...")
    # Keep appdata/ itself so its setgid + ACLs are inherited by what `up` recreates.
    for d in (
        "qbittorrent",
        "sonarr",
        "radarr",
        "prowlarr",
        "jellyfin",
        "recyclarr",
        "slskd",
        "picard",
    ):
        shutil.rmtree(lib.APPDATA / d, ignore_errors=True)
    if pihole:
        shutil.rmtree(lib.APPDATA / "pihole", ignore_errors=True)
        shutil.rmtree(lib.APPDATA / "ddclient", ignore_errors=True)
    else:
        lib.info("preserving appdata/pihole + appdata/ddclient (use --pihole to wipe)")
    shutil.rmtree(render.CONF_D, ignore_errors=True)
    if certs:
        lib.info("wiping certs/ (next `up` requests a fresh LE certificate)...")
        shutil.rmtree(lib.CERTS, ignore_errors=True)
    else:
        lib.info("keeping certs/ — next `up` renews the existing certificate.")
    lib.ok("clean. DATA_ROOT (media + downloads) untouched. Next: ./scripts/stack up")


def restart(env: dict[str, str], assume_yes: bool = False) -> None:
    """down + up (keeps data)."""
    down(assume_yes=assume_yes)
    up(env, assume_yes)


def setup(
    env: dict[str, str], only: str | None = None, assume_yes: bool = False
) -> None:
    """Run host prep (users/ownership/ACLs + firewall + host resolver)."""
    host.setup(env, only, assume_yes)


def status(env: dict[str, str]) -> None:
    """Read-only: the setup-step checklist + `docker compose ps`."""
    host_name = env["JELLYFIN_HOSTNAME"]
    lib.step("setup")
    lib.mark("host setup (media group, ufw-docker)", _host_ready())
    lib.mark(
        "config rendered (nginx + appdata)",
        (render.CONF_D / "default.conf").exists()
        and (lib.APPDATA / "sonarr/config.xml").exists(),
    )
    lib.mark(
        "API keys filled",
        all(
            env.get(k) for k in ("PROWLARR_API_KEY", "SONARR_API_KEY", "RADARR_API_KEY")
        ),
    )
    lib.mark("LAN self-signed cert", (lib.CERTS / "selfsigned/lan.crt").exists())
    le_issued = (render.LE / "renewal" / f"{host_name}.conf").exists()
    if render.CERT_RETRY_FILE.exists():
        detail = (
            "rate-limit hold-off until "
            + render.CERT_RETRY_FILE.read_text(encoding="utf-8").strip()
        )
    else:
        detail = "" if le_issued else "using self-signed placeholder"
    lib.mark("Let's Encrypt cert issued", le_issued, detail)
    lib.mark("Jellyfin seeded", (lib.APPDATA / "jellyfin/.mediastack-seeded").exists())
    lib.mark(
        "Pi-hole password set",
        (lib.APPDATA / "pihole/etc-pihole/.password-configured").exists(),
    )
    lib.mark("slskd configured", (lib.APPDATA / "slskd/slskd.yml").exists())

    lib.step("containers")
    lib.compose("ps", check=False)


def logs(services: list[str], follow: bool = False) -> None:
    """Show container logs (all or named services); -f/--follow streams until Ctrl-C."""
    lib.compose("logs", *(["-f"] if follow else []), *services, check=False)


# --- helpers ----------------------------------------------------------------
def _host_ready() -> bool:
    """True once first-run host setup is done (media group + ufw-docker present)."""
    return lib.succeeds(["getent", "group", "media"]) and os.access(
        "/usr/local/bin/ufw-docker", os.X_OK
    )


def _pihole_password() -> None:
    """Pi-hole starts unauthenticated — prompt now to keep that window short."""
    marker = lib.APPDATA / "pihole/etc-pihole/.password-configured"
    if marker.exists():
        return
    lib.poll(
        lambda: lib.succeeds(["docker", "exec", "pihole", "pihole", "status"]),
        attempts=30,
        delay=2,
        desc="Pi-hole to initialize",
    )
    lib.info(
        "Pi-hole has no password set — enter one now (Ctrl-C to skip, set it later):"
    )
    try:
        r = lib.run(
            ["docker", "exec", "-it", "pihole", "pihole", "setpassword"], check=False
        )
        if r.returncode == 0:
            marker.touch()
            return
    except KeyboardInterrupt:
        pass
    lib.warn("password setup skipped — run: docker exec pihole pihole setpassword")


def _jellyfin_seed(env: dict[str, str]) -> None:
    """Drop in system/network/NFO config after Jellyfin's first boot (they crash a
    fresh-DB import if pre-seeded). Marker-guarded so a later run never clobbers a
    server you've since set up. Written via `sg media` so any media-group member can
    write without a re-login."""
    marker = lib.APPDATA / "jellyfin/.mediastack-seeded"
    if marker.exists():
        return
    url = f"http://{env['LAN_IP']}:{env['JELLYFIN_PORT']}/System/Info/Public"
    lib.poll(
        lambda: lib.http(url)[0] == 200,
        attempts=40,
        delay=3,
        desc="Jellyfin to initialize its database",
    )
    lib.compose("stop", "jellyfin")
    jf, seed = lib.APPDATA / "jellyfin", lib.SEED / "jellyfin/config"
    _sg_write(
        jf / "system.xml",
        lib.render_str(
            (seed / "system.xml").read_text(encoding="utf-8"),
            {"JELLYFIN_SERVER_NAME": env["JELLYFIN_SERVER_NAME"]},
        ),
    )
    _sg_write(
        jf / "network.xml",
        lib.render_str(
            (seed / "network.xml").read_text(encoding="utf-8"),
            {"JELLYFIN_HOSTNAME": env["JELLYFIN_HOSTNAME"]},
        ),
    )
    _sg_write(
        jf / "xbmcmetadata.xml",
        (seed / "xbmcmetadata.xml").read_text(encoding="utf-8"),
    )
    _sg(f"touch {shlex.quote(str(marker))}")
    lib.compose("start", "jellyfin")
    lib.ok("seeded Jellyfin system + network + NFO config (server name, KnownProxies).")


def _sg_write(dest: Path, content: str) -> None:
    _sg(f"cat > {shlex.quote(str(dest))}", content)


def _sg(command: str, input_text: str | None = None) -> None:
    lib.run(["sg", "media", "-c", command], input_text=input_text)


def _final_banner(env: dict[str, str]) -> None:
    host_name, lan = env["JELLYFIN_HOSTNAME"], env["LAN_IP"]
    lib.step("done")
    lib.info(
        f"Jellyfin first-run: create your admin user + libraries at "
        f"https://{host_name}  (or http://{lan}:{env['JELLYFIN_PORT']})"
    )
    lib.info(
        f"Pi-hole admin UI: https://pihole.lan  "
        f"(or http://{lan}:{env['PIHOLE_WEBUI_PORT']}/admin)"
    )
    lib.info(
        "Set the Pi-hole password later with: docker exec pihole pihole setpassword"
    )

    # Surface qBittorrent's auto-generated temporary WebUI password.
    needle = "temporary password is provided for this session:"
    pw = ""
    for line in lib.output(["docker", "logs", "qbittorrent"]).splitlines():
        if needle in line:
            pw = line.split(needle)[-1].strip()
    if pw:
        lib.info(
            f"qBittorrent WebUI: https://qbittorrent.lan  "
            f"(or http://{lan}:{env['QBIT_WEBUI_PORT']}) — username 'admin'"
        )
        lib.info(f"   Temporary password (regenerated each restart): {pw}")
        lib.info("   Set a permanent one in Tools > Options > Web UI > Authentication.")
