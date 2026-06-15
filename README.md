# Media Stack

A self-hosted media server you run with a single command. Jellyfin streams your
movies and shows, Sonarr/Radarr/Prowlarr find and organize them automatically,
qBittorrent downloads through a Mullvad VPN, slskd grabs music off the Soulseek
network (also via the VPN) and Picard re-tags it before it joins the library,
Pi-hole handles split-horizon DNS and ad blocking, and nginx serves Jellyfin to
the internet over HTTPS. Built for CachyOS.

## What you need

- A mini PC running CachyOS (Intel N150 or better — it handles the video
  transcoding). Reference build: Bee-Link MINI S13 — N150, 16GB RAM, 500GB NVMe, 2.5GbE.
- Your router forwarding ports **80** and **443** to this machine.
- A **Mullvad** account (you'll need its WireGuard key + address).
- A **domain name** (this guide assumes Namecheap) pointed at your home IP.

## Setup

1. Install packages:
   ```bash
   sudo pacman -S --needed docker docker-compose openssl ufw acl curl
   ```

2. Turn Docker on and let yourself use it without sudo:
   ```bash
   sudo systemctl enable --now docker.service
   sudo usermod -aG docker "$USER"
   ```
   Log out and back in so that takes effect.

3. Keep the box awake. It runs headless and must never sleep, so mask the sleep
   targets and tell logind to power off — never suspend — on any button press:
   ```bash
   sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
   sudo install -Dm644 /dev/stdin /etc/systemd/logind.conf.d/10-no-sleep.conf <<'EOF'
   [Login]
   HandlePowerKey=poweroff
   HandlePowerKeyLongPress=poweroff
   HandleSuspendKey=poweroff
   HandleSuspendKeyLongPress=poweroff
   HandleHibernateKey=poweroff
   HandleHibernateKeyLongPress=poweroff
   HandleLidSwitch=ignore
   IdleAction=ignore
   EOF
   sudo systemctl restart systemd-logind
   ```
   This box exposes both a Power and a Sleep ACPI button, so every key maps to
   `poweroff`: the one physical button always shuts down cleanly and can never suspend.

4. Bring it up. The first run creates `.env` and stops so you can fill it in; the
   second run does everything:
   ```bash
   ./scripts/stack up          # creates .env, then exits
   vim .env                    # set domain, hostname, email, Mullvad key/address,
                               # LAN IP, media path, Namecheap DDNS password
   ./scripts/stack up          # host setup + stack, in one go
   ```
   Run it as your **normal user** (not with `sudo`) — on first run it asks for your
   sudo password once to set up users and the firewall, then runs the rest as you.

   Host prep also points **this box's own** resolver at Pi-hole (via a loopback
   publish, since the host can't hairpin to its published `:53`). Confirm with
   `resolvectl status` — the **Current DNS Server** should read `127.0.0.1`. To
   re-apply just this part later: `sudo ./scripts/stack setup --only dns`.

That's the command line done. Finish in your browser:

5. Open `https://<your-jellyfin-hostname>` and complete Jellyfin's setup wizard:
   create your admin account and add three libraries —
   **Movies → `/media/movies`**, **Shows → `/media/tv`**, **Music → `/media/music`**.
   (Your base domain serves Jellyfin too — both names point at it.)
6. Open Prowlarr at `https://prowlarr.lan` (or `http://<LAN-IP>:9696`) and add your
   indexers. They sync to Sonarr and Radarr on their own. For Cloudflare-protected
   indexers, add a FlareSolverr proxy under **Settings → Indexer Proxies** with host
   `http://flaresolverr:8191`, give it a tag, and tag those indexers to match.
7. Apply recommended quality settings (also runs nightly on its own):
   ```bash
   docker compose run --rm recyclarr sync
   ```
8. Open slskd at `https://slskd.lan` and log in as **`slskd`** with the web UI
   password `stack up` asked you to set. In **System → Options**, enter your
   Soulseek account username and password, then save. (Stored only in slskd's own
   config, never in `.env`.)

Now add a movie in Radarr or a show in Sonarr — it downloads, gets organized, and
shows up in Jellyfin.

## Where your files live

```
<your media folder>/media/movies    films Jellyfin plays (managed by Radarr)
<your media folder>/media/tv         shows Jellyfin plays (managed by Sonarr)
<your media folder>/media/music      music Jellyfin plays (tag with Picard, or add manually)
<your media folder>/torrents         in-progress + completed torrent downloads
<your media folder>/downloads        music slskd pulls from Soulseek (then retag/move with Picard)
```

Music workflow: search and download in **slskd** → files land in `downloads/` →
open them in **Picard** (`https://picard.lan`), fix the tags, and save/move into
`media/music`, where Jellyfin picks them up on its next scan. Downloads and the
library share one filesystem, so Radarr/Sonarr imports are hardlinks/atomic moves,
not copies.

## Service addresses (on your home network)

Each app has a friendly hostname (no port to remember) **and** its direct
`LAN-IP:port`. The `.lan` names are LAN-only and use a self-signed certificate, so
your browser shows a one-time "not private" warning — click through once.

| App         | Friendly name             | Direct address               |
| ----------- | ------------------------- | ---------------------------- |
| Jellyfin    | `https://jellyfin.lan`    | `http://<LAN-IP>:8096`       |
| qBittorrent | `https://qbittorrent.lan` | `http://<LAN-IP>:8080`       |
| Sonarr      | `https://sonarr.lan`      | `http://<LAN-IP>:8989`       |
| Radarr      | `https://radarr.lan`      | `http://<LAN-IP>:7878`       |
| Prowlarr    | `https://prowlarr.lan`    | `http://<LAN-IP>:9696`       |
| slskd       | `https://slskd.lan`       | `http://<LAN-IP>:5030`       |
| Picard      | `https://picard.lan`      | `http://<LAN-IP>:5800`       |
| Pi-hole     | `https://pihole.lan`      | `http://<LAN-IP>:8082/admin` |

The `.lan` names resolve on this machine automatically (added to `/etc/hosts` by
`stack setup`). To use them from **every** device on the network, add Local DNS
Records in Pi-hole admin → Settings → Local DNS Records, mapping each name to this
machine's LAN IP.

## Managing the stack

```bash
./scripts/stack status               # setup-step checklist + container status
./scripts/stack logs [service...]    # follow logs (all, or named services)
./scripts/stack restart              # recreate the stack, keeping all data/config
./scripts/stack down                 # stop + remove containers (data kept)
sudo ./scripts/stack down --clean    # also wipe appdata to re-seed from seed/ + .env
```
Every command is safe to re-run. `--clean` needs sudo (it removes service-owned
files) but never touches your media; it keeps `certs/` (so the next start renews
rather than re-issues) and `appdata/pihole`. Add `--certs` to also wipe certs (only
after a domain change) or `--pihole` to also wipe Pi-hole's data. Pass `-y` to skip
the confirmation prompts.

## If something looks off

- **Permission errors / files owned by root:** re-assert ownership with
  `./scripts/stack down && sudo ./scripts/stack setup --only users`, then `./scripts/stack up`.
- **Can't reach Jellyfin from outside your house:** make sure the router forwards
  ports 80 and 443 to this machine, and your domain points at your current IP.
  Public reachability and HTTPS renewal both depend on the Namecheap record tracking
  your live IP (ddclient keeps it current); a stale record breaks both.
- **Downloads work but barely seed:** expected — Mullvad doesn't support port
  forwarding, so neither P2P client has an inbound listener. Fine for low-volume use.

---

## For developers

### Repo layout

| Path | What it is |
| --- | --- |
| `compose.yaml` | The whole stack: 14 services. Interpolates vars from `.env`. |
| `.env.example` | Template for `.env` (gitignored): infra defaults up top, deployment values + secrets at the bottom. |
| `scripts/` | `stack` — a stdlib-Python CLI (`up`/`down`/`restart`/`setup`/…) + the internal `_stack/` package it dispatches to. |
| `seed/` | Version-controlled config templates, rendered into `appdata/` on first run. |
| `nginx/` | The vhost template + shared `*.inc` snippets (rendered into `nginx/conf.d/`). |
| `pyproject.toml`, `.editorconfig` | Dev-tool config (black/isort/pylint/mypy) + editor defaults. |
| `appdata/`, `certs/`, `nginx/conf.d/` | Generated at runtime — gitignored. |

### How config flows

`seed/` + `.env` → **rendered** by `stack up` (scoped `${VAR}` substitution) into
`appdata/<service>/` and `nginx/conf.d/` → **mounted** into the containers. Editing a
`seed/` file does nothing until you re-seed, because `stack up` won't overwrite an
existing rendered config. To apply seed/`.env` changes:
`sudo ./scripts/stack down --clean` then `./scripts/stack up`.

`.env` is the one config file: non-secret infra (UIDs/ports/subnet) up top,
deployment-specific values and secrets at the bottom.

### Git boundary

Committed: `compose.yaml`, `.env.example`, `seed/`, `nginx/templates/` + `nginx/*.inc`,
`scripts/`, `pyproject.toml`, `.editorconfig`. Ignored: `.env` (secrets), `appdata/`
(runtime state), `certs/`, rendered `nginx/conf.d/`. A full rebuild is `git clone` +
fill in `.env` + `./scripts/stack up`.

### Development tooling

The runtime is stdlib-only Python — no dependencies, no venv. The dev tools are
separate; install them on the host:

```bash
sudo pacman -S --needed python-black python-isort mypy python-pylint python-pytest
```

Then drive them through the CLI (config lives in `pyproject.toml`):

```bash
./scripts/stack fmt      # isort + black (auto-format)
./scripts/stack lint     # pylint
./scripts/stack check    # mypy --strict
```

House style: black at 88 columns, isort, full type hints with a clean `mypy --strict`
pass, double quotes and f-strings, and terse, declarative comments. `.editorconfig`
mirrors the formatting so vim/kate/etc. agree. Testing is not set up yet (pytest is
installed and `pyproject.toml` is ready for a `tests/` dir).

### Design notes

Why the stack is built the way it is — the durable decisions:

- **Split-horizon DNS.** The ISP gateway is bridged so the edge router (TP-Link
  Archer AX21) holds the public IP and forwards only 80/443. Pi-hole resolves the
  *whole domain* to the LAN IP for internal clients, so in-house Jellyfin traffic
  never hairpins through the WAN; public DNS (Namecheap) answers the WAN IP.
- **The server uses Pi-hole.** Every LAN device gets Pi-hole as its resolver
  (the router hands it out), but this box can't reach it the same way: a query to
  its *own* published `${LAN_IP}:53` would have to hairpin back through Docker's NAT
  to the container, which fails — so systemd-resolved silently falls back to the
  router and loses ad-blocking + split-horizon. Pi-hole therefore also publishes on
  `127.0.0.1:53` (served straight by docker-proxy, no hairpin, no firewall hole),
  and `stack setup` prepends that on each LAN NetworkManager connection while
  leaving the DHCP-supplied servers as fallback. The box is intentionally dual-homed
  (wired + Wi-Fi on one subnet); this loopback path is immune to the resulting
  asymmetric routing.
- **Pi-hole listens in `ALL` mode, not `LOCAL`.** In the default `LOCAL` mode FTL
  answers only its own subnets; bridged, that's Docker's `172.x`, so LAN clients
  (`10.0.0.0/24`) look foreign and their queries are dropped. `ALL` accepts every
  origin; the firewall, not Pi-hole, scopes `:53` to the LAN.
- **VPN: Mullvad WireGuard, no port forwarding.** qBittorrent **and** slskd join
  gluetun's network namespace (`network_mode: service:gluetun`), so all their traffic
  egresses the tunnel and dies if it drops — gluetun's firewall is the kill switch.
  Mullvad dropped port forwarding, so there's no inbound listener; downloads still
  work, seeding/uploads are limited. Accepted, not worked around. Only the P2P clients
  are tunneled — Jellyfin (needs LAN + the local QSV device) and Picard never are.
- **Ownership model.** Each service runs as a dedicated `svc-*` UID in a shared
  `media` group. Three layers keep shared volumes writable without root-owned files:
  setgid on the data tree + `appdata/`, `UMASK=002`, and default POSIX ACLs
  (`d:g:media:rwx`) as an umask-proof backstop. `stack setup` (run as root on first
  `up`) is the single ownership authority. The one tightened exception is
  `appdata/slskd` (a real Soulseek password lives in `slskd.yml`), locked to
  `svc-slskd` and out of the `media` group's reach.
- **Fixed API keys.** The *arr API keys are generated once into `.env` and each
  `config.xml`, so `stack up` can wire services over their APIs (root folders, the
  qBittorrent download client, Prowlarr → Sonarr/Radarr full sync) idempotently
  without scraping generated keys.
- **FlareSolverr.** Some indexers sit behind Cloudflare's anti-bot challenge, which
  Prowlarr can't solve itself. FlareSolverr runs a headless browser that clears the
  challenge and returns the cookies. It's its own service (a browser, not part of the
  *arr stack) and publishes no port — only Prowlarr reaches it, in-cluster at
  `http://flaresolverr:8191`. Unlike the *arr wiring it isn't auto-configured: point
  Prowlarr at it once (see setup step 6), since which indexers need it is a per-deploy
  choice.
