# CLAUDE.md

Project memory for Claude Code — loaded at the start of every session in this repo.
Use this as the index: what each file is and where to look. `README.md` is the
human operator/developer guide; its **Design notes** section holds the durable "why"
(rationale otherwise lives in git history).

## What this is

A self-hosted media stack (Docker) on one box, plus the router that makes it
reachable. Two peers:

- **repo root + `scripts/`, `seed/`, `nginx/`** — the media server (Bee-Link MINI
  S13 — Intel N150, CachyOS): Jellyfin + Sonarr/Radarr/Prowlarr/Recyclarr +
  FlareSolverr (Prowlarr's Cloudflare proxy) + qBittorrent + slskd (Soulseek)
  both behind gluetun/Mullvad + Picard (music
  tagger) + nginx + certbot + Pi-hole split-horizon DNS + ddclient Namecheap DDNS.
  nginx also serves port-free LAN-only hostnames (`sonarr.lan`, etc.).
- **router** — manual setup guide for the TP-Link Archer AX21 in `README.md`
  (no script; stock firmware isn't cleanly scriptable).

Environment: flat `10.0.0.0/24` LAN; domain on Namecheap; the Xfinity gateway is
bridged so the AX21 holds the public IP and forwards only `80`/`443` to the server.

## Hard invariants (do not "fix" these)

- **OS is CachyOS (Arch) on both servers.** Use `pacman`/`yay`. No vanilla-Arch or
  other-distro fallbacks — all hardware/OS choices are constant.
- **VPN is Mullvad WireGuard, no port forwarding.** Outbound seeding only; intentional.
  slskd runs firewalled too (downloads work, inbound/uploads limited) — don't open a port.
- **No Lidarr.** Music = slskd → `downloads/`, retag/move into `media/music` with
  Picard (or add manually). Jellyfin just scans `media/music`.
- **Ownership model:** per-service users + shared `media` group + setgid + `UMASK=002`
  + default ACLs. Never create root-owned files in shared volumes.
- **Gitignore boundary:** `.env`, `appdata/`, `certs/`, rendered `nginx/conf.d/` are
  never committed. `seed/` and `.env.example` are tracked.

## Component & file map

### Top-level
| File | Purpose |
|---|---|
| `compose.yaml` | 14 services. Interpolates from `.env`; TZ/UMASK have inline `:-` defaults. |
| `.env` (gitignored) | Single config file: infra defaults (UIDs/GIDs, ports, `LAN_SUBNET`, `JELLYFIN_SERVER_NAME`/`DDNS_HOST`) up top, deployment values + secrets (domain, LAN IP, WireGuard key, DDNS password) at the bottom. API keys auto-appended on first run. |
| `.env.example` | Template for `.env` — defaults filled in, deployment section blank for the operator. |
| `pyproject.toml` | Dev-tool config: black/isort/pylint/mypy (and a ready-but-unused pytest stub). The mechanical source of truth for code style. |
| `.editorconfig` | Editor defaults mirroring the tool config (88 cols, indents) for vim/kate/etc. |

### `scripts/` — one stdlib-Python CLI: `stack` + the internal `_stack/` package
User-facing entrypoint `stack` (no extension); `_stack/` is internal (underscore =
not user-facing). Verbs: `stack up | down [--clean --certs --pihole] | restart |
setup [--only users|firewall|dns] | status | logs [service...] | fmt | lint | check`;
global `-y/--yes`. Confirms before destructive wipes, the LE request, and the
firewall reset.

| File | Role |
|---|---|
| `stack` | **One-command bring-up** etc. argparse dispatcher; chdir repo root, umask 002, top-level error handler. First `up` creates `.env` from the example, then exits. `fmt`/`lint`/`check` need no `.env` (dispatched first). |
| `_stack/lifecycle.py` | `up` (escalate setup on first run → render → `compose up` → wire → post-boot Jellyfin seed), `down`, `restart`, `setup`, `status`, `logs`. |
| `_stack/host.py` | (sudo, via `stack setup`) `media` group + per-service users, ownership/setgid/ACLs, locks `appdata/slskd`, `/etc/hosts` `*.lan`; UFW + ufw-docker; points the host's own resolver at Pi-hole's `127.0.0.1:53` publish (NetworkManager). `--only` targets users / firewall / dns. |
| `_stack/render.py` | Dirs, API-key/RENDER_GID auto-fill, seed + nginx rendering (scoped `${VAR}` substitution), TLS certs (self-signed, certbot reclaim, LE issue/renew + rate-limit hold-off). |
| `_stack/wiring.py` | Wires apps over their APIs (root folders, qBittorrent download client, Prowlarr→Sonarr/Radarr sync). Idempotent (GET-before-POST). |
| `_stack/dev.py` | Wraps the dev tools for the `fmt`/`lint`/`check` verbs (isort+black / pylint / mypy). Dev-only. |
| `_stack/lib.py` | Shared utilities: colour logging, prompts/confirm, `run()`, `.env` load/save, HTTP, `poll()`, `render()`. |

### `nginx/`
| File | Purpose |
|---|---|
| `templates/default.conf.template` | Public Jellyfin vhost(s) + LAN default_server + 8 `<svc>.lan` blocks. Rendered with `${PUBLIC_SERVER_NAMES}` (deduped), `${JELLYFIN_HOSTNAME}`, port vars. |
| `proxy_headers.inc` | The shared proxy header/buffering lines. Included by both proxy includes. |
| `jellyfin_proxy.inc` | Jellyfin location body (static `proxy_pass` + headers). |
| `lan_proxy.inc` | LAN vhost location body (Docker `resolver` + headers; pairs with per-block `set $upstream`). |
| `lan_vhost.inc` | Shared listen/TLS/ACL for the `.lan` blocks. Rendered with `${LAN_SUBNET}`. |

### `seed/` (rendered into `appdata/` once by `stack up`)
| File | Notes |
|---|---|
| `sonarr\|radarr\|prowlarr/config.xml` | `${*_API_KEY}` injected; fixed keys so bootstrap can wire over the API. |
| `qbittorrent/qBittorrent/{qBittorrent.conf,categories.json}` | Save paths + `AuthSubnetWhitelist` so the *arrs reach the API credential-free. |
| `jellyfin/config/encoding.xml` | QSV + HDR→SDR tone map. Pre-seeded before first boot. |
| `jellyfin/config/{system,network,xbmcmetadata}.xml` | Seeded **after** first boot — server name, KnownProxies, NFO. |
| `recyclarr/recyclarr.yml` | TRaSH quality profiles, custom-format groups (unwanted/streaming/movie versions), and Jellyfin-friendly `media_naming`; `${*_API_KEY}` injected. |
| `slskd/slskd.yml` | Web UI password prompted at first run; Soulseek login entered in the UI. Not in `.env`. |
| `pihole/dnsmasq/02-custom-dns.conf` | Split-horizon `address=/${DOMAIN}/${LAN_IP}`. |
| `ddclient/ddclient.conf` | Namecheap DDNS; regenerated from `.env` each run (single-file mount). |

## Conventions

- **`pyproject.toml` is the mechanical source of truth** for Python style. Before
  finishing any code change, run `./scripts/stack fmt && ./scripts/stack lint &&
  ./scripts/stack check` and leave them clean (black, isort, **pylint 10/10**,
  **mypy --strict** with zero issues).
- **Runtime is stdlib-only** — no third-party imports in `scripts/`. The dev tools
  (black/isort/pylint/mypy/pytest) are dev-only, installed via pacman.
- **Full type hints** on every function (params + return), modern syntax
  (`str | None`, `dict[str, str]`).
- **Formatting:** black at 88 cols; **double quotes**; **f-strings only** (no
  `.format`/`%`). `Path.read_text/write_text` take `encoding="utf-8"`.
- **Comments:** terse, concise, declarative — match the surrounding density. No verbose
  prose, no restating the code. Push long rationale into `README.md`'s Design notes.
- **Naming:** snake_case; private helpers `_prefixed`; UPPERCASE module constants.
- Editor: `vim`. Keep files concise; trace every variable to a real use before adding one.
- **Defaults vs secrets:** one `.env` — non-secret infra (UIDs/ports/subnet) up top,
  deployment values + secrets at the bottom.
- **Routing:** `DOMAIN` and `JELLYFIN_HOSTNAME` both serve Jellyfin directly (one
  `:443` vhost; `${PUBLIC_SERVER_NAMES}` dedupes them when equal — apex serving, no
  redirect). LE cert is a SAN over both; Pi-hole resolves the whole domain to the LAN IP.
- Re-seed to apply `seed/`/`.env` changes: `sudo ./scripts/stack down --clean` then
  `./scripts/stack up` (`up` won't overwrite existing rendered config). `--clean`
  keeps `certs/` (renew, no LE rate-limit burn) and `appdata/pihole`.

## Working with Claude

- **Plan first.** For everyday tasks, research → present a plan → wait for approval →
  execute. Don't jump straight into edits.
- **Running commands.** Safe/read-only commands (`status`, `logs`, the dev verbs,
  greps) run freely. **Confirm before destructive ones** — `down --clean`, firewall
  resets, cert/LE operations. This box runs the live stack.
- **Sudo.** Claude can't run `sudo`. For commands that need root (`pacman -S`,
  `stack setup`, `stack down --clean`), provide the exact command for the operator to
  run.
- **Commits.** Don't commit — leave changes in the working tree for the operator to
  review and commit.
- **Explanations.** Detailed: state what changed and why, with trade-offs worth flagging.
