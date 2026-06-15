"""Bring-up rendering: create dirs, auto-fill API keys/RENDER_GID, render seed +
nginx config (scoped ${VAR} substitution), and manage TLS certs — the self-signed
LAN cert, the certbot ownership reclaim, and Let's Encrypt issue/renew."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path

from . import lib

CONF_D = lib.NGINX / "conf.d"
LE = lib.CERTS / "letsencrypt"
CERT_RETRY_FILE = LE / ".cert-retry-after"


# --- directories ------------------------------------------------------------
def ensure_dirs(env: dict[str, str]) -> None:
    """Create the data tree, appdata subdirs, certs, and nginx/conf.d. Idempotent;
    setup_users already made the privileged ones with the right ownership."""
    data_root = env["DATA_ROOT"]
    for d in [
        "torrents/tv",
        "torrents/movies",
        "torrents/incomplete",
        "media/tv",
        "media/movies",
        "media/music",
        "downloads",
    ]:
        Path(data_root, d).mkdir(parents=True, exist_ok=True)
    for d in [
        "sonarr",
        "radarr",
        "prowlarr",
        "qbittorrent/qBittorrent",
        "jellyfin",
        "recyclarr",
        "pihole/etc-pihole",
        "pihole/etc-dnsmasq.d",
        "ddclient",
        "slskd",
        "picard",
    ]:
        (lib.APPDATA / d).mkdir(parents=True, exist_ok=True)
    for p in [
        lib.CERTS / "selfsigned",
        lib.CERTS / "acme-challenge",
        LE / "live" / env["JELLYFIN_HOSTNAME"],
        CONF_D,
    ]:
        p.mkdir(parents=True, exist_ok=True)


# --- API keys + RENDER_GID --------------------------------------------------
def fill_keys(env: dict[str, str]) -> None:
    """Generate any blank *arr API key and the render GID, persist to .env. Fixed
    keys let wiring reach the apps over their APIs without scraping."""
    for key in ("PROWLARR_API_KEY", "SONARR_API_KEY", "RADARR_API_KEY"):
        if not env.get(key):
            lib.save_env(key, lib.gen_key())
    if not env.get("RENDER_GID"):
        gid = lib.output(["getent", "group", "render"]).split(":")[2:3]
        lib.save_env("RENDER_GID", gid[0] if gid else "")


# --- seed configs -----------------------------------------------------------
def seed_configs(env: dict[str, str]) -> None:
    """Render seed/ into appdata/ once (never overwrites existing rendered config)."""
    lib.step("rendering seed config")
    arr_map = {
        k: env.get(k, "")
        for k in (
            "SONARR_API_KEY",
            "RADARR_API_KEY",
            "PROWLARR_API_KEY",
            "JELLYFIN_SERVER_NAME",
        )
    }
    for app in ("sonarr", "radarr", "prowlarr"):
        _seed(
            lib.APPDATA / app / "config.xml",
            lambda d, a=app: lib.render(lib.SEED / a / "config.xml", d, arr_map),
        )

    qb = lib.APPDATA / "qbittorrent" / "qBittorrent"
    _seed(
        qb / "qBittorrent.conf",
        lambda d: shutil.copyfile(
            lib.SEED / "qbittorrent/qBittorrent/qBittorrent.conf", d
        ),
    )
    _seed(
        qb / "categories.json",
        lambda d: shutil.copyfile(
            lib.SEED / "qbittorrent/qBittorrent/categories.json", d
        ),
    )

    # Only encoding.xml (QSV) is safe pre-boot; system/network/NFO go in post-boot.
    _seed(
        lib.APPDATA / "jellyfin" / "encoding.xml",
        lambda d: shutil.copyfile(lib.SEED / "jellyfin/config/encoding.xml", d),
    )

    _seed(
        lib.APPDATA / "recyclarr" / "recyclarr.yml",
        lambda d: lib.render(
            lib.SEED / "recyclarr/recyclarr.yml",
            d,
            {k: env.get(k, "") for k in ("SONARR_API_KEY", "RADARR_API_KEY")},
        ),
    )

    _seed(
        lib.APPDATA / "pihole/etc-dnsmasq.d/02-custom-dns.conf",
        lambda d: lib.render(
            lib.SEED / "pihole/dnsmasq/02-custom-dns.conf",
            d,
            {"DOMAIN": env["DOMAIN"], "LAN_IP": env["LAN_IP"]},
        ),
    )

    _seed_slskd()
    _seed_ddclient(env)


def _seed(dest: Path, produce: Callable[..., object]) -> None:
    """Run produce(dest) only if dest is absent (seed-once)."""
    if not dest.exists():
        produce(dest)


def _seed_slskd() -> None:
    """slskd web UI password is prompted here (never written to .env); the Soulseek
    account login is entered later in the slskd web UI. slskd rewrites this file, so
    we seed it once."""
    dest = lib.APPDATA / "slskd" / "slskd.yml"
    if dest.exists():
        return
    lib.info("Set a password for the slskd web UI (username 'slskd'):")
    pw = lib.prompt_password("slskd web UI")
    if pw is None:
        pw = lib.gen_key()
        lib.info(f"No TTY — generated a random slskd web UI password: {pw}")
    # Double single quotes so the password is a safe single-quoted YAML scalar.
    lib.render(
        lib.SEED / "slskd/slskd.yml",
        dest,
        {"SLSKD_WEBUI_PASSWORD": pw.replace("'", "''")},
    )
    dest.chmod(0o660)
    lib.ok(
        "slskd web UI password set. Enter your Soulseek account username/password "
        "once in the slskd web UI (System > Options) — not stored in .env."
    )


def _seed_ddclient(env: dict[str, str]) -> None:
    """Regenerate ddclient.conf from .env each run so DDNS changes propagate. The
    image chmods the bind-mounted file 0600, so rewrite then re-assert 0600."""
    ddclient_dir = lib.APPDATA / "ddclient"
    if not os.access(ddclient_dir, os.W_OK):
        raise lib.StackError(
            "appdata/ddclient is not operator-writable — likely owned by svc-ddclient "
            "from an earlier whole-dir mount. One-time fix:\n"
            "  docker compose down && sudo ./scripts/stack setup --only users"
            " && ./scripts/stack up"
        )
    dest = ddclient_dir / "ddclient.conf"
    dest.unlink(missing_ok=True)
    lib.render(
        lib.SEED / "ddclient/ddclient.conf",
        dest,
        {k: env[k] for k in ("DOMAIN", "DDNS_HOST", "NAMECHEAP_DDNS_PASSWORD")},
    )
    dest.chmod(0o600)


# --- nginx ------------------------------------------------------------------
def render_nginx(env: dict[str, str]) -> None:
    """Render the vhost + LAN includes into nginx/conf.d."""
    lib.step("rendering nginx config")
    # Dedupe the public server_name when DOMAIN == JELLYFIN_HOSTNAME (apex serving).
    host = env["JELLYFIN_HOSTNAME"]
    names = host if env["DOMAIN"] == host else f"{env['DOMAIN']} {host}"
    lib.render(
        lib.NGINX / "templates/default.conf.template",
        CONF_D / "default.conf",
        {
            "PUBLIC_SERVER_NAMES": names,
            "JELLYFIN_HOSTNAME": host,
            "LAN_IP": env["LAN_IP"],
            "QBIT_WEBUI_PORT": env["QBIT_WEBUI_PORT"],
            "SLSKD_PORT": env["SLSKD_PORT"],
        },
    )
    lib.render(
        lib.NGINX / "lan_vhost.inc",
        CONF_D / "lan_vhost.inc",
        {"LAN_SUBNET": env["LAN_SUBNET"]},
    )
    for inc in ("proxy_headers.inc", "lan_proxy.inc"):
        shutil.copyfile(lib.NGINX / inc, CONF_D / inc)
    lib.render(
        lib.NGINX / "jellyfin_proxy.inc",
        CONF_D / "jellyfin_proxy.inc",
        {"LAN_IP": env["LAN_IP"]},
    )


# --- certs ------------------------------------------------------------------
def lan_selfsigned_cert() -> None:
    """Self-signed cert for the *.lan HTTPS vhosts."""
    crt = lib.CERTS / "selfsigned" / "lan.crt"
    if crt.exists():
        return
    lib.info("Generating self-signed LAN certificate...")
    san = ",".join(f"DNS:{h}.lan" for h in lib.LAN_HOSTS) + ",IP:127.0.0.1"
    lib.run(
        [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-days",
            "3650",
            "-keyout",
            str(lib.CERTS / "selfsigned" / "lan.key"),
            "-out",
            str(crt),
            "-subj",
            "/CN=jellyfin.lan",
            "-addext",
            f"subjectAltName={san}",
        ]
    )
    (lib.CERTS / "selfsigned" / "lan.key").chmod(0o600)


def reclaim_certbot_tree() -> None:
    """certbot writes certs/letsencrypt root-owned 0700; a one-off root container
    chowns it back so this unprivileged CLI can rewrite it on re-runs."""
    if not ((LE / "accounts").exists() or (LE / "archive").exists()):
        return
    lib.info("reclaiming ownership of certs/letsencrypt from certbot (root)...")
    r = lib.compose(
        "run",
        "--rm",
        "--user",
        "0",
        "--entrypoint",
        "chown",
        "certbot",
        "-R",
        f"{os.getuid()}:{os.getgid()}",
        "/etc/letsencrypt",
        check=False,
    )
    if r.returncode != 0:
        lib.warn("could not reclaim certbot ownership (continuing).")


def place_dummy_cert(env: dict[str, str]) -> None:
    """A 1-day self-signed stand-in at the LE path so nginx can start before LE
    issues (a missing cert there crash-loops nginx)."""
    live = LE / "live" / env["JELLYFIN_HOSTNAME"]
    live.mkdir(parents=True, exist_ok=True)
    lib.run(
        [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-days",
            "1",
            "-keyout",
            str(live / "privkey.pem"),
            "-out",
            str(live / "fullchain.pem"),
            "-subj",
            f"/CN={env['JELLYFIN_HOSTNAME']}",
        ]
    )
    (live / "privkey.pem").chmod(0o600)


def place_dummy_cert_if_missing(env: dict[str, str]) -> None:
    """Place the dummy cert only when no cert exists at the LE path yet."""
    live = LE / "live" / env["JELLYFIN_HOSTNAME"]
    if not (live / "fullchain.pem").exists():
        lib.info(
            f"Placing temporary self-signed cert for {env['JELLYFIN_HOSTNAME']}..."
        )
        place_dummy_cert(env)


def obtain_or_renew_cert(env: dict[str, str], assume_yes: bool = False) -> None:
    """Renew if already issued; otherwise request once (confirmed). On an LE
    rate-limit, record the 'retry after' date and skip until it passes."""
    if _rate_limited():
        return
    host, domain = env["JELLYFIN_HOSTNAME"], env["DOMAIN"]
    renewal = LE / "renewal" / f"{host}.conf"

    if renewal.exists():
        lib.info(f"Renewing Let's Encrypt certificate for {host} if due...")
        r = lib.compose(
            "run",
            "--rm",
            "--entrypoint",
            "certbot",
            "certbot",
            "renew",
            "--webroot",
            "-w",
            "/var/www/certbot",
            "--non-interactive",
            check=False,
            capture=True,
        )
        print(r.stdout + r.stderr, end="")
        if r.returncode != 0:
            _save_retry(r.stdout + r.stderr)
            lib.err("certbot renew failed — keeping the existing certificate.")
        return

    if not lib.confirm(
        f"request a new Let's Encrypt certificate for {host}?", assume_yes
    ):
        lib.warn("skipped Let's Encrypt request.")
        return
    lib.info(f"Requesting Let's Encrypt certificate for {host}...")
    for p in (
        LE / "live" / host,
        LE / "archive" / host,
        renewal,
    ):  # drop the dummy lineage
        _rm(p)
    domains = ["-d", host] + (["-d", domain] if domain != host else [])
    r = lib.compose(
        "run",
        "--rm",
        "--entrypoint",
        "certbot",
        "certbot",
        "certonly",
        "--webroot",
        "-w",
        "/var/www/certbot",
        *domains,
        "--email",
        env["LETSENCRYPT_EMAIL"],
        "--agree-tos",
        "--no-eff-email",
        "--non-interactive",
        check=False,
        capture=True,
    )
    print(r.stdout + r.stderr, end="")
    if r.returncode == 0:
        lib.ok(f"certificate obtained for {host}.")
    else:
        _save_retry(r.stdout + r.stderr)
        lib.err(
            f"certbot failed — check that {host} and {domain} resolve to your IP "
            "and port 80 is open."
        )
        lib.info(
            "restoring temporary self-signed cert so nginx keeps serving"
            " (re-run to retry)."
        )
        place_dummy_cert(env)


def reload_nginx() -> None:
    """Tell the running nginx to reload its config."""
    lib.compose("exec", "nginx", "nginx", "-s", "reload", check=False)


def _rate_limited() -> bool:
    """True (skip cert step) while a recorded LE hold-off date is still in the
    future; clears the marker and returns False once it has passed."""
    if not CERT_RETRY_FILE.exists():
        return False
    try:
        retry = date.fromisoformat(CERT_RETRY_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        CERT_RETRY_FILE.unlink()
        return False
    if date.today() <= retry:
        lib.warn(f"LE rate-limit hold-off until {retry} — skipping cert step.")
        lib.info(f"Remove {CERT_RETRY_FILE} to retry sooner.")
        return True
    CERT_RETRY_FILE.unlink()
    return False


def _save_retry(out: str) -> None:
    m = re.search(r"retry after (\d{4}-\d{2}-\d{2})", out)
    if m:
        CERT_RETRY_FILE.write_text(m.group(1) + "\n", encoding="utf-8")
        lib.warn(f"LE rate-limited — cert step skipped until {m.group(1)}.")
        lib.info(f"Remove {CERT_RETRY_FILE} to retry sooner.")


def _rm(p: Path) -> None:
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)
