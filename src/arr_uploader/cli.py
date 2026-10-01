"""Command line entry point.

Subcommands exist so each phase can be exercised in isolation, which matters
more than usual here: you want to confirm the partition arithmetic and the
deletion gate against real files before allowing anything to touch Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_settings, ceiling_bytes
from .db.models import Job, JobState, Part, PartState
from .db.store import Store
from .logging_setup import configure_logging


def _db_path(settings) -> str:
    return str(Path(settings.paths.state_dir) / "uploader.db")


def cmd_worker(args: argparse.Namespace) -> int:
    from .worker import run_worker

    settings = load_settings(args.config)
    configure_logging(settings.logging)
    asyncio.run(run_worker(settings))
    return 0


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


def _render_job(job: Job, parts: list[Part]) -> str:
    """One human-readable line per job, with part progress."""
    uploaded = sum(1 for p in parts if p.file_id)
    verified = sum(1 for p in parts if p.state == PartState.VERIFIED)
    total = len(parts) or job.part_count
    name = job.title or Path(job.folder_path).name
    size = _human_bytes(job.size_bytes)
    # Job.state is typed str and arrives as a raw column value, so coerce rather
    # than assuming the enum.
    state = JobState(job.state).value

    if total:
        progress = f"{uploaded}/{total} parts, {verified} verified"
    else:
        progress = "not partitioned yet"

    line = f"  #{job.id:<4} {state:<12} {size:>10}  {progress:<28} {name}"
    if job.last_error:
        line += f"\n         last error: {job.last_error}"
    return line


def cmd_status(args: argparse.Namespace) -> int:
    """Human-readable pipeline state.

    ``status --json`` keeps the original machine-readable payload for scripting.
    """
    settings = load_settings(args.config)
    store = Store(_db_path(settings))

    if args.json:
        print(json.dumps({"states": store.stats(), "meta": {
            "schema_version": store.get_meta("schema_version"),
            "telegram_tier": store.get_meta("telegram_tier"),
        }}, indent=2))
        store.close()
        return 0

    states = store.stats()
    print("uploader status")
    print("=" * 72)

    tier = store.get_meta("telegram_tier") or "unknown"
    print(f"telegram tier : {tier}")
    print(f"media root    : {settings.paths.media_root}")
    print(f"state dir     : {settings.paths.state_dir}")

    # TorBox intake, so it is visible without tailing logs.
    tb = settings.torbox
    print(f"torbox intake : {'enabled' if tb.api_key and tb.watch_dir else 'disabled'}")
    if tb.api_key and tb.watch_dir:
        journal = Path(tb.staging_dir or settings.paths.state_dir) / ".torbox-journal.json"
        print(f"  watch dir   : {tb.watch_dir}")
        pending = 0
        if journal.exists():
            try:
                data = json.loads(journal.read_text(encoding="utf-8"))
                entries = data.get("torrents", {}) if isinstance(data, dict) else {}
                pending = sum(1 for v in entries.values() if v.get("state") != "fetched")
                print(f"  journal     : {len(entries)} torrents, {pending} awaiting fetch")
            except (json.JSONDecodeError, AttributeError, OSError):
                print("  journal     : unreadable")
        else:
            print("  journal     : none yet (no torrents submitted)")

        magnets = Path(tb.watch_dir)
        waiting = 0
        if magnets.is_dir():
            waiting = sum(
                1 for p in magnets.iterdir()
                if p.suffix in (".magnet", ".torrent") and p.is_file()
            )
        print(f"  magnets waiting: {waiting}")

    print()
    print("jobs")
    print("-" * 72)
    if not states or sum(states.values()) == 0:
        print("  no jobs yet")
    else:
        # Active and failed first: those are what an operator needs to see.
        order = [
            JobState.UPLOADING, JobState.VERIFYING, JobState.FINALIZING,
            JobState.SCANNING, JobState.PARTITIONING, JobState.DISCOVERED,
            JobState.FAILED, JobState.QUARANTINED, JobState.SKIPPED,
            JobState.DONE,
        ]
        jobs: list[Any] = []
        for state in order:
            if states.get(state.value):
                jobs.extend(store.jobs_in_state(state))
        for job in jobs[: args.limit]:
            print(_render_job(job, store.get_parts(job.id)))
        remaining = len(jobs) - args.limit
        if remaining > 0:
            print(f"  ... and {remaining} more (use --limit to see them)")

    print()
    print("recent events")
    print("-" * 72)
    events = store.recent_events(limit=args.limit)
    if not events:
        print("  nothing yet")
    for row in events:
        detail = row["detail"] or ""
        if len(detail) > 60:
            detail = detail[:57] + "..."
        job_ref = f"job={row['job_id']}" if row["job_id"] is not None else "-"
        print(f"  {row['ts']}  {row['level']:<7} {job_ref:<10} {row['event']}  {detail}")

    store.close()
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    """Queue a folder by hand, for testing without Radarr."""
    settings = load_settings(args.config)
    store = Store(_db_path(settings))

    from .intake.inbox import IntakeRequest
    from .statefs import resolve_under

    folder = resolve_under(settings.paths.media_root, args.path)
    job_id = store.upsert_job(
        str(folder),
        title=args.title or "",
        source="cli",
        priority=args.priority,
    )
    print(json.dumps({"job_id": job_id, "folder": str(folder)}))
    store.close()
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Show the split plan for a file without uploading anything.

    This is the fastest way to confirm the ceiling and balanced sizing are doing
    what you expect on a real release.
    """
    settings = load_settings(args.config)
    from .media.partition import balanced_slices
    from .naming import render_part_name

    target = Path(args.path)
    size = target.stat().st_size
    ceiling = ceiling_bytes(settings.telegram, is_premium=True if args.premium else None)

    slices = balanced_slices(size, ceiling)
    rows = [
        {
            "idx": sl.idx,
            "name": render_part_name(
                movie_filename=target.name,
                index=sl.idx,
                template=settings.naming.template,
                part_index_width=settings.naming.part_index_width,
                max_length=settings.naming.max_length,
            ),
            "offset": sl.offset,
            "size_mib": round(sl.size / 1048576, 2),
        }
        for sl in slices
    ]

    print(json.dumps({
        "file": str(target),
        "size_bytes": size,
        "ceiling_bytes": ceiling,
        "part_count": len(slices),
        "part_size_mib": round(slices[0].size / 1048576, 2),
        "first": rows[0],
        "last": rows[-1],
        "preview": rows[:3],
    }, indent=2))
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    store = Store(_db_path(settings))
    from .intake.reconciler import Reconciler

    reconciler = Reconciler(
        settings.paths.media_root,
        store,
        min_age_seconds=0 if args.all else settings.reconciler.min_age_seconds,
    )
    print(json.dumps({"enqueued": reconciler.sweep()}))
    store.close()
    return 0


