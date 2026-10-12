#!/data/data/com.termux/files/usr/bin/bash
# Run with:  bash start.sh
cd "$(dirname "$0")"
termux-wake-lock 2>/dev/null
pip install -q -U yt-dlp
python app.py
