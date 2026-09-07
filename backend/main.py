import asyncio
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
    title="TeraBox Telegram Video Extractor",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = "/data"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

os.makedirs(DATA_DIR, exist_ok=True)


class ExtractRequest(BaseModel):
    url: str


def safe_filename(filename: str) -> str:
    filename = os.path.basename(filename)
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
    return filename.strip() or "video.mp4"


async def telegram_api(
    session: aiohttp.ClientSession,
    method: str,
    data: dict,
):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/{method}"
    )

    async with session.post(
        url,
        data=data,
        timeout=aiohttp.ClientTimeout(total=120),
    ) as response:
        return await response.json()


async def send_telegram_message(
    session: aiohttp.ClientSession,
    chat_id: int,
    text: str,
):
    return await telegram_api(
        session,
        "sendMessage",
        {
            "chat_id": str(chat_id),
            "text": text,
        },
    )


async def send_telegram_video(
    session: aiohttp.ClientSession,
    chat_id: int,
    video_url: str,
    filename: str,
):
    return await telegram_api(
        session,
        "sendVideo",
        {
            "chat_id": str(chat_id),
            "video": video_url,
            "caption": filename,
            "supports_streaming": "true",
        },
    )


async def extract_terabox(url: str):
    files = await fetch_direct_links(url)

    if isinstance(files, dict) and files.get("error"):
        raise Exception(files.get("error"))

    if not files:
        raise Exception("No files found")

    results = []

    for item in files:
        direct_url = (
            item.get("direct_link")
            or item.get("download_link")
            or item.get("link")
        )

        if not direct_url:
            continue

        filename = safe_filename(
            item.get("filename", "video.mp4")
        )

        results.append(
            {
                "filename": filename,
                "direct_url": direct_url,
                "size": item.get("size"),
            }
        )

    if not results:
        raise Exception(
            "Could not get direct download link"
        )

    return results


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "service": "terabox-telegram-extractor",
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
        files = await extract_terabox(request.url)

        return {
            "success": True,
            "count": len(files),
            "files": files,
        }

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


async def handle_telegram_update(
    session: aiohttp.ClientSession,
    update: dict,
):
    message = update.get("message")

    if not message:
        return

    chat = message.get("chat")
    text = message.get("text", "").strip()

    if not chat or not text:
        return

    chat_id = chat["id"]

    if text == "/start":
        await send_telegram_message(
            session,
            chat_id,
            "Send me a TeraBox link and I will extract the video.",
        )
        return

    if text == "/help":
        await send_telegram_message(
            session,
            chat_id,
            "Send a public TeraBox sharing link.",
        )
        return

    if not (
        "terabox" in text.lower()
        or "terashare" in text.lower()
    ):
        await send_telegram_message(
            session,
            chat_id,
            "Please send a valid TeraBox link.",
        )
        return

    await send_telegram_message(
        session,
        chat_id,
        "⏳ Extracting video...",
    )

    try:
        files = await extract_terabox(text)

        for item in files:
            result = await send_telegram_video(
                session,
                chat_id,
                item["direct_url"],
                item["filename"],
            )

            if not result.get("ok"):
                await send_telegram_message(
                    session,
                    chat_id,
                    "Telegram could not send this video. "
                    "The file may be larger than 50 MB.",
                )

    except Exception as error:
        await send_telegram_message(
            session,
            chat_id,
            f"❌ Extraction failed:\n{error}",
        )


async def telegram_polling():
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not configured")
        return

    offset = 0

    async with aiohttp.ClientSession() as session:
        await telegram_api(
            session,
            "deleteWebhook",
            {
                "drop_pending_updates": "false",
            },
        )

        print("Telegram bot polling started")

        while True:
            try:
                result = await telegram_api(
                    session,
                    "getUpdates",
                    {
                        "offset": str(offset),
                        "timeout": "30",
                        "allowed_updates": '["message"]',
                    },
                )

                if not result.get("ok"):
                    await asyncio.sleep(5)
                    continue

                updates = result.get("result", [])

                for update in updates:
                    offset = update["update_id"] + 1

                    await handle_telegram_update(
                        session,
                        update,
                    )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                print(
                    f"Telegram polling error: {error}"
                )
                await asyncio.sleep(5)


telegram_task = None


@app.on_event("startup")
async def startup_event():
    global telegram_task
    telegram_task = asyncio.create_task(
        telegram_polling()
    )


@app.on_event("shutdown")
async def shutdown_event():
    if telegram_task:
        telegram_task.cancel()

        try:
            await telegram_task
        except asyncio.CancelledError:
            pass