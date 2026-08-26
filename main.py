"""Video catalogue and streaming server."""

from __future__ import annotations

import datetime
import logging
import shutil
import subprocess
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined
from pydantic import BaseModel, Field, field_validator

import boorus
import database
import downloads
import utils
from config import settings
from middleware import whitelist_middleware
from ranges import RangeNotSatisfiable, parse_range
from tasks import registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("streamserver")

MIME_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
STREAM_CHUNK_SIZE = 1024 * 1024

# Serialises library scans so a directory switch cannot start a second scan
# while the first is still transcoding.
_scan_lock = threading.Lock()


def _run_scan(task_id: str, directory: Path) -> None:
    """Scan a library in a worker thread, reporting through the task registry."""
    if not _scan_lock.acquire(blocking=False):
        registry.update(task_id, status="failed", error="A library scan is already running")
        return
    try:
        registry.update(task_id, status="scanning", progress=10)
        result = utils.scan_library(directory)
        registry.update(task_id, status="completed", progress=100, error=None)
        log.info("Scan of %s finished: %s", directory, result)
    except Exception as exc:  # noqa: BLE001 - surfaced to the client
        log.exception("Library scan of %s failed", directory)
        registry.update(task_id, status="failed", error=str(exc))
    finally:
        _scan_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)
    database.init_db()

    # Transcoding on the event loop would stall every in-flight stream, so the
    # initial scan runs on a worker thread and the server starts serving now.
    task_id = registry.create("scan")
    app.state.startup_scan = task_id
    threading.Thread(
        target=_run_scan, args=(task_id, database.current_dir()), daemon=True
    ).start()

    yield


app = FastAPI(title="StreamServer", lifespan=lifespan)
app.middleware("http")(whitelist_middleware)

# No CORS middleware: the UI is served from this same origin, and the previous
# allow_origins=["*"] with allow_credentials=True let any site a whitelisted
# user visited call these unauthenticated POST and DELETE routes.

templates = Jinja2Templates(directory="templates")
# Surfaces a template variable typo as an error instead of rendering an empty
# string, which is how the player title silently went missing.
templates.env.undefined = StrictUndefined


def _get_video_or_404(video_id: str) -> dict[str, Any]:
    video = database.get_video_by_id(video_id)
    if not video:
        raise HTTPException(status_code=404, detail=f"Video with ID '{video_id}' not found.")
    return video


def _resolved_video_path(video: dict[str, Any]) -> Path:
    """Containment-checked path for a row, 404 rather than leaking the reason."""
    try:
        path = utils.video_path(video)
    except utils.UnsafePathError:
        log.error("Row %s has a path escaping its directory: %r", video["id"], video["path"])
        raise HTTPException(status_code=404, detail="Video file not found.") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found.")
    return path


# --- pages -------------------------------------------------------------------


SORT_OPTIONS = ("newest", "title", "longest", "largest")

# Enough to browse a large tag vocabulary without rendering a wall of chips.
MAX_TAG_CHIPS = 40


@app.get("/")
async def index(
    request: Request,
    sort: str = Query(default="newest"),
    q: str = Query(default="", max_length=200),
    tag: Annotated[list[str] | None, Query()] = None,
    page: int = Query(default=1, ge=1),
):
    """The catalogue grid.

    ``q`` is a substring search over title, description and tags; ``tag`` may
    be repeated and narrows conjunctively. Both are query parameters rather
    than server-side state, so two people browsing at once do not fight over
    one shared filter -- the same reasoning that already applies to ``sort``.
    """
    if sort not in SORT_OPTIONS:
        sort = "newest"

    query = q.strip()
    # Deduplicated, order preserved, and capped so a hand-written URL cannot
    # turn one page render into thousands of set comparisons per row.
    selected_tags: list[str] = []
    for value in tag or []:
        cleaned = value.strip().casefold()
        if cleaned and cleaned not in selected_tags:
            selected_tags.append(cleaned)
        if len(selected_tags) >= MAX_TAG_CHIPS:
            break

    directory = database.current_dir()
    results = utils.browse_videos(
        sort_by=sort, directory=directory, query=query, tags=selected_tags, page=page
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "video_files": results.videos,
            "page": results,
            "timestamp": int(datetime.datetime.now().timestamp()),
            "sibling_folders": utils.get_sibling_folders(directory),
            "current_folder": directory.name,
            "current_sort": sort,
            "current_query": query,
            "selected_tags": selected_tags,
            "available_tags": utils.collect_tags(directory)[:MAX_TAG_CHIPS],
            "is_filtered": bool(query or selected_tags),
            "scan_task_id": getattr(app.state, "startup_scan", None),
        },
    )


