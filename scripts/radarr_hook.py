#!/usr/bin/env python3
"""Radarr Custom Script entry point.

Runs inside the Radarr container. It must be fast and must not fail: if the
uploader is unreachable, Radarr still has to import the movie. So this writes a
small JSON file into the shared inbox and returns immediately, using the
write-temp-then-rename pattern so the watcher never sees a partial payload.

Radarr versions disagree on Custom Script environment variables, so both spellings
are read:

  v3/v4   RADARR_MOVIE_PATH, RADARR_MOVIE_ID, RADARR_MOVIE_TITLE, RADARR_MOVIE_YEAR
  v5      RADARR_MOVIE_ID plus movieFolder from the script's own JSON on stdin

Configure in Radarr: Settings > Connect > Custom Scripts > Add, pointed at this
file with the inbox path passed as the first argument (or UPLOADER_INBOX set in
the container environment).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Events we act on. Download fires before the file is ready; Test is a
# connectivity check.
WANTED_EVENTS = {"Import", "MovieFileImported", "MovieFolderImported", "Rename", "MovieFileRenamed"}


def read_stdin_payload() -> dict:
    """Radarr v5 passes the script event as JSON on stdin.

    A closed or empty stdin is normal when invoked manually, so it is not an
    error.
    """
    if sys.stdin is None or sys.stdin.closed:
        return {}
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_payload(stdin_data: dict) -> dict | None:
    """Merge stdin and environment into one payload, or None to skip."""
    env = os.environ

    event = (
        stdin_data.get("eventType")
        or env.get("RADARR_EVENTTYPE")
        or env.get("RADARR_EVENT_TYPE")
        or ""
    ).strip()

    if event and event not in WANTED_EVENTS:
        # Download and Test are deliberately ignored: acting on them would
        # enqueue a movie whose file is still being written.
        return None

    folder = (
        stdin_data.get("movieFolder")
        or stdin_data.get("folderPath")
        or env.get("RADARR_MOVIE_PATH")
        or ""
    ).strip()

    if not folder:
        # Nothing to do. Not an error: Radarr fires this hook for other events too.
        return None

    movie_id = stdin_data.get("movieId") or env.get("RADARR_MOVIE_ID") or ""
    title = stdin_data.get("movieTitle") or env.get("RADARR_MOVIE_TITLE") or ""
    year = stdin_data.get("movieYear") or env.get("RADARR_MOVIE_YEAR") or ""

    imdb = ""
    tmdb = ""
    if isinstance(movie_id, str) and movie_id.startswith("tt"):
        imdb = movie_id
    elif str(movie_id).isdigit():
        tmdb = str(movie_id)

    return {
        "folderPath": folder,
        "movieId": int(movie_id) if str(movie_id).isdigit() else None,
        "title": title,
        "year": int(year) if str(year).isdigit() else None,
        "imdbId": imdb or None,
        "tmdbId": int(tmdb) if tmdb else None,
        "eventType": event or "Import",
        "source": "radarr",
    }


def resolve_inbox(argv: list[str]) -> Path | None:
    if len(argv) > 1 and argv[1]:
        return Path(argv[1])
    env = os.environ.get("UPLOADER_INBOX")
    return Path(env) if env else None


def main() -> int:
    payload = build_payload(read_stdin_payload())
    if payload is None:
        print("radarr_hook: nothing to enqueue")
        return 0

    inbox = resolve_inbox(sys.argv)
    if inbox is None:
        # Misconfiguration, but we must not break Radarr's import.
        print("radarr_hook: no inbox configured (argv[1] or UPLOADER_INBOX)", file=sys.stderr)
        return 0

    try:
        inbox.mkdir(parents=True, exist_ok=True)
        # Name by movie and event so a re-run is obvious, and hash the payload so
        # duplicate events within the same second collapse to one file.
        stem = Path(payload["folderPath"]).name.replace(" ", "_")
        key = f"{stem}.{payload.get('eventType', 'Import')}.{abs(hash(json.dumps(payload, sort_keys=True)))}"
        final = inbox / f"{key}.json"
        tmp = inbox / f".{key}.json.tmp"

        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, final)  # atomic
        print(f"radarr_hook: queued {final.name}")
    except Exception as exc:  # noqa: BLE001 - never break Radarr's import
        print(f"radarr_hook: failed to queue: {exc}", file=sys.stderr)

    # Always exit 0. A non-zero status makes Radarr report the import as failed,
    # which would be worse than a silently missed upload the reconciler can find.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())