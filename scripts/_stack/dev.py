"""Dev tooling wrappers: isort + black (fmt), pylint (lint), mypy (check).
Dev-only — the runtime stays stdlib-only; install via pacman (see pyproject.toml)."""

from __future__ import annotations

from . import lib

# The package + the extension-less entrypoint the tools operate on.
TARGETS = ["scripts/_stack", "scripts/stack"]


def fmt() -> None:
    """Sort imports (isort) then format (black)."""
    lib.step("formatting (isort + black)")
    lib.run(["isort", *TARGETS])
    lib.run(["black", *TARGETS])


def lint() -> None:
    """Lint with pylint."""
    lib.step("linting (pylint)")
    lib.run(["pylint", *TARGETS])


def check() -> None:
    """Type-check with mypy --strict."""
    lib.step("type-checking (mypy)")
    lib.run(["mypy", *TARGETS])
