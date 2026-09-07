import os
import uuid

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel


app = FastAPI(
    title="Video Extractor API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = "/data"

os.makedirs(DATA_DIR, exist_ok=True)


class ExtractRequest(BaseModel):
    url: str


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "service": "video-extractor",
    }


@app.post("/extract")
def extract_video(request: ExtractRequest):
    job_id = str(uuid.uuid4())

    output_template = os.path.join(
        DATA_DIR,
        f"{job_id}.%(ext)s",
    )

    options = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                request.url,
                download=True,
            )

            filename = ydl.prepare_filename(info)

        base, _ = os.path.splitext(filename)
        mp4_file = f"{base}.mp4"

        if os.path.exists(mp4_file):
            final_file = mp4_file
        elif os.path.exists(filename):
            final_file = filename
        else:
            raise Exception("Downloaded file was not found")

        final_filename = os.path.basename(final_file)

        return {
            "success": True,
            "id": job_id,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "filename": final_filename,
            "download_url": f"/download/{final_filename}",
        }

    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )


@app.get("/download/{filename}")
def download_video(filename: str):
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(DATA_DIR, safe_filename)

    if not os.path.isfile(file_path):
        raise HTTPException(
            status_code=404,
            detail="Video not found",
        )

    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=safe_filename,
    )