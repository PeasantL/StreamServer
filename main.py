from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Header, BackgroundTasks, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import os
import tempfile
import subprocess
import shutil
import json
import ffmpeg
import requests
import datetime
from pydantic import BaseModel

app = FastAPI()

# Configuration
CONFIG_FILE = "config.json"
THUMBNAIL_DIR = "thumbnails"
templates = Jinja2Templates(directory="templates")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load JSON Config
def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

config = load_config()
VIDEO_DIR = config.get("video_dir")
os.makedirs(THUMBNAIL_DIR, exist_ok=True)
print(f"Looking for videos in: {VIDEO_DIR}")
print(f"Directory exists: {os.path.exists(VIDEO_DIR)}")
print(f"Directory contents: {os.listdir(VIDEO_DIR)}")

# Utility functions
def has_audio_stream(video_path: str) -> bool:
    """Check if a video file contains an audio stream using FFmpeg."""
    try:
        probe = ffmpeg.probe(video_path, select_streams='a')
        return bool(probe['streams'])
    except ffmpeg.Error:
        return False

def generate_thumbnail(video_path, thumbnail_path_base, has_audio, time="00:00:01"):
    suffix = "_1" if has_audio else "_0"
    thumbnail_path = f"{thumbnail_path_base}{suffix}.jpg"
    
    # Optimized CPU-only FFmpeg command
    ffmpeg_command = [
        "ffmpeg",
        "-ss", time,               # Fast input seeking
        "-i", video_path,
        "-vf", "scale='min(320,iw)':-2",  # Smart scaling w/fast algorithm
        "-frames:v", "1",          # Only capture one frame
        "-qscale:v", "4",          # Faster JPEG encoding (2-31, lower=faster)
        "-compression_level", "1", # Fastest compression
        "-threads", "2",           # Optimal for small operations
        "-y",                      # Overwrite existing files
        "-loglevel", "error",      # Suppress non-critical output
        thumbnail_path
    ]
    
    try:
        # Timeout after 2 seconds (adjust based on testing)
        subprocess.run(
            ffmpeg_command, 
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            check=True
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        print(f"Thumbnail generation failed: {str(e)}")
        return None
    
    return thumbnail_path


def get_video_files():
    """Fetch all video files in the directory and check if they have audio based on thumbnail name."""
    video_files = []
    for video_name in os.listdir(VIDEO_DIR):
        if video_name.endswith(('.mp4', '.webm', '.mov', '.avi', '.mkv')):
            video_path = os.path.join(VIDEO_DIR, video_name)
            thumbnail_path_base = os.path.join(THUMBNAIL_DIR, os.path.splitext(video_name)[0])
            
            # Check for existing thumbnails with audio status
            if os.path.exists(f"{thumbnail_path_base}_1.jpg"):
                has_audio = True
            elif os.path.exists(f"{thumbnail_path_base}_0.jpg"):
                has_audio = False        
            video_files.append({"name": video_name, "has_audio": has_audio})
    return video_files

def get_sibling_folders():
    """Get a list of sibling folders for navigation."""
    parent_directory = Path(VIDEO_DIR).parent
    return [
        folder.name
        for folder in parent_directory.iterdir()
        if folder.is_dir() and folder != Path(VIDEO_DIR)
    ]

def get_original_webm_dir():
    """Return the path to the original WebM storage directory."""
    return os.path.join(VIDEO_DIR, "original_webm")

def process_existing_webm_files():
    """Process existing WebM files and convert them to MP4 format."""
    original_webm_dir = get_original_webm_dir()
    os.makedirs(original_webm_dir, exist_ok=True)

    for filename in os.listdir(VIDEO_DIR):
        if filename.lower().endswith(".webm"):
            webm_path = os.path.join(VIDEO_DIR, filename)
            base_name = os.path.splitext(filename)[0]
            mp4_filename = base_name + ".mp4"
            mp4_path = os.path.join(VIDEO_DIR, mp4_filename)

            # Check if MP4 already exists
            if os.path.exists(mp4_path):
                unique_name = get_unique_filename(filename, original_webm_dir)
                shutil.move(webm_path, os.path.join(original_webm_dir, unique_name))
                continue

            # Convert WebM to MP4
            try:
                (
                    ffmpeg
                    .input(webm_path)
                    .output(mp4_path, vcodec='libx264', acodec='aac')
                    .run(capture_stdout=True, capture_stderr=True)
                )
                # Move original WebM to archive
                unique_name = get_unique_filename(filename, original_webm_dir)
                shutil.move(webm_path, os.path.join(original_webm_dir, unique_name))
            except ffmpeg.Error as e:
                print(f"Error converting {filename}: {e.stderr.decode()}")

def get_unique_filename(original_filename, directory):
    """Generate a unique filename by appending a number if a file with the same name exists."""
    base_name = Path(original_filename).stem
    extension = Path(original_filename).suffix
    filename = f"{base_name}{extension}"
    counter = 1
    
    # Increment counter until filename is unique
    while os.path.exists(os.path.join(directory, filename)):
        filename = f"{base_name}_{counter}{extension}"
        counter += 1
    
    return filename

# Application Events
@app.on_event("startup")
async def startup_tasks():
    """Run startup tasks including WebM conversion and thumbnail generation."""
    process_existing_webm_files()
    create_thumbnails_on_startup()

def create_thumbnails_on_startup():
    """Generate thumbnails for all videos at startup if they don't exist."""
    for video_name in os.listdir(VIDEO_DIR):
        if video_name.endswith(('.mp4', '.webm', '.mov', '.avi', '.mkv')):
            video_path = os.path.join(VIDEO_DIR, video_name)
            thumbnail_path_base = os.path.join(THUMBNAIL_DIR, os.path.splitext(video_name)[0])
            
            # Only generate if neither thumbnail exists
            if not (os.path.exists(f"{thumbnail_path_base}_1.jpg") or 
                   os.path.exists(f"{thumbnail_path_base}_0.jpg")):
                has_audio = has_audio_stream(video_path)
                generate_thumbnail(video_path, thumbnail_path_base, has_audio)

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
            "timestamp": datetime.datetime.now().timestamp()  # Add this line
        }
    )

