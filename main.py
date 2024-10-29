from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import os
import cv2
import requests
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
VIDEO_DIR = config.get("video_dir")  # Default to 'D:' if not found
THUMBNAIL_DIR = "thumbnails"

# Create thumbnails directory if it doesn't exist
os.makedirs(THUMBNAIL_DIR, exist_ok=True)

# Initialize Jinja2 templates directory
templates = Jinja2Templates(directory="templates")

def get_video_files():
    """Fetch all video files in the directory."""
    return [f for f in os.listdir(VIDEO_DIR) if f.endswith(('.mp4', '.webm', '.mov', '.avi', '.mkv'))]

def generate_thumbnail(video_path, thumbnail_path):
    """Generate a thumbnail for a video file."""
    cap = cv2.VideoCapture(video_path)
    success, frame = cap.read()
    if success:
        cv2.imwrite(thumbnail_path, frame)
    cap.release()

# Mount the static files directory
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.on_event("startup")
def generate_all_thumbnails():
    """Generate thumbnails for all videos."""
    video_files = get_video_files()
    for video_file in video_files:
        video_path = os.path.join(VIDEO_DIR, video_file)
        thumbnail_path = os.path.join(THUMBNAIL_DIR, f"{Path(video_file).stem}.jpg")
        if not os.path.exists(thumbnail_path):
            generate_thumbnail(video_path, thumbnail_path)

@app.get("/")
async def list_videos(request: Request):
    """Root endpoint to list available video files."""
    video_files = get_video_files()
    if not video_files:
        raise HTTPException(status_code=404, detail="No videos found.")
    return templates.TemplateResponse("index.html", {"request": request, "video_files": video_files})

@app.post("/api/download")
async def download_video(request: Request):
    data = await request.json()
    url = data.get('url')

    if not url or not url.lower().endswith(('.webm', '.mp4')):
        raise HTTPException(status_code=400, detail="Invalid URL or unsupported file format.")

    filename = url.split("/")[-1]
    save_path = os.path.join(VIDEO_DIR, filename)

    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()

        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=1024*1024):
                if chunk:
                    f.write(chunk)

        # Generate a thumbnail for the downloaded video
        thumbnail_path = os.path.join(THUMBNAIL_DIR, f"{Path(filename).stem}.jpg")
        generate_thumbnail(save_path, thumbnail_path)

        return {"message": f"Video '{filename}' downloaded and thumbnail generated successfully."}

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Failed to download video: {str(e)}")


@app.get("/thumbnails/{thumbnail_name}")
async def get_thumbnail(thumbnail_name: str):
    """Endpoint to fetch a video thumbnail."""
    thumbnail_path = os.path.join(THUMBNAIL_DIR, thumbnail_name)
    if not os.path.isfile(thumbnail_path):
        raise HTTPException(status_code=404, detail="Thumbnail not found.")
    return FileResponse(thumbnail_path)

@app.get("/play/{video_name}")
async def play_video(video_name: str, request: Request):
    """Render an HTML page to play the video."""
    video_path = os.path.join(VIDEO_DIR, video_name)
    if not os.path.isfile(video_path):
        raise HTTPException(status_code=404, detail="Video not found.")
    
    # Determine which template to use based on video format
    if video_name.endswith(".webm"):
        template_name = "play_webm.html"  # Template for .webm files
    else:
        template_name = "play_other.html"  # Template for other video formats

    return templates.TemplateResponse(
        template_name,
        {"request": request, "video_name": video_name}
    )


@app.get("/videos/{video_name}")
async def stream_video(request: Request, video_name: str, range: str = Header(None)):
    """Endpoint to stream video files with range support."""
    video_path = os.path.join(VIDEO_DIR, video_name)
    if not os.path.isfile(video_path):
        raise HTTPException(status_code=404, detail="Video not found.")
    
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
