import json
import os


DB_FILE = "video_db.json"

# Database functions
def init_db():
    """Initialize the database if it doesn't exist."""
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, 'w') as f:
            json.dump({"videos": []}, f)

def load_db():
    """Load the video database."""
    if not os.path.exists(DB_FILE):
        init_db()
    with open(DB_FILE, 'r') as f:
        return json.load(f)

def save_db(db):
    """Save the database to disk."""
    with open(DB_FILE, 'w') as f:
        json.dump(db, f, indent=2)

def get_video_by_id(video_id):
    """Get a video entry by its ID."""
    db = load_db()
    for video in db["videos"]:
        if video["id"] == video_id:
            return video
    return None

def add_video_to_db(video_data):
    """Add a new video entry to the database."""
    db = load_db()
    db["videos"].append(video_data)
    save_db(db)
    return video_data

def update_video_in_db(video_id, updated_data):
    """Update an existing video entry in the database."""
    db = load_db()
    for i, video in enumerate(db["videos"]):
        if video["id"] == video_id:
            # Update fields
            for key, value in updated_data.items():
                db["videos"][i][key] = value
            save_db(db)
            return db["videos"][i]
    return None

def delete_video_from_db(video_id):
    """Delete a video entry from the database."""
    db = load_db()
    for i, video in enumerate(db["videos"]):
        if video["id"] == video_id:
            del db["videos"][i]
            save_db(db)
            return True
    return False