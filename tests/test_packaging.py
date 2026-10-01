"""Packaging layout guards.

These are cheap and catch failures that no functional test would notice: the
suite runs with ``src/`` on ``sys.path`` and so stays green even when the
package is not actually installable or a data file fails to ship.

The import-check CI step is what caught that the project itself was never
installed by ``requirements.txt`` alone.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import arr_uploader

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def test_every_subpackage_has_init():
    """A directory without __init__.py is not importable after installation."""
    missing = []
    for path in SRC.rglob("*"):
        if not path.is_dir() or path.name == "__pycache__":
            continue
        if not any(path.glob("*.py")):
            continue
        if not (path / "__init__.py").exists():
            missing.append(str(path.relative_to(ROOT)))

    assert not missing, f"subpackages missing __init__.py: {missing}"


def test_schema_sql_ships_with_the_package():
    """schema.sql is read via __file__, so it must be present next to store.py.

    If package-data is misconfigured the package installs cleanly and then
    raises FileNotFoundError on the first database open.
    """
    schema = Path(arr_uploader.__file__).with_name("db") / "schema.sql"
    assert schema.is_file(), f"schema.sql missing at {schema}"
    assert "CREATE TABLE" in schema.read_text(encoding="utf-8")


def test_store_resolves_its_schema():
    """The path store.py computes must actually exist."""
    from arr_uploader.db.store import SCHEMA_PATH

    assert SCHEMA_PATH.is_file(), f"store.py points at a missing file: {SCHEMA_PATH}"


def test_pyproject_declares_the_console_script():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["arr-uploader"] == "arr_uploader.cli:main"
    assert data["project"]["requires-python"].startswith(">=3.11")


def test_pyproject_ships_schema_as_package_data():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pkg_data = data["tool"]["setuptools"]["package-data"]["arr_uploader"]
    assert any("sql" in pattern for pattern in pkg_data), (
        f"schema.sql is not declared as package data: {pkg_data}"
    )


def test_version_matches_the_package():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert arr_uploader.__version__ == data["project"]["version"]


def test_dockerfile_installs_the_project():
    """The image installs with --no-deps, so requirements must be a prior layer."""
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements.txt" in dockerfile, "dependencies are never installed"
    assert "pip install" in dockerfile and "-e ." in dockerfile, (
        "the project itself must be installed into the image"
    )
    assert dockerfile.index("requirements.txt") < dockerfile.index("-e ."), (
        "requirements must be installed before the project for layer caching"
    )


def test_gitignore_excludes_secrets():
    """A committed session string is full account access."""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "*.session", "*.db", "quarantine/"):
        assert pattern in ignore, f".gitignore is missing {pattern}"