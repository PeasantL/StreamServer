# utils.py
import os
import subprocess
import shutil
import json
from pathlib import Path
import ffmpeg

# Configuration
CONFIG_FILE = "config.json"
THUMBNAIL_DIR = "thumbnails"

# Load JSON Config
def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

# Initialize configuration
config = load_config()
VIDEO_DIR = config.get("video_dir")
os.makedirs(THUMBNAIL_DIR, exist_ok=True)

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
            else:
                has_audio = has_audio_stream(video_path)
            
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
