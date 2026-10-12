import glob
import hmac
import os
import secrets
import shutil
import tempfile
import threading
import time
from datetime import timedelta
from functools import wraps
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, redirect, request, send_file, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

BASE = os.path.dirname(os.path.abspath(__file__))

# ---- Config (from environment, never hard-coded in source) ----
APP_PASSWORD = os.environ.get("APP_PASSWORD")
if not APP_PASSWORD:
    raise SystemExit("Set the APP_PASSWORD environment variable before starting.")

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=4096,
)
# Trust one proxy hop (Render / Cloudflare) so we see the real client IP.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

# ---- Brute-force protection (in memory, no database) ----
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
_fails = {}  # ip -> {"count": int, "until": float}
_fails_lock = threading.Lock()

# Limit simultaneous downloads so the server doesn't run out of disk or memory.
_dl_slots = threading.BoundedSemaphore(2)


def locked_for(ip):
    with _fails_lock:
        rec = _fails.get(ip)
        if rec and rec["until"] > time.time():
            return int(rec["until"] - time.time())
        if rec and rec["until"]:
            _fails.pop(ip, None)
    return 0


def record_fail(ip):
    with _fails_lock:
        rec = _fails.setdefault(ip, {"count": 0, "until": 0})
        rec["count"] += 1
        if rec["count"] >= MAX_FAILS:
            rec["until"] = time.time() + LOCK_SECONDS
            rec["count"] = 0


def clear_fails(ip):
    with _fails_lock:
        _fails.pop(ip, None)


def login_required(api=False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not session.get("ok"):
                if api:
                    return jsonify(error="Please log in."), 401
                return redirect("/login")
            return fn(*a, **kw)
        return wrapper
    return deco


@app.after_request
def secure_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' https: data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return resp


def is_valid(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and p.hostname in ALLOWED_HOSTS


# ---- Auth routes ----
@app.get("/login")
def login_page():
    if session.get("ok"):
        return redirect("/")
    return send_from_directory(BASE, "login.html")


@app.post("/login")
def login():
    ip = request.remote_addr or "unknown"
    wait = locked_for(ip)
    if wait:
        return jsonify(error=f"Too many attempts. Try again in {wait // 60 + 1} min."), 429

    given = str((request.get_json(silent=True) or {}).get("password", ""))
    if hmac.compare_digest(given.encode(), APP_PASSWORD.encode()):
        clear_fails(ip)
        session.clear()
        session.permanent = True
        session["ok"] = True
        return jsonify(ok=True)

    record_fail(ip)
    time.sleep(1)  # slow down guessing
    return jsonify(error="Wrong password."), 401


@app.post("/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# ---- App routes ----
@app.get("/")
@login_required()
def home():
    return send_from_directory(BASE, "index.html")


@app.post("/api/info")
@login_required(api=True)
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not is_valid(url):
        return jsonify(error="Paste a valid YouTube link."), 400

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            d = ydl.extract_info(url, download=False)
    except Exception:
        return jsonify(error="Couldn't read this video. It may be private or unavailable."), 422

    formats = d.get("formats", [])

    def size_of(f):
        return f.get("filesize") or f.get("filesize_approx") or 0

    audio_size = max(
        [size_of(f) for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"] or [0]
    )

    sizes = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        sizes[h] = max(sizes.get(h, 0), size_of(f))

    qualities = [
        {"height": h, "size": (s + audio_size) if s else 0}
        for h, s in sorted(sizes.items(), reverse=True)  # highest first
    ]

    return jsonify(
        title=d.get("title", "Untitled"),
        channel=d.get("uploader", ""),
        thumbnail=d.get("thumbnail", ""),
        duration=d.get("duration") or 0,
        qualities=qualities,
        audio_size=audio_size,
    )


@app.get("/api/download")
@login_required(api=True)
def download():
    url = request.args.get("url", "").strip()
    quality = request.args.get("quality", "")

    if not is_valid(url):
        return "Invalid link", 400
    if not (quality == "audio" or (quality.isdigit() and 100 <= int(quality) <= 4320)):
        return "Invalid quality", 400
    if not _dl_slots.acquire(blocking=False):
        return "Server is busy with other downloads. Try again shortly.", 429

    tmp = tempfile.mkdtemp(prefix="ytsaver_")
    opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": os.path.join(tmp, "%(title).80s.%(ext)s"),
    }
    if quality == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        h = int(quality)
        opts["format"] = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = glob.glob(os.path.join(tmp, "*"))
        if not files:
            raise RuntimeError("no file")
        path = files[0]
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        _dl_slots.release()
        return "Download failed. Try another quality or try again later.", 500

    response = send_file(path, as_attachment=True, download_name=os.path.basename(path))

    def cleanup():
        shutil.rmtree(tmp, ignore_errors=True)
        _dl_slots.release()

    response.call_on_close(cleanup)
    return response


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
