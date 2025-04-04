#!/bin/bash

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
