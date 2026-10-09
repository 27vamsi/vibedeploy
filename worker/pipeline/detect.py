"""What is this repository. Readme.md section 15.

The signal table, read literally and nothing else. No file is executed and no
dependency is installed to find out: detection happens in the worker, so it has
to be safe to run on a tree nobody has vetted yet, which means reading names and
text and drawing conclusions.

Two different questions come out of it, and confusing them would be the whole
bug:

  - **protection** is whether we can enforce anything at all. Section 4: a stack
    we cannot protect is labelled, never quietly treated as safe.
  - **migrations kind** is whether the build job knows how to get the schema
    into a database. A protected stack with migrations we cannot run is still a
    protected stack; it just cannot be deployed yet, and says so.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from control_plane.models import Protection

# Section 15's first two rows.
PYTHON_MARKERS = ("requirements.txt", "pyproject.toml", "Pipfile")
NODE_MARKERS = ("package.json",)

# Where a folder of plain `.sql` files is allowed to live.
SQL_MIGRATION_DIRS = ("migrations", "db/migrations")

FRAMEWORKS = ("fastapi", "flask", "starlette")

# Local convention until the buildpack lands in M8: the module in the repo root
# that exposes an ASGI application called `app`.
ENTRYPOINT_FILES = ("app.py", "main.py", "application.py")

NOT_PYTHON = (
    "This app is not a Python app. V0.5 can only prove a Python app's access"
    " rules, so it would have to be deployed unprotected."
)
NO_SQLALCHEMY = (
    "This app does not use SQLAlchemy. The identity shim that puts the logged in"
    " person into every query only exists for SQLAlchemy, so nothing would"
    " enforce the rules."
)


@dataclass(frozen=True)
class Stack:
    language: str
    framework: str | None
    db_lib: str | None
    migrations_kind: str
    migrations_path: str | None
    python_version: str | None
    entrypoint: str | None
    protection: str
    # Plain English, and only set when `protection` is not `protected`. It is
    # what a builder is shown instead of a shrug.
    unprotected_reason: str | None

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return ""


def _dependency_text(root: Path) -> str:
    return "\n".join(_read(root / name) for name in PYTHON_MARKERS)


def _migrations(root: Path) -> tuple[str, str | None]:
    if (root / "alembic.ini").is_file() and (root / "alembic").is_dir():
        return "alembic", "alembic"
    for candidate in SQL_MIGRATION_DIRS:
        directory = root / candidate
        if directory.is_dir() and any(directory.glob("*.sql")):
            return "sql", candidate
    return "none", None


def _entrypoint(root: Path) -> str | None:
    for name in ENTRYPOINT_FILES:
        if (root / name).is_file():
            return f"{Path(name).stem}:app"
    return None


def detect(root: Path) -> Stack:
    deps = _dependency_text(root)
    is_python = any((root / name).is_file() for name in PYTHON_MARKERS)
    is_node = any((root / name).is_file() for name in NODE_MARKERS)

    language = "python" if is_python else "node" if is_node else "unknown"
    db_lib = "sqlalchemy" if "sqlalchemy" in deps else None
    framework = next((name for name in FRAMEWORKS if name in deps), None)
    kind, path = _migrations(root)

    version_file = root / ".python-version"
    python_version = (
        version_file.read_text(encoding="utf-8").strip()
        if version_file.is_file()
        else None
    )

    if language != "python":
        protection, reason = Protection.unprotected.value, NOT_PYTHON
    elif db_lib != "sqlalchemy":
        protection, reason = Protection.unprotected.value, NO_SQLALCHEMY
    else:
        protection, reason = Protection.protected.value, None

    return Stack(
        language=language,
        framework=framework,
        db_lib=db_lib,
        migrations_kind=kind,
        migrations_path=path,
        python_version=python_version,
        entrypoint=_entrypoint(root),
        protection=protection,
        unprotected_reason=reason,
    )
