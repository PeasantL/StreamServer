from fastapi import FastAPI, HTTPException, Request, Header, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import os
import tempfile
import subprocess
import shutil
import json

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load configuration from a JSON file
CONFIG_FILE = "config.json"

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

config = load_config()
VIDEO_DIR = config.get("video_dir")
THUMBNAIL_DIR = "thumbnails"

os.makedirs(THUMBNAIL_DIR, exist_ok=True)

templates = Jinja2Templates(directory="templates")

def get_video_files():
    """Fetch all video files in the directory."""
    return [f for f in os.listdir(VIDEO_DIR) if f.endswith(('.mp4', '.webm', '.mov', '.avi', '.mkv'))]

@app.get("/")
async def list_videos(request: Request):
    """Root endpoint to list available video files."""
    video_files = get_video_files()
    if not video_files:
        raise HTTPException(status_code=404, detail="No videos found.")
    return templates.TemplateResponse("index.html", {"request": request, "video_files": video_files})

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
            remaining = end - start + 1  # Calculate the remaining bytes to send
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


@app.head("/hls/{video_name}/index.m3u8")
@app.get("/hls/{video_name}/index.m3u8")
async def serve_hls_playlist(video_name: str):
    """Serve HLS playlist for .webm files."""
    hls_output_dir = os.path.join(tempfile.gettempdir(), video_name)
    hls_playlist = os.path.join(hls_output_dir, "index.m3u8")
    
    if os.path.exists(hls_playlist):
        return FileResponse(hls_playlist, media_type="application/vnd.apple.mpegurl")
    else:
        raise HTTPException(status_code=404, detail="Playlist not found") 

def delete_temp_hls_dir(hls_output_dir: str):
    """Background task to delete HLS directory after use."""
    shutil.rmtree(hls_output_dir, ignore_errors=True)

def delete_other_hls_dirs(current_hls_dir: str):
    """Delete all HLS output directories except the current one."""
    temp_dir = tempfile.gettempdir()
    for video_name in os.listdir(temp_dir):
        hls_output_dir = os.path.join(temp_dir, video_name)
        # Skip the current directory and delete others
        if os.path.isdir(hls_output_dir) and hls_output_dir != current_hls_dir:
            shutil.rmtree(hls_output_dir, ignore_errors=True)

@app.get("/play/{video_name}")
async def play_video(video_name: str, request: Request, background_tasks: BackgroundTasks):
    """Render an HTML page to play the video."""
    video_path = os.path.join(VIDEO_DIR, video_name)

    if not os.path.isfile(video_path):
        raise HTTPException(status_code=404, detail=f"Video '{video_name}' not found in '{VIDEO_DIR}'.")

    # If the video is .webm, convert to HLS
    if video_name.endswith(".webm"):
        hls_output_dir = os.path.join(tempfile.gettempdir(), video_name)
        os.makedirs(hls_output_dir, exist_ok=True)

        hls_playlist = os.path.join(hls_output_dir, "index.m3u8")
        
        if not os.path.exists(hls_playlist):
            ffmpeg_command = [
                "ffmpeg",
                "-i", video_path,
                "-c:v", "h264_nvenc",  # Use 'libx264' if NVENC is not available
                "-preset", "fast",
                "-c:a", "aac",
                "-f", "hls",
                "-hls_time", "4",
                "-hls_list_size", "0",
                "-hls_flags", "delete_segments",
                "-hls_segment_filename", os.path.join(hls_output_dir, "segment_%03d.ts"),
                hls_playlist
            ]
            subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Add background task to delete all other HLS directories except the current one
        background_tasks.add_task(delete_other_hls_dirs, hls_output_dir)

        # Render the play_hls.html template
        return templates.TemplateResponse("play_hls.html", {"request": request, "video_name": video_name})

    # If not a .webm file, use a regular video template
    return templates.TemplateResponse("play_other.html", {"request": request, "video_name": video_name})

@app.post("/rename_video/{video_name}")
async def rename_video(video_name: str, request: Request):
    data = await request.json()
    new_name = data.get("new_name")

    if not new_name:
        raise HTTPException(status_code=400, detail="New name not provided")

    old_path = os.path.join(VIDEO_DIR, video_name)
    new_path = os.path.join(VIDEO_DIR, new_name)

    if os.path.exists(old_path):
        os.rename(old_path, new_path)
        return {"detail": "Video renamed successfully"}
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

@app.get("/hls/{video_name}/index.m3u8")
async def serve_hls_playlist(video_name: str):
    """Serve HLS playlist for .webm files."""
    hls_output_dir = os.path.join(tempfile.gettempdir(), video_name)
    hls_playlist = os.path.join(hls_output_dir, "index.m3u8")
    
    if os.path.exists(hls_playlist):
        return FileResponse(hls_playlist, media_type="application/vnd.apple.mpegurl")
    else:
        raise HTTPException(status_code=404, detail="Playlist not found")

@app.get("/hls/{video_name}/{segment}")
async def serve_hls_segment(video_name: str, segment: str):
    """Serve HLS segments."""
    hls_output_dir = os.path.join(tempfile.gettempdir(), video_name)
    segment_path = os.path.join(hls_output_dir, segment)
    
    if os.path.exists(segment_path):
        return FileResponse(segment_path, media_type="video/mp2t")
    else:
        raise HTTPException(status_code=404, detail="Segment not found")

app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
