import glob
import hashlib
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

# ---- Password (kept in code on purpose; keep the GitHub repo PRIVATE) ----
APP_PASSWORD = os.environ.get("APP_PASSWORD", "Abhishek862009")

app = Flask(__name__, static_folder=None)
# Secret key is derived from the password, so login survives restarts.
app.secret_key = hashlib.sha256(("ytsaver-v2:" + APP_PASSWORD).encode()).digest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=4096,
)
if os.environ.get("BEHIND_PROXY") == "1":  # only trust X-Forwarded-For behind a real proxy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

# ---- Brute-force lockout (memory only, no database) ----
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
_fails, _fails_lock = {}, threading.Lock()


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
            rec["until"], rec["count"] = time.time() + LOCK_SECONDS, 0


def login_required(api=False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not session.get("ok"):
                return (jsonify(error="Please log in."), 401) if api else redirect("/login")
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
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' https: data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return resp


def is_valid(url):
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and p.hostname in ALLOWED_HOSTS


# ---------------- Auth ----------------
@app.get("/login")
def login_page():
    return redirect("/") if session.get("ok") else send_from_directory(BASE, "login.html")


@app.post("/login")
def login():
    ip = request.remote_addr or "unknown"
    wait = locked_for(ip)
    if wait:
        return jsonify(error=f"Too many attempts. Try again in {wait // 60 + 1} min."), 429
    given = str((request.get_json(silent=True) or {}).get("password", ""))
    if hmac.compare_digest(given.encode(), APP_PASSWORD.encode()):
        with _fails_lock:
            _fails.pop(ip, None)
        session.clear()
        session.permanent = True
        session["ok"] = True
        return jsonify(ok=True)
    record_fail(ip)
    time.sleep(1)
    return jsonify(error="Wrong password."), 401


@app.post("/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/")
@login_required()
def home():
    return send_from_directory(BASE, "index.html")


# ---------------- Video info (cached) ----------------
BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "noprogress": True,
    "retries": 10,
    "fragment_retries": 10,
    "socket_timeout": 20,
    "concurrent_fragment_downloads": 4,
    "http_chunk_size": 10 * 1024 * 1024,  # avoids YouTube throttling
}
# yt-dlp needs a JavaScript runtime to solve YouTube's challenges.
# Its default is only deno, so also enable node if it is installed.
_runtimes = {n: {} for n in ("deno", "node") if shutil.which(n)}
if _runtimes:
    BASE_OPTS["js_runtimes"] = _runtimes
else:
    print("WARNING: no JavaScript runtime (deno/node) found - YouTube downloads will likely fail.", flush=True)
_info_cache, _info_lock = {}, threading.Lock()


def size_of(f):
    return f.get("filesize") or f.get("filesize_approx") or 0


