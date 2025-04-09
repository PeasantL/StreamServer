from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Header, Query, BackgroundTasks
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import os
import tempfile
import uuid
import shutil
import json
import ffmpeg
import requests
import datetime
from pydantic import BaseModel
from typing import List

from middleware import add_cors_middleware, whitelist_middleware
from utils import *
from database import *

app = FastAPI()

# Configuration
THUMBNAIL_DIR = "thumbnails"


os.makedirs(THUMBNAIL_DIR, exist_ok=True)

# Get video files using the database
def get_video_files():
    """Get a list of video files from the database."""
    db = load_db()
    video_files = []
    
    for video in db["videos"]:
        # Check if the file actually exists
        video_path = os.path.join(get_video_dir(), video["path"])
        if os.path.exists(video_path):
            thumbnail_path = f"/thumbnails/{video['id']}.jpg"
            thumbnail_exists = os.path.exists(os.path.join(THUMBNAIL_DIR, f"{video['id']}.jpg"))
            
            video_files.append({
                "id": video["id"],
                "title": video["title"],
                "path": video["path"],
                "thumbnail": thumbnail_path if thumbnail_exists else None,
                "has_thumbnail": thumbnail_exists,
                "has_audio": video.get("has_audio", True),
                "creation_date": video.get("creation_date"),
                "description": video.get("description", ""),
                "tags": video.get("tags", [])
            })
    
    # Sort by creation date, newest first
    video_files.sort(key=lambda x: x.get("creation_date", ""), reverse=True)
    return video_files

templates = Jinja2Templates(directory="templates")
add_cors_middleware(app)
app.middleware("http")(whitelist_middleware)

# Application Events
@app.on_event("startup")
async def startup_tasks():
    """Run startup tasks including database initialization and thumbnail generation."""
    init_db()  # Initialize the database if it doesn't exist
    migrate_existing_videos()  # Migrate existing videos to the database
    process_existing_webm_files()
    create_thumbnails_on_startup()

def migrate_existing_videos():
    """Migrate existing videos to the database if they're not already there."""
    db = load_db()
    # Create a set of paths that are already in the database
    existing_paths = {video["path"] for video in db["videos"]}
    
    for file in os.listdir(get_video_dir()):
        if file.lower().endswith(('.mp4', '.webm')) and file not in existing_paths:
            # This video is not in the database, add it
            video_id = str(uuid.uuid4())
            creation_time = datetime.datetime.fromtimestamp(
                os.path.getctime(os.path.join(get_video_dir(), file))
            ).isoformat()
            
            # Check if the video has audio
            has_audio = has_audio_stream(os.path.join(get_video_dir(), file))
            
            # Add to database
            add_video_to_db({
                "id": video_id,
                "original_filename": file,
                "title": os.path.splitext(file)[0],  # Default title is filename without extension
                "path": file,
                "thumbnail_path": f"{video_id}.jpg",
                "creation_date": creation_time,
                "description": "",
                "tags": [],
                "has_audio": has_audio
            })
            
            # Generate thumbnail if it doesn't exist
            thumbnail_path = os.path.join(THUMBNAIL_DIR, f"{video_id}.jpg")
            if not os.path.exists(thumbnail_path):
                try:
                    generate_thumbnail(
                        os.path.join(get_video_dir(), file), 
                        os.path.join(THUMBNAIL_DIR, video_id),
                        has_audio
                    )
                except Exception as e:
                    print(f"Error generating thumbnail for {file}: {e}")

# Routes
@app.get("/")
async def list_videos(request: Request):
    video_files = get_video_files()
    sibling_folders = get_sibling_folders()
    
    if not video_files:
        raise HTTPException(status_code=404, detail="No videos found.")
    
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "video_files": video_files,
            "sibling_folders": sibling_folders,
            "timestamp": datetime.datetime.now().timestamp()
        }
    )

