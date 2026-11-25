"""
Description: 多平台 URL 分流抓取器（X/Twitter、V2EX、Reddit、微信公众号、GitHub 和通用网页）
  专用接口 → 通用抓取链 → 通知上层尝试 Gemini 网页取证
Changelog:
- v1.0 (2026-11): 方案原始版本
- v1.1 (2026-11): 短链展开；Reddit 路径拼接 .json；微信 data-src 懒加载修复；X 长文解析；通用网页直连兜底；记录降级原因
- v1.3 (2026-11): 新增 Firecrawl 渠道（免费版每月 1,000 credits），通用网页/微信/Reddit 在 Jina 之后自动尝试
- v1.2 (2026-11): Reddit 增加官方 OAuth 渠道（配置 REDDIT_CLIENT_ID/SECRET 后优先使用，令牌自动缓存与续期）；
  微信改为"直连优先、Jina 兜底"（Jina 对公众号常只返回空壳页），新增删除/违规/验证页识别、图片消息（小绿书）解析、
  发布时间与公众号名提取；拆出单通道函数供 tests/wechat_check.py 分别体检
- v2.0 (2026-11): 新增 Tavily Extract 和 ZenRows 渠道；通用抓取链优先使用月度刷新额度的服务（Tavily → Firecrawl → ZenRows），
  Jina 一次性额度退至第四位；FetchResult 增加 channel 字段追踪实际使用的抓取渠道
"""
import asyncio
import base64
import html
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from config import env
from utils import TRUNCATED_GITHUB_RE, detect_source, normalize_url, parse_jina_output, rewrite_wechat_images, with_web_scheme

DEFAULT_UA = "Mozilla/5.0 (compatible; tg-notion-clipper/2.2; personal knowledge archiver)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
WECHAT_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50(0x1800323b) NetType/WIFI Language/zh_CN")
SHORT_HOSTS = {"t.co", "redd.it", "b23.tv", "bit.ly", "tinyurl.com", "url.cn", "dwz.cn"}
CN_TZ = timezone(timedelta(hours=8))

WECHAT_VERIFY_HINTS = ("环境异常", "完成验证", "去验证", "当前环境", "轻点两下取消")
WECHAT_GONE_HINTS = ("该内容已被发布者删除", "此内容因违规无法查看", "内容已被删除", "此内容被投诉且经审核",
                     "该公众号已迁移", "此帐号已被屏蔽", "此帐号已自主注销", "参数错误")
JINA_SHELL_TITLES = ("Weixin Official Accounts Platform", "微信公众平台")
TIER1 = "Tier1-API"
TIER2 = "Tier2-Chain"
NEEDS_GEMINI_WEB = "NEEDS_GEMINI_WEB"
MIN_CONTENT_LENGTH = 151
MAX_HTML_BYTES = 2 * 1024 * 1024
REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com", "m.reddit.com"}
BLOCK_TITLES = re.compile(
    r"^(?:just a moment\b|attention required\b|access denied\b|forbidden\b|request blocked\b|"
    r"security verification\b|captcha\b|403 forbidden\b|404 not found\b|page not found\b|"
    r"登录验证|安全验证|访问受限|页面不存在)", re.IGNORECASE
)
BLOCK_MARKERS = re.compile(
    r"verifying you are human|verify (?:that )?you are (?:a )?human|checking your browser before accessing|"
    r"enable javascript and cookies to continue|you've been blocked by network security|"
    r"the request could not be satisfied|please complete the security check to access|"
    r"^(?:403 forbidden|404 not found|access denied|page not found|this page (?:isn't|is not) available)\b|"
    r"请完成安全验证|当前环境异常.*完成验证|iframe that are currently hidden",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class FetchResult:
    source_type: str          # X / Reddit / V2EX / WeChat / GitHub / Web
    clean_url: str
    title: str
    content: str
    status: str = "PASS"
    note: str = ""            # 降级原因，写入 Notion 提示块
    channel: str = ""         # 实际使用的抓取渠道（Tavily / Firecrawl / ZenRows / Jina / 直连等）
    fetch_tier: str = ""


class UnsafeURL(ValueError):
    pass


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_reserved


def _checked_url(url: str) -> httpx.URL:
    try:
        parsed = httpx.URL(url)
        host = parsed.raw_host.decode("ascii").lower().rstrip(".")
        if (parsed.scheme not in {"http", "https"} or not host or parsed.userinfo or
                parsed.port not in {None, 80, 443} or "\\" in url or "%" in host):
            raise UnsafeURL("链接需要使用无凭据的公开 HTTP(S) 地址")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if ("." not in host or host.endswith((".localhost", ".local", ".internal", ".home.arpa")) or
                    not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)):
                raise UnsafeURL("不访问内网主机")
        else:
            if not _public_address(host):
                raise UnsafeURL("不访问非公网地址")
        return parsed
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise UnsafeURL("链接格式无效") from exc


