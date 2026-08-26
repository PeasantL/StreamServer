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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import settings
from database import (
    add_videos_to_db,
    current_dir,
    list_videos,
    prune_missing,
    update_video_in_db,
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


@dataclass(frozen=True)
class MediaInfo:
    """What one ffprobe call tells us about a file.

    Every field is optional because ffprobe reports what a container happens
    to carry: a stream copy from a source with no duration header, or a probe
    that timed out, leaves gaps rather than failing the import.
    """

    has_audio: bool = False
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    size_bytes: int | None = None

    def as_row_fields(self) -> dict[str, Any]:
        """The subset stored on a database row."""
        return {
            "has_audio": self.has_audio,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "size_bytes": self.size_bytes,
        }


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # ffprobe reports "N/A" as nan for some containers.
    return result if result == result and result >= 0 else None


def probe_media(path: Path) -> MediaInfo:
    """Read duration, dimensions, codecs and size in a single ffprobe call.

    This used to be ``has_audio_stream``, which paid the full cost of spawning
    ffprobe and then discarded everything except whether an audio stream
    existed. The same invocation with ``-show_format -show_streams`` returns
    the rest for free, which is what the duration badges, the resolution
    labels and the remux decision in ``convert_to_mp4`` are built on.

    A failure is not fatal: an unreadable file yields an empty ``MediaInfo``
    and the video is still catalogued.
    """
    command = [
        "ffprobe",
        "-v", "error",
        "-show_format",
        "-show_streams",
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
        data = json.loads(result.stdout or b"{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        log.warning("ffprobe failed for %s: %s", path, exc)
        return MediaInfo()

    if not isinstance(data, dict):
        return MediaInfo()

    streams = [item for item in data.get("streams") or [] if isinstance(item, dict)]
    container = data.get("format") if isinstance(data.get("format"), dict) else {}

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # Duration lives on the container for MP4 and on the video stream for some
    # WebM files, so fall back rather than reporting an unknown length.
    duration = _as_float(container.get("duration"))
    if duration is None and video is not None:
        duration = _as_float(video.get("duration"))

    return MediaInfo(
        has_audio=audio is not None,
        duration=duration,
        width=_as_int(video.get("width")) if video else None,
        height=_as_int(video.get("height")) if video else None,
        video_codec=str(video.get("codec_name")) if video and video.get("codec_name") else None,
        audio_codec=str(audio.get("codec_name")) if audio and audio.get("codec_name") else None,
        size_bytes=_as_int(container.get("size")),
    )


def has_audio_stream(path: Path) -> bool:
    """True when the file carries at least one audio stream."""
    return probe_media(path).has_audio


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
        **probe_media(path).as_row_fields(),
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


# Present on every row written since media probing was introduced. Its absence
# is what distinguishes a row that predates the feature from one whose probe
# genuinely found nothing.
_PROBE_MARKER = "video_codec"


def backfill_media_info(directory: Path) -> int:
    """Probe rows catalogued before duration and size were recorded.

    Without this an existing library would show no badges until every file was
    re-added by hand. Rows already carrying the fields are skipped, so the
    cost is paid once rather than on every scan.
    """
    filled = 0
    for row in list_videos(directory):
        if _PROBE_MARKER in row:
            continue
        source = Path(directory) / row["path"]
        if not source.exists():
            continue
        update_video_in_db(row["id"], probe_media(source).as_row_fields(), directory)
        filled += 1

    if filled:
        log.info("Backfilled media info for %d row(s) in %s", filled, directory)
    return filled


def scan_library(directory: Path | None = None) -> dict[str, int]:
    """Bring the database in line with what is on disk for one directory."""
    directory = Path(directory or current_dir())
    if not directory.is_dir():
        log.warning("Cannot scan %s: not a directory", directory)
        return {"converted": 0, "added": 0, "pruned": 0, "backfilled": 0}

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

    filled = backfill_media_info(directory)

    log.info(
        "Scanned %s: %d converted, %d added, %d pruned, %d backfilled",
        directory, converted, len(new_rows), pruned, filled,
    )
    return {
        "converted": converted,
        "added": len(new_rows),
        "pruned": pruned,
        "backfilled": filled,
    }


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


def format_duration(seconds: float | None) -> str:
    """``H:MM:SS`` past an hour, ``M:SS`` below it. Empty when unknown."""
    if seconds is None or seconds < 0:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_size(size_bytes: int | None) -> str:
    """Human-readable file size. Empty when unknown."""
    if size_bytes is None or size_bytes < 0:
        return ""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def format_resolution(height: int | None) -> str:
    """The shorthand people actually read: 1080p, 720p, and so on."""
    return f"{height}p" if height else ""


@dataclass(frozen=True)
class VideoPage:
    """One page of the catalogue, plus what is needed to page through it."""

    videos: list[dict[str, Any]]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, -(-self.total // self.page_size))

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def first_index(self) -> int:
        """1-based index of the first item shown, for "x-y of z"."""
        return 0 if not self.total else (self.page - 1) * self.page_size + 1

    @property
    def last_index(self) -> int:
        return min(self.page * self.page_size, self.total)


def _sort_key(sort_by: str):
    """(key, reverse) for one of the supported orderings.

    A row probed before the media fields existed, or one whose probe failed,
    sorts to the end of a duration or size ordering rather than to the front.
    """
    if sort_by == "title":
        return (lambda row: str(row.get("title") or Path(row["path"]).stem).lower()), False
    if sort_by == "longest":
        return (lambda row: row.get("duration") or 0), True
    if sort_by == "largest":
        return (lambda row: row.get("size_bytes") or 0), True
    return (lambda row: str(row.get("creation_date") or "")), True


def _view_model(row: dict[str, Any]) -> dict[str, Any]:
    """The shape the template renders. Built per *page*, not per library."""
    has_thumbnail = thumbnail_path(row["id"]).exists()
    return {
        "id": row["id"],
        "title": row.get("title") or Path(row["path"]).stem,
        "path": row["path"],
        "thumbnail": f"/thumbnails/{row['id']}.jpg" if has_thumbnail else None,
        "has_thumbnail": has_thumbnail,
        "has_audio": row.get("has_audio", True),
        "duration": row.get("duration"),
        "duration_label": format_duration(row.get("duration")),
        "resolution_label": format_resolution(row.get("height")),
        "size_bytes": row.get("size_bytes"),
        "size_label": format_size(row.get("size_bytes")),
        "creation_date": row.get("creation_date") or "",
        "description": row.get("description", ""),
        "tags": row.get("tags", []),
    }


def browse_videos(
    sort_by: str = "newest",
    directory: Path | None = None,
    query: str = "",
    tags: Sequence[str] = (),
    page: int = 1,
    page_size: int | None = None,
) -> VideoPage:
    """Filter, sort and page the catalogue for one directory.

    Rows are filtered and sorted first and only the surviving page is turned
    into a view model, so the per-tile thumbnail stat is paid for the videos
    actually rendered rather than for the whole library. The file-existence
    check cannot be deferred the same way: it decides whether a row counts at
    all, and a total that included missing files would page to empty screens.
    """
    directory = Path(directory or current_dir())
    query = query.strip()
    page_size = settings.page_size if page_size is None else page_size

    matched = []
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
        matched.append(row)

    key, reverse = _sort_key(sort_by)
    matched.sort(key=key, reverse=reverse)

    total = len(matched)
    if page_size <= 0:
        return VideoPage([_view_model(row) for row in matched], total, 1, max(total, 1))

    # Clamped rather than 404: a deletion can shrink the library under a
    # bookmarked page number, and an empty screen is a worse answer than the
    # last real page.
    pages = max(1, -(-total // page_size))
    page = min(max(page, 1), pages)
    start = (page - 1) * page_size

    return VideoPage(
        videos=[_view_model(row) for row in matched[start:start + page_size]],
        total=total,
        page=page,
        page_size=page_size,
    )


def get_video_files(
    sort_by: str = "newest",
    directory: Path | None = None,
    query: str = "",
    tags: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Every matching row for a directory, unpaged."""
    return browse_videos(
        sort_by=sort_by, directory=directory, query=query, tags=tags, page_size=0
    ).videos
