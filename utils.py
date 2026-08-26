"""Filesystem, ffmpeg and library-scanning helpers.

ffmpeg is driven through ``subprocess`` with an explicit argument list rather
than through ffmpeg-python: it removes a dependency, and it lets every
invocation carry a timeout, which the library did not make convenient.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from config import settings
from database import (
    add_videos_to_db,
    current_dir,
    list_videos,
    prune_missing,
)

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = (".mp4", ".webm")
ORIGINAL_WEBM_DIRNAME = "original_webm"
TIMECODE_RE = re.compile(r"^\d{1,2}:[0-5]\d:[0-5]\d(\.\d{1,3})?$")


class UnsafePathError(ValueError):
    """A user-supplied path escaped the directory it was meant to stay in."""


# --- path safety -------------------------------------------------------------


def resolve_within(base: Path, candidate: str | Path) -> Path:
    """Join *candidate* onto *base* and refuse anything that escapes it.

    Guards two distinct traps: ``Path`` and ``os.path.join`` both discard the
    left operand when the right one is absolute, and ``..`` segments walk up.
    """
    base_resolved = Path(base).resolve()
    target = (base_resolved / candidate).resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise UnsafePathError(f"{candidate!r} resolves outside {base_resolved}")
    return target


def video_path(video: dict[str, Any]) -> Path:
    """Absolute on-disk path for a database row, containment-checked."""
    directory = Path(video.get("directory") or current_dir())
    return resolve_within(directory, video["path"])


def thumbnail_path(video_id: str) -> Path:
    return resolve_within(settings.thumbnail_dir, f"{video_id}.jpg")


def get_original_webm_dir(directory: Path | None = None) -> Path:
    return Path(directory or current_dir()) / ORIGINAL_WEBM_DIRNAME


def get_unique_filename(original_filename: str, directory: Path) -> str:
    """Append a counter until the name is free in *directory*."""
    stem = Path(original_filename).stem
    suffix = Path(original_filename).suffix
    filename = f"{stem}{suffix}"
    counter = 1
    while (Path(directory) / filename).exists():
        filename = f"{stem}_{counter}{suffix}"
        counter += 1
    return filename


def get_sibling_folders(directory: Path | None = None) -> list[str]:
    """Names of the directories offered as switchable libraries."""
    directory = Path(directory or current_dir())
    parent = settings.parent_dir
    try:
        return sorted(
            child.name
            for child in parent.iterdir()
            if child.is_dir() and child.resolve() != directory.resolve()
        )
    except OSError as exc:
        log.warning("Cannot list sibling folders of %s: %s", parent, exc)
        return []


# --- ffmpeg ------------------------------------------------------------------


def has_audio_stream(path: Path) -> bool:
    """True when the file carries at least one audio stream."""
    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=settings.ffmpeg_timeout,
            check=True,
        )
        return bool(json.loads(result.stdout or b"{}").get("streams"))
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        log.warning("ffprobe failed for %s: %s", path, exc)
        return False


def generate_thumbnail(source: Path, destination: Path, timestamp: str = "00:00:01") -> Path:
    """Write a JPEG thumbnail for *source* at *timestamp*.

    The frame is rendered to a temporary file and moved into place only on
    success, so a failure never destroys the thumbnail that was already there.
    Raises ``subprocess.SubprocessError`` on failure rather than returning None,
    so callers cannot mistake a failure for success.
    """
    if not TIMECODE_RE.match(timestamp):
        raise ValueError(f"Invalid timecode {timestamp!r}; expected HH:MM:SS")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # delete=False because ffmpeg writes the file and it is renamed into place.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        dir=destination.parent,
        prefix=f".{destination.stem}.",
        suffix=".jpg",
        delete=False,
    )
    handle.close()
    temp_path = Path(handle.name)

    command = [
        "ffmpeg",
        "-ss", timestamp,
        "-i", str(source),
        "-vf", f"scale='min({settings.thumbnail_width},iw)':-2",
        "-frames:v", "1",
        "-qscale:v", "4",
        "-y",
        "-loglevel", "error",
        "-f", "image2",
        str(temp_path),
    ]

    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=settings.ffmpeg_timeout,
            check=True,
        )
        if not temp_path.exists() or temp_path.stat().st_size == 0:
            raise subprocess.SubprocessError(
                f"ffmpeg produced no frame for {source} at {timestamp}"
            )
        os.replace(temp_path, destination)
        return destination
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def convert_to_mp4(source: Path, destination: Path) -> Path:
    """Transcode to H.264/AAC, writing to a temp file until it succeeds."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.partial.mp4")

    command = [
        "ffmpeg",
        "-i", str(source),
        "-c:v", "libx264",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-y",
        "-loglevel", "error",
        str(temp_path),
    ]

    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=settings.convert_timeout,
            check=True,
        )
        os.replace(temp_path, destination)
        return destination
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


# --- library scanning --------------------------------------------------------


def _new_row(directory: Path, filename: str, *, source_name: str | None = None) -> dict[str, Any]:
    path = Path(directory) / filename
    video_id = str(uuid.uuid4())
    created = datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    return {
        "id": video_id,
        "directory": str(Path(directory).resolve()),
        "original_filename": source_name or filename,
        "title": Path(source_name or filename).stem,
        "path": filename,
        "thumbnail_path": f"{video_id}.jpg",
        "creation_date": created,
        "description": "",
        "tags": [],
        "has_audio": has_audio_stream(path),
        # Recorded so a re-scan can tell that this WebM was already converted.
        # The previous check compared against a freshly generated UUID and so
        # could never be true.
        "source_name": source_name,
    }


