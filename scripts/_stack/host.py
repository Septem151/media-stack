"""Root-only host provisioning: the media group + per-service users, shared-tree
ownership/setgid/ACLs, the slskd lock, *.lan /etc/hosts entries, and the
UFW + ufw-docker firewall. Invoked as `stack setup` (auto-run on first `up`)."""

from __future__ import annotations

import ipaddress
import os
import shutil
from pathlib import Path

from . import lib

HOSTS_FILE = Path("/etc/hosts")
HOSTS_MARK = ("# >>> media-stack hostnames >>>", "# <<< media-stack hostnames <<<")
UFW_DOCKER = Path("/usr/local/bin/ufw-docker")
JELLYFIN_DISCOVERY_PORT = "7359"
PIHOLE_LOOPBACK = "127.0.0.1"

# (username, env UID key). All share the media GID; pihole runs as root:media.
SERVICE_USERS = [
    ("svc-qbittorrent", "QBIT_UID"),
    ("svc-sonarr", "SONARR_UID"),
    ("svc-radarr", "RADARR_UID"),
    ("svc-prowlarr", "PROWLARR_UID"),
    ("svc-jellyfin", "JELLYFIN_UID"),
    ("svc-recyclarr", "RECYCLARR_UID"),
    ("svc-ddclient", "DDCLIENT_UID"),
    ("svc-slskd", "SLSKD_UID"),
    ("svc-picard", "PICARD_UID"),
]

# Shared tree under DATA_ROOT (group=media, setgid 2775) + appdata subdirs.
DATA_DIRS = [
    "torrents/tv",
    "torrents/movies",
    "torrents/incomplete",
    "media/tv",
    "media/movies",
    "media/music",
    "downloads",
]
APPDATA_DIRS = [
    "qbittorrent",
    "sonarr",
    "radarr",
    "prowlarr",
    "jellyfin",
    "recyclarr",
    "pihole/etc-pihole",
    "pihole/etc-dnsmasq.d",
    "ddclient",
    "slskd",
    "picard",
]


def setup(
    env: dict[str, str], only: str | None = None, assume_yes: bool = False
) -> None:
    """Run host prep. `only` ('users'|'firewall'|'dns') targets one part."""
    lib.require_root()
    if only in (None, "users"):
        setup_users(env)
    if only in (None, "firewall"):
        setup_firewall(env, assume_yes)
    if only in (None, "dns"):
        setup_host_dns(env)


# --- users, ownership, ACLs -------------------------------------------------
def setup_users(env: dict[str, str]) -> None:
    """Create the media group + per-service users and set shared-tree ownership."""
    lib.step("host: users, ownership, ACLs")
    admin = env_admin()
    gid = env["MEDIA_GID"]

    if not lib.succeeds(["getent", "group", "media"]):
        lib.run(["groupadd", "-g", gid, "media"])
    lib.info(f"group media = {gid}")

    for name, uid_key in SERVICE_USERS:
        uid = env[uid_key]
        if not lib.succeeds(["getent", "passwd", name]):
            lib.run(
                [
                    "useradd",
                    "--system",
                    "--no-create-home",
                    "--shell",
                    "/usr/sbin/nologin",
                    "--uid",
                    uid,
                    "--gid",
                    gid,
                    name,
                ]
            )
        lib.info(f"user {name} = {uid} (group media)")

    # /dev/dri (QSV) for Jellyfin; let the human admin manage files too.
    if lib.succeeds(["getent", "group", "render"]):
        lib.run(["usermod", "-aG", "render", "svc-jellyfin"], check=False)
    if admin != "root":
        lib.run(["usermod", "-aG", "media", admin])

    # Shared /data tree: admin-owned, media group, setgid so new files inherit it.
    data_root = env["DATA_ROOT"]
    for d in DATA_DIRS:
        Path(data_root, d).mkdir(parents=True, exist_ok=True)
    lib.run(["chown", "-R", f"{admin}:media", data_root])
    lib.run(["chmod", "-R", "2775", data_root])

    # appdata: admin-owned, media group, setgid so seeding + services work.
    for d in APPDATA_DIRS:
        (lib.APPDATA / d).mkdir(parents=True, exist_ok=True)
    lib.run(["chown", "-R", f"{admin}:media", str(lib.APPDATA)])
    lib.run(["chmod", "-R", "2775", str(lib.APPDATA)])

    # Default ACLs: force group rwx onto future files even under a tight umask.
    if not shutil.which("setfacl"):
        lib.run(["pacman", "-S", "--noconfirm", "acl"], check=False)
    if shutil.which("setfacl"):
        lib.run(
            [
                "setfacl",
                "-R",
                "-m",
                "g:media:rwx",
                "-m",
                "d:g:media:rwx",
                data_root,
                str(lib.APPDATA),
            ]
        )
        lib.info("default ACLs applied (media:rwx inherited by new files)")
        _lock_slskd(admin)
    else:
        lib.warn(
            "setfacl unavailable — relying on setgid + UMASK only; "
            "appdata/slskd NOT locked (slskd.yml stays media-readable)"
        )

    _write_lan_hosts(env["LAN_IP"])
    lib.ok(
        "users, group, and ownership configured "
        "(services share the media group; appdata/slskd locked to svc-slskd)."
    )