@app.get("/play/{video_id}")
async def play_video(video_id: str, request: Request):
    video = _get_video_or_404(video_id)
    return templates.TemplateResponse(
        request,
        "play_mp4.html",
        {
            "video_id": video_id,
            "video_title": video.get("title", ""),
            "video_description": video.get("description", ""),
            "video_tags": video.get("tags", []),
        },
    )


# --- streaming ---------------------------------------------------------------


@app.get("/videos/{video_id}")
async def stream_video(video_id: str, range: str | None = Header(default=None)):
    """Serve a video, honouring byte ranges so clients can seek."""
    video = _get_video_or_404(video_id)
    path = _resolved_video_path(video)

    file_size = path.stat().st_size
    mime_type = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")

    try:
        byte_range = parse_range(range, file_size)
    except RangeNotSatisfiable:
        # RFC 9110 requires the unsatisfied-range form of Content-Range here.
        return Response(
            status_code=416,
            headers={
                "Content-Range": f"bytes */{file_size}",
                "Accept-Ranges": "bytes",
            },
        )

    if byte_range is None:
        # No usable Range header: the whole entity, as 200, not a bogus 206.
        return StreamingResponse(
            downloads.iter_file_range(path, 0, max(file_size - 1, 0), STREAM_CHUNK_SIZE),
            status_code=200,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Content-Type": mime_type,
            },
        )

    start, end = byte_range
    return StreamingResponse(
        downloads.iter_file_range(path, start, end, STREAM_CHUNK_SIZE),
        status_code=206,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": mime_type,
        },
    )


# --- library management ------------------------------------------------------


class ChangeDirectoryRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=255)


@app.post("/api/change-directory")
async def change_directory(payload: ChangeDirectoryRequest):
    """Switch to a sibling library.

    Only names the server itself offered are accepted. Joining an arbitrary
    string onto the parent path is what let ``{"folder": "/etc"}`` repoint the
    whole application: both pathlib and os.path.join discard the left operand
    when the right one is absolute.
    """
    directory = database.current_dir()
    if payload.folder not in utils.get_sibling_folders(directory):
        raise HTTPException(status_code=404, detail="Folder not found")

    try:
        target = utils.resolve_within(settings.parent_dir, payload.folder)
    except utils.UnsafePathError:
        raise HTTPException(status_code=404, detail="Folder not found") from None

    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")

    database.set_current_dir(target)

    # Scanning can transcode; it must not run on the event loop.
    task_id = registry.create("scan")
    app.state.startup_scan = task_id
    threading.Thread(target=_run_scan, args=(task_id, target), daemon=True).start()

    return {"message": f"Directory changed to {payload.folder}", "task_id": task_id}


@app.get("/api/task-status/{task_id}")
async def get_task_status(task_id: str):
    task = registry.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# --- downloading -------------------------------------------------------------


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


