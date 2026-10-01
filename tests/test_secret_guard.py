"""The CI secret guard is itself tested.

A guard that silently matches nothing is worse than no guard: it looks like
protection in the CI log while catching nothing. These tests run the exact
patterns from ``.github/workflows/tests.yml`` against a scratch repository.

The patterns were wrong twice while writing this project (``\\s`` instead of a
POSIX class, and a pattern that missed ``TORBOX_API_KEY``), so they get the same
scrutiny as the code they protect. Note that these assertions deliberately use
plain loops rather than ``pytest.param``, because the bundled runner's shim does
not implement it and the suite must pass in both environments.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO / "_devtools"))

# Single source of truth, shared with the workflow. Imported rather than
# duplicated so the two cannot drift; the workflow reads the same module.
from secret_patterns import ALL_PATTERNS, KEY_PATTERN, SESSION_PATTERN  # noqa: E402

FAKE_SECRET = "0123456789abcdef0123456789abcdef0123456789"

_git = shutil.which("git")


def _matches(content: str, pattern: str) -> bool | None:
    """Run git grep in a scratch repo, because that is what CI runs.

    Python's ``re`` treats ``\\s`` as whitespace while POSIX ERE does not, so
    testing these patterns in Python would have missed the real bug. Returns
    ``None`` when git is unavailable.
    """
    if _git is None:
        return None
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        subprocess.run([_git, "init", "-q", str(root)], check=True, capture_output=True)
        probe = root / "probe.py"
        probe.write_text(content + "\n", encoding="utf-8")
        subprocess.run([_git, "-C", str(root), "add", "-f", "probe.py"], check=True, capture_output=True)
        found = subprocess.run(
            [_git, "-C", str(root), "grep", "-qiE", pattern, "--", "probe.py"],
            capture_output=True,
        )
        return found.returncode == 0


# --------------------------------------------------------------- must detect


def test_key_guard_detects_every_shape() -> None:
    shapes = [
        f'api_key = "{FAKE_SECRET}"',
        f"api_key = '{FAKE_SECRET}'",
        f"api_key = {FAKE_SECRET}",
        f"api_key={FAKE_SECRET}",
        f"TORBOX_API_KEY='{FAKE_SECRET}'",
        f"torbox.api_key = '{FAKE_SECRET}'",
        f"torbox_api_key = '{FAKE_SECRET}'",
    ]
    for content in shapes:
        result = _matches(content, KEY_PATTERN)
        if result is None:
            pytest.skip("git is not on PATH")
        assert result, f"key guard missed: {content}"


def test_session_guard_detects_every_shape() -> None:
    shapes = [
        f'session_string = "{FAKE_SECRET}"',
        f"session_string = {FAKE_SECRET}",
        f"TELEGRAM_SESSION_STRING={FAKE_SECRET}",
    ]
    for content in shapes:
        result = _matches(content, SESSION_PATTERN)
        if result is None:
            pytest.skip("git is not on PATH")
        assert result, f"session guard missed: {content}"


# ------------------------------------------------------- must not false-fire


def test_key_guard_ignores_placeholders() -> None:
    safe = [
        'api_key = ""',
        'api_key = "${TORBOX_API_KEY}"',
        "api_key = 'your_key_here'",
        "api_id = 1",
        "webhook_secret = 'short'",
        "# api_key is documented below",
    ]
    for content in safe:
        result = _matches(content, KEY_PATTERN)
        if result is None:
            pytest.skip("git is not on PATH")
        # A guard that fires on documentation teaches people to disable it.
        assert not result, f"false positive on: {content}"


# ------------------------------------------------------ guard and repo agree


def _workflow() -> str:
    return (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")


def test_workflow_reads_patterns_from_the_shared_module() -> None:
    """One source of truth.

    The workflow used to inline its own copy of these patterns, and then to
    source them from this test module. Both drifted: the inline copy used ``\\s``
    and lacked ``-i``, and importing from a pytest module silently produced an
    empty pattern, which makes git grep match every file.
    """
    workflow = _workflow()
    assert "import secret_patterns as p" in workflow
    assert "p.KEY_PATTERN" in workflow
    assert "p.SESSION_PATTERN" in workflow
    # The patterns must not also be spelled out inline in the workflow.
    assert "api_?key" not in workflow, "pattern duplicated inline; read it from Python"
    assert "session_string[[:space:]]" not in workflow, "pattern duplicated inline"


def test_workflow_refuses_to_run_with_empty_patterns() -> None:
    """An empty pattern matches every file.

    This is the failure that made the guard useless: the extraction failed, the
    pattern was empty, and ``git grep -E ""`` flagged the whole repository.
    """
    workflow = _workflow()
    assert '[ -z "$key_pat" ]' in workflow
    assert '[ -z "$session_pat" ]' in workflow
    assert "guard not run" in workflow


def test_patterns_module_has_no_imports() -> None:
    """It is imported by the workflow in a bare interpreter.

    If this module ever needs pytest or anything else, the workflow's extraction
    breaks and the guard silently degrades.
    """
    source = (REPO / "_devtools" / "secret_patterns.py").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    for forbidden in ("import pytest", "import httpx", "import arr_uploader", "from arr_uploader"):
        assert forbidden not in body, f"{forbidden} would break the CI extraction"


def test_workflow_uses_case_insensitive_grep() -> None:
    # TORBOX_API_KEY only matches with -i. Regression guard for that mistake.
    workflow = _workflow()
    assert 'git grep -niE "$session_pat"' in workflow
    assert 'git grep -niE "$key_pat"' in workflow


def test_guard_patterns_do_not_use_backslash_s() -> None:
    """``\\s`` is unreliable in POSIX ERE.

    Kept as a test because the original inline guard used it. Checked against the
    live patterns, not the workflow prose, since the prose legitimately mentions
    the mistake it is warning about.
    """
    for pattern in ALL_PATTERNS:
        assert "\\s" not in pattern, "use [[:space:]] with git grep -E"


def test_patterns_are_not_empty() -> None:
    """An empty pattern is worse than no pattern: it matches every file."""
    for pattern in ALL_PATTERNS:
        assert pattern.strip(), "an empty pattern would flag the whole repo"


# ------------------------------------------- the real repo must be clean


def test_this_repository_has_no_committed_secrets() -> None:
    if _git is None:
        pytest.skip("git is not on PATH")
    for pattern in (KEY_PATTERN, SESSION_PATTERN):
        result = subprocess.run(
            [_git, "-C", str(REPO), "grep", "-niE", pattern, "--", "."],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, f"committed secret matched:\n{result.stdout}"