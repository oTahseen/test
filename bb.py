import asyncio
import os
import traceback
from pathlib import Path
from typing import List, Optional

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from gemini_webapi import GeminiClient, GeneratedImage, WebImage

from utils.misc import modules_help, prefix
from utils.db import db

TEMP_IMAGE_DIR = Path("./temp_gemini_images")
TEMP_FILE_DIR = Path("./temp_gemini_files")
GEMINI_COOKIE_DIR = Path("./gemini_cookies")
DEFAULT_MODEL = "gemini-2.5-flash"

TEMP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
TEMP_FILE_DIR.mkdir(parents=True, exist_ok=True)
GEMINI_COOKIE_DIR.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("GEMINI_COOKIE_PATH", str(GEMINI_COOKIE_DIR))

def _safe_remove(p: Path):
    try:
        if p and p.exists():
            p.unlink()
    except Exception:
        pass

async def get_client():
    cookie = db.get("custom.gemini", "cookie", None)
    if not cookie:
        raise ValueError("No Gemini cookies configured. Use set_gemini command.")

    parts = cookie.strip().split("|")
    if len(parts) != 2:
        raise ValueError("Invalid cookie format. Use: __Secure-1PSID|__Secure-1PSIDTS")

    psid, psidts = parts

    if not psid or not psidts:
        raise ValueError("Invalid cookie values. Both parts must be non-empty.")

    os.environ["GEMINI_COOKIE_PATH"] = str(GEMINI_COOKIE_DIR)
    GEMINI_COOKIE_DIR.mkdir(parents=True, exist_ok=True)

    client = GeminiClient(secure_1psid=psid, secure_1psidts=psidts)

    await client.init(
        timeout=30,
        auto_close=False,
        close_delay=300,
        auto_refresh=True,
    )

    try:
        new_psidts = client.cookies.get("__Secure-1PSIDTS")
        if new_psidts and new_psidts != psidts:
            db.set("custom.gemini", "cookie", f"{psid}|{new_psidts}")
    except Exception:
        pass

    return client

async def _download_replied_media(message: Message) -> List[Path]:
    files: List[Path] = []
    replied = message.reply_to_message
    if not replied:
        return files

    for attr in ("document", "audio", "video", "voice", "video_note"):
        media_obj = getattr(replied, attr, None)
        if media_obj:
            filename = getattr(media_obj, "file_name", None)
            ext_map = {
                "voice": ".ogg",
                "video_note": ".mp4",
                "video": ".mp4",
                "audio": ".mp3",
                "document": None,
            }
            ext = None
            if filename:
                ext = Path(filename).suffix or None
            if not ext:
                ext = ext_map.get(attr, ".bin")
            dest = TEMP_FILE_DIR / f"{media_obj.file_unique_id}{ext}"
            await replied.download(file_name=str(dest))
            files.append(dest)
            break

    if replied.photo and not files:
        dest = TEMP_FILE_DIR / f"{replied.photo.file_unique_id}.jpg"
        await replied.download(file_name=str(dest))
        files.append(dest)

    return files

async def _save_generated_image(image: GeneratedImage, index: int) -> Optional[Path]:
    filename = f"gemini_gen_{index}.png"
    filepath = TEMP_IMAGE_DIR / filename
    try:
        await image.save(path=str(TEMP_IMAGE_DIR), filename=filename, verbose=True)
        if filepath.exists():
            return filepath
    except Exception:
        return None
    return None

@Client.on_message(filters.command(["set_gemini"], prefix))
async def set_gemini(_, message: Message):
    is_self = message.from_user and message.from_user.is_self
    if len(message.command) < 2:
        usage = "<b>Usage:</b> <code>set_gemini __Secure-1PSID|__Secure-1PSIDTS</code>"
        return await (message.edit(usage) if is_self else message.reply(usage))

    cookies = message.text.split(maxsplit=1)[1].strip()
    if "|" not in cookies or len(cookies.split("|")) != 2:
        return await (message.edit("❌ Invalid format. Use: __Secure-1PSID|__Secure-1PSIDTS") if is_self else message.reply("❌ Invalid format. Use: __Secure-1PSID|__Secure-1PSIDTS"))

    db.set("custom.gemini", "cookie", cookies)
    await (message.edit if is_self else message.reply)("✅ Gemini cookies set successfully.")

@Client.on_message(filters.command(["gemini", "ai"], prefix))
async def gemini_query(app: Client, message: Message):
    is_self = message.from_user and message.from_user.is_self

    if len(message.command) < 2:
        usage = "<b>Usage:</b> <code>gemini [prompt]</code>"
        return await (message.edit(usage) if is_self else message.reply(usage))

    prompt = " ".join(message.command[1:]).strip()
    if not prompt:
        return await (message.edit("❌ Prompt cannot be empty.") if is_self else message.reply("❌ Prompt cannot be empty."))

    wait_msg = await (message.edit if is_self else message.reply)("<code>Thinking...</code>")

    downloaded_files: List[Path] = []
    generated_image_paths: List[Path] = []

    try:
        downloaded_files = await _download_replied_media(message)

        client = await get_client()

        metadata = db.get("custom.gemini", "chat_metadata", None)

        chat_kwargs = {"model": DEFAULT_MODEL}
        if metadata:
            try:
                chat = client.start_chat(metadata=metadata, **chat_kwargs)
            except Exception:
                chat = client.start_chat(**chat_kwargs)
                db.delete("custom.gemini", "chat_metadata")
        else:
            chat = client.start_chat(**chat_kwargs)

        files_arg = [str(p) for p in downloaded_files] if downloaded_files else None
        response = await chat.send_message(prompt, files=files_arg if files_arg else None)

        if getattr(chat, "metadata", None):
            db.set("custom.gemini", "chat_metadata", chat.metadata)

        response_text = response.text or "❌ No answer found."

        final_text = f"**Question:**\n{prompt}\n\n**Answer:**\n{response_text}"
        await wait_msg.edit(final_text, parse_mode=enums.ParseMode.MARKDOWN)

        if getattr(response, "images", None):
            for i, image in enumerate(response.images):
                try:
                    if isinstance(image, GeneratedImage):
                        saved = await _save_generated_image(image, i)
                        if saved and saved.exists():
                            await app.send_photo(chat_id=message.chat.id, photo=str(saved), reply_to_message_id=message.id)
                            generated_image_paths.append(saved)
                        else:
                            if getattr(image, "url", None):
                                await app.send_photo(chat_id=message.chat.id, photo=image.url, reply_to_message_id=message.id)
                    elif isinstance(image, WebImage):
                        if getattr(image, "url", None):
                            await app.send_photo(chat_id=message.chat.id, photo=image.url, reply_to_message_id=message.id)
                except Exception:
                    await app.send_message(chat_id=message.chat.id, text=f"⚠️ Failed to send one image.", reply_to_message_id=message.id)

    except ValueError as e:
        await wait_msg.edit(f"❌ {e}")
    except Exception:
        await wait_msg.edit("❌ Gemini encountered an error. Please try again or re-set cookies with set_gemini.")
    finally:
        for p in downloaded_files:
            _safe_remove(p)
        for p in generated_image_paths:
            _safe_remove(p)

modules_help["gemini"] = {
    "gemini [prompt]*": "Ask anything from Gemini AI. Supports memory, images, videos, voice, PDFs, etc.",
    "set_gemini [__Secure-1PSID|__Secure-1PSIDTS]*": "Set Gemini cookies. Use '|' to separate values."
}
