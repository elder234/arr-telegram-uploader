# arr-telegram-uploader

Uploads Radarr imports to Telegram, then deletes the local file — but only after
every part has been uploaded **and** its size confirmed by Telegram.

## The rule this project exists to enforce

```
upload all parts  →  verify every size  →  re-prove folder is unchanged  →  delete
```

If any step is uncertain, the local data stays. Failures produce a quarantined or
retained folder, never a silent loss.

## How it differs from mirror-leech-telegram-bot

That project was the reference for the split/upload mechanics, and it deletes on
failure:

```python
# bot/helper/mirror_leech_utils/upload_utils/telegram_uploader.py
if not self._listener.is_cancelled and await aiopath.exists(self._up_path):
    await remove(self._up_path)
```

It also calls `clean_download(self.dir)` from `on_upload_error`, so a failed
upload destroys local data. Its job state lives in memory (`task_dict`,
`queued_dl`), so a restart loses progress. This project takes the naming and
retry behaviour and inverts the deletion logic.

Borrowed from it:

| Pattern | Where |
|---|---|
| `split --numeric-suffixes=1 --suffix-length=3` naming | `Movie.mkv.001`, byte-compatible |
| `FloodWait` slept off at ×1.3 | `telegram/uploader.py` |
| `tenacity` exponential backoff | `backoff_delay()` with jitter |
| `BadRequest` → document fallback | `telegram/uploader.py` |
| `HYBRID_LEECH` session escalation | ceiling resolves from the probed tier |

Not adopted: delete-on-attempt, disk-backed splitting (peaks at ~2× the movie),
in-memory job state, renaming source files to shorten them.

## Architecture

```
Radarr ──Custom Script──▶ inbox/*.json ─┐
webhook (optional) ─────────────────────┼──▶ SQLite queue ──▶ worker ──▶ Telegram
reconciler (sweep) ──────────────────────┘                      │
                                                               └─▶ verified? delete : keep
```

Three intake paths converge on one idempotent enqueue keyed by movie folder, so
duplicate events cannot create duplicate jobs. The reconciler only considers
folders unknown to the database and older than `min_age_seconds`, so an in-flight
import is never picked up.

## Layout

```
src/arr_uploader/
  config.py          validated settings; env overrides TOML
  statefs.py         realpath containment, hardlink-aware sizing, quarantine
  naming.py          part names; never renames the source file
  db/                SQLite (WAL) queue, part ledger, event log
  media/
    scan.py          picks the feature over samples, collects sidecar subs
    stability.py     two-sample quiescence check
    partition.py     balanced split arithmetic
    ranged_reader.py streams one part's byte range without touching disk
    partbuffer.py    single-part fallback if streaming is not usable
  telegram/
    limits.py        Premium probe → 1900 / 3800 MiB ceiling
    uploader.py      retry, flood handling, cancellation
    verify.py        size gate — the licence to delete
  pipeline.py        state machine; deletion is last and gated
  worker.py          claim loop, leases, shutdown
```

## Splitting

Parts are balanced, not full-then-runt. For a file of `size` bytes:

```
n         = ceil(size / ceiling)
part_size = ceil(size / n)
```

`part_size ≤ ceiling` holds by construction. An exact multiple splits evenly, and
the smallest part is never a runt. No temporary files: `RangedFileReader`
presents one byte range as a file-like object, so peak extra disk is zero. If a
Kurigram release ever needs a real path, `partbuffer.py` buffers *one* part at a
time instead.

## Deletion gate

All must pass, re-read fresh from the database:

1. `deletion.enabled`
2. every part has a `file_id` and matches its planned size
3. `resolve_under(media_root, folder)` — symlinks and traversal rejected
4. folder size unchanged since the scan (catches a file appearing mid-upload)
5. folder quiescent

Failure at any step keeps the folder and records why. A failed `rmtree` moves
the folder to `deletion.quarantine_dir` instead.

## Setup

### 1. Get a Telegram session

```bash
cp .env.example .env          # then fill in TELEGRAM_API_ID / TELEGRAM_API_HASH
python scripts/login.py       # logs in, writes TELEGRAM_SESSION_STRING into .env
```

