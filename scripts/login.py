"""Generate a Kurigram session string for arr-uploader.

Run this ONCE on a machine where you can enter the login code. It prints a
session string that you paste into ``.env`` as ``TELEGRAM_SESSION_STRING``;
the uploader itself then needs no interactive login.

    python scripts/login.py
    python scripts/login.py --api-id 12345 --api-hash abcdef... --session-file /tmp/x

Why a session string rather than a ``.session`` file: the uploader usually runs
in a container with an ephemeral filesystem, and a session file would be lost on
recreate. A session string is portable and can live in an env var.

SECURITY: the session string grants full access to the Telegram account as if it
were a logged-in client. Treat it like a password. Do not commit it, do not paste
it into a chat, and revoke it from another session if it leaks.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

ENV_PATH = Path(".env")


async def _login(api_id: int, api_hash: str, session_file: str | None) -> str:
    try:
        from kurigram import Client
    except ImportError:
        print("kurigram is not installed. Try:", file=sys.stderr)
        print("  pip install -r requirements.txt", file=sys.stderr)
        print("or install it directly:", file=sys.stderr)
        print("  pip install kurigram tgcrypto", file=sys.stderr)
        raise SystemExit(2) from None

    kwargs: dict[str, object] = {"api_id": api_id, "api_hash": api_hash}

    if session_file:
        path = Path(session_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        kwargs["name"] = path.stem
        kwargs["workdir"] = str(path.parent)
    else:
        # No persistence needed: we only want the exported string.
        kwargs["name"] = "arr_uploader_login"
        kwargs["in_memory"] = True

    print("Contacting Telegram. Enter the login code when prompted.")
    print("(If you have 2FA, you will be asked for your password next.)\n")

    async with Client(**kwargs) as app:  # type: ignore[arg-type]
        me = await app.get_me()
        username = getattr(me, "username", None)
        print(f"\nLogged in as: {username or getattr(me, 'first_name', 'unknown')}")
        is_premium = getattr(me, "premium", None)
        print(f"Telegram Premium: {is_premium if is_premium is not None else 'unknown'}")

        if is_premium is False:
            print("  -> 1900 MiB ceiling per part (the standard 2 GiB limit).")
        elif is_premium is True:
            print("  -> 3800 MiB ceiling per part (the 4 GiB Premium limit).")

        return await app.export_session_string()


def _read_env_value(key: str) -> str:
    """Pull a value out of .env if it is already there."""
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip("'\"")
    return ""


def _write_env_value(key: str, value: str) -> bool:
    """Set a key in .env, preserving everything else. Returns True on success."""
    if not ENV_PATH.exists():
        ENV_PATH.write_text(f"{key}={value}\n", encoding="utf-8")
        return True

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.partition("=")[0].strip()
        if name == key:
            lines[i] = f"{key}={value}"
            replaced = True

    if not replaced:
        lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a Kurigram session string for arr-uploader.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Get api_id and api_hash from https://my.telegram.org/apps.\n"
            "TELEGRAM_SESSION_STRING is written to .env unless --no-write is given."
        ),
    )
    parser.add_argument("--api-id", type=int, default=None, help="defaults to $TELEGRAM_API_ID")
    parser.add_argument("--api-hash", default=None, help="defaults to $TELEGRAM_API_HASH")
    parser.add_argument(
        "--session-file",
        default=None,
        help="also persist a .session file here (default: memory only)",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="print the session string instead of writing it to .env",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    api_id = args.api_id or os.environ.get("TELEGRAM_API_ID") or _read_env_value("TELEGRAM_API_ID")
    api_hash = args.api_hash or os.environ.get("TELEGRAM_API_HASH") or _read_env_value("TELEGRAM_API_HASH")

    if not api_id:
        api_id = input("API id (from https://my.telegram.org/apps): ").strip()
    if not api_hash:
        api_hash = getpass.getpass("API hash: ").strip()

    try:
        api_id_int = int(str(api_id).strip())
    except ValueError:
        print(f"api_id must be numeric, got {api_id!r}", file=sys.stderr)
        return 2

    api_hash = str(api_hash).strip()
    if not api_hash:
        print("api_hash is required", file=sys.stderr)
        return 2

    try:
        session_string = asyncio.run(_login(api_id_int, api_hash, args.session_file))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - surface whatever Telegram said
        print(f"\nlogin failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.no_write:
        print("\nTELEGRAM_SESSION_STRING=" + session_string)
        print("\nCopy the line above into your .env. Keep it secret.")
        return 0

    _write_env_value("TELEGRAM_SESSION_STRING", session_string)
    print(f"\nWrote TELEGRAM_SESSION_STRING to {ENV_PATH.resolve()}")
    print("That value grants full account access. Do not commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())