def cmd_tier(args: argparse.Namespace) -> int:
    """Report the Telegram Premium tier and the resulting ceiling."""
    settings = load_settings(args.config)
    configure_logging(settings.logging)

    from .telegram.client import TelegramClient
    from .telegram.limits import async_probe_account

    async def run() -> int:
        client = TelegramClient(settings.telegram)
        await client.start()
        try:
            # client.client, not client.inner.client: TelegramClient exposes no
            # .inner, and the sync probe raised RuntimeError inside this loop.
            probe = await async_probe_account(client.client)
            print(json.dumps({
                "premium": probe.is_premium,
                "source": probe.source,
                "ceiling_mb": probe.ceiling_mb,
                "resolved_bytes": ceiling_bytes(settings.telegram, probe.is_premium),
            }, indent=2))
        finally:
            await client.stop()
        return 0

    return asyncio.run(run())


def cmd_check(args: argparse.Namespace) -> int:
    """Validate configuration and print the effective settings."""
    settings = load_settings(args.config)
    print(json.dumps({
        "ok": True,
        "version": __version__,
        "media_root": settings.paths.media_root,
        "state_dir": settings.paths.state_dir,
        "chat_id": settings.telegram.chat_id,
        "part_ceiling_mb": settings.telegram.part_ceiling_mb,
        "deletion_enabled": settings.deletion.enabled,
        "subs": settings.naming.upload_subtitles,
    }, indent=2))
    return 0


