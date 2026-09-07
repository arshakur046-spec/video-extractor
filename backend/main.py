import asyncio
import os
import re

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from terabox_gateway import fetch_direct_links


app = FastAPI(
    title="TeraBox Telegram Video Extractor",
    version="3.1.0",
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


async def telegram_request(
    session: aiohttp.ClientSession,
    method: str,
    data: dict,
):
    if not TELEGRAM_BOT_TOKEN:
        raise Exception("TELEGRAM_BOT_TOKEN is missing")

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/{method}"
    )

    async with session.post(
        url,
        data=data,
        timeout=aiohttp.ClientTimeout(total=120),
    ) as response:

        text = await response.text()

        if response.status != 200:
            raise Exception(
                f"Telegram API error {response.status}: {text}"
            )

        return await response.json()


async def send_message(
    session: aiohttp.ClientSession,
    chat_id: int,
    text: str,
):
    return await telegram_request(
        session,
        "sendMessage",
        {
            "chat_id": str(chat_id),
            "text": text,
        },
    )


async def send_video(
    session: aiohttp.ClientSession,
    chat_id: int,
    video_url: str,
    filename: str,
):
    return await telegram_request(
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
        raise Exception(
            str(files.get("error"))
        )

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
            item.get(
                "filename",
                "video.mp4",
            )
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
            "Could not get direct TeraBox link"
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
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN
        ),
    }


@app.post("/extract")
async def extract_video(request: ExtractRequest):
    if not request.url:
        raise HTTPException(
            status_code=400,
            detail="TeraBox URL is required",
        )

    try:
        files = await extract_terabox(
            request.url
        )

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


async def process_telegram_message(
    session: aiohttp.ClientSession,
    message: dict,
):
    chat = message.get("chat")
    text = message.get("text", "").strip()

    if not chat or not text:
        return

    chat_id = chat["id"]

    print(
        f"Telegram message received: {text}",
        flush=True,
    )

    if text == "/start":
        await send_message(
            session,
            chat_id,
            "👋 Send me a TeraBox link.\n\n"
            "I will extract the video and send it here.",
        )
        return

    if text == "/help":
        await send_message(
            session,
            chat_id,
            "Send a public TeraBox sharing link.",
        )
        return

    if (
        "terabox" not in text.lower()
        and "terashare" not in text.lower()
    ):
        await send_message(
            session,
            chat_id,
            "❌ Please send a valid TeraBox link.",
        )
        return

    await send_message(
        session,
        chat_id,
        "⏳ Extracting your video...",
    )

    try:
        files = await extract_terabox(text)

        print(
            f"TeraBox files found: {len(files)}",
            flush=True,
        )

        for item in files:
            filename = item["filename"]
            direct_url = item["direct_url"]

            print(
                f"Sending to Telegram: {filename}",
                flush=True,
            )

            result = await send_video(
                session,
                chat_id,
                direct_url,
                filename,
            )

            if result.get("ok"):
                print(
                    f"Video sent successfully: {filename}",
                    flush=True,
                )
            else:
                print(
                    f"Telegram video error: {result}",
                    flush=True,
                )

                await send_message(
                    session,
                    chat_id,
                    "❌ Telegram could not send this video.\n\n"
                    "The video may be larger than 50 MB "
                    "or the TeraBox link may have expired.",
                )

    except Exception as error:
        print(
            f"Extraction error: {error}",
            flush=True,
        )

        await send_message(
            session,
            chat_id,
            "❌ Extraction failed.\n\n"
            f"{error}",
        )


async def telegram_polling():
    if not TELEGRAM_BOT_TOKEN:
        print(
            "❌ TELEGRAM_BOT_TOKEN is missing",
            flush=True,
        )
        return

    print(
        "✅ Telegram bot starting...",
        flush=True,
    )

    async with aiohttp.ClientSession() as session:

        try:
            webhook_result = await telegram_request(
                session,
                "deleteWebhook",
                {
                    "drop_pending_updates": "false",
                },
            )

            print(
                f"Webhook removed: {webhook_result}",
                flush=True,
            )

        except Exception as error:
            print(
                f"Webhook cleanup error: {error}",
                flush=True,
            )

        offset = 0

        print(
            "✅ Telegram polling started",
            flush=True,
        )

        while True:
            try:
                result = await telegram_request(
                    session,
                    "getUpdates",
                    {
                        "offset": str(offset),
                        "timeout": "30",
                        "allowed_updates": '["message"]',
                    },
                )

                if not result.get("ok"):
                    print(
                        f"Telegram getUpdates error: {result}",
                        flush=True,
                    )

                    await asyncio.sleep(5)
                    continue

                updates = result.get(
                    "result",
                    [],
                )

                if updates:
                    print(
                        f"Telegram updates received: {len(updates)}",
                        flush=True,
                    )

                for update in updates:
                    offset = (
                        update["update_id"] + 1
                    )

                    message = update.get(
                        "message"
                    )

                    if message:
                        try:
                            await process_telegram_message(
                                session,
                                message,
                            )

                        except Exception as error:
                            print(
                                f"Message processing error: {error}",
                                flush=True,
                            )

            except asyncio.CancelledError:
                print(
                    "Telegram polling stopped",
                    flush=True,
                )
                raise

            except Exception as error:
                print(
                    f"Telegram polling error: {error}",
                    flush=True,
                )

                await asyncio.sleep(5)


telegram_task = None


@app.on_event("startup")
async def startup_event():
    global telegram_task

    print(
        "🚀 FastAPI startup complete",
        flush=True,
    )

    telegram_task = asyncio.create_task(
        telegram_polling()
    )


@app.on_event("shutdown")
async def shutdown_event():
    global telegram_task

    if telegram_task:
        telegram_task.cancel()

        try:
            await telegram_task
        except asyncio.CancelledError:
            pass