def _lock_slskd(admin: str) -> None:
    """slskd.yml holds a real Soulseek password — keep it out of the media
    group's reach. Must run AFTER the recursive media ACL above."""
    slskd = lib.APPDATA / "slskd"
    lib.run(["chown", "svc-slskd", str(slskd)])
    lib.run(["setfacl", "-b", str(slskd)])
    lib.run(["chmod", "2700", str(slskd)])
    if admin != "root":
        lib.run(["setfacl", "-m", f"u:{admin}:rwx", str(slskd)])
    lib.info(f"appdata/slskd locked to svc-slskd (+ {admin}); media group excluded")


def _write_lan_hosts(lan_ip: str) -> None:
    """Rewrite the *.lan -> this box block in /etc/hosts (handles a changed IP)."""
    start, end = HOSTS_MARK
    kept, skip = [], False
    for line in HOSTS_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip() == start:
            skip = True
        elif line.strip() == end:
            skip = False
        elif not skip:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    block = [start] + [f"{lan_ip} {h}.lan" for h in lib.LAN_HOSTS] + [end]
    HOSTS_FILE.write_text("\n".join(kept + block) + "\n", encoding="utf-8")
    lib.info(f"service hostnames written to /etc/hosts (*.lan -> {lan_ip})")


# --- firewall ---------------------------------------------------------------
def setup_firewall(env: dict[str, str], assume_yes: bool = False) -> None:
    """Reset UFW and install ufw-docker with the route policy for the stack."""
    lib.step("host: firewall (UFW + ufw-docker)")
    if not lib.confirm(
        "reset and reconfigure the firewall "
        "(ufw --force reset briefly drops all rules)?",
        assume_yes,
    ):
        lib.warn("firewall setup skipped.")
        return

    if not shutil.which("ufw"):
        lib.run(["pacman", "-S", "--noconfirm", "ufw"])

    lib.run(["ufw", "--force", "reset"])
    lib.run(["ufw", "default", "deny", "incoming"])
    lib.run(["ufw", "default", "allow", "outgoing"])
    lib.run(["ufw", "default", "deny", "routed"])  # explicit route rules below
    lib.run(
        [
            "ufw",
            "allow",
            "from",
            env["LAN_SUBNET"],
            "to",
            "any",
            "port",
            "22",
            "proto",
            "tcp",
            "comment",
            "SSH (LAN only)",
        ]
    )
    # ufw-docker install refuses to run unless ufw is already active.
    lib.run(["ufw", "--force", "enable"])

    if not UFW_DOCKER.exists():
        lib.info("installing ufw-docker...")
        lib.run(
            [
                "curl",
                "-fsSL",
                "-o",
                str(UFW_DOCKER),
                "https://github.com/chaifeng/ufw-docker/raw/master/ufw-docker",
            ]
        )
        UFW_DOCKER.chmod(0o755)
    lib.run([str(UFW_DOCKER), "install"])  # appends to after.rules

    # Public: nginx 80/443.
    for port in ("80", "443"):
        lib.run(
            [
                "ufw",
                "route",
                "allow",
                "proto",
                "tcp",
                "from",
                "any",
                "to",
                "any",
                "port",
                port,
                "comment",
                "nginx (public)",
            ]
        )
    # LAN only: *arr + qBittorrent + slskd + Picard direct ports.
    lan = env["LAN_SUBNET"]
    app_ports = [
        env[k]
        for k in (
            "QBIT_WEBUI_PORT",
            "SLSKD_PORT",
            "SONARR_PORT",
            "RADARR_PORT",
            "PROWLARR_PORT",
            "PICARD_PORT",
        )
    ]
    for port in app_ports:
        lib.run(
            [
                "ufw",
                "route",
                "allow",
                "proto",
                "tcp",
                "from",
                lan,
                "to",
                "any",
                "port",
                port,
                "comment",
                "media app (LAN only)",
            ]
        )
    # Jellyfin client auto-discovery (UDP probe) — LAN only.
    lib.run(
        [
            "ufw",
            "allow",
            "from",
            lan,
            "to",
            "any",
            "port",
            JELLYFIN_DISCOVERY_PORT,
            "proto",
            "udp",
            "comment",
            "Jellyfin discovery (LAN only)",
        ]
    )
    # Jellyfin HTTP (host net): LAN clients + Docker bridge (nginx), on INPUT.
    for src in (lan, "172.16.0.0/12"):
        lib.run(
            [
                "ufw",
                "allow",
                "from",
                src,
                "to",
                "any",
                "port",
                env["JELLYFIN_PORT"],
                "proto",
                "tcp",
                "comment",
                "Jellyfin HTTP (host net)",
            ]
        )
    # Pi-hole DNS + admin.
    for proto in ("udp", "tcp"):
        lib.run(
            [
                "ufw",
                "route",
                "allow",
                "proto",
                proto,
                "from",
                lan,
                "to",
                "any",
                "port",
                "53",
                "comment",
                "Pi-hole DNS (LAN only)",
            ]
        )
    lib.run(
        [
            "ufw",
            "route",
            "allow",
            "proto",
            "tcp",
            "from",
            lan,
            "to",
            "any",
            "port",
            env["PIHOLE_WEBUI_PORT"],
            "comment",
            "Pi-hole admin (LAN only)",
        ]
    )

    lib.run(["ufw", "reload"])
    lib.run(["systemctl", "restart", "ufw"])
    lib.ok("firewall configured:")
    lib.run(["ufw", "status", "verbose"])