@app.get("/videos/{video_name}")
async def stream_video(video_name: str, range: str = Header(None)):
    """Stream video files with range support."""
    video_path = os.path.join(VIDEO_DIR, video_name)
    
    # Check if the video file exists
    if not os.path.isfile(video_path):
        raise HTTPException(status_code=404, detail=f"Video '{video_name}' not found in '{VIDEO_DIR}'.")

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
    mime_type = "video/mp4" if video_name.endswith(".mp4") else "video/webm"
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
    global VIDEO_DIR
    new_folder = request.folder
    
    # Check if the folder exists as a sibling of the current VIDEO_DIR
    parent_directory = Path(VIDEO_DIR).parent
    new_video_dir = parent_directory / new_folder
    
    if not new_video_dir.exists() or not new_video_dir.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")
    
    # Update the config.json file
    config["video_dir"] = str(new_video_dir)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)
    
    # Reload the VIDEO_DIR setting
    VIDEO_DIR = config["video_dir"]
    
    return {"message": f"Directory changed to {new_folder}"}


class DownloadRequest(BaseModel):
    url: str

@app.post("/api/download")
def download_video(download_request: DownloadRequest):
    url = download_request.url
    if not url or not url.lower().endswith(('.webm', '.mp4')):
        raise HTTPException(status_code=400, detail="Invalid URL or unsupported file format.")
    
    original_filename = url.split("/")[-1]
    is_webm = url.lower().endswith('.webm')
    
    try:
        if is_webm:
            # Process WebM conversion
            original_webm_dir = get_original_webm_dir()
            os.makedirs(original_webm_dir, exist_ok=True)
            
            base_name = Path(original_filename).stem
            mp4_filename = get_unique_filename(f"{base_name}.mp4", VIDEO_DIR)
            mp4_path = os.path.join(VIDEO_DIR, mp4_filename)
            original_webm_path = os.path.join(original_webm_dir, get_unique_filename(original_filename, original_webm_dir))
            
            # Download and process in temp directory
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_webm_path = os.path.join(tmp_dir, original_filename)
                
                # Download the WebM
                response = requests.get(url, stream=True)
                response.raise_for_status()
                with open(tmp_webm_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024*1024):
                        if chunk:
                            f.write(chunk)
                
                # Convert to MP4
                (
                    ffmpeg
                    .input(tmp_webm_path)
                    .output(mp4_path, vcodec='libx264', acodec='aac')
                    .run(capture_stdout=True, capture_stderr=True)
                )
                
                # Move WebM to original directory
                shutil.move(tmp_webm_path, original_webm_path)
        else:
            # Direct MP4 download
            filename = get_unique_filename(original_filename, VIDEO_DIR)
            save_path = os.path.join(VIDEO_DIR, filename)
            
            response = requests.get(url, stream=True)
            response.raise_for_status()
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
            mp4_path = save_path
            mp4_filename = filename

        # Generate thumbnail
        has_audio = has_audio_stream(mp4_path)
        thumbnail_path_base = os.path.join(THUMBNAIL_DIR, Path(mp4_path).stem)
        generate_thumbnail(mp4_path, thumbnail_path_base, has_audio)

        return {"message": f"Video '{mp4_filename}' processed successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")

