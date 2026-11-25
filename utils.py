"""
Description: 纯函数工具集（不依赖第三方库，可离线自测）：URL 提取与归一化、微信图片代理、Jina 输出解析、Notion 属性与正文块组装
Changelog:
- v1.1 (2026-11): 从方案 v1.0 的 router.py / notion_writer.py 抽出；修正 Telegram 实体 UTF-16 偏移、微信 URL 白名单参数、
  Notion 多选标签不能含逗号等问题
"""
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, quote

# ---------------- URL 提取 ----------------
URL_RE = re.compile(r"https?://[^\s<>\"'，。；！？、（）【】《》「」]+", re.IGNORECASE)
TRAILING_PUNCT = ".,;:!?)]}'\"，。；：！？）】」》"


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Telegram entities 的 offset/length 以 UTF-16 码元计，中文/emoji 混排时不能直接按 Python 下标切"""
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def extract_url(text: str, entities: list | None = None) -> tuple[str | None, str]:
    """返回 (第一个链接, 去掉链接后的用户批注)。优先使用 Telegram 实体（可识别“文字超链接”）"""
    text = text or ""
    for ent in entities or []:
        etype = ent.get("type")
        if etype == "text_link" and ent.get("url"):
            return ent["url"], _clean_note(text)
        if etype == "url":
            raw = _utf16_slice(text, ent.get("offset", 0), ent.get("length", 0))
            if raw:
                return raw, _clean_note(text.replace(raw, " "))
    match = URL_RE.search(text)
    if match:
        raw = match.group(0).rstrip(TRAILING_PUNCT)
        return raw, _clean_note(text.replace(raw, " "))
    return None, _clean_note(text)


def _clean_note(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


# ---------------- URL 归一化（用于去重） ----------------
HOST_ALIASES = {
    "twitter.com": "x.com", "www.twitter.com": "x.com", "mobile.twitter.com": "x.com",
    "www.x.com": "x.com", "mobile.x.com": "x.com", "fxtwitter.com": "x.com",
    "vxtwitter.com": "x.com", "fixupx.com": "x.com", "fixvx.com": "x.com",
    "reddit.com": "www.reddit.com", "old.reddit.com": "www.reddit.com", "new.reddit.com": "www.reddit.com",
    "np.reddit.com": "www.reddit.com", "m.reddit.com": "www.reddit.com",
    "v2ex.com": "www.v2ex.com", "cn.v2ex.com": "www.v2ex.com", "global.v2ex.com": "www.v2ex.com",
    "www.github.com": "github.com",
}
GLOBAL_DROP = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref_src", "spm", "share_source", "from_source"}
DOMAIN_DROP = {
    "x.com": {"s", "t", "ref_url"},
    "www.reddit.com": {"share_id", "context", "rdt"},
}
WECHAT_KEEP = ["__biz", "mid", "idx", "sn"]  # 微信长链接只有这 4 个参数决定文章身份，其余均为会话/追踪参数


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    host = HOST_ALIASES.get(host, host)
    path = parsed.path or "/"

    if host == "x.com":  # 去掉 /photo/1、/analytics 等尾巴，只保留 /用户/status/ID
        m = re.match(r"^(/[^/]+/status/\d+)", path)
        if m:
            path = m.group(1)
    if len(path) > 1:
        path = path.rstrip("/")

    pairs = parse_qsl(parsed.query, keep_blank_values=False)
    if host == "mp.weixin.qq.com":
        kept = {k: v for k, v in pairs if k in WECHAT_KEEP}
        query = urlencode([(k, kept[k]) for k in WECHAT_KEEP if k in kept])
    else:
        drop = GLOBAL_DROP | DOMAIN_DROP.get(host, set())
        query = urlencode([(k, v) for k, v in pairs
                           if not k.lower().startswith("utm_") and k.lower() not in drop])
    fragment = parsed.fragment if host == "github.com" else ""
    return urlunparse(("https", host, path, "", query, fragment))


def detect_source(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host == "x.com":
        return "X"
    if host in {"reddit.com", "redd.it"} or host.endswith(".reddit.com"):
        return "Reddit"
    if host == "v2ex.com" or host.endswith(".v2ex.com"):
        return "V2EX"
    if host == "mp.weixin.qq.com":
        return "WeChat"
    if host in {"github.com", "www.github.com"}:
        return "GitHub"
    return "Web"


# ---------------- 内容处理 ----------------
WECHAT_IMG_RE = re.compile(r"(?<![=%])(https?://mmbiz\.(?:qpic|qlogo)\.cn/[^\s)\"'<>\]]+)")


def rewrite_wechat_images(text: str) -> str:
    """微信图片有 Referer 防盗链，改写为 wsrv.nl 开源图片代理，写入 Notion 后可正常显示"""
    return WECHAT_IMG_RE.sub(lambda m: f"https://wsrv.nl/?url={quote(m.group(1), safe='')}", text)


def parse_jina_output(text: str) -> tuple[str, str]:
    """Jina Reader 返回 'Title: … / URL Source: … / Markdown Content: …'，拆出标题和正文"""
    text = (text or "").strip()
    m = re.search(r"^Title:[ \t]*(.*)$", text, re.MULTILINE)  # 只匹配同一行，标题为空时不能吞掉下一行
    title = m.group(1).strip() if m else ""
    marker = "Markdown Content:"
    idx = text.find(marker)
    body = text[idx + len(marker):].strip() if idx != -1 else text
    return title, body


def chunk_text(text: str, size: int = 1800) -> list[str]:
    """按行优先切块；Notion 单个文本对象上限 2,000 字符，留余量取 1,800"""
    chunks, buf = [], ""
    for line in (text or "").splitlines(keepends=True):
        while len(line) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:size])
            line = line[size:]
        if len(buf) + len(line) > size:
            chunks.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        chunks.append(buf)
    return [c.strip("\n") for c in chunks if c.strip()]


def clean_tags(tags: list[str], limit: int = 4) -> list[str]:
    """Notion 多选选项名不能含英文逗号，且需去重；超长截断"""
    out = []
    for tag in tags or []:
        tag = re.sub(r"[,，]", "/", str(tag)).strip().lstrip("#").strip()[:50]
        if tag and tag not in out:
            out.append(tag)
    return out[:limit]


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------------- Notion 载荷组装 ----------------
def rich_text(content: str, limit: int = 1900) -> list[dict]:
    return [{"type": "text", "text": {"content": truncate(content, limit)}}]


def build_properties(title: str, category: str, tags: list[str], source: str, url: str,
                     summary: str, status: str, created_iso: str) -> dict:
    props = {
        "Name": {"title": rich_text(title or "未命名", 100)},
        "Category": {"select": {"name": category}},
        "Tags": {"multi_select": [{"name": t} for t in clean_tags(tags, limit=5)]},
        "Source": {"select": {"name": source}},
        "Summary": {"rich_text": rich_text(summary)},
        "Status": {"select": {"name": status}},
        "Created": {"date": {"start": created_iso}},
    }
    if url:
        props["URL"] = {"url": url[:2000]}
    return props


def build_children(key_points: list[str], content: str, user_note: str = "", url: str = "",
                   status: str = "PASS", fetch_note: str = "", max_chunks: int = 20) -> list[dict]:
    blocks: list[dict] = []
    if user_note:
        blocks.append({"object": "block", "type": "callout", "callout": {
            "rich_text": rich_text(f"个人批注：{user_note}"), "icon": {"type": "emoji", "emoji": "📝"}}})
    if status != "PASS":
        label = "处理状态" if status == "AI_PENDING" else "抓取状态"
        blocks.append({"object": "block", "type": "callout", "callout": {
            "rich_text": rich_text(f"{label}：{status}" + (f"（{fetch_note}）" if fetch_note else "")),
            "icon": {"type": "emoji", "emoji": "⚠️"}}})
    if url:
        blocks.append({"object": "block", "type": "bookmark", "bookmark": {"url": url[:2000]}})
    blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich_text("核心要点")}})
    for point in key_points or []:
        blocks.append({"object": "block", "type": "bulleted_list_item",
                       "bulleted_list_item": {"rich_text": rich_text(point)}})
    pieces = chunk_text(content or "", 1800)[:max_chunks]
    if pieces:
        blocks.append({"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": rich_text("抓取正文存档（点击展开）"), "is_toggleable": True,
            "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text(p, 2000)}}
                         for p in pieces]}})
    return blocks