# --- host resolver ----------------------------------------------------------
def setup_host_dns(env: dict[str, str]) -> None:
    """Point the host's resolver at Pi-hole via its loopback publish, per LAN
    NetworkManager connection (DHCP servers stay as fallback)."""
    lib.step("host: resolver -> Pi-hole")
    if not shutil.which("nmcli") or not lib.succeeds(
        ["systemctl", "is-active", "--quiet", "NetworkManager"]
    ):
        lib.warn(
            f"NetworkManager inactive — point the host resolver at {PIHOLE_LOOPBACK} "
            "(Pi-hole's loopback publish) by hand."
        )
        return
    net = ipaddress.ip_network(env["LAN_SUBNET"], strict=False)
    cons = _lan_nm_connections(net)
    if not cons:
        lib.warn("no LAN NetworkManager connection found — host resolver unchanged.")
        return
    for dev, con in cons:
        lib.run(["nmcli", "connection", "modify", con, "ipv4.dns", PIHOLE_LOOPBACK])
        lib.run(["nmcli", "device", "reapply", dev], check=False)
        lib.info(f"{con} ({dev}): prefer {PIHOLE_LOOPBACK}, DHCP servers kept")
    lib.run(["resolvectl", "flush-caches"], check=False)
    lib.ok(f"host resolver prefers Pi-hole via {PIHOLE_LOOPBACK} (hairpin-proof).")


def _lan_nm_connections(
    net: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> list[tuple[str, str]]:
    """Active ethernet/wifi (device, connection) pairs with an address on `net`."""
    found: list[tuple[str, str]] = []
    for row in lib.output(
        ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"]
    ).splitlines():
        dev, _, rest = row.partition(":")
        dtype, _, state = rest.partition(":")
        if dtype not in ("ethernet", "wifi") or state != "connected":
            continue
        if _device_on_net(dev, net):
            con = lib.output(
                ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", dev]
            )
            if con:
                found.append((dev, con))
    return found


def _device_on_net(
    dev: str, net: ipaddress.IPv4Network | ipaddress.IPv6Network
) -> bool:
    """True if `dev` holds an IPv4 address inside `net`."""
    out = lib.output(["nmcli", "-g", "IP4.ADDRESS", "device", "show", dev])
    for token in out.replace(",", " ").split():
        try:
            if ipaddress.ip_address(token.split("/")[0]) in net:
                return True
        except ValueError:
            continue
    return False


def env_admin() -> str:
    """The invoking human (sudo's caller), or root if none."""
    return os.environ.get("SUDO_USER", "root")