def cmd_torbox(args: argparse.Namespace) -> int:
    """List torrent states from TorBox.

    The quickest way to tell intake is configured and the key is accepted,
    without submitting anything.
    """
    settings = load_settings(args.config)
    if not settings.torbox.api_key:
        print("torbox is not configured: set TORBOX_API_KEY", file=sys.stderr)
        return 2

    import httpx

    from .torbox import TorboxClient

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient(follow_redirects=True) as http:
            client = TorboxClient(settings.torbox)
            torrents = await client.list_torrents(http)
        return [
            {
                "id": t.id,
                "name": t.name,
                "state": t.state.value,
                "raw_state": t.raw_state,
                "progress": t.progress,
                "size": t.size,
                "files": len(t.files),
                "ready": t.ready,
            }
            for t in torrents
        ]

    print(json.dumps(asyncio.run(run()), indent=2))
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Submit watched magnets and download finished torrents.

    Runs one cycle and exits, so it is inspectable. The worker does the same
    thing on a loop.
    """
    settings = load_settings(args.config)
    if not settings.torbox.api_key or not settings.torbox.watch_dir:
        print(
            "torbox intake is off: set TORBOX_API_KEY and TORBOX_WATCH_DIR to enable it",
            file=sys.stderr,
        )
        return 2

    import httpx

    from .intake.torbox_intake import TorboxIntake

    async def run() -> dict[str, object]:
        intake = TorboxIntake(
            settings.torbox,
            fetch_dir=settings.torbox.staging_dir or settings.paths.state_dir,
            # Without this the fetched folder sits on disk forever: one-shot
            # fetch has no claim loop behind it.
            inbox_dir=settings.paths.inbox_dir,
        )
        fetched: list[str] = []
        ready_count = 0
        async with httpx.AsyncClient(follow_redirects=True) as http:
            stats = await intake.submit_all(http, dry_run=args.dry_run)
            if not args.dry_run:
                ready = await intake.poll(http)
                ready_count = len(ready)
                for torrent in ready:
                    folder = await intake.fetch(http, torrent)
                    if folder is not None:
                        fetched.append(str(folder))
        return {
            "dry_run": bool(args.dry_run),
            "submitted": stats.submitted,
            "skipped_cached": stats.skipped_cached,
            "failed": stats.failed,
            "ready": ready_count,
            "fetched": fetched,
            "errors": stats.errors,
        }

    print(json.dumps(asyncio.run(run()), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arr-uploader", description=__doc__)
    parser.add_argument("--config", default=None, help="path to uploader.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("worker", help="run the upload worker").set_defaults(func=cmd_worker)
    st = sub.add_parser("status", help="show pipeline state: jobs, parts, torbox, recent events")
    st.add_argument("--json", action="store_true", help="machine-readable output (the old payload)")
    st.add_argument("--limit", type=int, default=10, help="max jobs and events to show (default 10)")
    st.set_defaults(func=cmd_status)
    sub.add_parser("check", help="validate configuration").set_defaults(func=cmd_check)
    sub.add_parser("tier", help="probe Telegram Premium and the size ceiling").set_defaults(func=cmd_tier)

    p = sub.add_parser("plan", help="show the split plan for a file")
    p.add_argument("path")
    p.add_argument("--premium", action="store_true", help="assume a Premium account")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("enqueue", help="queue a movie folder manually")
    p.add_argument("path")
    p.add_argument("--title", default="")
    p.add_argument("--priority", type=int, default=50)
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("sweep", help="run the reconciler once")
    p.add_argument("--all", action="store_true", help="ignore the min-age filter")
    p.set_defaults(func=cmd_sweep)

    sub.add_parser("torbox", help="list torrent states from TorBox").set_defaults(func=cmd_torbox)

    p = sub.add_parser("fetch", help="submit watched magnets and fetch finished torrents")
    p.add_argument("--dry-run", action="store_true", help="report what would be submitted, submit nothing")
    p.set_defaults(func=cmd_fetch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())