- **Reverse proxy & TLS.** nginx (standard image: master root binds 80/443 and reads
  the LE key, workers drop privileges) terminates TLS and proxies Jellyfin by vhost;
  the `<svc>.lan` blocks ride the same ports but are confined to the LAN subnet by a
  source-IP ACL. certbot issues/renews over HTTP-01; a self-signed dummy cert bridges
  the bootstrap chicken-and-egg (nginx needs a cert to start, certbot needs nginx to
  serve the challenge), and on an LE rate-limit the "retry after" date is recorded and
  the cert step skipped until it passes.
- **Jellyfin first run isn't fully declarative.** On a fresh DB, 10.11's legacy-config
  import crashes if `system.xml`/`network.xml` are pre-seeded, so only `encoding.xml`
  (QSV + HDR→SDR tone map) is seeded before first boot; the rest is dropped in
  afterward, once, marker-guarded. The startup wizard stays enabled so you create the
  admin user in the UI — never set `IsStartupWizardCompleted=true` in the seed.
- **N150 over N305.** Both are Alder Lake-N with the same fixed-function media engine,
  so single-stream transcode throughput is ~equal. The N305's extra EUs only matter for
  sustained multi-stream HDR tone-mapping; real load here is 0–1 concurrent viewers
  ~90% of the time.
