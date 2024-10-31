@echo off
REM Check if .venv exists, if not, create it
if not exist ".venv" (
    python -m venv .venv
)

REM Activate the virtual environment
call .venv\Scripts\activate

REM Install requirements
pip install -r requirements.txt

REM Run main.py
python main.py