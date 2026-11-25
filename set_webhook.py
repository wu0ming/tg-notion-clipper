"""
Description: 绑定 / 检查 Telegram Webhook（替代方案中的 curl 命令；Windows PowerShell 的 curl 是别名，直接照抄会失败）
用法：python set_webhook.py https://tg-notion-clipper-xxxx.us-central1.run.app   绑定
      python set_webhook.py --info                                             查看当前状态
      python set_webhook.py --delete                                           解绑
Changelog:
- v1.1 (2026-11): 新增，仅用标准库
"""
import json
import sys
import urllib.request

from config import env


def call(method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    if not env("TELEGRAM_BOT_TOKEN"):
        sys.exit("请先在 .env 填写 TELEGRAM_BOT_TOKEN")
    arg = sys.argv[1] if len(sys.argv) > 1 else "--info"
    if arg == "--info":
        print(json.dumps(call("getWebhookInfo"), ensure_ascii=False, indent=2))
    elif arg == "--delete":
        print(call("deleteWebhook", {"drop_pending_updates": True}))
    else:
        if not env("WEBHOOK_SECRET"):
            sys.exit("请先在 .env 填写 WEBHOOK_SECRET（deploy.ps1 会自动生成）")
        base = arg.rstrip("/")
        url = base if base.endswith("/webhook") else base + "/webhook"
        result = call("setWebhook", {"url": url, "secret_token": env("WEBHOOK_SECRET"),
                                     "drop_pending_updates": True,
                                     "allowed_updates": ["message", "edited_message"],
                                     "max_connections": 5})
        print(result)
        print(json.dumps(call("getWebhookInfo"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
