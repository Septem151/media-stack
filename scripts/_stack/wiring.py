"""Wire the apps over their APIs once they're up (idempotent): *arr root folders,
the qBittorrent download client, and Prowlarr->Sonarr/Radarr full sync.
GET-before-POST; on a 400, diff the payload against /api/v3/<endpoint>/schema."""

from __future__ import annotations

import json
from typing import Any

from . import lib

# Internal qBittorrent shares gluetun's namespace; the *arrs reach it there.
QBIT_HOST = "gluetun"


def wire(env: dict[str, str]) -> None:
    """Configure root folders, download clients, and Prowlarr app sync."""
    lib.step("wiring apps (root folders, download client, app sync)")
    lan = env["LAN_IP"]
    sonarr = f"http://{lan}:{env['SONARR_PORT']}"
    radarr = f"http://{lan}:{env['RADARR_PORT']}"
    prowlarr = f"http://{lan}:{env['PROWLARR_PORT']}"
    qbit_port = int(env["QBIT_WEBUI_PORT"])

    for base, key, name in (
        (sonarr, env["SONARR_API_KEY"], "Sonarr"),
        (radarr, env["RADARR_API_KEY"], "Radarr"),
        (prowlarr, env["PROWLARR_API_KEY"], "Prowlarr"),
    ):
        if not _wait(base, key, name):
            raise lib.StackError(f"{name} did not come up — cannot wire apps.")

    lib.info("Sonarr")
    post_once(
        sonarr,
        env["SONARR_API_KEY"],
        "v3",
        "rootfolder",
        "/data/media/tv",
        {"path": "/data/media/tv"},
    )
    post_once(
        sonarr,
        env["SONARR_API_KEY"],
        "v3",
        "downloadclient",
        "qBittorrent",
        _qbit_client("tvCategory", "tv", qbit_port),
    )

    lib.info("Radarr")
    post_once(
        radarr,
        env["RADARR_API_KEY"],
        "v3",
        "rootfolder",
        "/data/media/movies",
        {"path": "/data/media/movies"},
    )
    post_once(
        radarr,
        env["RADARR_API_KEY"],
        "v3",
        "downloadclient",
        "qBittorrent",
        _qbit_client("movieCategory", "movies", qbit_port),
    )

    # Prowlarr needs no download client — it syncs indexers to Sonarr/Radarr, which
    # dispatch downloads via their own qBittorrent clients above.
    lib.info("Prowlarr — Applications (Sonarr + Radarr, full sync)")
    post_once(
        prowlarr,
        env["PROWLARR_API_KEY"],
        "v1",
        "applications",
        "Sonarr",
        _prowlarr_app(
            "Sonarr",
            "http://sonarr:8989",
            env["SONARR_API_KEY"],
            [5000, 5010, 5020, 5030, 5040, 5045, 5050],
        ),
    )
    post_once(
        prowlarr,
        env["PROWLARR_API_KEY"],
        "v1",
        "applications",
        "Radarr",
        _prowlarr_app(
            "Radarr",
            "http://radarr:7878",
            env["RADARR_API_KEY"],
            [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060],
        ),
    )

    lib.ok(
        "bootstrap complete. Add your indexers in Prowlarr; they sync to "
        "Sonarr/Radarr automatically. Initial Recyclarr sync: "
        "docker compose run --rm recyclarr sync"
    )


def _wait(base: str, key: str, name: str) -> bool:
    def up() -> bool:
        for ver in ("v3", "v1"):
            st, _ = lib.http(
                f"{base}/api/{ver}/system/status", headers={"X-Api-Key": key}
            )
            if st == 200:
                return True
        return False

    return lib.poll(up, attempts=60, delay=3, desc=name)


def post_once(
    base: str,
    key: str,
    ver: str,
    endpoint: str,
    match: str,
    payload: dict[str, Any],
) -> None:
    """POST payload to <endpoint> unless an entry already matches `match` (by name
    or path). Parses the JSON list rather than substring-matching."""
    url = f"{base}/api/{ver}/{endpoint}"
    headers = {"X-Api-Key": key}
    st, body = lib.http(url, headers=headers)
    items = json.loads(body) if st == 200 and body.strip() else []
    if any(
        match in (it.get("name", ""), it.get("path", ""))
        for it in items
        if isinstance(it, dict)
    ):
        lib.info(f"   - {endpoint} '{match}' already present, skipping")
        return
    st, body = lib.http(
        url,
        method="POST",
        data=lib.jdump(payload),
        headers={**headers, "Content-Type": "application/json"},
    )
    if 200 <= st < 300:
        lib.ok(f"   + added {endpoint} '{match}'")
    else:
        lib.warn(
            f"failed to add {endpoint} '{match}' (HTTP {st}): {body.strip()[:300]}"
        )


def _qbit_client(category_field: str, category_value: str, port: int) -> dict[str, Any]:
    """Download-client payload. The category field differs per app:
    Sonarr=tvCategory, Radarr=movieCategory."""
    return {
        "enable": True,
        "protocol": "torrent",
        "priority": 1,
        "name": "qBittorrent",
        "implementation": "QBittorrent",
        "configContract": "QBittorrentSettings",
        "fields": [
            {"name": "host", "value": QBIT_HOST},
            {"name": "port", "value": port},
            {"name": "useSsl", "value": False},
            {"name": category_field, "value": category_value},
        ],
    }


def _prowlarr_app(
    name: str, base_url: str, api_key: str, sync_categories: list[int]
) -> dict[str, Any]:
    return {
        "name": name,
        "syncLevel": "fullSync",
        "implementation": name,
        "configContract": f"{name}Settings",
        "fields": [
            {"name": "prowlarrUrl", "value": "http://prowlarr:9696"},
            {"name": "baseUrl", "value": base_url},
            {"name": "apiKey", "value": api_key},
            {"name": "syncCategories", "value": sync_categories},
        ],
    }
