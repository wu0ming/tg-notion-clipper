"""
Description: 多平台 URL 分流抓取器（X/Twitter、V2EX、Reddit、微信公众号、通用网页）
  三级降级：专用接口 → 通用读取器（Tavily / Firecrawl / ZenRows / Jina）→ 直连解析 → 仅保留链接（FALLBACK），任一渠道失败都不中断整条流水线
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
import html
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from config import env
from utils import normalize_url, detect_source, rewrite_wechat_images, parse_jina_output

DEFAULT_UA = "Mozilla/5.0 (compatible; tg-notion-clipper/2.0; personal knowledge archiver)"
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


@dataclass
class FetchResult:
    source_type: str          # X / Reddit / V2EX / WeChat / Web
    clean_url: str
    title: str
    content: str
    status: str = "PASS"      # PASS / PARTIAL / FALLBACK
    note: str = ""            # 降级原因，写入 Notion 提示块
    channel: str = ""         # 实际使用的抓取渠道（Tavily / Firecrawl / ZenRows / Jina / 直连等）


async def resolve_short_url(client: httpx.AsyncClient, url: str) -> str:
    host = urlparse(url).netloc.lower()
    is_reddit_share = "reddit.com" in host and re.search(r"/r/[^/]+/s/", url)
    if host not in SHORT_HOSTS and not is_reddit_share:
        return url
    try:
        resp = await client.get(url, headers={"User-Agent": BROWSER_UA}, timeout=10.0)
        return str(resp.url)
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
        if len(text) >= 200:
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
    resp = await client.get(url, headers={"User-Agent": ua}, timeout=20.0)
    resp.raise_for_status()
    return _html_to_markdown(resp.text)


async def fetch_generic_chain(client: httpx.AsyncClient, url: str, min_len: int = 200) -> tuple[str, str, str, list[str]]:
    """通用读取链：Tavily → Firecrawl → ZenRows → Jina → 直连解析；返回 (标题, 正文, 使用的渠道, 失败记录)"""
    notes = []
    for name, fn in _CHAIN_ORDER:
        key_name = _CHAIN_KEY.get(name)
        if key_name and not env(key_name):
            continue
        actual_fn = fn or fetch_direct_html
        try:
            title, text = await actual_fn(client, url)
            if len(text) >= min_len:
                return title, text, name, notes
            notes.append(f"{name}正文过短")
        except Exception as exc:
            notes.append(f"{name}失败：{type(exc).__name__} {str(exc)[:80]}")
    return "", "", "", notes


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
    match = re.search(r"x\.com/([^/]+)/status/(\d+)", url)
    if not match:
        title, text, used, _ = await fetch_generic_chain(client, url, min_len=50)
        if not used:
            raise ValueError("非单条推文链接且通用抓取失败")
        return FetchResult("X", url, title, text, "PARTIAL", f"非单条推文链接，经 {used} 通用抓取", channel=used)
    user, status_id = match.groups()
    resp = await client.get(f"https://api.fxtwitter.com/{user}/status/{status_id}",
                            headers={"User-Agent": DEFAULT_UA}, timeout=15.0)
    resp.raise_for_status()
    tweet = resp.json().get("tweet") or {}
    if not tweet:
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
    return FetchResult("X", url, f"X 推文 - {author}", "\n\n".join(parts), status, note, channel="FxTwitter")


# ---------------- V2EX ----------------
async def fetch_v2ex(client: httpx.AsyncClient, url: str) -> FetchResult:
    match = re.search(r"v2ex\.com/t/(\d+)", url)
    if not match:
        title, text, used, _ = await fetch_generic_chain(client, url, min_len=50)
        if not used:
            raise ValueError("非帖子链接且通用抓取失败")
        return FetchResult("V2EX", url, title, text, "PARTIAL", f"非帖子链接，经 {used} 通用抓取", channel=used)
    tid = match.group(1)
    headers = {"User-Agent": DEFAULT_UA}
    try:
        t_resp = await client.get(f"https://www.v2ex.com/api/topics/show.json?id={tid}", headers=headers, timeout=15.0)
        t_resp.raise_for_status()
        topics = t_resp.json()
    except Exception as exc:
        title, text, used, notes = await fetch_generic_chain(client, url, min_len=100)
        if not used:
            raise exc
        return FetchResult("V2EX", url, title or f"V2EX 帖子 #{tid}", text, "PARTIAL",
                           f"V2EX API 失败（{type(exc).__name__}），改由 {used} 抓取", channel=used)
    if not topics:
        return FetchResult("V2EX", url, f"V2EX 帖子 #{tid}", "该帖子需要登录权限查看（公开 API 无内容）。",
                           "FALLBACK", "登录可见节点，仅保留链接")
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
    return FetchResult("V2EX", url, topic.get("title", ""), content, "PASS", channel="V2EX API")


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
                return FetchResult("Reddit", url, title, content, "PASS", channel="Reddit OAuth")
        except Exception as exc:
            notes.append(f"OAuth 失败：{str(exc)[:100]}")
        # 2) 公开 JSON（未登录，云端 IP 多半 403）
        try:
            resp = await client.get(f"https://www.reddit.com{path}.json",
                                    params={"limit": 30, "raw_json": 1, "sort": "top"},
                                    headers={"User-Agent": reddit_ua()}, timeout=15.0)
            if resp.status_code == 200:
                title, content = parse_reddit_listing(resp.json())
                return FetchResult("Reddit", url, title, content, "PASS", channel="Reddit JSON")
            notes.append(f"公开 JSON HTTP {resp.status_code}")
        except Exception as exc:
            notes.append(f"公开 JSON 失败：{type(exc).__name__}")
    # 3) 通用抓取链兜底
    title, text, used, more = await fetch_generic_chain(client, url, min_len=600)
    notes += more
    if not used:
        return FetchResult("Reddit", url, "Reddit 帖子", f"Reddit 拦截了全部渠道，请人工打开原文查看：{url}", "FALLBACK",
                           "Reddit 拦截了全部渠道（" + "；".join(notes) + "）")
    if len(text) < 600 or re.search(r"blocked|network security|whoa there", text[:600], re.IGNORECASE):
        return FetchResult("Reddit", url, title or "Reddit 帖子", text, "FALLBACK",
                           "Reddit 拦截了全部渠道，仅保留链接与少量内容（" + "；".join(notes) + "）", channel=used)
    return FetchResult("Reddit", url, title, text, "PARTIAL", f"经 {used} 抓取，评论可能不全（" + "；".join(notes) + "）",
                       channel=used)


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
    resp = await client.get(url, headers={"User-Agent": WECHAT_UA, "Accept-Language": "zh-CN,zh;q=0.9"}, timeout=20.0)
    page = resp.text
    soup = BeautifulSoup(page, "html.parser")
    visible = soup.get_text(" ", strip=True)[:3000]
    gone = next((h for h in WECHAT_GONE_HINTS if h in visible), None)
    if gone:
        return FetchResult("WeChat", url, "微信公众号文章（不可访问）", f"原文提示「{gone}」：{url}", "FALLBACK",
                           f"文章不可访问：{gone}", channel="直连")
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
                           channel="直连")
    # 图片消息 / 小绿书：没有 #js_content，正文在描述与图片列表里
    pics = list(dict.fromkeys(re.findall(r'(https?://mmbiz\.qpic\.cn/[^"\'\\\s]+)', page)))
    if meta["title"] and (meta["description"] or pics):
        imgs = "\n".join(f"![]({p})" for p in pics[:20])
        text = f"{_wechat_header(meta)}\n\n{meta['description']}\n\n{imgs}".strip()
        return FetchResult("WeChat", url, meta["title"], rewrite_wechat_images(text), "PARTIAL",
                           "图片消息，仅提取文字描述与图片，图中文字未识别", channel="直连")
    if any(h in visible for h in WECHAT_VERIFY_HINTS):
        return None  # 触发验证，交给下一通道
    return None


async def wechat_jina(client: httpx.AsyncClient, url: str) -> FetchResult | None:
    title, text = await fetch_via_jina(client, url)
    shell = title in JINA_SHELL_TITLES or "iframe that are currently hidden" in text[:400]
    if shell or len(text) < 300 or any(h in text[:800] for h in WECHAT_VERIFY_HINTS + WECHAT_GONE_HINTS):
        return None
    return FetchResult("WeChat", url, title, rewrite_wechat_images(text), "PASS", channel="Jina")


async def wechat_firecrawl(client: httpx.AsyncClient, url: str) -> FetchResult | None:
    if not env("FIRECRAWL_API_KEY"):
        return None
    title, text = await fetch_via_firecrawl(client, url)
    if len(text) < 300 or any(h in text[:800] for h in WECHAT_VERIFY_HINTS + WECHAT_GONE_HINTS):
        return None
    return FetchResult("WeChat", url, title, rewrite_wechat_images(text), "PASS", channel="Firecrawl")


async def wechat_tavily(client: httpx.AsyncClient, url: str) -> FetchResult | None:
    if not env("TAVILY_API_KEY"):
        return None
    title, text = await fetch_via_tavily(client, url)
    if len(text) < 300 or any(h in text[:800] for h in WECHAT_VERIFY_HINTS + WECHAT_GONE_HINTS):
        return None
    return FetchResult("WeChat", url, title, rewrite_wechat_images(text), "PASS", channel="Tavily")


async def wechat_zenrows(client: httpx.AsyncClient, url: str) -> FetchResult | None:
    if not env("ZENROWS_API_KEY"):
        return None
    title, text = await fetch_via_zenrows(client, url)
    if len(text) < 300 or any(h in text[:800] for h in WECHAT_VERIFY_HINTS + WECHAT_GONE_HINTS):
        return None
    return FetchResult("WeChat", url, title, rewrite_wechat_images(text), "PASS", channel="ZenRows")


async def fetch_wechat(client: httpx.AsyncClient, url: str) -> FetchResult:
    notes = []
    for name, fn in (("直连", wechat_direct), ("Tavily", wechat_tavily), ("Firecrawl", wechat_firecrawl),
                     ("Jina", wechat_jina), ("ZenRows", wechat_zenrows)):
        try:
            result = await fn(client, url)
            if result:
                if notes and result.status == "PASS":
                    result.note = "；".join(notes)
                return result
            notes.append(f"{name}未拿到正文")
        except Exception as exc:
            notes.append(f"{name}失败：{type(exc).__name__} {str(exc)[:80]}")
    return FetchResult("WeChat", url, "微信公众号文章（触发验证）",
                       f"抓取触发微信验证或页面为空，请人工打开原文查看：{url}", "FALLBACK", "；".join(notes))


# ---------------- 通用网页 ----------------
async def fetch_web(client: httpx.AsyncClient, url: str) -> FetchResult:
    title, text, used, notes = await fetch_generic_chain(client, url)
    if not used:
        raise ValueError("正文过短或为空（可能是强 JS 渲染页面）：" + "；".join(notes))
    if used == "直连":
        return FetchResult("Web", url, title, text, "PARTIAL",
                           "通用读取器不可用，直连 HTML 解析，排版可能不完整（" + "；".join(notes) + "）", channel="直连")
    return FetchResult("Web", url, title, text, "PASS",
                       ("经 " + used + " 抓取（" + "；".join(notes) + "）") if notes else "", channel=used)


FETCHERS = {"X": fetch_x, "V2EX": fetch_v2ex, "Reddit": fetch_reddit, "WeChat": fetch_wechat, "Web": fetch_web}


async def smart_fetch(raw_url: str) -> FetchResult:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        expanded = await resolve_short_url(client, raw_url.strip())
        clean_url = normalize_url(expanded)
        source = detect_source(clean_url)
        try:
            return await FETCHERS[source](client, clean_url)
        except Exception as exc:
            return FetchResult(source, clean_url, clean_url,
                               f"自动抓取失败，已保留原文链接待后续查看：{clean_url}",
                               "FALLBACK", f"{type(exc).__name__}: {str(exc)[:200]}")
