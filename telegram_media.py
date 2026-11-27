"""Parse Telegram media updates, extract optional PDF text layers, and download bounded attachments."""
from dataclasses import dataclass
import io
import mimetypes
from urllib.parse import quote

import httpx

MAX_MEDIA_BYTES = 20 * 1024 * 1024
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


@dataclass(frozen=True)
class TelegramMedia:
    input_type: str
    file_id: str
    mime_type: str
    file_name: str
    size: int | None = None
    unique_id: str = ""
    duration: int | None = None
    width: int | None = None
    height: int | None = None

    def metadata(self, message: dict) -> dict:
        return {
            "file_id": self.file_id, "file_unique_id": self.unique_id,
            "mime_type": self.mime_type, "filename": self.file_name,
            "file_size": self.size, "duration": self.duration,
            "width": self.width, "height": self.height,
            "chat_id": message.get("chat", {}).get("id"),
            "message_id": message.get("message_id"), "media_group_id": message.get("media_group_id"),
            "date": message.get("date"),
        }


def extract_media(message: dict) -> TelegramMedia | None:
    if item := message.get("voice"):
        kind, default_mime = "Voice", "audio/ogg"
    elif item := message.get("audio"):
        kind, default_mime = "Voice", "audio/mpeg"
    elif item := message.get("video"):
        kind, default_mime = "Video", "video/mp4"
    elif item := message.get("video_note"):
        kind, default_mime = "Video", "video/mp4"
    elif photos := message.get("photo"):
        item = max(photos, key=lambda photo: (photo.get("width", 0) * photo.get("height", 0),
                                             photo.get("file_size", 0)))
        kind, default_mime = "Image", "image/jpeg"
    elif item := message.get("document"):
        fname = item.get("file_name") or ""
        default_mime = mimetypes.guess_type(fname)[0] or ""
        mime = item.get("mime_type") or default_mime
        if mime == "application/octet-stream":
            mime = default_mime
            item = {**item, "mime_type": mime}
        ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
        if mime == "application/pdf":
            kind = "PDF"
        elif mime.startswith("image/"):
            kind = "Image"
        elif mime.startswith("video/") or ext in VIDEO_EXTS:
            kind, default_mime = "Video", mime or "video/mp4"
        elif mime.startswith("audio/"):
            kind, default_mime = "Voice", mime
        else:
            return None
    else:
        return None

    mime = (item.get("mime_type") or default_mime).lower().split(";", 1)[0]
    if kind == "Voice" and not mime.startswith("audio/"):
        mime = default_mime
    if kind == "Video" and not mime.startswith("video/"):
        mime = default_mime
    extension = mimetypes.guess_extension(mime) or (".mp4" if kind == "Video" else "")
    name = (item.get("file_name") or f"telegram-{kind.lower()}-{message.get('message_id', 'file')}{extension}")
    return TelegramMedia(
        kind, item["file_id"], mime, name, item.get("file_size"),
        item.get("file_unique_id", ""), item.get("duration"),
        item.get("width"), item.get("height"),
    )


def extract_pdf_text_layer(pdf_bytes: bytes, min_chars: int = 200) -> str:
    """#10 PDF 优先文本层：若已安装 pypdf/pymupdf 且含可读文字，优先送纯文本以降低视觉 Token 消耗。"""
    if not pdf_bytes:
        return ""
    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for idx, page in enumerate(reader.pages[:40], 1):
            txt = (page.extract_text() or "").strip()
            if txt:
                pages.append(f"[Page {idx}]\n{txt}")
        text = "\n\n".join(pages).strip()
        if len(text) >= min_chars:
            return text
    except Exception:
        pass
    try:
        import fitz  # type: ignore
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        for idx, page in enumerate(doc[:40], 1):
            txt = (page.get_text() or "").strip()
            if txt:
                pages.append(f"[Page {idx}]\n{txt}")
        text = "\n\n".join(pages).strip()
        if len(text) >= min_chars:
            return text
    except Exception:
        pass
    return ""


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
