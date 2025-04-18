#!/bin/bash

# Clean up properly first
echo "Cleaning up previous containers..."
docker-compose down

# Extract the video_dir from config.json
VIDEO_DIR=$(grep -o '"video_dir": *"[^"]*"' config.json | cut -d'"' -f4)

# Get parent directory and folder name from VIDEO_DIR
export PARENT_DIRECTORY=$(dirname "$VIDEO_DIR")
export VIDEO_FOLDER=$(basename "$VIDEO_DIR")

echo "Using parent directory: $PARENT_DIRECTORY"
echo "Using video folder: $VIDEO_FOLDER"

# Run docker-compose
docker-compose up -d

echo "Container started. Access the application at http://localhost:6969"