The session string grants full access to the account. It is in `.env` (git- and
Docker-ignored) and must never be pasted into a log, an issue, or a commit. If it
leaks, terminate the session from another Telegram client.

`login.py` needs `kurigram` and `tgcrypto` installed, so run it on the host rather
than inside the image:

```bash
pip install -r requirements.txt
```

### 2. Deploy

```bash
docker compose -f docker/docker-compose.yml up -d
```

Compose builds locally. To use the published image instead:

```bash
docker pull ghcr.io/elder234/arr-telegram-uploader:main
```

> The GHCR package is created on first publish and defaults to **private**, so an
> anonymous pull returns `401`. Either authenticate (`docker login ghcr.io -u
> elder234` with a `read:packages` token) or flip it once in
> <https://github.com/users/elder234/packages/container/package/arr-telegram-uploader/settings>
> → General → Danger Zone → Change visibility → Public. `GITHUB_TOKEN` cannot do
> this for you; the API needs a user-scoped token with `admin:packages`.

### 3. Start with deletion off

Set `deletion.enabled = false` in `config/uploader.toml` and confirm the first
real upload is verified before enabling it. Deletion is the irreversible part of
this pipeline, and the default should never be the dangerous one.

```bash
docker compose -f docker/docker-compose.yml exec uploader arr-uploader check
docker compose -f docker/docker-compose.yml exec uploader arr-uploader plan /data/media/Some\ Movie\ (2024)
```

`plan` uploads nothing. It shows the parts, the ceiling in use, and the exact byte
counts, which is the cheapest way to confirm sizing against a real release.

### 4. Radarr

Radarr → Settings → Connect → Custom Scripts → Add:

```
python3 /scripts/radarr_hook.py /srv/arr/state/uploader/inbox
```

The hook writes JSON and always exits 0, so a uploader outage never fails
Radarr's import. The reconciler recovers anything missed.

First test: a small release to Saved Messages. Confirm the parts land, then check
that deletion is still disabled before you consider turning it on.

## CLI

```bash
arr-uploader check              # validate config
arr-uploader plan <file>        # show the split plan, uploads nothing
arr-uploader tier               # probe Premium and the resulting ceiling
arr-uploader enqueue <folder>   # queue manually
arr-uploader sweep              # run the reconciler once
arr-uploader status             # job counts
arr-uploader worker             # run the worker
```

`plan` is the quickest way to confirm ceiling and sizing on a real release
before letting anything touch Telegram.

## Tests

```bash
python _devtools/run_tests.py            # all
python _devtools/run_tests.py partition  # one module
```

223 tests. `pytest` is used when installed; `_devtools/` provides a small shim and
runner so the suite also runs with no package index available.

Coverage includes the negative cases that matter most: verification failure must
retain local data, size drift must block deletion, an unsafe path must be skipped
without touching anything, and a resumed job must not resend verified parts.

`tests/test_packaging.py` covers what a functional test structurally cannot: the
runner injects `src/` into `sys.path`, so the suite stays green even when the
package is not installable. Those tests assert every subpackage has an
`__init__.py`, that `schema.sql` is declared as package data and resolves from the
installed location, that the image can build `tgcrypto` without shipping a
compiler, and that `.gitignore` still excludes secrets.

CI (`.github/workflows/`) runs the suite on 3.11 and 3.12, then imports the
package the way a user would — through the installed distribution and the
`arr-uploader` console script, with no `sys.path` games. It also scans tracked
files for committed session strings. `tests.yml` runs `pip install -e .`;
`docker.yml` builds the image, pushes to GHCR, and smoke-tests it.

What the suite does **not** cover: real Telegram, Radarr, or TorBox traffic, a
multi-GB upload, throughput, live FloodWait, or a crash mid-upload. Those need a
real run.

## Configuration notes

`download.concurrency` and `uploader.max_concurrent_jobs` default to 1. Parallel
uploads split the same uplink without raising throughput and make FloodWait more
likely. On a TorBox free tier the download leg is the bottleneck, so the queue
is expected to back up rather than the uploader being made concurrent.

State must live outside `media_root`; config validation rejects it, because state
inside the library would invite the reconciler to upload its own database.