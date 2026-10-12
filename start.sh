#!/data/data/com.termux/files/usr/bin/bash
# Run with:  bash start.sh
cd "$(dirname "$0")"
termux-wake-lock 2>/dev/null
command -v node >/dev/null || pkg install -y nodejs   # JS runtime needed for YouTube
pip install -q -U "yt-dlp[default]"
python app.py
