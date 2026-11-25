"""
Description: 配置加载。本地运行时读取同目录 .env；部署到 Cloud Run 后读取环境变量 / Secret Manager 注入值。
Changelog:
- v1.1 (2026-11): 新增，无第三方依赖的 .env 读取，已存在的环境变量优先
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def allowed_user_ids() -> set[int]:
    ids = set()
    for part in env("ALLOWED_USER_ID").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


REQUIRED_KEYS = ["TELEGRAM_BOT_TOKEN", "ALLOWED_USER_ID", "NOTION_TOKEN"]


def missing_keys(extra: list[str] | None = None) -> list[str]:
    return [k for k in REQUIRED_KEYS + (extra or []) if not env(k)]