@app.get("/videos/{video_id}")
async def stream_video(video_id: str, range: str = Header(None)):
    """Stream video files with range support."""
    # Get video info from database
    video = get_video_by_id(video_id)
    if not video:
        raise HTTPException(status_code=404, detail=f"Video with ID '{video_id}' not found.")
    
    video_path = os.path.join(get_video_dir(), video["path"])
    
    # Check if the video file exists
    if not os.path.isfile(video_path):
        raise HTTPException(status_code=404, detail=f"Video file not found at '{video_path}'.")

    file_size = os.path.getsize(video_path)
    start = 0
    end = file_size - 1

    # Parse the Range header, if present
    if range:
        range_str = range.replace("bytes=", "")
        range_values = range_str.split("-")
        try:
            if range_values[0]:
                start = int(range_values[0])
            if len(range_values) > 1 and range_values[1]:
                end = int(range_values[1])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Range header")

    # Calculate content length for the response
    content_length = end - start + 1
    ext = os.path.splitext(video["path"])[1].lower()
    mime_type = "video/mp4" if ext == ".mp4" else "video/webm"
    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Type": mime_type,
    }

    def iter_file(path, start, end):
        """Read and yield file data in chunks."""
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            chunk_size = 1024 * 1024  # 1MB chunks

            while remaining > 0:
                read_size = min(chunk_size, remaining)
                data = f.read(read_size)
                if not data:
                    break
                yield data
                remaining -= len(data)

    return StreamingResponse(
        iter_file(video_path, start, end),
        headers=headers,
        status_code=206  # Partial Content
    )

class ChangeDirectoryRequest(BaseModel):
    folder: str

@app.post("/api/change-directory")
async def change_directory(request: ChangeDirectoryRequest):
    new_folder = request.folder
    
    # Check if the folder exists as a sibling of the current video directory
    current_video_dir = get_video_dir()
    parent_directory = Path(current_video_dir).parent
    new_video_dir = parent_directory / new_folder
    
    if not new_video_dir.exists() or not new_video_dir.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")
    
    # Update the config.json file
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    config["video_dir"] = str(new_video_dir)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)
    
    return {"message": f"Directory changed to {new_folder}"}


task_status = {}

class DownloadRequest(BaseModel):
    url: str

@app.post("/api/download")
def download_video(download_request: DownloadRequest, background_tasks: BackgroundTasks):
    url = download_request.url
    if not url or not url.lower().endswith(('.webm', '.mp4')):
        raise HTTPException(status_code=400, detail="Invalid URL or unsupported file format.")
    
    task_id = str(uuid.uuid4())
    task_status[task_id] = {"status": "in_progress", "progress": 0, "error": None}

    background_tasks.add_task(process_download_task, task_id, url)

    return {"task_id": task_id}

