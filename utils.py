"""
Description: 纯函数工具集（不依赖第三方库，可离线自测）：URL 提取与归一化、项目身份识别、微信图片代理、
  Jina 输出解析、Notion 属性与差异化正文块组装
Changelog:
- v1.1 (2026-11): 从方案 v1.0 的 router.py / notion_writer.py 抽出；修正 Telegram 实体 UTF-16 偏移、微信 URL 白名单参数、
  Notion 多选标签不能含逗号等问题
- v2.4 (2026-11): 增加 canonical_video_id、extract_project_key、url_fingerprint 与六类实体差异化 Notion 模板渲染
"""
import hashlib
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, quote

# ---------------- URL 提取 ----------------
URL_RE = re.compile(r"https?://[^\s<>\"'，。；！？、（）【】《》「」]+", re.IGNORECASE)
TRAILING_PUNCT = ".,;:!?)]}'\"，。；：！？）】」》"
GITHUB_LINK_RE = re.compile(
    r"(?<![A-Za-z0-9./@_-])(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9-]+/"
    r"[^\s<>\"'，。；！？、（）【】《》「」]+", re.IGNORECASE,
)
TRUNCATED_GITHUB_RE = re.compile(
    r"((?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9-]+/)([A-Za-z0-9_.-]+?)(?:…|\.{3})$", re.IGNORECASE,
)
BARE_WEB_RE = re.compile(r"^(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+(?:[/:?#]|$)")

ENTITY_TYPES = (
    "技术开源",
    "行业研报/政策法规",
    "深度长文/博客",
    "视频教程/访谈",
    "社媒观点",
    "薅羊毛/优惠/工具书签",
    "通用",
)

