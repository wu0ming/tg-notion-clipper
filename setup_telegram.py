"""
Description: 从 Telegram 里还没被取走的消息中识别你的用户 ID，写入 .env 的 ALLOWED_USER_ID（白名单），并让 Bot 给你回一条确认
原理：Bot 没设 Webhook 时，Telegram 会把还没被取走的消息保留最多 24 小时，getUpdates 能读到；
      不带 offset 读取不会把消息标记为已取走，所以这一步不会“吃掉”消息
用法：python setup_telegram.py             只有 1 个私聊用户时自动写入
      python setup_telegram.py --user 123   多人时手动指定
Changelog:
- v1.2 (2026-11): 新增
"""
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

from config import env, BASE_DIR

ENV_PATH = BASE_DIR / ".env"
UPDATE_KEYS = ("message", "edited_message", "channel_post", "my_chat_member", "callback_query")


def api(method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}"
    req = urllib.request.Request(url, data=json.dumps(payload or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": f"HTTP {exc.code}"}


def write_env(key: str, value: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8-sig") if ENV_PATH.exists() else ""
    if re.search(rf"^{key}=.*$", text, flags=re.MULTILINE):
        text = re.sub(rf"^{key}=.*$", lambda m: f"{key}={value}", text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def collect_users(updates: list[dict]) -> dict[int, dict]:
    users: dict[int, dict] = {}
    for up in updates:
        for key in UPDATE_KEYS:
            obj = up.get(key)
            if not obj:
                continue
            frm = obj.get("from") or {}
            chat = obj.get("chat") or (obj.get("message") or {}).get("chat") or {}
            uid = frm.get("id")
            if not uid or frm.get("is_bot"):
                continue
            info = users.setdefault(uid, {"username": frm.get("username", ""), "name": frm.get("first_name", ""),
                                          "chats": set(), "count": 0, "last": "", "last_text": ""})
            info["chats"].add((chat.get("id"), chat.get("type")))
            info["count"] += 1
            if obj.get("date"):
                info["last"] = datetime.fromtimestamp(obj["date"]).strftime("%m-%d %H:%M")
            info["last_text"] = (obj.get("text") or obj.get("caption") or "")[:30]
    return users


def main() -> None:
    if not env("TELEGRAM_BOT_TOKEN"):
        sys.exit("请先在 .env 填写 TELEGRAM_BOT_TOKEN")
    me = api("getMe")
    if not me.get("ok"):
        sys.exit(f"Bot Token 无效：{me.get('description')}")
    print(f"Bot：@{me['result']['username']}（{me['result'].get('first_name', '')}）")

    wh = api("getWebhookInfo").get("result", {})
    if wh.get("url"):
        sys.exit(f"Bot 已绑定 Webhook（{wh['url']}），此时无法用 getUpdates 读消息。"
                 "如需重新识别，先运行 python set_webhook.py --delete")

    data = api("getUpdates", {"timeout": 0, "limit": 100})
    if not data.get("ok"):
        sys.exit(f"getUpdates 失败：{data.get('description')}")
    updates = data.get("result", [])
    users = collect_users(updates)
    print(f"读到 {len(updates)} 条未取走的消息，涉及 {len(users)} 个用户：")
    for uid, u in users.items():
        chats = "、".join(f"{t}:{cid}" for cid, t in u["chats"])
        print(f"  用户 ID {uid}  @{u['username'] or '-'}  {u['name']}  {u['count']} 条  最近 {u['last']}  "
              f"会话 {chats}  最近内容「{u['last_text']}」")

    chosen = None
    if "--user" in sys.argv:
        chosen = int(sys.argv[sys.argv.index("--user") + 1])
    else:
        private = [uid for uid, u in users.items() if any(t == "private" for _, t in u["chats"])]
        if len(private) == 1:
            chosen = private[0]
        elif not private:
            sys.exit("没有读到私聊消息：请在 Telegram 里给这个 Bot 发一条任意消息后重试（消息只保留 24 小时）")
        else:
            sys.exit("有多个私聊用户，请用 --user <ID> 指定你自己的 ID")

    old = env("ALLOWED_USER_ID")
    write_env("ALLOWED_USER_ID", str(chosen))
    print(f"已写入 ALLOWED_USER_ID={chosen}" + (f"（原值 {old}）" if old and old != str(chosen) else ""))
    sent = api("sendMessage", {"chat_id": chosen, "text": f"✅ tg-notion-clipper 已把你（ID {chosen}）加入白名单。"
                                                          "部署完成后，把链接或文字发给我就会自动归档到 Notion。"})
    print("确认消息：" + ("已发送" if sent.get("ok") else f"发送失败 {sent.get('description')}"))


if __name__ == "__main__":
    main()
