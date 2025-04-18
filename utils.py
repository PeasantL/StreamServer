# utils.py
import os
import subprocess
import shutil
from pathlib import Path
import ffmpeg
import uuid
import datetime

from database import init_db, load_db, save_db, add_video_to_db
from config import config


def has_audio_stream(video_path: str) -> bool:
    """Check if a video file contains an audio stream using FFmpeg."""
    try:
        probe = ffmpeg.probe(video_path, select_streams='a')
        return bool(probe['streams'])
    except ffmpeg.Error:
        return False

def generate_thumbnail(video_path, thumbnail_path_base, has_audio, time="00:00:01"):
    """Generate a thumbnail for a video at the specified time."""
    # Use simple naming scheme: videoId.jpg
    thumbnail_path = f"{thumbnail_path_base}.jpg"
    
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

def get_sibling_folders():
    """Get a list of sibling folders for navigation."""
    # Get the parent directory path from the video_dir
    current_dir = Path(config.video_dir)
    parent_directory = current_dir.parent
    current_folder = current_dir.name
    
    try:
        return [
            folder.name
            for folder in parent_directory.iterdir()
            if folder.is_dir() and folder.name != current_folder
        ]
    except Exception as e:
        print(f"Error getting sibling folders: {str(e)}")
        return []




def get_original_webm_dir():
    """Return the path to the original WebM storage directory."""
    return os.path.join(config.video_dir, "original_webm")

def process_existing_webm_files():
    """Process existing WebM files and convert them to MP4 format."""
    init_db()  # Make sure database exists
    db = load_db()
    original_webm_dir = get_original_webm_dir()
    os.makedirs(original_webm_dir, exist_ok=True)

    for filename in os.listdir(config.video_dir):
        if filename.lower().endswith(".webm"):
            webm_path = os.path.join(config.video_dir, filename)
            base_name = os.path.splitext(filename)[0]
            video_id = str(uuid.uuid4())
            mp4_filename = f"{video_id}.mp4"
            mp4_path = os.path.join(config.video_dir, mp4_filename)

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
                original_webm_path = os.path.join(original_webm_dir, unique_name)
                shutil.move(webm_path, original_webm_path)
                
                # Check if the video has audio
                has_audio = has_audio_stream(mp4_path)
                
                # Create database entry
                creation_time = datetime.datetime.now().isoformat()
                db["videos"].append({
                    "id": video_id,
                    "title": base_name,
                    "path": mp4_filename,
                    "has_audio": has_audio,
                    "creation_date": creation_time,
                    "description": "",
                    "tags": [],
                    "original_webm": unique_name
                })
                save_db(db)
                
                # Generate thumbnail
                generate_thumbnail(mp4_path, os.path.join(config.thumbnail_dir, video_id), has_audio)
                
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
    """Generate thumbnails for all videos in the database if they don't exist."""
    db = load_db()
    
    for video in db["videos"]:
        video_path = os.path.join(config.video_dir, video["path"])
        thumbnail_path = os.path.join(config.thumbnail_dir, f"{video['id']}.jpg")
        
        if os.path.exists(video_path) and not os.path.exists(thumbnail_path):
            has_audio = video.get("has_audio", has_audio_stream(video_path))
            generate_thumbnail(video_path, os.path.join(config.thumbnail_dir, video["id"]), has_audio)

def migrate_existing_videos():
    """Migrate existing videos to the database if they're not already there."""
    db = load_db()
    # Create a set of paths that are already in the database
    existing_paths = {video["path"] for video in db["videos"]}
    
    for file in os.listdir(config.video_dir):
        if file.lower().endswith(('.mp4', '.webm')) and file not in existing_paths:
            # This video is not in the database, add it
            video_id = str(uuid.uuid4())
            creation_time = datetime.datetime.fromtimestamp(
                os.path.getctime(os.path.join(config.video_dir, file))
            ).isoformat()
            
            # Check if the video has audio
            has_audio = has_audio_stream(os.path.join(config.video_dir, file))
            
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
            thumbnail_path = os.path.join(config.thumbnail_dir, f"{video_id}.jpg")
            if not os.path.exists(thumbnail_path):
                try:
                    generate_thumbnail(
                        os.path.join(config.video_dir, file), 
                        os.path.join(config.thumbnail_dir, video_id),
                        has_audio
                    )
                except Exception as e:
                    print(f"Error generating thumbnail for {file}: {e}")

def get_video_files(sort_by="newest"):
    db = load_db()
    video_files = []
    
    for video in db["videos"]:
        # Check if the file actually exists
        video_path = os.path.join(config.video_dir, video["path"])
        if os.path.exists(video_path):
            thumbnail_path = f"/thumbnails/{video['id']}.jpg"
            thumbnail_exists = os.path.exists(os.path.join(config.thumbnail_dir, f"{video['id']}.jpg"))
            
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
    
    # Sort the videos based on the sort_by parameter
    if sort_by == "title":
        video_files.sort(key=lambda x: x["title"].lower())  # Case-insensitive sort by title
    else:  # Default to "newest"
        video_files.sort(key=lambda x: x.get("creation_date", ""), reverse=True)
    
    return video_files