def process_download_task(task_id, url):
    try:
        is_webm = url.lower().endswith('.webm')
        original_filename = url.split("/")[-1]

        # Download
        task_status[task_id]["status"] = "downloading"
        task_status[task_id]["progress"] = 0
        response = requests.get(url, stream=True)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        downloaded_size = 0
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_webm_path = os.path.join(tmp_dir, original_filename)
            with open(tmp_webm_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        task_status[task_id]["progress"] = int((downloaded_size / total_size) * 30)

            # Generate a unique ID for the video
            video_id = str(uuid.uuid4())
            
            if is_webm:
                # Convert to MP4
                task_status[task_id]["status"] = "converting"
                task_status[task_id]["progress"] = 30
                base_name = Path(original_filename).stem
                mp4_filename = f"{video_id}.mp4"  # Use the video ID as filename
                mp4_path = os.path.join(get_video_dir(), mp4_filename)
                (
                    ffmpeg
                    .input(tmp_webm_path)
                    .output(mp4_path, vcodec='libx264', acodec='aac')
                    .run(capture_stdout=True, capture_stderr=True)
                )
                task_status[task_id]["progress"] = 60

                # Move WebM to original directory
                original_webm_dir = get_original_webm_dir()
                os.makedirs(original_webm_dir, exist_ok=True)
                original_webm_path = os.path.join(
                    original_webm_dir, 
                    f"{video_id}_original.webm"
                )
                shutil.move(tmp_webm_path, original_webm_path)
                
                # Set path for database
                saved_path = mp4_filename
            else:
                # Direct MP4 download
                filename = f"{video_id}.mp4"  # Use the video ID as filename
                save_path = os.path.join(get_video_dir(), filename)
                shutil.move(tmp_webm_path, save_path)
                mp4_path = save_path
                saved_path = filename
                task_status[task_id]["progress"] = 60

            # Generate thumbnail
            task_status[task_id]["status"] = "generating_thumbnail"
            task_status[task_id]["progress"] = 80
            has_audio = has_audio_stream(mp4_path)
            thumbnail_path_base = os.path.join(THUMBNAIL_DIR, video_id)
            generate_thumbnail(mp4_path, thumbnail_path_base, has_audio)
            task_status[task_id]["progress"] = 90
            
            # Add to database
            creation_time = datetime.datetime.now().isoformat()
            add_video_to_db({
                "id": video_id,
                "original_filename": original_filename,
                "title": Path(original_filename).stem,  # Default title is original filename w/o extension
                "path": saved_path,
                "thumbnail_path": f"{video_id}.jpg",
                "creation_date": creation_time,
                "description": "",
                "tags": [],
                "has_audio": has_audio
            })
            task_status[task_id]["progress"] = 100

        task_status[task_id]["status"] = "completed"
    except Exception as e:
        task_status[task_id]["status"] = "failed"
        task_status[task_id]["error"] = str(e)

@app.get("/api/task-status/{task_id}")
def get_task_status(task_id: str):
    if task_id not in task_status:
        raise HTTPException(status_code=404, detail="Task not found")
    return task_status[task_id]

@app.get("/play/{video_id}")
async def play_video(video_id: str, request: Request):
    """Render an HTML page to play the video."""
    video = get_video_by_id(video_id)
    if not video:
        raise HTTPException(status_code=404, detail=f"Video with ID '{video_id}' not found.")
    
    return templates.TemplateResponse(
        "play_mp4.html", 
        {
            "request": request, 
            "video_id": video_id,
            "video_title": video.get("title", "")
        }
    )

class UpdateVideoRequest(BaseModel):
    title: str
    description: str = ""
    tags: List[str] = []

@app.post("/api/videos/{video_id}/update")
async def update_video_metadata(video_id: str, request: UpdateVideoRequest):
    """Update video metadata."""
    video = get_video_by_id(video_id)
    if not video:
        raise HTTPException(status_code=404, detail=f"Video with ID '{video_id}' not found.")
    
    update_data = {
        "title": request.title,
        "description": request.description,
        "tags": request.tags
    }
    
    updated_video = update_video_in_db(video_id, update_data)
    if updated_video:
        return {"detail": "Video metadata updated successfully", "video": updated_video}
    else:
        raise HTTPException(status_code=500, detail="Failed to update video metadata")

@app.delete("/api/videos/{video_id}")
async def delete_video(video_id: str):
    """Delete a video and its thumbnail."""
    video = get_video_by_id(video_id)
    if not video:
        raise HTTPException(status_code=404, detail=f"Video with ID '{video_id}' not found.")
    
    # Delete the video file
    video_path = os.path.join(get_video_dir(), video["path"])
    if os.path.exists(video_path):
        os.remove(video_path)
    
    # Delete the thumbnail
    thumbnail_path = os.path.join(THUMBNAIL_DIR, f"{video_id}.jpg")
    if os.path.exists(thumbnail_path):
        os.remove(thumbnail_path)
    
    # Remove from database
    if delete_video_from_db(video_id):
        return {"detail": "Video deleted successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to delete video from database")

@app.post("/api/videos/{video_id}/thumbnail")
async def generate_custom_thumbnail(
    video_id: str, 
    time: str = Query(default="00:00:01", description="Time in HH:MM:SS format")
):
    """Generate a custom thumbnail for a video at the specified time."""
    video = get_video_by_id(video_id)
    if not video:
        raise HTTPException(status_code=404, detail=f"Video with ID '{video_id}' not found.")
    
    video_path = os.path.join(get_video_dir(), video["path"])
    if not os.path.isfile(video_path):
        raise HTTPException(status_code=404, detail=f"Video file not found at '{video_path}'.")
    
    has_audio = video.get("has_audio", has_audio_stream(video_path))
    thumbnail_base = os.path.join(THUMBNAIL_DIR, video_id)
    
    # Delete existing thumbnails
    existing_thumbnails = [
        f for f in os.listdir(THUMBNAIL_DIR) 
        if f.startswith(f"{video_id}") and f.endswith(('.jpg'))
    ]
    for thumbnail in existing_thumbnails:
        os.remove(os.path.join(THUMBNAIL_DIR, thumbnail))
    
    try:
        generate_thumbnail(video_path, thumbnail_base, has_audio, time=time)
        return {"detail": "Thumbnail successfully updated."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate thumbnail: {str(e)}")

# Serve the thumbnails directory as static files
app.mount("/thumbnails", StaticFiles(directory=THUMBNAIL_DIR), name="thumbnails")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=6969)