ENTITY_TEMPLATE_FIELDS: dict[str, tuple[str, ...]] = {
    "技术开源": ("定位", "特性与场景", "技术栈与许可证", "部署与排查", "局限与注意"),
    "行业研报/政策法规": ("发布机构与时间", "适用主体与范围", "核心义务与指标", "业务影响", "原文条款参考"),
    "深度长文/博客": ("TL;DR", "论证大纲", "数据与案例", "方法论与行动项"),
    "视频教程/访谈": ("核心结论", "可点击时间轴", "工具与资源", "实操要点"),
    "社媒观点": ("原始观点", "背景脉络", "争议点", "关联主题"),
    "薅羊毛/优惠/工具书签": ("权益与价格", "有效期", "领取路径", "限制与风险"),
}


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Telegram entities 的 offset/length 以 UTF-16 码元计，中文/emoji 混排时不能直接按 Python 下标切"""
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def with_web_scheme(url: str) -> str:
    """Telegram 的 url 实体可能只有域名和路径。保留其他协议给路由校验。"""
    url = url.strip()
    return f"https://{url}" if BARE_WEB_RE.match(url) else url


def _find_link(text: str) -> re.Match | None:
    return min((match for pattern in (URL_RE, GITHUB_LINK_RE) if (match := pattern.search(text))),
               key=lambda match: match.start(), default=None)


def _extract_visible_url(raw: str, note: str, next_char: str = "") -> tuple[str, str]:
    match = _find_link(raw)
    visible = match.group(0) if match else raw.strip()
    url = visible if TRUNCATED_GITHUB_RE.fullmatch(visible) else visible.rstrip(TRAILING_PUNCT)
    if next_char == "…" and TRUNCATED_GITHUB_RE.fullmatch(url + next_char):
        url += next_char
    if truncated := TRUNCATED_GITHUB_RE.fullmatch(url):
        root, prefix = truncated.groups()
        # 只依据同一条消息里完整而唯一的项目名补全。GitHub API 会继续校验该仓库。
        names = {token.rstrip(".").lower() for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", note)}
        matches = {name for name in names if name.startswith(prefix.lower()) and len(name) > len(prefix)}
        if len(matches) == 1:
            url = root + next(iter(matches))
    return with_web_scheme(url), _clean_note(note)


def extract_all_urls(text: str, entities: list | None = None) -> list[str]:
    """按出现顺序提取文本与 Telegram 实体中的所有 HTTP/GitHub 链接并去重。"""
    text = text or ""
    found: list[str] = []
    for ent in entities or []:
        etype = ent.get("type")
        if etype == "text_link" and ent.get("url"):
            found.append(with_web_scheme(ent["url"]))
        elif etype == "url":
            raw = _utf16_slice(text, ent.get("offset", 0), ent.get("length", 0))
            if raw:
                contained = _find_link(raw)
                found.append(with_web_scheme((contained.group(0) if contained else raw).rstrip(TRAILING_PUNCT)))
    for pattern in (URL_RE, GITHUB_LINK_RE):
        for match in pattern.finditer(text):
            candidate = match.group(0)
            if not TRUNCATED_GITHUB_RE.fullmatch(candidate):
                candidate = candidate.rstrip(TRAILING_PUNCT)
            found.append(with_web_scheme(candidate))
    return list(dict.fromkeys(u for u in found if u))


def extract_url(text: str, entities: list | None = None) -> tuple[str | None, str]:
    """返回 (第一个链接, 去掉链接后的用户批注)。优先使用 Telegram 实体（可识别“文字超链接”）"""
    text = text or ""
    for ent in entities or []:
        etype = ent.get("type")
        if etype == "text_link" and ent.get("url"):
            return with_web_scheme(ent["url"]), _clean_note(text)
        if etype == "url":
            offset, length = ent.get("offset", 0), ent.get("length", 0)
            raw = _utf16_slice(text, offset, length)
            if raw:
                before = _utf16_slice(text, 0, offset)
                after = text[len(before) + len(raw):]
                contained = _find_link(raw)
                if contained:
                    before += raw[:contained.start()]
                    after = raw[contained.end():] + after
                    raw = contained.group(0)
                trailing_marker = "…" if after.startswith("…") and TRUNCATED_GITHUB_RE.fullmatch(raw + "…") else ""
                return _extract_visible_url(raw, before + " " + after[len(trailing_marker):], trailing_marker)
    match = _find_link(text)
    if match:
        return _extract_visible_url(match.group(0), text[:match.start()] + " " + text[match.end():])
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
    "youtube.com": "www.youtube.com", "m.youtube.com": "www.youtube.com",
    "bilibili.com": "www.bilibili.com", "m.bilibili.com": "www.bilibili.com",
}
GLOBAL_DROP = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref_src", "spm", "share_source", "from_source"}
DOMAIN_DROP = {
    "x.com": {"s", "t", "ref_url"},
    "www.reddit.com": {"share_id", "context", "rdt"},
}
VIDEO_LINK_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
                    "bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}
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
    if host == "github.com":
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[1].endswith(".git") and len(parts[1]) > 4:
            path = f"/{parts[0]}/{parts[1][:-4]}"

    pairs = parse_qsl(parsed.query, keep_blank_values=False)
    if host == "mp.weixin.qq.com":
        kept = {k: v for k, v in pairs if k in WECHAT_KEEP}
        query = urlencode([(k, kept[k]) for k in WECHAT_KEEP if k in kept])
    else:
        drop = GLOBAL_DROP | DOMAIN_DROP.get(host, set())
        if host in VIDEO_LINK_HOSTS:
            drop.add("si")
        query = urlencode([(k, v) for k, v in pairs
                           if not k.lower().startswith("utm_") and k.lower() not in drop])
    if host == "github.com":
        parts = [p for p in path.split("/") if p]
        fragment = "" if (len(parts) == 2 and parsed.fragment.lower() == "readme") else parsed.fragment
    elif host in VIDEO_LINK_HOSTS:
        fragment = parsed.fragment
    else:
        fragment = ""
    return urlunparse(("https", host, path, "", query, fragment))


def url_fingerprint(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest() if url else ""


def canonical_video_id(url: str) -> str:
    """提取 YouTube / B站 的稳定视频主键（含 B站分 P），用于缓存与跨形态识别。"""
    if not url:
        return ""
    parsed = urlparse(normalize_url(url))
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host == "youtu.be" and parts:
        return f"youtube:{parts[0]}"
    if host in {"www.youtube.com", "youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            vid = dict(parse_qsl(parsed.query)).get("v", "")
            if vid:
                return f"youtube:{vid}"
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return f"youtube:{parts[1]}"
    if host in {"www.bilibili.com", "bilibili.com", "m.bilibili.com"}:
        if len(parts) >= 2 and parts[0] == "video":
            vid = parts[1]
            p = dict(parse_qsl(parsed.query)).get("p", "1")
            return f"bilibili:{vid}:p{p}"
    return ""


def _github_identity_from_url(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    if (parsed.hostname or "").lower() != "github.com":
        return ""
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return ""
    owner, repo = parts[0].lower(), parts[1].lower()
    if len(parts) == 2:
        return f"github:{owner}/{repo}"
    if len(parts) >= 4 and parts[2] in {"issues", "pull", "discussions"} and parts[3].isdigit():
        return f"github:{owner}/{repo}#{parts[2]}/{parts[3]}"
    if len(parts) >= 4 and parts[2] == "releases":
        return f"github:{owner}/{repo}#releases/{'/'.join(parts[3:])}"
    return ""


def extract_project_key(url: str, text: str = "", source_type: str = "") -> str:
    """提取强关联项目标识（如 github:owner/repo），避免把偶然提及的列表误合为一篇。"""
    if direct := _github_identity_from_url(url):
        return direct
    if source_type in {"GitHub"}:
        return ""
    # 非 GitHub 来源（如 X 推文、随手记、网页介绍单一开源项目）仅当正文中唯一指向一个 GitHub 仓库根地址时才关联
    candidates = extract_all_urls(text)
    gh_ids = []
    for cand in candidates:
        try:
            ident = _github_identity_from_url(cand)
        except ValueError:
            continue
        if ident:
            gh_ids.append(ident)
    unique = list(dict.fromkeys(gh_ids))
    if len(unique) == 1 and "#" not in unique[0]:
        return unique[0]
    return ""


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
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        return "YouTube"
    if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}:
        return "Bilibili"
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
def rich_text(content: str, limit: int = 1900, link_url: str = "") -> list[dict]:
    item: dict = {"type": "text", "text": {"content": truncate(content, limit)}}
    if link_url:
        item["text"]["link"] = {"url": link_url[:2000]}
    return [item]


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


def build_entity_sections(entity_type: str, entity_details: dict | list | None) -> list[dict]:
    """根据 #11 六类实体生成差异化结构化区块；缺失字段显式标注未提及或不编造。"""
    if not entity_type or entity_type not in ENTITY_TEMPLATE_FIELDS:
        return []
    details_map: dict[str, str] = {}
    if isinstance(entity_details, dict):
        for k, v in entity_details.items():
            if str(k).strip() and str(v or "").strip():
                details_map[str(k).strip()] = str(v).strip()
    elif isinstance(entity_details, list):
        for item in entity_details:
            if isinstance(item, dict):
                label = str(item.get("label") or item.get("key") or "").strip()
                value = str(item.get("value") or "").strip()
                if label and value:
                    details_map[label] = value
    if not details_map:
        return []
    blocks: list[dict] = [{"object": "block", "type": "heading_2", "heading_2": {
        "rich_text": rich_text(f"结构化模板 · {entity_type}")}}]
    ordered_keys = list(ENTITY_TEMPLATE_FIELDS[entity_type])
    for extra in details_map:
        if extra not in ordered_keys:
            ordered_keys.append(extra)
    for field_name in ordered_keys:
        val = details_map.get(field_name) or "未提及"
        for idx, piece in enumerate(chunk_text(val, 1800)[:4]):
            prefix = f"【{field_name}】" if idx == 0 else ""
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": rich_text(f"{prefix}{piece}")}})
    return blocks


