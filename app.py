import os
import uuid
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, send_file, abort
import yt_dlp

# =========================
# Configuration
# =========================

HOST = "0.0.0.0"
PORT = 80

DOWNLOAD_DIR = "downloads"
MAX_PARALLEL_DOWNLOADS = 4

DEFAULT_AUDIO_BITRATE = "192"
TOKEN_EXPIRE_SECONDS = 300  # 5 minutes - give users time to download
FILE_DELETE_SECONDS = 360   # 6 minutes - delete after token expires

MAX_DOWNLOADS_PER_DAY = 10

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# =========================
# Global State
# =========================

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL_DOWNLOADS)

DOWNLOADS = {}   # token -> info
IP_LIMITS = {}   # ip -> {date, count}
LOCK = threading.Lock()

# =========================
# Helpers
# =========================

def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def check_ip_limit(ip):
    with LOCK:
        entry = IP_LIMITS.get(ip)
        if not entry or entry["date"] != today():
            IP_LIMITS[ip] = {"date": today(), "count": 1}
            return True
        if entry["count"] >= MAX_DOWNLOADS_PER_DAY:
            return False
        entry["count"] += 1
        return True


def schedule_delete(path, delay):
    def worker():
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
        except:
            pass
    threading.Thread(target=worker, daemon=True).start()


def schedule_token_expire(token, delay):
    def worker():
        time.sleep(delay)
        with LOCK:
            DOWNLOADS.pop(token, None)
    threading.Thread(target=worker, daemon=True).start()


def build_format(res, audio, fmt):
    video_ext = "mp4" if fmt == "mp4" else "webm"
    audio_ext = "m4a" if fmt == "mp4" else "webm"

    video = f"bestvideo[ext={video_ext}]"
    if res:
        video += f"[height<={res}]"

    audio_part = f"bestaudio[ext={audio_ext}][abr<={audio}]"
    return f"{video}+{audio_part}/best"


# =========================
# Download Worker
# =========================

def download_worker(token, video_id, res, audio, fmt, ip):
    url = f"https://www.youtube.com/watch?v={video_id}"
    output_path = os.path.join(DOWNLOAD_DIR, f"{token}.{fmt}")

    def progress_hook(d):
        with LOCK:
            info = DOWNLOADS.get(token)
            if not info:
                return

            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)

                info.update({
                    "status": "downloading",
                    "downloaded_bytes": downloaded,
                    "total_bytes": total,
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                    "percent": round((downloaded / total) * 100, 2) if total else 0
                })

            elif d["status"] == "finished":
                info["status"] = "processing"
                info["percent"] = 100.0

    ydl_opts = {
        "format": build_format(res, audio, fmt),
        "outtmpl": output_path,
        "merge_output_format": fmt,
        "progress_hooks": [progress_hook],
        "concurrent_fragment_downloads": 8,
        "retries": 10,
        "fragment_retries": 10,
        "quiet": True,
        "no_warnings": True,
        # Fix for HTTP 403 errors - add headers to mimic browser
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-us,en;q=0.5",
            "Sec-Fetch-Mode": "navigate",
        },
        # Additional fixes for YouTube
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "player_skip": ["webpage", "configs"],
            }
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        with LOCK:
            if token in DOWNLOADS:
                DOWNLOADS[token]["status"] = "done"
                DOWNLOADS[token]["file"] = output_path

        # Schedule cleanup - file deleted after token expires
        schedule_token_expire(token, TOKEN_EXPIRE_SECONDS)
        schedule_delete(output_path, FILE_DELETE_SECONDS)

    except Exception as e:
        with LOCK:
            if token in DOWNLOADS:
                DOWNLOADS[token]["status"] = "error"
                DOWNLOADS[token]["error"] = str(e)

# =========================
# Routes
# =========================

@app.route("/watch")
def watch():
    video_id = request.args.get("v")
    not_json = "not-json" in request.args

    if not video_id:
        if not_json:
            abort(400)
        return jsonify({"error": "Missing v"}), 400

    ip = request.remote_addr
    if not check_ip_limit(ip):
        if not_json:
            abort(429)
        return jsonify({"error": "Daily limit reached"}), 429

    res = request.args.get("res")
    audio = request.args.get("audio", DEFAULT_AUDIO_BITRATE)
    fmt = request.args.get("format", "mp4").lower()
    token = request.args.get("token") or uuid.uuid4().hex

    if fmt not in ("mp4", "webm"):
        if not_json:
            abort(400)
        return jsonify({"error": "Invalid format"}), 400

    with LOCK:
        if token in DOWNLOADS:
            if not_json:
                abort(409)
            return jsonify({"error": "Token in use"}), 409

        DOWNLOADS[token] = {
            "token": token,
            "ip": ip,
            "video_id": video_id,
            "format": fmt,
            "status": "queued",
            "percent": 0,
            "speed": None,
            "eta": None,
            "downloaded_bytes": 0,
            "total_bytes": None,
            "started": time.time(),
            "file": None,
            "error": None
        }

    executor.submit(download_worker, token, video_id, res, audio, fmt, ip)

    # NOT-JSON MODE: wait until done, then send file
    if not_json:
        while True:
            with LOCK:
                info = DOWNLOADS.get(token)
                if not info:
                    abort(410)
                if info["status"] == "done" and info["file"]:
                    file_path = info["file"]
            
            # Check status outside lock to avoid holding it during file send
            with LOCK:
                info = DOWNLOADS.get(token)
                if not info:
                    abort(410)
                if info["status"] == "done" and info["file"]:
                    return send_file(info["file"], as_attachment=True)
                if info["status"] == "error":
                    abort(500)
            
            time.sleep(0.25)

    # JSON MODE (default)
    return jsonify({
        "status": "queued",
        "token": token,
        "progress": f"/progress?token={token}",
        "download": f"/download?token={token}"
    })


@app.route("/progress")
def progress():
    token = request.args.get("token")
    if not token:
        return jsonify({"error": "Missing token"}), 400

    ip = request.remote_addr
    with LOCK:
        info = DOWNLOADS.get(token)
        if not info:
            return jsonify({"error": "Invalid or expired token"}), 404
        if info["ip"] != ip:
            return jsonify({"error": "Forbidden"}), 403

        return jsonify({
            "token": token,
            "status": info["status"],
            "percent": info["percent"],
            "speed_bps": info["speed"],
            "eta_seconds": info["eta"],
            "downloaded_bytes": info["downloaded_bytes"],
            "total_bytes": info["total_bytes"],
            "format": info["format"],
            "elapsed": round(time.time() - info["started"], 2),
            "ready": info["status"] == "done",
            "error": info.get("error")
        })


@app.route("/download")
def download():
    token = request.args.get("token")
    if not token:
        return jsonify({"error": "Missing token"}), 400

    ip = request.remote_addr
    with LOCK:
        info = DOWNLOADS.get(token)
        if not info or info["status"] != "done":
            return jsonify({"error": "Not ready or expired"}), 409
        if info["ip"] != ip:
            return jsonify({"error": "Forbidden"}), 403

        file_path = info["file"]
    
    # Send file outside the lock
    return send_file(file_path, as_attachment=True)

# =========================
# Main
# =========================

if __name__ == "__main__":
    app.run(host=HOST, port=PORT)