async def _pinned_address(url: httpx.URL) -> str:
    loop = asyncio.get_running_loop()
    records = await asyncio.wait_for(loop.getaddrinfo(url.raw_host.decode("ascii"), url.port or 0,
                                                      type=socket.SOCK_STREAM), timeout=5.0)
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses or any(not _public_address(address) for address in addresses):
        raise UnsafeURL("域名解析到了非公网地址")
    return next((addr for addr in addresses if ":" not in addr), addresses[0])


async def _public_get(client: httpx.AsyncClient, url: str, headers: dict, timeout: float,
                      trusted_hosts=(), read_body=True) -> tuple[str, httpx.Response]:
    """每次跳转确认目标为公网地址。直连使用已检查的 IP 并保留原域名用于 TLS 验证。"""
    async with asyncio.timeout(timeout):
        for _ in range(6):
            original = _checked_url(url)
            host = original.raw_host.decode("ascii")
            extensions = {}
            request_url = original.copy_with(fragment=None)
            request_headers = dict(headers)
            if host not in trusted_hosts:
                request_url = request_url.copy_with(host=await _pinned_address(original))
                request_headers.update({"Host": original.netloc.decode("ascii"), "Connection": "close"})
                extensions["sni_hostname"] = host
            async with client.stream("GET", request_url, headers=request_headers, timeout=timeout,
                                     extensions=extensions, follow_redirects=False) as response:
                location = response.headers.get("location")
                if response.is_redirect and location:
                    url = urljoin(url, location)
                    continue
                if not read_body:
                    return url, response
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_HTML_BYTES:
                        raise ValueError("直连页面超过大小限制")
                    body.extend(chunk)
                content_type = response.headers.get("content-type", "text/html")
                return url, httpx.Response(response.status_code, headers={"content-type": content_type},
                                           content=bytes(body), request=response.request)
    raise ValueError("链接跳转次数过多")