def format_timestamp(seconds: float | int) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def build_video_step_blocks(steps: list[dict], image_upload_ids: dict[int, str] | None = None) -> list[dict]:
    """渲染 #4 视频图文步骤（带可点击时间戳、讲解、注意事项与真实关键帧）。"""
    if not steps:
        return []
    upload_map = image_upload_ids or {}
    blocks: list[dict] = [{"object": "block", "type": "heading_2", "heading_2": {
        "rich_text": rich_text("视频分步图文解析")}}]
    for idx, step in enumerate(steps, 1):
        start_s = step.get("start_sec")
        end_s = step.get("end_sec")
        ts_label = ""
        if start_s is not None:
            ts_label = format_timestamp(start_s) + (f"-{format_timestamp(end_s)}" if end_s is not None else "")
        heading_text = f"{idx}. {step.get('title') or f'步骤 {idx}'}"
        rt = rich_text(heading_text)
        if ts_label:
            ts_url = step.get("timestamp_url") or ""
            rt = rich_text(f"[{ts_label}] ", link_url=ts_url) + rt
        blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": rt}})
        if explanation := (step.get("explanation") or "").strip():
            for piece in chunk_text(explanation, 1800)[:4]:
                blocks.append({"object": "block", "type": "paragraph",
                               "paragraph": {"rich_text": rich_text(piece)}})
        for cue in step.get("cues") or []:
            if str(cue).strip():
                blocks.append({"object": "block", "type": "bulleted_list_item",
                               "bulleted_list_item": {"rich_text": rich_text(str(cue).strip())}})
        if caution := (step.get("precautions") or "").strip():
            blocks.append({"object": "block", "type": "callout", "callout": {
                "rich_text": rich_text(f"注意事项：{caution}"),
                "icon": {"type": "emoji", "emoji": "⚠️"},
            }})
        frame_idx = step.get("frame_index")
        if frame_idx is not None and frame_idx in upload_map:
            cap = f"真实视频关键帧 · {format_timestamp(step.get('frame_sec', start_s or 0))}"
            blocks.append({"object": "block", "type": "image", "image": {
                "type": "file_upload",
                "file_upload": {"id": upload_map[frame_idx]},
                "caption": rich_text(cap),
            }})
    return blocks


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
