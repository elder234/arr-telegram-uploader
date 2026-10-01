"""Movie folder scanning.

Selects the primary video and any sidecar subtitles, and computes the folder
size that the deletion gate will later compare against.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger(__name__)

VIDEO_EXTS = frozenset({".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".mov", ".wmv", ".flv", ".mpg", ".mpeg"})

SUBTITLE_EXTS = frozenset({".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup"})

# Artwork and metadata Radarr manages itself; never uploaded, never counted as
# part of the movie payload.
IGNORED_EXTS = frozenset({
    ".nfo", ".jpg", ".jpeg", ".png", ".txt", ".sfv", ".md5", ".part", ".!qb", ".torrent",
})

# Release-group noise that looks like a split suffix but is part of the name.
_SPLIT_SUFFIX = re.compile(r"\.\d{2,3}$")


class ScanError(Exception):
    """Raised when a folder cannot yield a usable upload."""


@dataclass(slots=True)
class ScanResult:
    folder: Path
    video: Path
    video_size: int
    folder_size: int
    subtitles: list[Path] = field(default_factory=list)
    ignored: list[Path] = field(default_factory=list)

    @property
    def title(self) -> str:
        stem = _SPLIT_SUFFIX.sub("", self.video.stem)
        return stem


def _is_real_file(path: Path) -> bool:
    """Reject symlinks and anything that vanished mid-scan."""
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _looks_like_sample(name: str) -> bool:
    lowered = name.lower()
    return "sample" in lowered or lowered.startswith("._") or lowered.endswith(".partial")


def find_videos(folder: str | os.PathLike[str]) -> list[Path]:
    """Every playable video in *folder*, recursively, sorted by size descending.

    Sorting by size puts the main feature first when a folder contains samples
    or extras, which is the ordering the picker below relies on.
    """
    root = Path(folder)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if _looks_like_sample(name):
                continue
            path = Path(dirpath) / name
            if path.suffix.lower() not in VIDEO_EXTS:
                continue
            if not _is_real_file(path):
                continue
            try:
                if path.stat().st_size > 0:
                    found.append(path)
            except OSError:
                continue

    found.sort(key=lambda p: p.stat().st_size, reverse=True)
    return found


def find_subtitles(folder: str | os.PathLike[str], video: Path) -> list[Path]:
    """Sidecar subtitle files, excluding the video's own language duplicates.

    Subtitles are uploaded separately rather than folded into the video parts,
    so they are collected separately here and never partitioned.
    """
    root = Path(folder)
    video_stem = video.stem

    subs: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() not in SUBTITLE_EXTS:
                continue
            if _looks_like_sample(name):
                continue
            # Skip a subtitle that is just another language track's sidecar
            # duplicate of the same stem when one already exists for that stem.
            if path.stem == video_stem and subs:
                continue
            if not _is_real_file(path):
                continue
            subs.append(path)

    return sorted(subs)


def scan(folder: str | os.PathLike[str], *, upload_subtitles: bool = True) -> ScanResult:
    """Analyse *folder* and pick what to upload.

    Raises :class:`ScanError` when the folder has no usable video. An empty or
    still-copying folder is a transient condition, so the pipeline should retry
    rather than fail the job permanently.
    """
    root = Path(folder)
    if not root.is_dir():
        raise ScanError(f"not a directory: {root}")

    videos = find_videos(root)
    if not videos:
        raise ScanError(f"no playable video found in {root}")

    video = videos[0]
    extras = videos[1:]

    try:
        video_size = video.stat().st_size
    except OSError as exc:
        raise ScanError(f"cannot stat {video}: {exc}") from exc

    subs = find_subtitles(root, video) if upload_subtitles else []

    ignored: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path in (video, *extras, *subs):
                continue
            if path.suffix.lower() in IGNORED_EXTS or path in extras:
                ignored.append(path)

    folder_size = _folder_size(root)

    LOG.info(
        "folder scanned",
        extra={
            "folder": str(root),
            "video": video.name,
            "video_size": video_size,
            "folder_size": folder_size,
            "subs": len(subs),
            "extras": len(extras),
        },
    )

    return ScanResult(
        folder=root,
        video=video,
        video_size=video_size,
        folder_size=folder_size,
        subtitles=subs,
        ignored=ignored,
    )


def _folder_size(root: Path) -> int:
    """Size of everything under *root*, counting hardlinked inodes once.

    Counting an inode twice would inflate the total and make the pre/post-upload
    comparison fail, so deleted files would look like a mismatch forever.
    """
    total = 0
    seen: set[tuple[int, int]] = set()
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                st = path.lstat()
            except OSError:
                continue
            if st.st_nlink > 1:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            total += st.st_size
    return total


def parse_movie_identity(folder_name: str) -> tuple[str, int | None]:
    """Split a Radarr folder name into title and year.

    Radarr's default is ``Title (2024)``, so the trailing parenthesised year is
    the reliable marker.
    """
    match = re.match(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)\s*$", folder_name)
    if match:
        return match.group("title").strip(), int(match.group("year"))
    return folder_name.strip(), None