@app.get("/play/{video_name}")
async def play_video(video_name: str, request: Request):
    """Render an HTML page to play the video."""
    video_path = os.path.join(VIDEO_DIR, video_name)
    if not os.path.isfile(video_path):
        raise HTTPException(status_code=404, detail=f"Video '{video_name}' not found in '{VIDEO_DIR}'.")
    return templates.TemplateResponse("play_mp4.html", {"request": request, "video_name": video_name})

@app.post("/rename_video/{video_name}")
async def rename_video(video_name: str, request: Request):
    data = await request.json()
    new_name = data.get("new_name")

    # Extract file extension and add it to the new name
    extension = os.path.splitext(video_name)[1]
    new_name_with_ext = f"{new_name}{extension}"

    old_path = os.path.join(VIDEO_DIR, video_name)
    new_path = os.path.join(VIDEO_DIR, new_name_with_ext)

    if not new_name:
        raise HTTPException(status_code=400, detail="New name not provided")
    
    if os.path.exists(old_path):
        try:
            # Rename the video file
            os.rename(old_path, new_path)

            # Delete the old thumbnail
            old_thumbnail_path = os.path.join(THUMBNAIL_DIR, f"{os.path.splitext(video_name)[0]}.jpg")
            if os.path.exists(old_thumbnail_path):
                os.remove(old_thumbnail_path)

            # Generate a new thumbnail for the renamed video
            new_thumbnail_path = os.path.join(THUMBNAIL_DIR, f"{os.path.splitext(new_name_with_ext)[0]}.jpg")
            generate_thumbnail(new_path, new_thumbnail_path)

            return {"detail": "Video renamed and thumbnail updated successfully"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to rename video: {str(e)}")
    else:
        raise HTTPException(status_code=404, detail="Video not found")

@app.delete("/delete_video/{video_name}")
async def delete_video(video_name: str):
    video_path = os.path.join(VIDEO_DIR, video_name)

    if os.path.exists(video_path):
        os.remove(video_path)
        return {"detail": "Video deleted successfully"}
    else:
        raise HTTPException(status_code=404, detail="Video not found")

@app.post("/generate_thumbnail/{video_name}")
async def generate_custom_thumbnail(
    video_name: str, 
    time: str = Query(default="00:00:01", description="Time in HH:MM:SS format")
):
    video_path = os.path.join(VIDEO_DIR, video_name)
    if not os.path.isfile(video_path):
        raise HTTPException(status_code=404, detail=f"Video '{video_name}' not found.")
    
    has_audio = has_audio_stream(video_path)
    thumbnail_base = os.path.join(THUMBNAIL_DIR, os.path.splitext(video_name)[0])
    
    # Delete existing thumbnails
    existing_thumbnails = [f for f in os.listdir(THUMBNAIL_DIR) if f.startswith(os.path.basename(thumbnail_base)) and f.endswith(('.jpg'))]
    for thumbnail in existing_thumbnails:
        os.remove(os.path.join(THUMBNAIL_DIR, thumbnail))
    
    try:
        generate_thumbnail(video_path, thumbnail_base, has_audio, time=time)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate thumbnail: {str(e)}")
    
    return {"detail": "Thumbnail successfully updated."}

# Serve the thumbnails directory as static files
app.mount("/thumbnails", StaticFiles(directory=THUMBNAIL_DIR), name="thumbnails")
app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=6969)