@app.post("/api/download")
async def download_video(payload: DownloadRequest, background_tasks: BackgroundTasks):
    """Accept either a direct media URL or a booru post page.

    A booru post is resolved here rather than inside the background task so the
    user finds out immediately, and specifically, that the post they pasted is
    a PNG or is login-restricted -- instead of watching a progress bar and then
    reading a generic failure.
    """
    url = payload.url.strip()
    page_url: str | None = None
    title: str | None = None
    tags: list[str] = []

    if boorus.find_site(url) is not None:
        try:
            # Resolution makes two blocking HTTP calls; the event loop is
            # serving in-flight video streams and must not do them.
            post = await run_in_threadpool(boorus.resolve_post, url)
        except boorus.BooruError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except (OSError, ValueError) as exc:
            log.warning("Booru resolution of %s failed: %s", url, exc)
            raise HTTPException(
                status_code=400, detail=f"Could not read that booru page: {exc}"
            ) from None
        page_url, title, url = post.page_url, post.title, post.file_url
        # A booru post already carries the vocabulary this catalogue wants to
        # search by, so the import brings it along rather than landing untagged.
        tags = list(post.tags)

    extension = downloads.url_extension(url)
    if extension not in downloads.ALLOWED_EXTENSIONS:
        supported = ", ".join(downloads.ALLOWED_EXTENSIONS)
        if page_url is not None:
            detail = (
                f"That post's original file is {extension or 'of an unknown type'}; "
                f"this server stores video files only ({supported})."
            )
        else:
            detail = f"URL must point at a video file ({supported})."
        raise HTTPException(status_code=400, detail=detail)

    try:
        downloads.assert_safe_url(url)
    except downloads.UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    task_id = registry.create("download")
    background_tasks.add_task(
        process_download_task,
        task_id,
        url,
        extension,
        database.current_dir(),
        page_url=page_url,
        title=title,
        tags=tags,
    )
    return {"task_id": task_id}