def _failure(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _quality_issue(title: str, text: str, min_len: int = MIN_CONTENT_LENGTH) -> str:
    preview = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", "", text[:600]).strip()
    first_line = preview.partition("\n")[0]
    if (title.strip() in JINA_SHELL_TITLES or BLOCK_TITLES.search(title.strip()) or BLOCK_TITLES.search(first_line) or
            BLOCK_MARKERS.search(preview) or any(hint in preview for hint in WECHAT_GONE_HINTS)):
        return "返回了验证、拦截或错误页"
    if len(re.sub(r"\s+", "", text)) < min_len:
        return "正文过短"
    return ""


async def resolve_short_url(client: httpx.AsyncClient, url: str) -> str:
    host = _checked_url(url).host
    is_reddit_share = host in REDDIT_HOSTS and re.search(r"/r/[^/]+/s/", urlparse(url).path)
    if host not in SHORT_HOSTS and not is_reddit_share:
        return url
    try:
        expanded, _ = await _public_get(client, url, {"User-Agent": BROWSER_UA}, 10.0,
                                        trusted_hosts=SHORT_HOSTS | REDDIT_HOSTS, read_body=False)
        return expanded
    except UnsafeURL:
        raise
    except Exception:
        return url


async def fetch_via_jina(client: httpx.AsyncClient, url: str, timeout: float = 30.0) -> tuple[str, str]:
    headers = {"User-Agent": DEFAULT_UA, "X-Return-Format": "markdown"}
    key = env("JINA_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = await client.get(f"https://r.jina.ai/{url}", headers=headers, timeout=timeout)
    resp.raise_for_status()
    return parse_jina_output(resp.text)


async def fetch_via_firecrawl(client: httpx.AsyncClient, url: str, timeout: float = 45.0) -> tuple[str, str]:
    """Firecrawl 免费版：每月刷新 1,000 credits（1 页 = 1 credit），会执行页面 JS，适合 Jina 拿不到的动态页面"""
    key = env("FIRECRAWL_API_KEY")
    if not key:
        raise RuntimeError("未配置 FIRECRAWL_API_KEY")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"url": url, "formats": ["markdown"], "onlyMainContent": True}
    resp = await client.post("https://api.firecrawl.dev/v2/scrape", json=body, headers=headers, timeout=timeout)
    if resp.status_code == 404:  # 兼容旧版接口
        resp = await client.post("https://api.firecrawl.dev/v1/scrape", json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    meta = data.get("metadata") or {}
    return (meta.get("title") or meta.get("ogTitle") or "").strip(), (data.get("markdown") or "").strip()


def _html_to_markdown(html_text: str) -> tuple[str, str]:
    """清洗 HTML 噪声标签并提取 (标题, Markdown 正文)"""
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    main = soup.find("article") or soup.find("main") or soup.body or soup
    return title, md(str(main), heading_style="ATX").strip()


async def fetch_via_tavily(client: httpx.AsyncClient, url: str, timeout: float = 30.0) -> tuple[str, str]:
    """Tavily Extract：每月 1,000 credits（基础模式 5 URL = 1 credit，约 5,000 页/月），返回 Markdown"""
    key = env("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("未配置 TAVILY_API_KEY")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"urls": [url], "extract_depth": "basic", "format": "markdown"}
    resp = await client.post("https://api.tavily.com/extract", json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}
    results = payload.get("results") or []
    if not results:
        failed = payload.get("failed_results") or []
        err = failed[0].get("error", "") if failed else ""
        raise ValueError(f"Tavily 未返回内容{('：' + err) if err else ''}")
    r = results[0]
    text = (r.get("raw_content") or r.get("text") or "").strip()
    title = (r.get("title") or "").strip()
    if not title and text:
        m = re.match(r"^#\s+(.+)$", text, re.MULTILINE)
        if m:
            title = m.group(1).strip()[:120]
    return title, text


async def fetch_via_zenrows(client: httpx.AsyncClient, url: str, timeout: float = 35.0) -> tuple[str, str]:
    """ZenRows：每月 5,000 credits；先试静态抓取（1 credit），若遇反爬或 JS 页再试 js_render=true（5 credits）"""
    key = env("ZENROWS_API_KEY")
    if not key:
        raise RuntimeError("未配置 ZENROWS_API_KEY")
    resp = await client.get("https://api.zenrows.com/v1/", params={"apikey": key, "url": url}, timeout=timeout)
    if resp.status_code in (401, 402, 403):
        resp.raise_for_status()
    if resp.status_code == 200:
        title, text = _html_to_markdown(resp.text)
        if not _quality_issue(title, text):
            return title, text
    elif resp.status_code not in (422, 520):
        resp.raise_for_status()
    # 静态抓取正文过短或触发反爬，开启 JS 渲染重试一次
    resp = await client.get("https://api.zenrows.com/v1/",
                            params={"apikey": key, "url": url, "js_render": "true", "wait": "2000"},
                            timeout=max(timeout, 45.0))
    resp.raise_for_status()
    return _html_to_markdown(resp.text)


# 通用抓取链：优先使用月度刷新额度的服务，Jina 一次性额度退至第四位
_CHAIN_KEY = {"Tavily": "TAVILY_API_KEY", "Firecrawl": "FIRECRAWL_API_KEY", "ZenRows": "ZENROWS_API_KEY"}
_CHAIN_ORDER = [
    ("Tavily", fetch_via_tavily),       # 每月约 5,000 页
    ("Firecrawl", fetch_via_firecrawl), # 每月 1,000 页
    ("ZenRows", fetch_via_zenrows),     # 每月 1,000~5,000 页
    ("Jina", fetch_via_jina),           # 一次性额度，保护性使用
    ("直连", None),                      # 永久可用
]


async def fetch_direct_html(client: httpx.AsyncClient, url: str, ua: str = BROWSER_UA) -> tuple[str, str]:
    _, resp = await _public_get(client, url, {"User-Agent": ua}, 20.0, trusted_hosts={"www.v2ex.com"})
    resp.raise_for_status()
    return _html_to_markdown(resp.text)


async def fetch_generic_chain(client: httpx.AsyncClient, url: str,
                              min_len: int = MIN_CONTENT_LENGTH) -> tuple[str, str, str, list[str]]:
    """通用读取链：Tavily → Firecrawl → ZenRows → Jina → 直连解析；返回 (标题, 正文, 使用的渠道, 失败记录)"""
    notes = []
    _checked_url(url)
    for name, fn in _CHAIN_ORDER:
        key_name = _CHAIN_KEY.get(name)
        if key_name and not env(key_name):
            continue
        actual_fn = fn or fetch_direct_html
        try:
            title, text = await actual_fn(client, url)
            issue = _quality_issue(title, text, min_len)
            if not issue:
                return title, text, name, notes
            notes.append(f"{name}{issue}")
        except UnsafeURL:
            raise
        except Exception as exc:
            notes.append(f"{name}失败：{_failure(exc)}")
    return "", "", "", notes


# ---------------- GitHub ----------------
async def _github_json(client: httpx.AsyncClient, endpoint: str, params: dict | None = None):
    headers = {"User-Agent": DEFAULT_UA, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = env("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = await client.get(f"https://api.github.com{endpoint}", params=params, headers=headers,
                                timeout=12.0, follow_redirects=False)
    response.raise_for_status()
    return response.json()


def _github_file_text(data: dict) -> str:
    if data.get("type") != "file" or data.get("encoding") != "base64":
        raise ValueError("GitHub 未返回可读取的文件内容")
    raw = base64.b64decode(data["content"])
    if b"\x00" in raw:
        raise ValueError("GitHub 文件为二进制")
    return raw.decode("utf-8-sig").strip()


async def _github_repository(client: httpx.AsyncClient, endpoint: str) -> tuple[str, str, str]:
    repo = await _github_json(client, endpoint)
    title = repo["full_name"]
    license_name = (repo.get("license") or {}).get("spdx_id") or "未标注"
    topics = ", ".join(repo.get("topics") or []) or "未标注"
    content = (f"仓库：{title}\n描述：{repo.get('description') or '未提供'}\n"
               f"Stars：{repo.get('stargazers_count', 0)}\nForks：{repo.get('forks_count', 0)}\n"
               f"主要语言：{repo.get('language') or '未标注'}\n主题：{topics}\n许可证：{license_name}")
    readme_note = ""
    try:
        readme = _github_file_text(await _github_json(client, f"{endpoint}/readme"))
        if readme:
            content += f"\n\n## README\n\n{readme}"
    except (httpx.HTTPError, ValueError) as exc:
        readme_note = f"README 未取得（{_failure(exc)}），保留仓库信息"
    return title, content, readme_note


async def _github_tree_or_blob(client: httpx.AsyncClient, endpoint: str, kind: str,
                              parts: list[str]) -> tuple[str, str]:
    if len(parts) < (2 if kind == "blob" else 1):
        raise ValueError("GitHub 链接缺少分支、提交或文件路径")

    # GitHub URL 中分支名可含 '/'。先试最常见的首段 ref，404 时再依次纳入后续段。
    max_ref_segments = min(6, len(parts) - (kind == "blob"))
    for split in range(1, max_ref_segments + 1):
        ref = "/".join(unquote(part) for part in parts[:split])
        path_parts = [unquote(part) for part in parts[split:]]
        if any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in path_parts):
            raise ValueError("GitHub 文件路径无效")
        path = "/".join(path_parts)
        contents_endpoint = f"{endpoint}/contents" + (f"/{quote(path)}" if path else "")
        try:
            data = await _github_json(client, contents_endpoint, {"ref": ref})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404 and split < max_ref_segments:
                continue
            raise
        if kind == "tree" and isinstance(data, list):
            entries = "\n".join(f"- {entry.get('name', '')}{'/' if entry.get('type') == 'dir' else ''}"
                                for entry in data[:60])
            if len(data) > 60:
                entries += "\n- ……（仅显示前 60 项）"
            return f"{path or '根目录'} ({ref})", f"GitHub 目录：{path or '/'}\n分支或提交：{ref}\n{entries}"
        if kind == "blob" and isinstance(data, dict):
            return f"{path} ({ref})", f"GitHub 文件：{path}\n分支或提交：{ref}\n\n{_github_file_text(data)}"
        raise ValueError("GitHub 返回的内容类型与链接不匹配")
    raise ValueError("GitHub 路径未找到")


async def fetch_github(client: httpx.AsyncClient, url: str) -> FetchResult:
    parts = urlparse(url).path.strip("/").split("/")
    if (len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9-]+", parts[0]) or
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*|\.[A-Za-z0-9_-][A-Za-z0-9_.-]*", parts[1])):
        raise ValueError("不是 GitHub 仓库或详情页链接")
    repo = "/".join(parts[:2])
    endpoint = f"/repos/{repo}"
    note = ""
    if len(parts) == 2:
        title, content, note = await _github_repository(client, endpoint)
    elif parts[2] in {"tree", "blob"}:
        title, content = await _github_tree_or_blob(client, endpoint, parts[2], parts[3:])
        title = f"{repo} · {title}"
    elif len(parts) == 4 and parts[2] in {"issues", "pull"} and re.fullmatch(r"[0-9]+", parts[3]):
        kind, number = parts[2:]
        api_kind, label = ("issues", "Issue") if kind == "issues" else ("pulls", "Pull Request")
        item = await _github_json(client, f"{endpoint}/{api_kind}/{number}")
        title = f"{repo} #{number} · {item['title']}"
        labels = ", ".join(entry["name"] for entry in item.get("labels", []))
        state = "merged" if item.get("merged") else item.get("state", "")
        content = (f"{label}：{item['title']}\n状态：{state}\n"
                   f"作者：{(item.get('user') or {}).get('login', '')}\n发布时间：{item.get('created_at', '')}\n"
                   f"标签：{labels or '无'}\n\n{item.get('body') or '（未填写正文）'}")
    else:
        raise ValueError("GitHub 接口未覆盖该详情页")
    return FetchResult("GitHub", url, title, content, "PARTIAL" if note else "PASS", note,
                       channel="GitHub API", fetch_tier=TIER1)


# ---------------- X / Twitter ----------------
def _x_article_text(article: dict) -> str:
    parts = [article.get("title", ""), article.get("preview_text", "")]
    content = article.get("content")
    if isinstance(content, dict):
        for block in content.get("blocks", []) or []:
            if isinstance(block, dict) and block.get("text"):
                parts.append(block["text"])
    elif isinstance(content, str):
        parts.append(content)
    return "\n".join(p for p in parts if p)


async def fetch_x(client: httpx.AsyncClient, url: str) -> FetchResult:
    match = re.match(r"^/([A-Za-z0-9_]+)/status/([0-9]+)(?:/|$)", urlparse(url).path)
    if not match:
        return await fetch_web(client, url, "X", ["非单条推文链接"])
    user, status_id = match.groups()
    resp = await client.get(f"https://api.fxtwitter.com/{user}/status/{status_id}",
                            headers={"User-Agent": DEFAULT_UA}, timeout=15.0)
    resp.raise_for_status()
    tweet = resp.json().get("tweet") or {}
    if not any(tweet.get(field) for field in ("text", "article", "quote", "media")):
        raise ValueError("FxTwitter 未返回推文（可能已删除或受保护）")
    author_obj = tweet.get("author") or {}
    author = f"{author_obj.get('name', user)} (@{author_obj.get('screen_name', user)})"
    parts = [f"作者：{author}", f"发布时间：{tweet.get('created_at', '')}", f"正文：\n{tweet.get('text', '')}"]
    if tweet.get("article"):
        parts.append("X 长文 Article：\n" + _x_article_text(tweet["article"]))
    quote = tweet.get("quote")
    if quote:
        q_author = (quote.get("author") or {}).get("screen_name", "")
        parts.append(f"引用推文 (@{q_author})：\n{quote.get('text', '')}")
    media = tweet.get("media") or {}
    photos = [p.get("url") for p in media.get("photos", []) or [] if p.get("url")]
    if photos:
        parts.append("图片：\n" + "\n".join(photos))
    status, note = "PASS", ""
    if media.get("videos"):
        status, note = "PARTIAL", "含视频，视频内容未解析"
    return FetchResult("X", url, f"X 推文 - {author}", "\n\n".join(parts), status, note,
                       channel="FxTwitter", fetch_tier=TIER1)


# ---------------- V2EX ----------------
async def fetch_v2ex(client: httpx.AsyncClient, url: str) -> FetchResult:
    match = re.match(r"^/t/([0-9]+)(?:/|$)", urlparse(url).path)
    if not match:
        return await fetch_web(client, url, "V2EX", ["非帖子链接"])
    tid = match.group(1)
    headers = {"User-Agent": DEFAULT_UA}
    try:
        t_resp = await client.get(f"https://www.v2ex.com/api/topics/show.json?id={tid}", headers=headers, timeout=15.0)
        t_resp.raise_for_status()
        topics = t_resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return await fetch_web(client, url, "V2EX", [f"V2EX API 失败：{_failure(exc)}"])
    if not topics:
        return FetchResult("V2EX", url, f"V2EX 帖子 #{tid}", "该帖子需要登录权限查看（公开 API 无内容）。",
                           "FALLBACK", "登录可见节点，仅保留链接", channel="V2EX API", fetch_tier=TIER1)
    topic = topics[0]
    replies = []
    try:
        r_resp = await client.get(f"https://www.v2ex.com/api/replies/show.json?topic_id={tid}",
                                  headers=headers, timeout=15.0)
        if r_resp.status_code == 200:
            replies = r_resp.json()[:20]
    except Exception:
        pass
    reply_text = "\n".join(f"- {(r.get('member') or {}).get('username', '匿名')}: {r.get('content', '')}"
                           for r in replies)
    content = (f"节点：{(topic.get('node') or {}).get('title', '')}\n"
               f"作者：{(topic.get('member') or {}).get('username', '')}\n"
               f"正文：\n{topic.get('content', '')}\n\n精选评论（前20条）：\n{reply_text or '（暂无）'}")
    return FetchResult("V2EX", url, topic.get("title", ""), content, "PASS", channel="V2EX API", fetch_tier=TIER1)


# ---------------- Reddit ----------------
_reddit_token = {"value": "", "expires": 0.0}


def reddit_ua() -> str:
    user = env("REDDIT_USERNAME")
    return f"python:tg-notion-clipper:v2.0 (by /u/{user})" if user else "python:tg-notion-clipper:v2.0 (personal use)"


async def reddit_oauth_token(client: httpx.AsyncClient, force: bool = False) -> str:
    """应用级 OAuth（client_credentials）：只读公开内容，无需用户密码；令牌约 1 小时有效，提前 60 秒续期"""
    cid, secret = env("REDDIT_CLIENT_ID"), env("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        return ""
    if not force and _reddit_token["value"] and time.time() < _reddit_token["expires"]:
        return _reddit_token["value"]
    resp = await client.post("https://www.reddit.com/api/v1/access_token", auth=(cid, secret),
                             data={"grant_type": "client_credentials"},
                             headers={"User-Agent": reddit_ua()}, timeout=15.0)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    token = data.get("access_token")
    if resp.status_code != 200 or not token:
        raise PermissionError(f"Reddit OAuth 获取令牌失败：HTTP {resp.status_code} {data.get('error', resp.text[:120])}")
    _reddit_token.update(value=token, expires=time.time() + int(data.get("expires_in", 3600)) - 60)
    return token


def parse_reddit_listing(data: list) -> tuple[str, str]:
    """把 Reddit 帖子 JSON（[帖子, 评论]）整理为 (标题, 正文)；评论按赞数取前 12 条"""
    post = data[0]["data"]["children"][0]["data"]
    comments = [c["data"] for c in data[1]["data"]["children"] if c.get("kind") == "t1"]
    comments.sort(key=lambda c: c.get("score", 0), reverse=True)
    comment_text = "\n---\n".join(f"[{c.get('score', 0)}赞] u/{c.get('author', '')}: {c.get('body', '')}"
                                  for c in comments[:12])
    body = post.get("selftext", "") or post.get("url_overridden_by_dest", "") or "（链接帖，无正文）"
    created = datetime.fromtimestamp(post.get("created_utc", 0), CN_TZ).strftime("%Y-%m-%d %H:%M") \
        if post.get("created_utc") else ""
    content = (f"Subreddit: r/{post.get('subreddit')}\n作者：u/{post.get('author')}\n发布时间：{created}\n"
               f"赞数：{post.get('score', 0)}  评论数：{post.get('num_comments', 0)}\n"
               f"正文：\n{body}\n\n热门评论：\n{comment_text or '（暂无）'}")
    return post.get("title", ""), content


async def _reddit_oauth_fetch(client: httpx.AsyncClient, path: str) -> list | None:
    token = await reddit_oauth_token(client)
    if not token:
        return None
    for attempt in range(2):
        resp = await client.get(f"https://oauth.reddit.com{path}",
                                params={"limit": 30, "raw_json": 1, "sort": "top"},
                                headers={"Authorization": f"bearer {token}", "User-Agent": reddit_ua()},
                                timeout=15.0)
        if resp.status_code == 401 and attempt == 0:  # 令牌过期，强制续期后重试一次
            token = await reddit_oauth_token(client, force=True)
            continue
        resp.raise_for_status()
        return resp.json()
    return None


async def fetch_reddit(client: httpx.AsyncClient, url: str) -> FetchResult:
    path = urlparse(url).path.rstrip("/")
    notes = []
    if "/comments/" in path:
        # 1) 官方 OAuth（已配置凭证时）
        try:
            data = await _reddit_oauth_fetch(client, path)
            if data:
                title, content = parse_reddit_listing(data)
                return FetchResult("Reddit", url, title, content, "PASS", channel="Reddit OAuth", fetch_tier=TIER1)
        except Exception as exc:
            notes.append(f"Reddit OAuth失败：{_failure(exc)}")
        # 2) 公开 JSON（未登录，云端 IP 多半 403）
        try:
            resp = await client.get(f"https://www.reddit.com{path}.json",
                                    params={"limit": 30, "raw_json": 1, "sort": "top"},
                                    headers={"User-Agent": reddit_ua()}, timeout=15.0)
            if resp.status_code == 200:
                title, content = parse_reddit_listing(resp.json())
                return FetchResult("Reddit", url, title, content, "PASS", channel="Reddit JSON", fetch_tier=TIER1)
            notes.append(f"公开 JSON HTTP {resp.status_code}")
        except Exception as exc:
            notes.append(f"公开 JSON 失败：{_failure(exc)}")
    # 3) 通用抓取链兜底
    return await fetch_web(client, url, "Reddit", notes)


# ---------------- 微信公众号 ----------------
def _wechat_meta(page: str, soup: BeautifulSoup) -> dict:
    def js_var(name: str) -> str:
        m = re.search(rf'var\s+{name}\s*=\s*(?:htmlDecode\()?(["\'])(.*?)\1', page, re.DOTALL)
        return html.unescape(m.group(2)).strip() if m else ""

    def meta(prop: str) -> str:
        el = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (el.get("content") or "").strip() if el else ""

    title_el = soup.select_one("#activity-name")
    author_el = soup.select_one("#js_name")
    ct = js_var("ct") or (re.search(r'create_time\s*[:=]\s*["\']?(\d{10})', page) or [None, ""])[1]
    published = datetime.fromtimestamp(int(ct), CN_TZ).strftime("%Y-%m-%d %H:%M") if ct and ct.isdigit() else ""
    return {
        "title": (title_el.get_text(strip=True) if title_el else "") or js_var("msg_title") or meta("og:title"),
        "account": (author_el.get_text(strip=True) if author_el else "") or js_var("nickname"),
        "author": meta("author"),
        "published": published,
        "description": js_var("msg_desc") or meta("og:description") or meta("description"),
    }


def _wechat_header(meta: dict) -> str:
    rows = [("公众号", meta.get("account")), ("作者", meta.get("author")), ("发布时间", meta.get("published"))]
    return "\n".join(f"{k}：{v}" for k, v in rows if v)


def _wechat_fix_code_blocks(soup: BeautifulSoup, content_el) -> None:
    """微信代码块：每行是一个 <code>，旁边还有行号列表；不处理会挤成一行并夹杂 'ounter(line' 残片"""
    for ul in content_el.select("ul.code-snippet__line-index"):
        ul.decompose()
    for pre in content_el.find_all("pre"):
        codes = pre.find_all("code")
        lang = pre.get("data-lang", "")
        if len(codes) <= 1 and not lang:
            continue
        lines = [c.get_text() for c in codes] if codes else [pre.get_text()]
        new_pre = soup.new_tag("pre")
        if lang:
            new_pre["data-lang"] = lang
        new_pre.string = "\n".join(lines)
        pre.replace_with(new_pre)


async def wechat_direct(client: httpx.AsyncClient, url: str) -> FetchResult | None:
    """直连：模拟微信内置浏览器解析 #js_content；None 表示该通道未拿到内容"""
    _, resp = await _public_get(client, url, {"User-Agent": WECHAT_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                                20.0, trusted_hosts={"mp.weixin.qq.com"})
    page = resp.text
    soup = BeautifulSoup(page, "html.parser")
    visible = soup.get_text(" ", strip=True)[:3000]
    gone = next((h for h in WECHAT_GONE_HINTS if h in visible), None)
    if gone:
        return FetchResult("WeChat", url, "微信公众号文章（不可访问）", f"原文提示「{gone}」：{url}", "FALLBACK",
                           f"文章不可访问：{gone}", channel="直连", fetch_tier=TIER1)
    resp.raise_for_status()
    meta = _wechat_meta(page, soup)
    content_el = soup.select_one("#js_content")
    if content_el and len(content_el.get_text(strip=True)) > 50:
        for tag in content_el(["script", "style"]):
            tag.decompose()
        for img in content_el.find_all("img"):
            if img.get("data-src"):
                img["src"] = img["data-src"]
        _wechat_fix_code_blocks(soup, content_el)
        body = md(str(content_el), heading_style="ATX",
                  code_language_callback=lambda el: el.get("data-lang") or None)
        body = re.sub(r"(?m)^[a-z]?(?:c?ounter\(line)+\n?", "", body)  # 兜底清理行号残片（含行首残留的单个字母）
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        text = f"{_wechat_header(meta)}\n\n{body}".strip()
        return FetchResult("WeChat", url, meta["title"] or "微信公众号文章", rewrite_wechat_images(text), "PASS",
                           channel="直连", fetch_tier=TIER1)
    # 图片消息 / 小绿书：没有 #js_content，正文在描述与图片列表里
    pics = list(dict.fromkeys(re.findall(r'(https?://mmbiz\.qpic\.cn/[^"\'\\\s]+)', page)))
    if meta["title"] and (meta["description"] or pics):
        imgs = "\n".join(f"![]({p})" for p in pics[:20])
        text = f"{_wechat_header(meta)}\n\n{meta['description']}\n\n{imgs}".strip()
        return FetchResult("WeChat", url, meta["title"], rewrite_wechat_images(text), "PARTIAL",
                           "图片消息，仅提取文字描述与图片，图中文字未识别", channel="直连", fetch_tier=TIER1)
    if any(h in visible for h in WECHAT_VERIFY_HINTS):
        return None  # 触发验证，交给下一通道
    return None


async def wechat_jina(client: httpx.AsyncClient, url: str) -> FetchResult | None:
    title, text = await fetch_via_jina(client, url)
    if _quality_issue(title, text) or any(h in text[:800] for h in WECHAT_VERIFY_HINTS):
        return None
    return FetchResult("WeChat", url, title, rewrite_wechat_images(text), "PASS", channel="Jina", fetch_tier=TIER2)


async def wechat_firecrawl(client: httpx.AsyncClient, url: str) -> FetchResult | None:
    if not env("FIRECRAWL_API_KEY"):
        return None
    title, text = await fetch_via_firecrawl(client, url)
    if _quality_issue(title, text) or any(h in text[:800] for h in WECHAT_VERIFY_HINTS):
        return None
    return FetchResult("WeChat", url, title, rewrite_wechat_images(text), "PASS", channel="Firecrawl", fetch_tier=TIER2)


async def wechat_tavily(client: httpx.AsyncClient, url: str) -> FetchResult | None:
    if not env("TAVILY_API_KEY"):
        return None
    title, text = await fetch_via_tavily(client, url)
    if _quality_issue(title, text) or any(h in text[:800] for h in WECHAT_VERIFY_HINTS):
        return None
    return FetchResult("WeChat", url, title, rewrite_wechat_images(text), "PASS", channel="Tavily", fetch_tier=TIER2)


async def wechat_zenrows(client: httpx.AsyncClient, url: str) -> FetchResult | None:
    if not env("ZENROWS_API_KEY"):
        return None
    title, text = await fetch_via_zenrows(client, url)
    if _quality_issue(title, text) or any(h in text[:800] for h in WECHAT_VERIFY_HINTS):
        return None
    return FetchResult("WeChat", url, title, rewrite_wechat_images(text), "PASS", channel="ZenRows", fetch_tier=TIER2)


async def fetch_wechat(client: httpx.AsyncClient, url: str) -> FetchResult:
    notes = []
    for name, fn in (("直连", wechat_direct), ("Tavily", wechat_tavily), ("Firecrawl", wechat_firecrawl),
                     ("ZenRows", wechat_zenrows), ("Jina", wechat_jina)):
        if name in _CHAIN_KEY and not env(_CHAIN_KEY[name]):
            continue
        try:
            result = await fn(client, url)
            if result:
                if notes and result.status == "PASS":
                    result.note = "；".join(notes)
                return result
            notes.append(f"{name}未拿到正文")
        except UnsafeURL:
            raise
        except Exception as exc:
            notes.append(f"{name}失败：{_failure(exc)}")
    return FetchResult("WeChat", url, "微信公众号文章", "", NEEDS_GEMINI_WEB, "；".join(notes))


# ---------------- 通用网页 ----------------
async def fetch_web(client: httpx.AsyncClient, url: str, source: str = "Web",
                    prior_notes: list[str] | None = None) -> FetchResult:
    title, text, used, notes = await fetch_generic_chain(client, url)
    notes = (prior_notes or []) + notes
    if not used:
        return FetchResult(source, url, url, "", NEEDS_GEMINI_WEB, "；".join(notes))
    if used == "直连":
        notes.append("直连 HTML 解析，排版可能不完整")
    if source == "Reddit":
        notes.append("Reddit 评论可能不全")
    status = "PARTIAL" if used == "直连" or source in {"X", "V2EX", "Reddit"} else "PASS"
    return FetchResult(source, url, title, text, status, "；".join(notes), channel=used, fetch_tier=TIER2)


FETCHERS = {"X": fetch_x, "V2EX": fetch_v2ex, "Reddit": fetch_reddit, "WeChat": fetch_wechat,
            "GitHub": fetch_github, "Web": fetch_web}


async def smart_fetch(raw_url: str) -> FetchResult:
    source = "Web"
    clean_url = ""
    url = with_web_scheme(raw_url)
    if TRUNCATED_GITHUB_RE.fullmatch(url):
        return FetchResult("GitHub", "", "GitHub 链接被截断", "", "FALLBACK", "已保留原文，请发送完整的仓库链接")
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            expanded = await resolve_short_url(client, url)
            normalized = normalize_url(expanded)
            _checked_url(normalized)
            clean_url = normalized
            source = detect_source(clean_url)
            try:
                return await FETCHERS[source](client, clean_url)
            except UnsafeURL:
                raise
            except Exception as exc:
                if source == "Web":
                    raise
                return await fetch_web(client, clean_url, source, [f"{source} API 失败：{_failure(exc)}"])
    except UnsafeURL as exc:
        return FetchResult(source, "", "链接无法安全访问", "", "FALLBACK", str(exc))
    except Exception as exc:
        status = NEEDS_GEMINI_WEB if clean_url else "FALLBACK"
        return FetchResult(source, clean_url, clean_url or "链接抓取失败", "", status, f"抓取失败：{_failure(exc)}")
