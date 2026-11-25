"""Parse Telegram media updates and download attachments within the Bot API size limit."""
from dataclasses import dataclass
import mimetypes
from urllib.parse import quote

import httpx

MAX_MEDIA_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class TelegramMedia:
    input_type: str
    file_id: str
    mime_type: str
    file_name: str
    size: int | None = None
    unique_id: str = ""
    duration: int | None = None

    def metadata(self, message: dict) -> dict:
        return {
            "file_id": self.file_id, "file_unique_id": self.unique_id,
            "mime_type": self.mime_type, "filename": self.file_name,
            "file_size": self.size, "duration": self.duration,
            "chat_id": message.get("chat", {}).get("id"),
            "message_id": message.get("message_id"), "media_group_id": message.get("media_group_id"),
            "date": message.get("date"),
        }


def extract_media(message: dict) -> TelegramMedia | None:
    if item := message.get("voice"):
        kind, default_mime = "Voice", "audio/ogg"
    elif item := message.get("audio"):
        kind, default_mime = "Voice", "audio/mpeg"
    elif photos := message.get("photo"):
        item = max(photos, key=lambda photo: (photo.get("width", 0) * photo.get("height", 0),
                                             photo.get("file_size", 0)))
        kind, default_mime = "Image", "image/jpeg"
    elif item := message.get("document"):
        default_mime = mimetypes.guess_type(item.get("file_name") or "")[0] or ""
        mime = item.get("mime_type") or default_mime
        if mime == "application/octet-stream":
            mime = default_mime
            item = {**item, "mime_type": mime}
        if mime == "application/pdf":
            kind = "PDF"
        elif mime.startswith("image/"):
            kind = "Image"
        else:
            return None
    else:
        return None

    mime = (item.get("mime_type") or default_mime).lower().split(";", 1)[0]
    if kind == "Voice" and not mime.startswith("audio/"):
        mime = default_mime
    extension = mimetypes.guess_extension(mime) or ""
    name = (item.get("file_name") or f"telegram-{kind.lower()}-{message.get('message_id', 'file')}{extension}")
    return TelegramMedia(kind, item["file_id"], mime, name, item.get("file_size"),
                         item.get("file_unique_id", ""), item.get("duration"))


async def download_media(client: httpx.AsyncClient, token: str, media: TelegramMedia) -> bytes:
    if media.size and media.size > MAX_MEDIA_BYTES:
        raise ValueError("附件超过 Telegram Bot API 的 20 MB 下载上限")

    response = await client.get(f"https://api.telegram.org/bot{token}/getFile",
                                params={"file_id": media.file_id}, timeout=10.0)
    response.raise_for_status()
    file = response.json().get("result") or {}
    if file.get("file_size", 0) > MAX_MEDIA_BYTES:
        raise ValueError("附件超过 Telegram Bot API 的 20 MB 下载上限")
    file_path = file.get("file_path")
    if not file_path:
        raise ValueError("Telegram 未返回可下载的附件路径")

    data = bytearray()
    async with client.stream("GET", f"https://api.telegram.org/file/bot{token}/{quote(file_path, safe='/')}",
                             timeout=20.0) as download:
        download.raise_for_status()
        if int(download.headers.get("content-length", 0)) > MAX_MEDIA_BYTES:
            raise ValueError("附件超过 Telegram Bot API 的 20 MB 下载上限")
        async for chunk in download.aiter_bytes():
            if len(data) + len(chunk) > MAX_MEDIA_BYTES:
                raise ValueError("附件超过 Telegram Bot API 的 20 MB 下载上限")
            data.extend(chunk)
    if not data:
        raise ValueError("Telegram 返回了一个空附件")
    return bytes(data)