def process_download_task(
    task_id: str,
    url: str,
    extension: str,
    directory: Path,
    page_url: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Fetch, transcode if needed, thumbnail, and register one remote video.

    ``page_url`` is the booru post a direct URL came from, when there was one.
    It is recorded as the description so the entry can be traced back, and sent
    as the Referer because booru CDNs commonly refuse hotlinked requests.
    ``tags`` are the post's own tags, when the site's API supplied them.
    """
    video_id = str(uuid.uuid4())
    remote_name = downloads.url_filename(url) or f"{video_id}{extension}"

    try:
        registry.update(task_id, status="downloading", progress=0)
        response = downloads.open_stream(
            url, headers={"Referer": page_url} if page_url else None
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            # The remote filename is metadata only; the file on disk is named
            # from our own UUID so a hostile name cannot steer the write.
            source = Path(tmp_dir) / f"download{extension}"

            def on_progress(fraction: float | None) -> None:
                if fraction is not None:
                    registry.update(task_id, progress=int(fraction * 30))

            with response:
                downloads.stream_to_file(response, source, on_progress)

            mp4_name = f"{video_id}.mp4"
            destination = directory / mp4_name
            archived_original = None

            if extension == ".mp4":
                shutil.move(str(source), destination)
                registry.update(task_id, progress=60)
            else:
                registry.update(task_id, status="converting", progress=30)
                # Often a remux rather than a re-encode: plenty of remote MKVs
                # and MOVs already hold H.264 that MP4 can carry untouched.
                utils.convert_to_mp4(source, destination)
                registry.update(task_id, progress=60)

                archive = utils.get_originals_dir(directory)
                archive.mkdir(parents=True, exist_ok=True)
                archived_original = utils.get_unique_filename(
                    f"{video_id}_original{extension}", archive
                )
                shutil.move(str(source), archive / archived_original)

            registry.update(task_id, status="generating_thumbnail", progress=80)
            try:
                utils.generate_thumbnail(destination, utils.thumbnail_path(video_id))
            except (subprocess.SubprocessError, OSError, ValueError) as exc:
                # A missing thumbnail is cosmetic; the video is still usable.
                log.warning("Thumbnail generation failed for %s: %s", destination, exc)

            database.add_video_to_db(
                {
                    "id": video_id,
                    "directory": str(directory.resolve()),
                    "original_filename": remote_name,
                    # A booru names its files by md5, which makes a useless
                    # label; the resolver supplies "gelbooru 12345" instead.
                    "title": title or Path(remote_name).stem,
                    "path": mp4_name,
                    "thumbnail_path": f"{video_id}.jpg",
                    "creation_date": datetime.datetime.now().isoformat(),
                    "description": page_url or "",
                    "tags": list(tags or []),
                    **utils.probe_media(destination).as_row_fields(),
                    "source_name": None,
                    "original_webm": archived_original,
                }
            )

        registry.update(task_id, status="completed", progress=100)
    except Exception as exc:  # noqa: BLE001 - reported through the task record
        log.exception("Download task %s failed", task_id)
        registry.update(task_id, status="failed", error=str(exc))


# --- metadata ----------------------------------------------------------------


MAX_TAG_LENGTH = 100


class UpdateVideoRequest(BaseModel):
    """A *partial* update: an omitted field is left as it was.

    Every field used to be mandatory, so the rename button -- which only has a
    title to send -- posted an empty description and an empty tag list along
    with it, silently destroying both. For a video imported from a booru that
    meant losing its tags and the post URL it came from, which is the only
    record of where the file originated.
    """

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = Field(default=None, max_length=50)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, tags: list[str] | None) -> list[str] | None:
        """Trim, lowercase and de-duplicate, preserving the order given."""
        if tags is None:
            return None
        cleaned: list[str] = []
        for tag in tags:
            value = tag.strip().lower()
            if not value or value in cleaned:
                continue
            if len(value) > MAX_TAG_LENGTH:
                raise ValueError(f"Tag {value[:20]!r}... exceeds {MAX_TAG_LENGTH} characters")
            cleaned.append(value)
        return cleaned

    def changes(self) -> dict[str, Any]:
        """Only the fields the client actually sent."""
        return {
            key: value
            for key, value in (
                ("title", self.title),
                ("description", self.description),
                ("tags", self.tags),
            )
            if value is not None
        }


@app.post("/api/videos/{video_id}/update")
async def update_video_metadata(video_id: str, payload: UpdateVideoRequest):
    _get_video_or_404(video_id)

    changes = payload.changes()
    if not changes:
        raise HTTPException(
            status_code=400, detail="Provide at least one of title, description or tags."
        )

    updated = database.update_video_in_db(video_id, changes)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update video metadata")
    return {"detail": "Video metadata updated successfully", "video": updated}


@app.delete("/api/videos/{video_id}")
async def delete_video(video_id: str):
    """Delete a video, its thumbnail, and any archived original."""
    video = _get_video_or_404(video_id)
    directory = Path(video.get("directory") or database.current_dir())

    try:
        utils.video_path(video).unlink(missing_ok=True)
    except (utils.UnsafePathError, OSError) as exc:
        log.warning("Could not remove video file for %s: %s", video_id, exc)

    try:
        utils.thumbnail_path(video_id).unlink(missing_ok=True)
    except (utils.UnsafePathError, OSError) as exc:
        log.warning("Could not remove thumbnail for %s: %s", video_id, exc)

    # The archived WebM is usually the largest file of the three; leaving it
    # behind meant deletions never actually freed space.
    archived = video.get("original_webm")
    if archived:
        try:
            utils.resolve_within(utils.get_originals_dir(directory), archived).unlink(
                missing_ok=True
            )
        except (utils.UnsafePathError, OSError) as exc:
            log.warning("Could not remove archived original for %s: %s", video_id, exc)

    if not database.delete_video_from_db(video_id, directory):
        raise HTTPException(status_code=500, detail="Failed to delete video from database")
    return {"detail": "Video deleted successfully"}


@app.post("/api/videos/{video_id}/thumbnail")
async def generate_custom_thumbnail(
    video_id: str,
    time: str = Query(default="00:00:01", description="Time in HH:MM:SS format"),
):
    """Regenerate a thumbnail from the frame at *time*."""
    video = _get_video_or_404(video_id)
    path = _resolved_video_path(video)

    try:
        # generate_thumbnail writes to a temp file and only replaces the
        # existing thumbnail once ffmpeg has actually produced a frame.
        utils.generate_thumbnail(path, utils.thumbnail_path(video_id), timestamp=time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (subprocess.SubprocessError, OSError) as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to generate thumbnail: {exc}"
        ) from None

    return {"detail": "Thumbnail successfully updated."}


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)
app.mount(
    "/thumbnails",
    StaticFiles(directory=str(settings.thumbnail_dir)),
    name="thumbnails",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        # uvicorn ships its own proxy-header middleware, enabled by default and
        # trusting 127.0.0.1, which rewrites scope["client"] from
        # X-Forwarded-For before any application middleware runs. Turning it off
        # keeps forwarded-header policy in one place: middleware.client_ip,
        # which honours the header only for configured trusted_proxies.
        proxy_headers=False,
    )
