
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from functools import wraps
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    render_template,
    request,
    send_file,
)
from yt_dlp import YoutubeDL


app = Flask(__name__)

# Limit incoming JSON/request bodies.
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

# Resource limits. These are per Python process.
JOB_TTL_SECONDS = 30 * 60
MAX_ACTIVE_JOBS = 4
MAX_FILESIZE_BYTES = 250 * 1024 * 1024
MAX_ANALYSES_AT_ONCE = 3

# Keep this app bound to localhost for local development.
# Use a production WSGI server if you deploy it.
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))

JOBS = {}
JOBS_LOCK = threading.Lock()

ANALYSIS_SEMAPHORE = threading.BoundedSemaphore(
    MAX_ANALYSES_AT_ONCE
)

# In-memory rate limiting. This resets when the process restarts
# and is not shared between multiple worker processes.
RATE_LIMITS = defaultdict(deque)
RATE_LIMIT_LOCK = threading.Lock()

RATE_LIMIT_RULES = {
    "analyze": (10, 60),
    "download": (5, 60),
    "status": (120, 60),
    "file": (30, 60),
}

ADSENSE_CLIENT_ID = os.getenv("ADSENSE_CLIENT_ID", "").strip()
ADSENSE_SLOT_TOP = os.getenv("ADSENSE_SLOT_TOP", "").strip()
ADSENSE_SLOT_BOTTOM = os.getenv("ADSENSE_SLOT_BOTTOM", "").strip()

DIRECT_ADS = {
    "top": {
        "url": os.getenv("DIRECT_AD_TOP_URL", "").strip(),
        "title": os.getenv("DIRECT_AD_TOP_TITLE", "Sponsored").strip(),
        "text": os.getenv("DIRECT_AD_TOP_TEXT", "").strip(),
        "image": os.getenv("DIRECT_AD_TOP_IMAGE", "").strip(),
    },
    "bottom": {
        "url": os.getenv("DIRECT_AD_BOTTOM_URL", "").strip(),
        "title": os.getenv("DIRECT_AD_BOTTOM_TITLE", "Sponsored").strip(),
        "text": os.getenv("DIRECT_AD_BOTTOM_TEXT", "").strip(),
        "image": os.getenv("DIRECT_AD_BOTTOM_IMAGE", "").strip(),
    },
}


def is_tiktok_url(value):
    """Accept HTTPS URLs on TikTok hostnames and standard HTTPS ports."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False

    value = value.strip()

    try:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port

        valid_host = (
            hostname == "tiktok.com"
            or hostname.endswith(".tiktok.com")
        )

        return (
            parsed.scheme.lower() == "https"
            and valid_host
            and port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not any(ord(char) < 32 for char in value)
        )
    except (ValueError, AttributeError):
        return False


def get_client_ip():
    """
    Use the address Flask sees directly.

    Do not trust X-Forwarded-For unless your reverse proxy is configured
    to remove incoming client-supplied forwarding headers and set its own.
    """
    return request.remote_addr or "unknown"


def rate_limit(bucket, limit, window_seconds=60):
    """Simple per-IP, in-memory rate limiter."""

    def decorator(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            now = time.monotonic()
            client_ip = get_client_ip()
            key = (bucket, client_ip)

            with RATE_LIMIT_LOCK:
                requests_for_ip = RATE_LIMITS[key]

                while (
                    requests_for_ip
                    and now - requests_for_ip[0] >= window_seconds
                ):
                    requests_for_ip.popleft()

                if len(requests_for_ip) >= limit:
                    response = jsonify({
                        "error": "Too many requests. Please wait and try again."
                    })
                    response.status_code = 429
                    response.headers["Retry-After"] = str(window_seconds)
                    return response

                requests_for_ip.append(now)

                # Remove old entries periodically to avoid unbounded growth.
                if len(RATE_LIMITS) > 5000:
                    for old_key in list(RATE_LIMITS.keys()):
                        old_queue = RATE_LIMITS[old_key]

                        while (
                            old_queue
                            and now - old_queue[0] >= window_seconds
                        ):
                            old_queue.popleft()

                        if not old_queue:
                            RATE_LIMITS.pop(old_key, None)

            return function(*args, **kwargs)

        return wrapper

    return decorator


@app.after_request
def add_security_headers(response):
    """Baseline response headers; the frontend may need CSP configuration."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    response.headers["Cache-Control"] = "no-store"

    # Enable HSTS only when the site is served exclusively over HTTPS.
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000"
        )

    return response


