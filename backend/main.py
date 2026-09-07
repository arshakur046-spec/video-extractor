import os
import re
import uuid

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from terabox_gateway import fetch_direct_links


app = FastAPI(
    title="TeraBox Video Extractor API",
    version="2.0.0",
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


def safe_filename(filename: str) -> str:
    filename = os.path.basename(filename)
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
    return filename.strip() or "video.mp4"


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "service": "terabox-video-extractor",
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
    }


@app.post("/extract")
async def extract_video(request: ExtractRequest):
    if not request.url:
        raise HTTPException(
            status_code=400,
            detail="TeraBox URL is required",
        )

    try:
        files = await fetch_direct_links(request.url)

        if isinstance(files, dict) and files.get("error"):
            raise HTTPException(
                status_code=400,
                detail=files.get("error"),
            )

        if not files:
            raise HTTPException(
                status_code=404,
                detail="No files found",
            )

        results = []

        async with aiohttp.ClientSession() as session:
            for item in files:
                direct_url = (
                    item.get("direct_link")
                    or item.get("download_link")
                    or item.get("link")
                )

                if not direct_url:
                    continue

                original_name = item.get(
                    "filename",
                    "video.mp4",
                )

                filename = safe_filename(original_name)

                if not filename.lower().endswith(
                    (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v")
                ):
                    filename = f"{filename}.mp4"

                job_id = str(uuid.uuid4())

                stored_filename = f"{job_id}_{filename}"
                output_path = os.path.join(
                    DATA_DIR,
                    stored_filename,
                )

                async with session.get(
                    direct_url,
                    timeout=aiohttp.ClientTimeout(
                        total=1800
                    ),
                ) as response:

                    if response.status != 200:
                        continue

                    with open(output_path, "wb") as output:
                        async for chunk in response.content.iter_chunked(
                            1024 * 1024
                        ):
                            output.write(chunk)

                file_size = os.path.getsize(output_path)

                results.append(
                    {
                        "id": job_id,
                        "filename": filename,
                        "size": file_size,
                        "download_url": (
                            f"/download/{stored_filename}"
                        ),
                    }
                )

        if not results:
            raise HTTPException(
                status_code=400,
                detail="Could not download files from TeraBox",
            )

        return {
            "success": True,
            "count": len(results),
            "files": results,
        }

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )


@app.get("/download/{filename}")
def download_video(filename: str):
    safe_name = os.path.basename(filename)

    file_path = os.path.join(
        DATA_DIR,
        safe_name,
    )

    if not os.path.isfile(file_path):
        raise HTTPException(
            status_code=404,
            detail="Video not found",
        )

    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=safe_name,
    )