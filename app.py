from fastapi import FastAPI, Request, Query, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import os
import asyncio
from datetime import datetime, timedelta
from urllib.parse import quote
import io
import logging
import json
import time
from collections import defaultdict
import uvicorn
import gradio as gr

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)
BANNED_IP_FILE = "bannedip.json"
MAX_REQUESTS_PER_MINUTE = 35

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

server_start_time = time.time()
total_request_count = 0
request_counter = defaultdict(list)


def load_banned_ips():
    try:
        with open(BANNED_IP_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_banned_ips(banned_ips):
    with open(BANNED_IP_FILE, "w") as f:
        json.dump(list(banned_ips), f)


async def delete_file_after_delay(file_path: str, delay: int = 600):
    await asyncio.sleep(delay)
    try:
        os.remove(file_path)
        logger.info(f"Deleted file {file_path} after {delay} seconds.")
    except FileNotFoundError:
        logger.warning(f"File {file_path} not found during deletion.")
    except Exception as e:
        logger.error(f"Error deleting file {file_path}: {e}", exc_info=True)


app = FastAPI(
    title="YouTube Downloader API",
    description="API to download video/audio from YouTube.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    global total_request_count
    total_request_count += 1

    client_ip = (
        request.headers.get("X-Forwarded-For", request.client.host)
        .split(",")[0]
        .strip()
    )
    banned_ips = load_banned_ips()

    if client_ip in banned_ips:
        return JSONResponse(
            status_code=403, content={"message": "Blocked due to excessive requests."}
        )

    now = datetime.now()
    request_counter[client_ip].append(now)
    request_counter[client_ip] = [
        t for t in request_counter[client_ip] if t > now - timedelta(minutes=1)
    ]

    if len(request_counter[client_ip]) > MAX_REQUESTS_PER_MINUTE:
        logger.warning(f"IP {client_ip} is blocked due to rate limiting.")
        banned_ips.add(client_ip)
        save_banned_ips(banned_ips)
        request_counter[client_ip] = []

    logger.info(f"{client_ip} requested {request.method} {request.url}")
    response = await call_next(request)
    return response


@app.get("/", summary="Root Endpoint")
async def root():
    return JSONResponse(status_code=200, content={"status": "Server Running"})


@app.get("/ytv/", summary="Download YouTube Video")
async def download_video(
    background_tasks: BackgroundTasks,
    url: str = Query(...),
    quality: int = Query(720),
    mode: str = Query("url"),
):
    if mode not in ["url", "buffer"]:
        return JSONResponse(
            status_code=400, content={"error": "Invalid mode. Use 'url' or 'buffer'."}
        )

    try:
        resx = f"_{quality}p"
        ydl_opts = {
            "format": f"bestvideo[height<={quality}]+bestaudio/best",
            "outtmpl": os.path.join(OUTPUT_DIR, "%(title)s" + resx + ".%(ext)s"),
            "merge_output_format": "mp4",
            "cookiefile": "yt.txt",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)
        file_ext = os.path.splitext(file_path)[-1].lstrip(".")
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found after download: {file_path}")
        background_tasks.add_task(delete_file_after_delay, file_path)
        if mode == "url":
            return {
                "status": "success",
                "title": info["title"],
                "thumb": info.get("thumbnail"),
                "url": f"https://ytdlsparky-1cf5cc06ac23.herokuapp.com/cdn/video/{quote(os.path.basename(file_path))}",
            }
        media_type = f"video/{file_ext}" if file_ext else "video/mp4"
        with open(file_path, "rb") as f:
            return StreamingResponse(
                io.BytesIO(f.read()),
                media_type=media_type,
                headers={
                    "Content-Disposition": f"attachment; filename={os.path.basename(file_path)}"
                },
            )
    except Exception as e:
        logger.error(f"Download error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/cdn/video/{filename}", summary="Serve Downloaded File")
async def download_file(filename: str):
    file_path = os.path.join(OUTPUT_DIR, filename)
    if os.path.exists(file_path):
        file_ext = os.path.splitext(filename)[-1].lstrip(".")
        media_type = f"video/{file_ext}" if file_ext else "video/mp4"
        return FileResponse(file_path, filename=filename, media_type=media_type)
    return JSONResponse(status_code=404, content={"error": "File not found"})


@app.get("/yta/m4a/", summary="Download YouTube Audio (.m4a)")
async def download_audio(
    background_tasks: BackgroundTasks,
    url: str = Query(..., description="YouTube video URL"),
    mode: str = Query("url", description="Response mode: 'url' or 'buffer'"),
):
    if mode not in {"url", "buffer"}:
        return JSONResponse(
            status_code=400, content={"error": "Invalid mode. Use 'url' or 'buffer'."}
        )

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": os.path.join(OUTPUT_DIR, "%(title).200s.%(ext)s"),
        "cookiefile": "yt.txt",
        "noplaylist": True,
        "no_warnings": True,
        "postprocessors": [{"key": "FFmpegMetadata"}],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info.get("requested_downloads"):
            file_path = info["requested_downloads"][0]["filepath"]
        else:
            file_path = ydl.prepare_filename(info)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        background_tasks.add_task(delete_file_after_delay, file_path)
        if mode == "url":
            return {
                "status": "success",
                "title": info.get("title"),
                "thumb": info.get("thumbnail"),
                "url": f"https://ytdlsparky-1cf5cc06ac23.herokuapp.com/cdn/audio/{quote(os.path.basename(file_path))}",
            }
        with open(file_path, "rb") as f:
            data = f.read()

        headers = {
            "Content-Disposition": f'attachment; filename="{os.path.basename(file_path)}"'
        }
        return StreamingResponse(
            io.BytesIO(data), media_type="audio/m4a", headers=headers
        )
    except Exception as e:
        logger.error(f"Download error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/yta/", summary="Download YouTube Audio (.mp3)")
async def download_audio(
    background_tasks: BackgroundTasks,
    url: str = Query(..., description="YouTube video URL"),
    mode: str = Query("url", description="Response mode: 'url' or 'buffer'"),
):
    if mode not in {"url", "buffer"}:
        return JSONResponse(
            status_code=400, content={"error": "Invalid mode. Use 'url' or 'buffer'."}
        )
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s"),
        "cookiefile": "yt.txt",
        "noplaylist": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
        "postprocessor_args": ["-vn", "-preset", "ultrafast", "-threads", "4"],
        "prefer_ffmpeg": True,
        "noplaylist": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info.get("requested_downloads"):
            file_path = info["requested_downloads"][0]["filepath"]
        else:
            base, _ = os.path.splitext(ydl.prepare_filename(info))
            file_path = base + ".m4a"
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        background_tasks.add_task(delete_file_after_delay, file_path)
        if mode == "url":
            return {
                "status": "success",
                "title": info.get("title"),
                "thumb": info.get("thumbnail"),
                "url": f"https://ytdlsparky-1cf5cc06ac23.herokuapp.com/cdn/audio/{quote(os.path.basename(file_path))}",
            }

        with open(file_path, "rb") as f:
            data = f.read()
        headers = {
            "Content-Disposition": f'attachment; filename="{os.path.basename(file_path)}"'
        }
        return StreamingResponse(
            io.BytesIO(data), media_type="audio/m4a", headers=headers
        )

    except Exception as e:
        logger.error(f"Download error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/cdn/audio/{filename}", summary="Serve Downloaded Audio File")
async def serve_audio_file(filename: str):
    file_path = os.path.join(OUTPUT_DIR, filename)
    if os.path.exists(file_path):
        ext = os.path.splitext(filename)[-1].lstrip(".")
        return FileResponse(file_path, filename=filename, media_type=f"audio/{ext}")
    return JSONResponse(status_code=404, content={"error": "File not found"})


def run_gradio_ui():
    def download_gradio(url, resolution):
        try:
            quality = int(resolution)
            ydl_opts = {
                "format": f"bestvideo[height<={quality}]+bestaudio/best",
                "outtmpl": os.path.join(
                    OUTPUT_DIR, "%(title)s" + f"_{quality}p" + ".%(ext)s"
                ),
                "merge_output_format": "mp4",
                "postprocessors": [
                    {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
                ],
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = ydl.prepare_filename(info)
            return {
                "status": "success",
                "title": info["title"],
                "url": f"/cdn/video/{quote(os.path.basename(file_path))}",
            }
        except Exception as e:
            return {"error": str(e)}

    interface = gr.Interface(
        fn=download_gradio,
        inputs=["text", gr.Slider(240, 1080)],
        outputs="json",
        title="YouTube Video Downloader",
    )
    interface.launch()


# if __name__ == "__main__":
#     uvicorn.run("app:app", host="0.0.0.0", port=8000)