def clean_expired_jobs():
    """Remove expired inactive jobs and their temporary directories."""
    now = time.time()
    expired_jobs = []

    with JOBS_LOCK:
        for job_id, job in list(JOBS.items()):
            if job.get("status") in {
                "queued",
                "downloading",
                "processing",
            }:
                continue

            created_at = job.get("created_at", now)

            if now - created_at > JOB_TTL_SECONDS:
                removed = JOBS.pop(job_id, None)

                if removed:
                    expired_jobs.append(removed)

    for job in expired_jobs:
        directory = job.get("directory")

        if directory:
            shutil.rmtree(directory, ignore_errors=True)



def ydl_options(extra=None):
    """Common yt-dlp options for metadata extraction."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "skip_download": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        },
    }

    if extra:
        options.update(extra)

    return options


def available_qualities(info):
    """Return unique video heights in descending order."""
    heights = set()

    for fmt in info.get("formats") or []:
        height = fmt.get("height")
        video_codec = fmt.get("vcodec")

        if (
            isinstance(height, (int, float))
            and not isinstance(height, bool)
            and 144 <= height <= 4320
            and video_codec
            and video_codec != "none"
        ):
            heights.add(int(height))

    return sorted(heights, reverse=True)


def public_error(exc):
    """Return a safe, user-friendly error without exposing internals."""
    message = str(exc).lower()

    if any(term in message for term in (
        "ffmpeg",
        "ffprobe",
        "ffmpeg is not installed",
    )):
        return (
            "FFmpeg is required for this conversion. Install FFmpeg "
            "and make sure it is available on your system PATH."
        )

    if any(term in message for term in (
        "private video",
        "video is private",
        "private account",
        "not available to you",
        "login required",
    )):
        return "This video is private or access-restricted."

    if any(term in message for term in (
        "http error 403",
        "access denied",
        "forbidden",
    )):
        return "TikTok denied access to this video. Try again later."

    if any(term in message for term in (
        "video unavailable",
        "video not found",
        "does not exist",
        "has been removed",
        "not available",
    )):
        return "This video is unavailable or may have been deleted."

    if any(term in message for term in (
        "unsupported url",
        "not a tiktok",
    )):
        return "Please enter a valid TikTok video URL."

    if "max-filesize" in message or "file is larger" in message:
        return "This file exceeds the allowed download size."

    return (
        "Could not process this TikTok link. Check the URL "
        "and try again later."
    )


def update_job(job_id, **changes):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

        if job is not None:
            job.update(changes)


def count_active_jobs():
    with JOBS_LOCK:
        return sum(
            1
            for job in JOBS.values()
            if job.get("status") in {
                "queued",
                "downloading",
                "processing",
            }
        )


def progress_hook(job_id):
    """Create a progress callback for one download job."""

    def hook(data):
        status = data.get("status")

        if status == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = (
                data.get("total_bytes")
                or data.get("total_bytes_estimate")
                or 0
            )

            if total > 0:
                percentage = min(94, int(downloaded * 94 / total))
                message = f"Downloading media: {percentage}%"
            else:
                percentage = 0
                message = "Downloading media..."

            update_job(
                job_id,
                status="downloading",
                progress=percentage,
                message=message,
            )

        elif status == "finished":
            update_job(
                job_id,
                status="processing",
                progress=95,
                message="Download finished. Preparing your file...",
            )

    return hook


def postprocessor_hook(job_id):
    """Track audio extraction and other post-processing."""

    def hook(data):
        status = data.get("status")

        if status == "started":
            update_job(
                job_id,
                status="processing",
                progress=96,
                message="Converting media...",
            )

        elif status == "finished":
            update_job(
                job_id,
                status="processing",
                progress=99,
                message="Finalizing your file...",
            )

    return hook


def find_output_file(directory, media_format):
    """
    Find a plausible final media file inside the job's own directory.

    Do not serve arbitrary files, symlinks, or partial download files.
    """
    directory_path = Path(directory).resolve()

    if not directory_path.is_dir():
        raise FileNotFoundError("The download directory is unavailable.")

    if media_format == "mp3":
        allowed_suffixes = {".mp3"}
    else:
        allowed_suffixes = {
            ".mp4",
            ".m4v",
            ".mkv",
            ".webm",
            ".mov",
        }

    candidates = []

    for path in directory_path.iterdir():
        if path.is_symlink() or not path.is_file():
            continue

        if path.suffix.lower() not in allowed_suffixes:
            continue

        if path.name.endswith((".part", ".ytdl", ".temp")):
            continue

        resolved = path.resolve()

        if resolved.parent != directory_path:
            continue

        if path.stat().st_size > 0:
            candidates.append(path)

    if not candidates:
        raise FileNotFoundError("The downloaded media file was not found.")

    return max(candidates, key=lambda path: path.stat().st_size)


def download_worker(job_id, url, quality, media_format, directory):
    """Download and process media in a background thread."""
    try:
        update_job(
            job_id,
            status="downloading",
            progress=0,
            message="Starting download...",
        )

        output_template = str(Path(directory) / "media.%(ext)s")

        options = {
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
            "retries": 1,
            "extractor_retries": 1,
            "max_filesize": MAX_FILESIZE_BYTES,
            "progress_hooks": [progress_hook(job_id)],
            "postprocessor_hooks": [postprocessor_hook(job_id)],
        }

        if media_format == "mp3":
            options.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })

        elif quality == "best":
            options.update({
                "format": "best[ext=mp4]/best",
                "merge_output_format": "mp4",
            })

        else:
            height = int(quality)

            options.update({
                "format": (
                    f"bestvideo[height={height}][ext=mp4]+bestaudio/"
                    f"best[height={height}][ext=mp4]/"
                    f"bestvideo[height={height}]+bestaudio/"
                    f"best[height={height}]/best"
                ),
                "merge_output_format": "mp4",
            })

        with YoutubeDL(options) as ydl:
            ydl.download([url])

        output_file = find_output_file(directory, media_format)

        if media_format == "mp3" and output_file.suffix.lower() != ".mp3":
            raise RuntimeError("MP3 conversion did not produce an MP3 file.")

        if output_file.stat().st_size <= 0:
            raise RuntimeError("The downloaded file is empty.")

        download_name = (
            f"tiktok-audio{output_file.suffix.lower()}"
            if media_format == "mp3"
            else f"tiktok-video{output_file.suffix.lower()}"
        )

        update_job(
            job_id,
            status="complete",
            progress=100,
            message="Your file is ready.",
            file_path=str(output_file.resolve()),
            download_name=download_name,
            media_format=media_format,
        )

    except Exception as exc:
        app.logger.warning(
            "TikDrop download job %s failed (%s)",
            job_id,
            type(exc).__name__,
        )

        message = public_error(exc)

        update_job(
            job_id,
            status="error",
            progress=0,
            message=message,
            error=message,
        )


@app.get("/")
def index():
    return render_template(
        "index.html",
        adsense_ready=bool(ADSENSE_CLIENT_ID),
        adsense_client_id=ADSENSE_CLIENT_ID,
        adsense_slot_top=ADSENSE_SLOT_TOP,
        adsense_slot_bottom=ADSENSE_SLOT_BOTTOM,
        direct_ads=DIRECT_ADS,
    )


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/api/analyze")
@rate_limit(*RATE_LIMIT_RULES["analyze"])
def analyze():
    clean_expired_jobs()

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({"error": "Please provide a valid JSON request."}), 400

    url = payload.get("url")

    if not is_tiktok_url(url):
        return jsonify({
            "error": "Please enter a valid HTTPS TikTok video URL."
        }), 400

    if not ANALYSIS_SEMAPHORE.acquire(blocking=False):
        return jsonify({
            "error": "The analyzer is busy. Please try again shortly."
        }), 429

    try:
        with YoutubeDL(ydl_options()) as ydl:
            info = ydl.extract_info(url.strip(), download=False)

        if not info:
            return jsonify({
                "error": "Could not retrieve details for this video."
            }), 422

        return jsonify({
            "url": url.strip(),
            "title": info.get("title") or "TikTok video",
            "creator": (
                info.get("uploader")
                or info.get("creator")
                or info.get("channel")
                or "TikTok creator"
            ),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "qualities": available_qualities(info),
        })

    except Exception as exc:
        app.logger.warning(
            "TikDrop analyze failed (%s)",
            type(exc).__name__,
        )
        return jsonify({"error": public_error(exc)}), 422

    finally:
        ANALYSIS_SEMAPHORE.release()


@app.post("/api/download")
@rate_limit(*RATE_LIMIT_RULES["download"])
def start_download():
    clean_expired_jobs()

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({"error": "Please provide a valid JSON request."}), 400

    url = payload.get("url")
    quality = str(payload.get("quality", "best")).strip().lower()
    media_format = str(payload.get("format", "video")).strip().lower()

    if not is_tiktok_url(url):
        return jsonify({
            "error": "Please enter a valid HTTPS TikTok video URL."
        }), 400

    if media_format not in {"video", "mp3"}:
        return jsonify({"error": "Choose video or MP3 format."}), 400

    if media_format == "video":
        if quality != "best":
            if not re.fullmatch(r"\d{3,4}", quality):
                return jsonify({
                    "error": "The requested video quality is not available."
                }), 400

            if not 144 <= int(quality) <= 4320:
                return jsonify({
                    "error": "The requested video quality is not available."
                }), 400

    if count_active_jobs() >= MAX_ACTIVE_JOBS:
        return jsonify({
            "error": "The downloader is busy. Please try again shortly."
        }), 429

    # Verify that the selected resolution exists before queuing.
    if media_format == "video" and quality != "best":
        if not ANALYSIS_SEMAPHORE.acquire(blocking=False):
            return jsonify({
                "error": "The analyzer is busy. Please try again shortly."
            }), 429

        try:
            with YoutubeDL(ydl_options()) as ydl:
                info = ydl.extract_info(url.strip(), download=False)

            if not info or int(quality) not in available_qualities(info):
                return jsonify({
                    "error": "The requested video quality is not available."
                }), 400

        except Exception as exc:
            app.logger.warning(
                "TikDrop quality check failed (%s)",
                type(exc).__name__,
            )
            return jsonify({"error": public_error(exc)}), 422

        finally:
            ANALYSIS_SEMAPHORE.release()

    job_id = uuid.uuid4().hex
    directory = tempfile.mkdtemp(prefix="tikdrop_")

    job = {
        "id": job_id,
        "status": "queued",
        "progress": 0,
        "message": "Your download is queued...",
        "created_at": time.time(),
        "directory": directory,
        "file_path": None,
        "download_name": None,
        "media_format": media_format,
        "error": None,
    }

    with JOBS_LOCK:
        active_count = sum(
            1
            for existing in JOBS.values()
            if existing.get("status") in {
                "queued",
                "downloading",
                "processing",
            }
        )

        if active_count >= MAX_ACTIVE_JOBS:
            shutil.rmtree(directory, ignore_errors=True)
            return jsonify({
                "error": "The downloader is busy. Please try again shortly."
            }), 429

        JOBS[job_id] = job

    try:
        thread = threading.Thread(
            target=download_worker,
            args=(job_id, url.strip(), quality, media_format, directory),
            daemon=True,
        )
        thread.start()

    except Exception:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)

        shutil.rmtree(directory, ignore_errors=True)

        app.logger.exception("TikDrop could not start a download worker")

        return jsonify({
            "error": "Could not start the download. Please try again."
        }), 500

    return jsonify({
        "job_id": job_id,
        "message": "Download started.",
    }), 202


@app.get("/api/download-status/<job_id>")
@rate_limit(*RATE_LIMIT_RULES["status"])
def download_status(job_id):
    clean_expired_jobs()

    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        abort(404)

    with JOBS_LOCK:
        job = JOBS.get(job_id)

        if not job:
            return jsonify({
                "error": "This download session expired. Please start again."
            }), 404

        result = {
            "job_id": job_id,
            "status": job.get("status"),
            "progress": job.get("progress", 0),
            "message": job.get("message", ""),
            "format": job.get("media_format", "video"),
        }

        if job.get("status") == "complete":
            result["download_url"] = f"/api/download-file/{job_id}"
            result["preview_url"] = (
                f"/api/download-file/{job_id}?inline=1"
            )
            result["filename"] = job.get("download_name")

        if job.get("status") == "error":
            result["error"] = job.get("error") or job.get("message")

    return jsonify(result)


@app.get("/api/download-file/<job_id>")
@rate_limit(*RATE_LIMIT_RULES["file"])
def download_file(job_id):
    clean_expired_jobs()

    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        abort(404)

    with JOBS_LOCK:
        job = JOBS.get(job_id)

        if not job or job.get("status") != "complete":
            abort(404)

        file_path = job.get("file_path")
        directory = job.get("directory")
        download_name = job.get("download_name") or "tiktok-file"
        media_format = job.get("media_format")

    if not file_path or not directory:
        abort(404)

    try:
        directory_path = Path(directory).resolve()
        output_path = Path(file_path).resolve()

        # The file must remain inside its own job directory.
        if output_path.parent != directory_path:
            abort(404)

        if output_path.is_symlink() or not output_path.is_file():
            abort(404)

        if media_format == "mp3":
            if output_path.suffix.lower() != ".mp3":
                abort(404)
            mimetype = "audio/mpeg"
        else:
            if output_path.suffix.lower() not in {
                ".mp4", ".m4v", ".mkv", ".webm", ".mov"
            }:
                abort(404)
            mimetype = guess_type(output_path.name)[0] or "application/octet-stream"

    except (OSError, ValueError):
        abort(404)

    inline = request.args.get("inline") == "1"

    return send_file(
        output_path,
        mimetype=mimetype,
        as_attachment=not inline,
        download_name=download_name,
        conditional=True,
        max_age=0,
    )


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({
        "error": "The request is too large."
    }), 413


@app.errorhandler(429)
def too_many_requests(_error):
    return jsonify({
        "error": "Too many requests. Please wait and try again."
    }), 429


if __name__ == "__main__":
    # Never enable Flask's development debugger for a public deployment.
    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        threaded=True,
    )
