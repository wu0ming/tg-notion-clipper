"""
Description: 本地轮询模式——不部署云端也能在自己电脑上端到端试跑（Telegram getUpdates 长轮询）
用法：python local_poll.py          （若 Bot 已绑定 Webhook，会提示并退出）
      python local_poll.py --force  （临时删除 Webhook 后轮询；之后需重新执行 set_webhook.py）
Changelog:
- v1.1 (2026-11): 新增
"""
import asyncio
import sys

import httpx

from config import env, missing_keys
from app import process_update


async def main(force: bool) -> None:
    missing = missing_keys()
    if missing:
        sys.exit(f"缺少配置：{', '.join(missing)}（请先填写 .env）")
    api = f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}"
    async with httpx.AsyncClient(timeout=70.0) as client:
        me = (await client.get(f"{api}/getMe")).json()
        if not me.get("ok"):
            sys.exit(f"Telegram Token 无效：{me}")
        info = (await client.get(f"{api}/getWebhookInfo")).json().get("result", {})
        if info.get("url"):
            if not force:
                sys.exit(f"Bot 已绑定 Webhook：{info['url']}\n云端正在接收消息，无需本地轮询。如需本地调试请加 --force。")
            await client.post(f"{api}/deleteWebhook")
            print("已临时删除 Webhook，调试结束后请运行 python set_webhook.py <CloudRun地址> 恢复。")
        print(f"本地轮询已启动：@{me['result']['username']}，在 Telegram 给它发消息即可（Ctrl+C 退出）")
        offset = None
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": '["message","edited_message"]'}
                if offset:
                    params["offset"] = offset
                data = (await client.get(f"{api}/getUpdates", params=params)).json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    text = (update.get("message") or {}).get("text", "")
                    print(f"→ 收到：{text[:60]}")
                    await process_update(update)
                    print("  已处理")
            except (httpx.HTTPError, ValueError) as exc:
                print(f"网络异常，5 秒后重试：{exc}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main("--force" in sys.argv))
    except KeyboardInterrupt:
        print("已退出")
