"""Separate Telegram forwarding metadata from the identity of the Bot sender."""
import re


def forward_source(message: dict) -> dict:
    origin = message.get("forward_origin")
    if not origin:
        if not any(message.get(key) for key in (
            "forward_from", "forward_from_chat", "forward_sender_name", "forward_date",
            "forward_from_message_id", "forward_signature", "is_automatic_forward",
        )):
            return {}
        chat = message.get("forward_from_chat") or {}
        if chat:
            kind = "channel" if chat.get("type") == "channel" else "chat"
        elif message.get("forward_from"):
            kind = "user"
        else:
            kind = "hidden_user" if message.get("forward_sender_name") else "unknown"
        origin = {
            "type": kind, "chat": chat, "sender_user": message.get("forward_from"),
            "sender_user_name": message.get("forward_sender_name"),
            "message_id": message.get("forward_from_message_id"),
            "author_signature": message.get("forward_signature"), "date": message.get("forward_date"),
        }

    chat = origin.get("chat") or origin.get("sender_chat") or {}
    author = origin.get("sender_user") or {}
    name = " ".join(filter(None, (author.get("first_name"), author.get("last_name"))))
    username = chat.get("username") or ""
    message_id = origin.get("message_id")
    kind = origin.get("type", "unknown")
    source = {
        "来源类型": {"user": "用户", "hidden_user": "隐藏作者", "chat": "聊天",
                 "channel": "频道"}.get(kind, "未知"),
        "作者": name or origin.get("sender_user_name") or origin.get("author_signature"),
        "聊天或频道": chat.get("title"), "原消息时间戳": origin.get("date"), "原消息编号": message_id,
    }
    if kind == "channel" and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username) and type(message_id) is int and message_id > 0:
        source["链接"] = f"https://t.me/{username}/{message_id}"
    return {key: value for key, value in source.items() if value}


def explicit_note(text: str) -> str | None:
    match = re.match(r"^批注\s*[:：]\s*(.*)$", text.strip(), re.DOTALL)
    return match.group(1).strip() if match else None