- **Firewall.** Docker bypasses UFW's INPUT chain, so `stack setup` installs
  ufw-docker (routing Docker traffic through `DOCKER-USER` so `ufw route` rules apply).
  Public 80/443; LAN-only for app ports, SSH, and Pi-hole DNS/admin; default deny.

---

## Router setup (TP-Link Archer AX21 V5)

The router has **no startup script** — stock TP-Link firmware has no stable CLI or
API, so this is a one-time manual setup through its web UI. Everything below is done
once.

**Goal**: the ISP gateway is bridged so the AX21 holds the public IP. The AX21 then:
- hands every device this **media server** as its DNS server (Pi-hole runs here),
- keeps the **media server** on a fixed LAN IP,
- forwards only **80** and **443** from the internet to the media server.

### 1. Bridge the ISP gateway (Xfinity)

1. In the Xfinity app: **WiFi → View WiFi Equipment → Advanced Settings →
   Admin Tool Online Access → ON**.
2. Browse to `http://10.0.0.1`, log in (`admin` / sticker password).
3. **Gateway → At a Glance → Bridge Mode → Enable**. Wait ~3 min.
4. Cable a gateway LAN port → the AX21's blue WAN/Internet port.

### 2. AX21 first-time setup

1. Connect to the AX21 and open `http://tplinkwifi.net` (or `192.168.0.1`).
2. Run the wizard; set a strong admin password and your WiFi name/password.
   **Skip / don't bind a TP-Link cloud ID.**
3. Confirm internet works.

> If your LAN is `10.0.0.0/24` (the `.env` default), set the AX21's LAN
> subnet to match under **Advanced → Network → LAN**.

### 3. Point all devices at Pi-hole for DNS

**Advanced → Network → DHCP Server** → set **Primary DNS** to the media server's LAN
IP (Pi-hole listens on port 53 there) → Save. Reboot the router so devices pick it up.

### 4. Reserve a fixed IP for the media server

**Advanced → Network → DHCP Server → Address Reservation → Add**, using
**VIEW CONNECTED DEVICES** — reserve the media server's MAC at the IP you set in
`.env` (e.g. `10.0.0.60`).

### 5. Forward ports to the media server

**Advanced → NAT Forwarding → Port Forwarding → Add**, twice:
- **HTTPS** (443) → media server reserved IP, TCP
- **HTTP** (80) → media server reserved IP, TCP

Do **not** forward any other ports — the *arr apps, qBittorrent, Jellyfin's direct
port, and Pi-hole's admin UI stay LAN-only.

### Notes

- Comcast residential must not block inbound 80/443 (verify on your plan).
- The AX21 supports NAT loopback (you may need to disable **NAT Boost**), but you
  don't need it — Pi-hole's split-horizon record handles local access.