def _ensure_thumbnail(video_id: str, source: Path) -> None:
    destination = thumbnail_path(video_id)
    if destination.exists():
        return
    try:
        generate_thumbnail(source, destination)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        log.warning("Thumbnail generation failed for %s: %s", source, exc)


def convert_webm_files(directory: Path) -> int:
    """Transcode loose WebM files to MP4 and archive the originals.

    Runs *before* the MP4 scan so a WebM is never registered under a path that
    conversion is about to move away.
    """
    directory = Path(directory)
    archive = get_original_webm_dir(directory)
    converted = 0

    already_converted = {
        row["source_name"]
        for row in list_videos(directory)
        if row.get("source_name") and (directory / row["path"]).exists()
    }

    for entry in sorted(directory.iterdir()):
        if not entry.is_file() or entry.suffix.lower() != ".webm":
            continue

        archive.mkdir(parents=True, exist_ok=True)

        if entry.name in already_converted:
            # Converted on an earlier run; just move the source out of the way.
            shutil.move(str(entry), archive / get_unique_filename(entry.name, archive))
            continue

        mp4_name = f"{uuid.uuid4()}.mp4"
        try:
            convert_to_mp4(entry, directory / mp4_name)
        except (subprocess.SubprocessError, OSError) as exc:
            log.error("Conversion failed for %s: %s", entry, exc)
            continue

        row = _new_row(directory, mp4_name, source_name=entry.name)
        archived = get_unique_filename(entry.name, archive)
        shutil.move(str(entry), archive / archived)
        row["original_webm"] = archived

        add_videos_to_db([row])
        _ensure_thumbnail(row["id"], directory / mp4_name)
        converted += 1

    return converted


def scan_library(directory: Path | None = None) -> dict[str, int]:
    """Bring the database in line with what is on disk for one directory."""
    directory = Path(directory or current_dir())
    if not directory.is_dir():
        log.warning("Cannot scan %s: not a directory", directory)
        return {"converted": 0, "added": 0, "pruned": 0}

    converted = convert_webm_files(directory)

    on_disk = {
        entry.name
        for entry in directory.iterdir()
        if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS
    }
    known = {row["path"] for row in list_videos(directory)}

    new_rows = [_new_row(directory, name) for name in sorted(on_disk - known)]
    add_videos_to_db(new_rows)

    # Rows whose file has gone are removed rather than filtered out on every
    # read, so they stop accumulating invisibly.
    pruned = prune_missing(directory, on_disk)

    for row in list_videos(directory):
        source = directory / row["path"]
        if source.exists():
            _ensure_thumbnail(row["id"], source)

    log.info(
        "Scanned %s: %d converted, %d added, %d pruned",
        directory, converted, len(new_rows), pruned,
    )
    return {"converted": converted, "added": len(new_rows), "pruned": pruned}


def matches_query(row: dict[str, Any], query: str) -> bool:
    """True when *query* appears in the row's title, description or tags.

    Matching is case-insensitive substring, across every field a user might
    remember the video by. Tags match as substrings too, so searching "hair"
    finds ``blue_hair`` without the user having to know the exact tag.
    """
    needle = query.casefold()
    if needle in str(row.get("title") or "").casefold():
        return True
    if needle in str(row.get("description") or "").casefold():
        return True
    return any(needle in str(tag).casefold() for tag in row.get("tags") or ())


def matches_tags(row: dict[str, Any], tags: Sequence[str]) -> bool:
    """True when the row carries *every* requested tag.

    Conjunctive rather than disjunctive: selecting a second tag should narrow
    the grid, which is what makes clicking through tags a way to find one
    video rather than a way to widen the result set.
    """
    present = {str(tag).casefold() for tag in row.get("tags") or ()}
    return all(tag.casefold() in present for tag in tags)


def collect_tags(directory: Path | None = None) -> list[tuple[str, int]]:
    """Every tag in *directory* with its use count, most-used first."""
    counts: Counter[str] = Counter()
    for row in list_videos(directory or current_dir()):
        counts.update(str(tag).casefold() for tag in row.get("tags") or ())
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def get_video_files(
    sort_by: str = "newest",
    directory: Path | None = None,
    query: str = "",
    tags: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Rows for the catalogue view: filtered, then newest or title-sorted."""
    directory = Path(directory or current_dir())
    videos = []
    query = query.strip()

    for row in list_videos(directory):
        if query and not matches_query(row, query):
            continue
        if tags and not matches_tags(row, tags):
            continue
        try:
            source = video_path(row)
        except UnsafePathError:
            log.error("Skipping row %s with unsafe path %r", row.get("id"), row.get("path"))
            continue
        if not source.exists():
            continue

        has_thumbnail = thumbnail_path(row["id"]).exists()
        videos.append(
            {
                "id": row["id"],
                "title": row.get("title") or Path(row["path"]).stem,
                "path": row["path"],
                "thumbnail": f"/thumbnails/{row['id']}.jpg" if has_thumbnail else None,
                "has_thumbnail": has_thumbnail,
                "has_audio": row.get("has_audio", True),
                "creation_date": row.get("creation_date") or "",
                "description": row.get("description", ""),
                "tags": row.get("tags", []),
            }
        )

    if sort_by == "title":
        videos.sort(key=lambda video: video["title"].lower())
    else:
        videos.sort(key=lambda video: video["creation_date"], reverse=True)
    return videos
