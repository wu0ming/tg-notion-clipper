"""
Description: 配置加载。本地运行时读取同目录 .env；部署到 Cloud Run 后读取环境变量 / Secret Manager 注入值。
Changelog:
- v1.1 (2026-11): 新增，无第三方依赖的 .env 读取，已存在的环境变量优先
- v2.4 (2026-11): 支持多用户独立 Notion 知识库映射（USER_CONFIGS）及可选 Fernet 凭证解密
"""
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("clipper")


def load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_secret_bundle() -> None:
    """Load optional JSON-packed secrets injected from a single Cloud Run Secret Manager slot."""
    raw = os.environ.get("CLIPPER_SECRETS_JSON", "").strip()
    if not raw:
        return
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning("CLIPPER_SECRETS_JSON 解析失败：%s", type(exc).__name__)
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and value is not None and str(value).strip():
                os.environ.setdefault(key.strip(), str(value).strip())


load_dotenv()
load_secret_bundle()


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def decrypt_secret(value: str) -> str:
    """支持可选 Fernet 加密字段（前缀 enc:，依赖 CLIPPER_MASTER_KEY）。"""
    if not value or not value.startswith("enc:"):
        return value
    master = env("CLIPPER_MASTER_KEY")
    if not master:
        raise RuntimeError("检测到加密凭证，但未配置 CLIPPER_MASTER_KEY")
    from cryptography.fernet import Fernet
    return Fernet(master.encode("utf-8")).decrypt(value[4:].encode("utf-8")).decode("utf-8")


def encrypt_secret(value: str) -> str:
    master = env("CLIPPER_MASTER_KEY")
    if not master or not value:
        return value
    from cryptography.fernet import Fernet
    return "enc:" + Fernet(master.encode("utf-8")).encrypt(value.encode("utf-8")).decode("utf-8")


@dataclass(frozen=True)
class UserContext:
    user_id: int
    archive_db: str
    config_db: str = ""
    inbox_db: str = ""
    notion_token: str = ""
    role: str = "member"
    monthly_budget_usd: float = 0.0
    quota_policy: str = "downgrade"

    def notion_scope(self) -> dict:
        return {
            "tenant_id": self.user_id,
            "archive_db": self.archive_db,
            "config_db": self.config_db,
            "inbox_db": self.inbox_db,
            "notion_token": self.notion_token,
        }


def get_user_configs() -> dict[int, UserContext]:
    raw = env("USER_CONFIGS")
    if not raw and (cfg_file := env("USER_CONFIGS_FILE")):
        path = Path(cfg_file)
        if path.exists():
            raw = path.read_text(encoding="utf-8")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning("USER_CONFIGS 解析失败：%s", type(exc).__name__)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, UserContext] = {}
    for key, val in data.items():
        if not str(key).lstrip("-").isdigit() or not isinstance(val, dict):
            continue
        uid = int(key)
        archive_db = str(val.get("archive_db") or "").strip()
        if not archive_db:
            continue
        raw_token = str(val.get("notion_token") or "").strip()
        token = decrypt_secret(raw_token) if raw_token else env("NOTION_TOKEN")
        out[uid] = UserContext(
            user_id=uid,
            archive_db=archive_db,
            config_db=str(val.get("config_db") or "").strip(),
            inbox_db=str(val.get("inbox_db") or "").strip(),
            notion_token=token,
            role=str(val.get("role") or "member").strip(),
            monthly_budget_usd=float(val.get("monthly_budget_usd") or 0.0),
            quota_policy=str(val.get("quota_policy") or "downgrade").strip(),
        )
    return out


def allowed_user_ids() -> set[int]:
    ids = set()
    for part in env("ALLOWED_USER_ID").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    configs = get_user_configs()
    if configs:
        return (ids & set(configs)) if ids else set(configs)
    return ids


def resolve_user_context(user_id: int | None) -> UserContext | None:
    """根据鉴权用户解析独立的 Notion 上下文。多用户模式下未知用户绝不回退到全局库。"""
    configs = get_user_configs()
    if configs:
        if user_id is None or user_id not in allowed_user_ids():
            return None
        return configs.get(user_id)
    allowed = allowed_user_ids()
    if user_id is not None and user_id not in allowed:
        return None
    budget = float(env("MONTHLY_BUDGET_USD") or 0.0)
    return UserContext(
        user_id=user_id or 0,
        archive_db=env("NOTION_DB_ARCHIVE"),
        config_db=env("NOTION_DB_CONFIG"),
        inbox_db=env("NOTION_DB_INBOX"),
        notion_token=env("NOTION_TOKEN"),
        role="admin",
        monthly_budget_usd=budget,
        quota_policy=env("QUOTA_POLICY") or "downgrade",
    )


REQUIRED_KEYS = ["TELEGRAM_BOT_TOKEN", "ALLOWED_USER_ID", "NOTION_TOKEN"]


def missing_keys(extra: list[str] | None = None) -> list[str]:
    return [k for k in REQUIRED_KEYS + (extra or []) if not env(k)]
