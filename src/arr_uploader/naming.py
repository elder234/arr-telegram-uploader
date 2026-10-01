"""Part naming.

Emitted part names are derived from the movie filename. The source file on disk
is never renamed -- unlike mirror-leech's ``_prepare_file``, which renames the
actual file when the name exceeds 60 characters.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# Anything Telegram would mangle or that confuses downstream joiners.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")
_WHITESPACE = re.compile(r"\s+")

# Colons are illegal in Windows filenames but are the most common punctuation in
# movie titles ("Dune: Part Two"). Dropping them reads better than substituting.
_COLON = re.compile(r":+")


def sanitize(name: str) -> str:
    """Strip characters that are illegal or ambiguous in a Telegram filename.

    Whitespace is collapsed before other characters are replaced, so a newline
    becomes a single space rather than an underscore. Whitespace is legal in a
    filename; control characters are not.
    """
    cleaned = _WHITESPACE.sub(" ", name).strip()
    cleaned = _COLON.sub(" ", cleaned)
    cleaned = _UNSAFE.sub("_", cleaned).strip()
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    # A trailing dot or space breaks some filesystems and Telegram's own client.
    return cleaned.rstrip(". ")


def render_part_name(
    *,
    movie_filename: str,
    index: int,
    template: str = "{basename}{ext}{part}",
    part_index_width: int = 3,
    max_length: int = 240,
    title: str = "",
    year: int | None = None,
) -> str:
    """Render the Telegram filename for part *index* (1-based).

    With the default template this turns ``Movie.mkv`` + index 7 into
    ``Movie.mkv.007`` -- byte-identical to what GNU ``split
    --numeric-suffixes=1 --suffix-length=3`` would produce, so parts stay
    compatible with tools people already use to rejoin them.

    Zero-padding is what guarantees correct ordering. ``.007`` sorts before
    ``.010`` naturally, which removes the need for a natsort dependency.
    """
    path = PurePosixPath(movie_filename)
    suffix = path.suffix
    raw_stem = path.name[: -len(suffix)] if suffix else path.name

    stem = sanitize(raw_stem)
    ext = sanitize(suffix) if suffix else ""

    part_token = f".{index:0{part_index_width}d}" if index > 0 else ""

    rendered = template.format(
        basename=stem,
        ext=ext,
        part=part_token,
        index=index,
        title=sanitize(title) if title else "",
        year=year if year is not None else "",
    )

    rendered = sanitize(rendered)

    if len(rendered) <= max_length:
        return rendered

    # Too long. The part suffix is structural -- cutting into it would break
    # rejoining -- so shrink the stem by exactly the overflow instead.
    overflow = len(rendered) - max_length
    if overflow >= len(stem):
        # Pathological template: keep the part suffix and nothing else.
        return part_token
    return stem[: len(stem) - overflow] + rendered[len(stem) :]


def subtitle_name(movie_filename: str, sub_filename: str, max_length: int = 240) -> str:
    """Keep the video's stem so subs are visually grouped with their movie."""
    video_stem = PurePosixPath(movie_filename).stem
    sub_stem = PurePosixPath(sub_filename).stem
    suffix = PurePosixPath(sub_filename).suffix

    name = sanitize(f"{video_stem}.{sub_stem}{suffix}")
    if len(name) > max_length:
        room = max_length - len(suffix)
        name = name[: max(room, 1)] + suffix
    return name