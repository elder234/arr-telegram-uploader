"""The compose file has to actually read the root .env.

This is a regression guard for a real failure: `cp .env.example .env` at the repo
root followed by `docker compose -f docker/docker-compose.yml up` reported every
variable as missing, because compose resolves ${VAR} against a .env next to the
compose file (docker/.env) and never looked at the root one the docs tell you to
create.

Two rules follow, and both are easy to break by editing the YAML casually:

1. User-configured values arrive via env_file, not ${VAR}.
2. Nothing user-configured is repeated under environment:, because a key there
   wins over env_file and would replace a good value with an empty default.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker" / "docker-compose.yml"

# Values the operator is expected to supply in .env.
USER_VARS = {
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION_STRING",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_THREAD_ID",
    "TELEGRAM_PART_CEILING_MB",
    "RADARR_API_KEY",
    "LOG_LEVEL",
    "TORBOX_API_KEY",
    "TORBOX_WATCH_DIR",
    "TORBOX_DELETE_AFTER_FETCH",
}


def _text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _environment_block() -> str:
    """The service's environment: mapping, without the env_file: block.

    Hand-parsed rather than loaded as YAML: the point is to inspect the raw text
    for a key, and a missing pyyaml dependency would defeat the test.
    """
    text = _text()
    start = text.index("\n    environment:")
    # Skip the "environment:" line itself; it sits one level out and would
    # otherwise end the block before it starts.
    lines = []
    for line in text[start + 1 :].splitlines()[1:]:
        if line.strip() and not line.startswith("      "):
            break
        lines.append(line)
    return "\n".join(lines)


def test_compose_reads_the_repo_root_env_file():
    assert "env_file" in _text(), "compose no longer declares env_file, so .env is ignored"
    assert "../.env" in _text(), "env_file must point at the repo-root .env"


def test_env_file_is_required_not_optional():
    """A missing .env must fail loudly at compose time."""
    block = _text()
    assert re.search(r"required:\s*true", block), "a missing .env would start a broken container"


def test_user_values_are_not_interpolated_from_the_wrong_place():
    """${VAR} only reads the shell or docker/.env, which is what broke this."""
    for var in USER_VARS:
        assert "${" + var not in _text(), f"{var} uses ${{...}}, which cannot see the root .env"


def test_user_values_are_not_shadowed_by_environment_defaults():
    """environment: beats env_file, so a repeat here would blank the real value."""
    block = _environment_block()
    for var in USER_VARS:
        assert not re.search(rf"^\s+{var}:", block, re.M), (
            f"{var} is declared in environment:, which overrides env_file"
        )


def test_container_path_constants_are_still_set():
    """The paths the volumes and the pipeline agree on must not be dropped."""
    block = _environment_block()
    for var in ("MEDIA_ROOT", "STATE_DIR", "INBOX_DIR", "TORBOX_FETCH_DIR"):
        assert re.search(rf"^\s+{var}:", block, re.M), f"{var} missing from environment:"


def test_readme_does_not_claim_a_docker_env_file():
    """Docs told people to create .env at the root; keep it that way."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "cp .env.example .env" in readme
    # The string "docker/.env" is legitimate in a sentence explaining that the
    # root .env is the right one, so only flag an actual instruction to create
    # a second file there.
    assert not re.search(r"cp\s+[^\n]*\.env[^\n]*docker/\.env", readme), (
        "docs should not tell users to create docker/.env"
    )
    assert not re.search(r"docker/\.env[^\n]*<\s*[:=]", readme), (
        "docs should not tell users to create docker/.env"
    )