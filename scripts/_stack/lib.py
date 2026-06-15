"""Shared utilities for the media-stack CLI: colour logging, prompts, subprocess,
.env load/save, HTTP, polling, and scoped template rendering. Not user-facing."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from getpass import getpass
from pathlib import Path
from typing import Any

# Repo root: scripts/_stack/lib.py -> parents[2]. Paths resolve off this so the
# CLI works from any CWD (the entrypoint also chdirs here for `docker compose`).
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
APPDATA = REPO_ROOT / "appdata"
CERTS = REPO_ROOT / "certs"
SEED = REPO_ROOT / "seed"
NGINX = REPO_ROOT / "nginx"

# LAN hostnames nginx serves and the self-signed cert covers (also /etc/hosts).
LAN_HOSTS = [
    "jellyfin",
    "sonarr",
    "radarr",
    "prowlarr",
    "qbittorrent",
    "pihole",
    "slskd",
    "picard",
]


class StackError(RuntimeError):
    """Fatal, already-explained error: the entrypoint prints it and exits 1."""


# --- colour logging (ANSI only on a TTY) -----------------------------------
def _sgr(code: str) -> str:
    return code if sys.stdout.isatty() else ""


_RESET, _BOLD, _DIM = _sgr("\033[0m"), _sgr("\033[1m"), _sgr("\033[2m")
_BLUE, _GREEN = _sgr("\033[34m"), _sgr("\033[32m")
_YELLOW, _RED = _sgr("\033[33m"), _sgr("\033[31m")


def step(msg: str) -> None:
    """A major phase heading."""
    print(f"\n{_BOLD}{_BLUE}==>{_RESET} {_BOLD}{msg}{_RESET}")


def info(msg: str) -> None:
    """An informational line."""
    print(f"{_BLUE}>>{_RESET} {msg}")


def ok(msg: str) -> None:
    """A success line."""
    print(f"{_GREEN}ok{_RESET} {msg}")


def warn(msg: str) -> None:
    """A warning line (stderr)."""
    print(f"{_YELLOW}!!{_RESET} {msg}", file=sys.stderr)


def err(msg: str) -> None:
    """An error line (stderr)."""
    print(f"{_RED}!!{_RESET} {msg}", file=sys.stderr)


def mark(label: str, good: bool, detail: str = "") -> None:
    """A ✓/✗ checklist line (for `status`)."""
    sym = f"{_GREEN}✓{_RESET}" if good else f"{_RED}✗{_RESET}"
    tail = f"  {_DIM}({detail}){_RESET}" if detail else ""
    print(f"  {sym} {label}{tail}")


# --- prompts ---------------------------------------------------------------
def confirm(msg: str, assume_yes: bool = False) -> bool:
    """y/N prompt. --yes auto-accepts; a closed stdin (EOF) declines safely."""
    if assume_yes:
        return True
    try:
        while True:
            ans = input(f"{_YELLOW}?{_RESET} {msg} [y/N] ").strip().lower()
            if ans in ("y", "yes"):
                return True
            if ans in ("", "n", "no"):
                return False
            print("  please answer y or n.")
    except EOFError:
        return False


def prompt_password(label: str) -> str | None:
    """Prompt for a password twice; return it, or None with no TTY (caller
    falls back to a generated one)."""
    if not sys.stdin.isatty():
        return None
    while True:
        pw = getpass(f"   {label} password: ")
        if pw and pw == getpass("   confirm:  "):
            return pw
        err("empty or mismatch — try again.")


# --- subprocess ------------------------------------------------------------
def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command (list form). Raises StackError on failure when `check`."""
    try:
        return subprocess.run(
            cmd,
            check=check,
            text=True,
            env=env,
            input=input_text,
            capture_output=capture,
        )
    except FileNotFoundError as e:
        raise StackError(f"command not found: {cmd[0]}") from e
    except subprocess.CalledProcessError as e:
        detail = ((e.stderr or "") + (e.stdout or "")).strip() if capture else ""
        raise StackError(
            f"command failed: {' '.join(map(str, cmd))}"
            + (f"\n{detail}" if detail else "")
        ) from e


def succeeds(cmd: list[str]) -> bool:
    """True if the command exits 0 (output discarded). For existence probes."""
    return (
        subprocess.run(cmd, capture_output=True, text=True, check=False).returncode == 0
    )


def output(cmd: list[str]) -> str:
    """stdout+stderr of the command, stripped ('' on failure)."""
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return (r.stdout + r.stderr).strip()


def compose(*args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
    """`docker compose ...` from the repo root (reads compose.yaml + .env)."""
    return run(["docker", "compose", *args], **kw)


# --- privilege -------------------------------------------------------------
def is_root() -> bool:
    """True if running as root."""
    return os.geteuid() == 0


def require_root() -> None:
    """Raise unless running as root."""
    if not is_root():
        raise StackError("run with sudo.")


# --- .env ------------------------------------------------------------------
def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Parse KEY=VALUE lines (ignoring comments/blanks), stripping matching
    surrounding quotes — what bash sourcing + compose effectively see."""
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        env[key.strip()] = val
    return env


def save_env(key: str, value: str, path: Path = ENV_FILE) -> None:
    """Set key=value in .env (update in place or append), then re-assert 0600."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


# --- HTTP + polling --------------------------------------------------------
def http(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 10,
) -> tuple[int, str]:
    """Return (status, body). Network errors -> (0, ''); HTTP error codes are
    returned, not raised."""
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (urllib.error.URLError, OSError):
        return 0, ""


def poll(check: Callable[[], bool], *, attempts: int, delay: float, desc: str) -> bool:
    """Print a progress line and call check() until truthy or attempts run out."""
    sys.stdout.write(f"{_BLUE}>>{_RESET} waiting for {desc}")
    sys.stdout.flush()
    for _ in range(attempts):
        if check():
            print(" ok")
            return True
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(delay)
    print(" TIMEOUT")
    return False


# --- misc ------------------------------------------------------------------
def gen_key() -> str:
    """32 hex chars — for *arr API keys and fallback passwords."""
    return secrets.token_hex(16)


def render_str(text: str, mapping: dict[str, str]) -> str:
    """Substitute only ${VAR} for VAR in `mapping`; every other $ / ${...} is
    left untouched, so an app config's own $-syntax survives."""
    if not mapping:
        return text
    pat = re.compile(r"\$\{(" + "|".join(re.escape(k) for k in mapping) + r")\}")
    return pat.sub(lambda m: mapping[m.group(1)], text)


def render(src: Path, dest: Path, mapping: dict[str, str]) -> None:
    """Render a template file through render_str()."""
    dest.write_text(
        render_str(src.read_text(encoding="utf-8"), mapping), encoding="utf-8"
    )


def jdump(obj: Any) -> bytes:
    """Compact JSON body for POSTs."""
    return json.dumps(obj).encode()
