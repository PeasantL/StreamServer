#!/bin/bash

# Define the drive and mount point
DRIVE="/dev/sdb2"
MOUNT_POINT="/media/peasantl/Risk"

# Check if the mount point directory exists; if not, create it
if [ ! -d "$MOUNT_POINT" ]; then
  sudo mkdir -p "$MOUNT_POINT"
fi

# Check if the drive is already mounted
if mount | grep -q "$MOUNT_POINT"; then
  echo "Drive is already mounted at $MOUNT_POINT"
else
  # Mount the drive if not already mounted
  sudo mount "$DRIVE" "$MOUNT_POINT"
  
  # Check if the mount was successful
  if [ $? -eq 0 ]; then
    echo "Drive mounted successfully at $MOUNT_POINT"
  else
    echo "Failed to mount drive"
  fi
fi

# Check if .venv exists, if not, create it
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

# Activate the virtual environment
source .venv/bin/activate

# Install requirements
pip install -r requirements.txt

# Run main.py
python main.py
