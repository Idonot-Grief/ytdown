import os
import subprocess
import ssl
import uuid
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, send_file, abort
import yt_dlp

# =========================
# Configuration
# =========================

HOST = "0.0.0.0"
PORT = 443  # HTTPS default
DOWNLOAD_DIR = "downloads"

DEFAULT_AUDIO_BITRATE = "192"
TOKEN_EXPIRE_SECONDS = 5
FILE_DELETE_SECONDS = 10
MAX_PARALLEL_DOWNLOADS = 4
MAX_DOWNLOADS_PER_DAY = 10

# Replace with your domain
DOMAIN = "example.com"

# Cert paths
CERT_DIR = f"certs/{DOMAIN}"
CERT_FILE = os.path.join(CERT_DIR, "fullchain.pem")
KEY_FILE = os.path.join(CERT_DIR, "privkey.pem")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CERT_DIR, exist_ok=True)

# =========================
# Global State
# =========================

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL_DOWNLOADS)
DOWNLOADS = {}   # token -> download info
IP_LIMITS = {}   # ip -> {date, count}
LOCK = threading.Lock()

# =========================
# Certificate Setup
# =========================

def ensure_certificates(domain):
    """
    Ensure certs exist for HTTPS.
    If not, attempt to auto-create using Certbot.
    If fails, pause execution and print instructions.
    """
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        print(f"[INFO] Certificates found for {domain}")
        return CERT_FILE, KEY_FILE

    print(f"[WARNING] Certificates not found for {domain}. Attempting to generate with certbot...")

    # Try to run certbot automatically
    try:
        result = subprocess.run([
            "certbot", "certonly", "--standalone", "-d", domain, "--non-interactive", "--agree-tos", "-m", "admin@"+domain
        ], capture_output=True, text=True)

        if result.returncode != 0:
            print("[ERROR] Certbot failed:")
            print(result.stdout)
            print(result.stderr)
            input("Please fix certificates manually and press Enter to continue...")
        else:
            print("[INFO] Certbot succeeded. Certificates should be in /etc/letsencrypt/live/")
            # Symlink or copy the certs to CERT_DIR
            live_dir = f"/etc/letsencrypt/live/{domain}"
            os.symlink(os.path.join(live_dir, "fullchain.pem"), CERT_FILE)
            os.symlink(os.path.join(live_dir, "privkey.pem"), KEY_FILE)

    except FileNotFoundError:
        print("[ERROR] Certbot not installed. Please install Certbot and rerun.")
        input("Press Enter to continue after installing certbot...")
    return CERT_FILE, KEY_FILE

# =========================
# Helpers (same as previous)
# =========================

def today():
    return datetime.utcnow().strftime("%Y-%m-%d")

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
    audio = f"bestaudio[ext={audio_ext}][abr<={audio}]"
    return f"{video}+{audio}/best"

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
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        with LOCK:
            DOWNLOADS[token]["status"] = "done"
            DOWNLOADS[token]["file"] = output_path
        schedule_delete(output_path, FILE_DELETE_SECONDS)
        schedule_token_expire(token, TOKEN_EXPIRE_SECONDS)
    except Exception as e:
        with LOCK:
            DOWNLOADS[token]["status"] = "error"
            DOWNLOADS[token]["error"] = str(e)

# =========================
# Routes (HTTPS only)
# =========================

@app.route("/watch")
def watch():
    video_id = request.args.get("v")
    not_json = "not-json" in request.args
    if not video_id:
        abort(400)

    ip = request.remote_addr
    if not check_ip_limit(ip):
        abort(429)

    res = request.args.get("res")
    audio = request.args.get("audio", DEFAULT_AUDIO_BITRATE)
    fmt = request.args.get("format", "mp4").lower()
    token = request.args.get("token") or uuid.uuid4().hex

    if fmt not in ("mp4", "webm"):
        abort(400)

    with LOCK:
        if token in DOWNLOADS:
            abort(409)
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
            "file": None
        }

    executor.submit(download_worker, token, video_id, res, audio, fmt, ip)

    if not_json:
        while True:
            with LOCK:
                info = DOWNLOADS.get(token)
                if not info:
                    abort(410)
                if info["status"] == "done":
                    return send_file(info["file"], as_attachment=True)
                if info["status"] == "error":
                    abort(500)
            time.sleep(0.25)

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
            "ready": info["status"] == "done"
        })

@app.route("/download")
def download():
    token = request.args.get("token")
    if not token:
        abort(400)
    ip = request.remote_addr
    with LOCK:
        info = DOWNLOADS.get(token)
        if not info or info["status"] != "done":
            abort(409)
        if info["ip"] != ip:
            abort(403)
        return send_file(info["file"], as_attachment=True)

# =========================
# Main
# =========================

if __name__ == "__main__":
    cert_file, key_file = ensure_certificates(DOMAIN)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    print(f"[INFO] HTTPS server running on https://{DOMAIN}:{PORT}")
    app.run(host=HOST, port=PORT, ssl_context=context)