@app.post("/api/info")
@login_required(api=True)
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not is_valid(url):
        return jsonify(error="Paste a valid YouTube link."), 400

    with _info_lock:
        hit = _info_cache.get(url)
    if hit and time.time() - hit[0] < 600:
        return jsonify(hit[1])

    try:
        with yt_dlp.YoutubeDL({**BASE_OPTS, "skip_download": True}) as ydl:
            d = ydl.extract_info(url, download=False)
    except Exception:
        import traceback; traceback.print_exc()
        return jsonify(error="Couldn't read this video. It may be private, or yt-dlp needs an update."), 422

    formats = d.get("formats", [])
    audio_size = max(
        [size_of(f) for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"] or [0]
    )
    sizes = {}
    for f in formats:
        h = f.get("height")
        if h and f.get("vcodec") not in (None, "none"):
            sizes[h] = max(sizes.get(h, 0), size_of(f))

    data = dict(
        title=d.get("title", "Untitled"),
        channel=d.get("uploader", ""),
        thumbnail=d.get("thumbnail", ""),
        duration=d.get("duration") or 0,
        qualities=[
            {"height": h, "size": (s + audio_size) if s else 0}
            for h, s in sorted(sizes.items(), reverse=True)  # highest first
        ],
        audio_size=audio_size,
    )
    with _info_lock:
        if len(_info_cache) > 50:
            _info_cache.clear()
        _info_cache[url] = (time.time(), data)
    return jsonify(data)


# ---------------- Download jobs (background + progress) ----------------
JOBS, JOBS_LOCK = {}, threading.Lock()
_slots = threading.BoundedSemaphore(2)
JOB_TTL = 30 * 60


def video_format(h):
    # Prefer H.264/AAC (plays on every phone, merges without re-encoding = fast).
    return (
        f"bv*[height={h}][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*[height={h}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height={h}]+ba[ext=m4a]/bv*[height={h}]+ba/b[height={h}]/"
        f"bv*[height<={h}]+ba/b"
    )


def friendly_error(e):
    print("yt-dlp error:", e, flush=True)  # real cause shows in the server log
    t = str(e).lower()
    if "ffmpeg" in t or "ffprobe" in t:
        return "ffmpeg is missing. In Termux run: pkg install ffmpeg"
    if "sign in" in t or "bot" in t or "403" in t:
        return ("YouTube blocked this request. Check the server log for the real error. "
                "Usual causes: no JavaScript runtime (install nodejs), outdated yt-dlp, or a blocked server IP.")
    if "no space" in t:
        return "Phone storage is full."
    return "Download failed. Update yt-dlp (pip install -U yt-dlp), then try again."


def run_job(job):
    streams = {}

    def hook(d):
        if job["cancel"]:
            raise yt_dlp.utils.DownloadCancelled()
        key = d.get("filename")
        if d["status"] == "downloading":
            streams[key] = (d.get("downloaded_bytes") or 0, d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
            done = sum(v[0] for v in streams.values())
            total = job["expected"] or sum(v[1] for v in streams.values())
            if total:
                job["percent"] = min(99.0, done * 100.0 / total)
            job["speed"] = d.get("speed") or 0
            job["eta"] = d.get("eta")
        elif d["status"] == "finished":
            n = d.get("downloaded_bytes") or d.get("total_bytes") or 0
            streams[key] = (n, n)

    def pp_hook(d):
        if d["status"] == "started":
            job["state"], job["speed"], job["eta"] = "merging", 0, None

    opts = {
        **BASE_OPTS,
        "outtmpl": os.path.join(job["dir"], "%(title).80s.%(ext)s"),
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
    }
    if job["quality"] == "audio":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio"  # no conversion = fast
    else:
        opts["format"] = video_format(int(job["quality"]))
        opts["merge_output_format"] = "mp4"

    job["state"] = "downloading"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([job["url"]])
        files = [f for f in glob.glob(os.path.join(job["dir"], "*")) if not f.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("no file produced")
        path = max(files, key=os.path.getsize)
        job.update(path=path, filename=os.path.basename(path), percent=100.0, state="done")
    except Exception as e:
        if job["cancel"]:
            job["state"] = "cancelled"
        else:
            job.update(state="error", error=friendly_error(e))
        shutil.rmtree(job["dir"], ignore_errors=True)
    finally:
        _slots.release()


@app.post("/api/start")
@login_required(api=True)
def start():
    body = request.get_json(silent=True) or {}
    url, quality = str(body.get("url", "")).strip(), str(body.get("quality", ""))
    if not is_valid(url):
        return jsonify(error="Invalid link."), 400
    if not (quality == "audio" or (quality.isdigit() and 100 <= int(quality) <= 4320)):
        return jsonify(error="Invalid quality."), 400
    if not _slots.acquire(blocking=False):
        return jsonify(error="Two downloads are already running. Wait for one to finish."), 429

    jid = secrets.token_urlsafe(12)
    job = dict(
        id=jid, url=url, quality=quality, state="queued", percent=0.0, speed=0, eta=None,
        error="", path="", filename="", cancel=False, created=time.time(),
        expected=int(body.get("expected") or 0) if str(body.get("expected") or "0").isdigit() else 0,
        dir=tempfile.mkdtemp(prefix="ytsaver_"),
    )
    with JOBS_LOCK:
        JOBS[jid] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify(id=jid)


@app.get("/api/status/<jid>")
@login_required(api=True)
def status(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify(error="Job expired. Start again."), 404
    return jsonify(state=job["state"], percent=round(job["percent"], 1), speed=job["speed"],
                   eta=job["eta"], error=job["error"], filename=job["filename"])


@app.post("/api/cancel/<jid>")
@login_required(api=True)
def cancel(jid):
    job = JOBS.get(jid)
    if job:
        job["cancel"] = True
    return jsonify(ok=True)


@app.get("/api/file/<jid>")
@login_required(api=True)
def get_file(jid):
    job = JOBS.get(jid)
    if not job or job["state"] != "done" or not os.path.exists(job["path"]):
        return jsonify(error="File not ready or expired."), 404
    return send_file(job["path"], as_attachment=True, download_name=job["filename"], conditional=True)


def janitor():
    while True:
        time.sleep(60)
        now = time.time()
        with JOBS_LOCK:
            for jid in [j for j, v in JOBS.items() if now - v["created"] > JOB_TTL and v["state"] != "downloading"]:
                shutil.rmtree(JOBS[jid]["dir"], ignore_errors=True)
                JOBS.pop(jid, None)


threading.Thread(target=janitor, daemon=True).start()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")),
            debug=False, threaded=True)
