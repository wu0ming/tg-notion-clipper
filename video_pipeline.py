"""Shared VideoAsset pipeline (#2 Telegram video files + #4 instruction-driven YouTube/Bilibili links)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import html
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlparse
import xml.etree.ElementTree as ET

import httpx

from config import env
from utils import build_video_step_blocks, canonical_video_id, format_timestamp

log = logging.getLogger("clipper")

VISUAL_STEPS_RE = re.compile(
    r"动作|拆解|步骤|分步|截图|关键帧|图文|教程|康复|训练|实操|逐帧|设计细节|环节|(\d{1,2}:\d{2})",
    re.IGNORECASE,
)
DEEP_VIDEO_RE = re.compile(r"精读|深挖|逐字|完整分析", re.IGNORECASE)
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
BILIBILI_ID_RE = re.compile(r"(BV[0-9A-Za-z]+|av[0-9]+)")
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class VideoAsset:
    source_type: str
    canonical_id: str
    url: str
    title: str = ""
    author: str = ""
    description: str = ""
    duration_sec: int = 0
    part: int = 1
    chapters: list[dict] = field(default_factory=list)
    transcript_segments: list[dict] = field(default_factory=list)
    access_status: str = "PASS"
    note: str = ""
    keyframes: list[tuple[int, bytes, str]] = field(default_factory=list)
    media_bytes: bytes | None = None
    media_mime: str = ""

    def format_transcript(self, max_chars: int = 12000) -> str:
        lines = [f"[{format_timestamp(seg['start_sec'])}] {seg['text']}"
                 for seg in self.transcript_segments if (seg.get("text") or "").strip()]
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        # 长字幕按时间分段平衡采样，保留开头、中段与结尾关键节点
        step = max(2, len(lines) // 80)
        sampled = [lines[i] for i in range(0, len(lines), step)]
        joined = "\n".join(sampled)
        if len(joined) > max_chars:
            half = max_chars // 2 - 40
            return joined[:half] + "\n…（中段字幕已按时间抽样省略）…\n" + joined[-half:]
        return joined

    def render_archive_text(self) -> str:
        parts = [f"视频标题：{self.title or '未命名视频'}"]
        if self.author:
            parts.append(f"作者/频道：{self.author}")
        if self.duration_sec:
            parts.append(f"时长：{format_timestamp(self.duration_sec)}（{self.duration_sec} 秒）")
        if self.chapters:
            chap_lines = [f"- [{format_timestamp(c.get('start_sec', 0))}] {c.get('title', '')}" for c in self.chapters[:30]]
            parts.append("章节：\n" + "\n".join(chap_lines))
        if self.description:
            parts.append("简介：\n" + self.description[:2000])
        if self.transcript_segments:
            parts.append("带时间戳字幕：\n" + self.format_transcript(max_chars=24000))
        return "\n\n".join(parts)

    def to_cache_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "canonical_id": self.canonical_id,
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "description": self.description,
            "duration_sec": self.duration_sec,
            "part": self.part,
            "chapters": self.chapters,
            "transcript_segments": self.transcript_segments,
            "access_status": self.access_status,
            "note": self.note,
        }

    @classmethod
    def from_cache_dict(cls, data: dict) -> "VideoAsset":
        return cls(**{k: v for k, v in data.items() if k in {
            "source_type", "canonical_id", "url", "title", "author", "description",
            "duration_sec", "part", "chapters", "transcript_segments", "access_status", "note",
        }})


def classify_video_intent(user_note: str = "", *, is_direct_file: bool = False) -> str:
    note = (user_note or "").strip()
    if VISUAL_STEPS_RE.search(note):
        return "visual_steps"
    if is_direct_file or DEEP_VIDEO_RE.search(note):
        return "deep_summary"
    return "standard_summary"


def timestamp_link(url: str, sec: int | float) -> str:
    sec_i = max(0, int(sec))
    cid = canonical_video_id(url)
    if cid.startswith("youtube:"):
        vid = cid.split(":", 1)[1]
        return f"https://youtu.be/{vid}?t={sec_i}"
    if cid.startswith("bilibili:"):
        _, vid, part = cid.split(":", 2)
        p_num = part.lstrip("p")
        q = f"p={p_num}&t={sec_i}" if p_num and p_num != "1" else f"t={sec_i}"
        return f"https://www.bilibili.com/video/{vid}?{q}"
    return url


def _parse_youtube_id(url: str) -> str:
    cid = canonical_video_id(url)
    if cid.startswith("youtube:"):
        vid = cid.split(":", 1)[1]
        return vid if YOUTUBE_ID_RE.match(vid) else ""
    return ""


def _parse_bilibili_id(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    m = BILIBILI_ID_RE.search(parsed.path)
    if not m:
        return "", 1
    vid = m.group(1)
    p_str = dict(parse_qsl(parsed.query)).get("p", "1")
    return vid, int(p_str) if p_str.isdigit() else 1


def _extract_yt_player_json(page_html: str) -> dict:
    marker = "ytInitialPlayerResponse"
    idx = page_html.find(marker)
    if idx == -1:
        return {}
    brace = page_html.find("{", idx)
    if brace == -1:
        return {}
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(page_html, brace)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def _parse_yt_xml_captions(xml_text: str) -> list[dict]:
    segments = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    for node in root.findall(".//text"):
        start = float(node.attrib.get("start", "0") or 0)
        dur = float(node.attrib.get("dur", "0") or 0)
        text = html.unescape("".join(node.itertext())).replace("\n", " ").strip()
        if text:
            segments.append({"start_sec": int(start), "end_sec": int(start + dur), "text": text})
    return segments


def _parse_yt_json3_captions(data: dict) -> list[dict]:
    segments = []
    for ev in data.get("events") or []:
        segs = ev.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if text:
            start = int((ev.get("tStartMs") or 0) / 1000)
            dur = int((ev.get("dDurationMs") or 0) / 1000)
            segments.append({"start_sec": start, "end_sec": start + dur, "text": text})
    return segments


def _pick_caption_track(tracks: list[dict]) -> dict | None:
    if not tracks:
        return None
    prefs = ("zh-Hans", "zh-CN", "zh-Hant", "zh-TW", "zh", "en")
    for pref in prefs:
        for tr in tracks:
            if (tr.get("languageCode") or "").lower().startswith(pref.lower()) and tr.get("kind") != "asr":
                return tr
    for pref in prefs:
        for tr in tracks:
            if (tr.get("languageCode") or "").lower().startswith(pref.lower()):
                return tr
    return tracks[0]


async def _fetch_youtube_asset(client: httpx.AsyncClient, url: str) -> VideoAsset:
    vid = _parse_youtube_id(url)
    cid = f"youtube:{vid}" if vid else url
    asset = VideoAsset(source_type="YouTube", canonical_id=cid, url=url)
    notes = []

    if vid:
        # 1) Optional youtube-transcript-api (zero video download)
        try:
            from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore

            def _get_transcript():
                api = YouTubeTranscriptApi()
                if hasattr(api, "fetch"):
                    fetched = api.fetch(vid, languages=["zh-Hans", "zh-Hant", "zh", "en"])
                    return [{"start_sec": int(item.start), "end_sec": int(item.start + item.duration), "text": item.text}
                            for item in fetched]
                rows = YouTubeTranscriptApi.get_transcript(vid, languages=["zh-Hans", "zh-Hant", "zh", "en"])
                return [{"start_sec": int(r["start"]), "end_sec": int(r["start"] + r.get("duration", 0)), "text": r["text"]}
                        for r in rows]

            asset.transcript_segments = await asyncio.to_thread(_get_transcript)
        except ImportError:
            pass
        except Exception as exc:
            notes.append(f"transcript-api: {type(exc).__name__}")

    # 2) Public oEmbed metadata
    try:
        oe = await client.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=10.0)
        if oe.is_success:
            data = oe.json()
            asset.title = data.get("title") or asset.title
            asset.author = data.get("author_name") or asset.author
    except Exception:
        pass

    # 3) Watch page playerResponse captions + duration/description when needed
    if not asset.transcript_segments or not asset.duration_sec:
        try:
            watch = await client.get(f"https://www.youtube.com/watch?v={vid}" if vid else url,
                                     headers=BROWSER_HEADERS, timeout=12.0, follow_redirects=True)
            if watch.is_success:
                pr = _extract_yt_player_json(watch.text)
                vd = pr.get("videoDetails") or {}
                asset.title = asset.title or vd.get("title", "")
                asset.author = asset.author or vd.get("author", "")
                asset.description = asset.description or vd.get("shortDescription", "")
                if str(vd.get("lengthSeconds", "")).isdigit():
                    asset.duration_sec = int(vd["lengthSeconds"])
                tracks = (((pr.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks") or [])
                if not asset.transcript_segments and (track := _pick_caption_track(tracks)) and (cap_url := track.get("baseUrl")):
                    cap_host = (urlparse(cap_url).hostname or "").lower()
                    if cap_url.startswith("https://") and (cap_host.endswith(".youtube.com") or cap_host == "www.youtube.com" or cap_host.endswith(".googlevideo.com")):
                        cap_resp = await client.get(cap_url + ("&fmt=json3" if "fmt=" not in cap_url else ""),
                                                    headers=BROWSER_HEADERS, timeout=10.0)
                        if cap_resp.is_success:
                            if cap_resp.text.lstrip().startswith("{"):
                                asset.transcript_segments = _parse_yt_json3_captions(cap_resp.json())
                            else:
                                asset.transcript_segments = _parse_yt_xml_captions(cap_resp.text)
        except Exception as exc:
            notes.append(f"watch-meta: {type(exc).__name__}")

    if asset.transcript_segments:
        asset.access_status = "PASS"
    elif asset.title or asset.description:
        asset.access_status = "PARTIAL"
        notes.append("未获取到可用字幕")
    else:
        asset.access_status = "FALLBACK"
        notes.append("无法读取 YouTube 元数据或字幕")
    asset.note = "；".join(dict.fromkeys(notes))
    return asset


async def _fetch_bilibili_asset(client: httpx.AsyncClient, url: str) -> VideoAsset:
    vid, part = _parse_bilibili_id(url)
    cid_key = f"bilibili:{vid}:p{part}" if vid else url
    asset = VideoAsset(source_type="Bilibili", canonical_id=cid_key, url=url, part=part)
    notes = []
    if not vid:
        asset.access_status = "FALLBACK"
        asset.note = "未识别到 B站 BV/av 号"
        return asset

    params = {"bvid": vid} if vid.startswith("BV") else {"aid": vid[2:]}
    headers = {**BROWSER_HEADERS, "Referer": "https://www.bilibili.com/"}
    cid = 0
    try:
        resp = await client.get("https://api.bilibili.com/x/web-interface/view", params=params, headers=headers, timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0 or not payload.get("data"):
            raise ValueError(payload.get("message") or f"code={payload.get('code')}")
        data = payload["data"]
        asset.title = data.get("title") or ""
        asset.author = (data.get("owner") or {}).get("name") or ""
        asset.description = data.get("desc") or ""
        asset.duration_sec = int(data.get("duration") or 0)
        pages = data.get("pages") or []
        if pages:
            idx = min(max(0, part - 1), len(pages) - 1)
            cid = int(pages[idx].get("cid") or data.get("cid") or 0)
            if pages[idx].get("duration"):
                asset.duration_sec = int(pages[idx]["duration"])
            if len(pages) > 1:
                asset.chapters = [{"title": p.get("part") or f"P{i}", "start_sec": 0}
                                  for i, p in enumerate(pages[:30], 1)]
        else:
            cid = int(data.get("cid") or 0)
    except Exception as exc:
        asset.access_status = "FALLBACK"
        asset.note = f"B站信息获取失败：{type(exc).__name__}"
        return asset

    if cid:
        try:
            sub_resp = await client.get(
                "https://api.bilibili.com/x/player/v2",
                params={**params, "cid": cid}, headers=headers, timeout=10.0,
            )
            if sub_resp.is_success:
                subtitles = (((sub_resp.json().get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
                for item in subtitles:
                    sub_url = item.get("subtitle_url") or ""
                    if sub_url.startswith("//"):
                        sub_url = "https:" + sub_url
                    sub_host = (urlparse(sub_url).hostname or "").lower()
                    if sub_url.startswith("https://") and sub_host.endswith((".hdslb.com", ".bilibili.com", ".bilivideo.com")):
                        s_json = await client.get(sub_url, headers=headers, timeout=10.0)
                        if s_json.is_success:
                            body = s_json.json().get("body") or []
                            asset.transcript_segments = [
                                {"start_sec": int(seg.get("from", 0)), "end_sec": int(seg.get("to", 0)),
                                 "text": str(seg.get("content") or "").strip()}
                                for seg in body if str(seg.get("content") or "").strip()
                            ]
                            if asset.transcript_segments:
                                break
        except Exception as exc:
            notes.append(f"字幕未取得：{type(exc).__name__}")

    if asset.transcript_segments:
        asset.access_status = "PASS"
    else:
        asset.access_status = "PARTIAL"
        notes.append("无公开字幕（仅元数据与简介）")
    asset.note = "；".join(dict.fromkeys(notes))
    return asset


async def fetch_video_asset(client: httpx.AsyncClient, url: str, *, use_cache: bool = True) -> VideoAsset:
    """获取 YouTube / B站 VideoAsset，优先使用缓存和 Tier 1 无下载字幕接口。"""
    cid = canonical_video_id(url)
    if use_cache and cid:
        try:
            from clip_store import get_store, store_available
            if store_available() and (cached := get_store().get_video_cache(cid)):
                return VideoAsset.from_cache_dict(cached)
        except Exception:
            pass

    host = (urlparse(url).hostname or "").lower()
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        asset = await _fetch_youtube_asset(client, url)
    else:
        asset = await _fetch_bilibili_asset(client, url)

    if use_cache and cid and asset.access_status in {"PASS", "PARTIAL"}:
        try:
            from clip_store import get_store, store_available
            if store_available():
                get_store().set_video_cache(cid, asset.to_cache_dict())
        except Exception:
            pass
    return asset


async def fetch_video_as_fetch_result(client: httpx.AsyncClient, url: str):
    from router import FetchResult
    asset = await fetch_video_asset(client, url)
    return FetchResult(
        source_type=asset.source_type,
        clean_url=url,
        title=asset.title or url,
        content=asset.render_archive_text(),
        status=asset.access_status,
        note=asset.note,
        channel=f"{asset.source_type} 字幕/API",
        fetch_tier="Video-Pipeline",
    )


def _dedup_frames(frames: list[tuple[int, bytes, str]]) -> list[tuple[int, bytes, str]]:
    seen: set[str] = set()
    out = []
    for sec, data, mime in frames:
        digest = hashlib.sha256(data).hexdigest()
        if digest not in seen:
            seen.add(digest)
            out.append((sec, data, mime))
    return out


def extract_frames_from_file(video_path: Path, target_secs: list[int]) -> list[tuple[int, bytes, str]]:
    """使用 ffmpeg 在真实时间戳截取最多 768px 宽 JPEG 关键帧并做哈希去重。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not video_path.exists():
        return []
    frames: list[tuple[int, bytes, str]] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for idx, sec in enumerate(target_secs):
            out_path = Path(tmpdir) / f"frame_{idx}.jpg"
            cmd = [
                ffmpeg, "-y", "-ss", str(max(0, int(sec))), "-i", str(video_path),
                "-frames:v", "1", "-vf", "scale='min(768,iw)':-2", "-q:v", "4", str(out_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=20)
            if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                frames.append((int(sec), out_path.read_bytes(), "image/jpeg"))
    return _dedup_frames(frames)


def extract_frames_from_bytes(video_bytes: bytes, suffix: str, duration_sec: int | None = None,
                              max_frames: int = 8) -> list[tuple[int, bytes, str]]:
    if not video_bytes or not shutil.which("ffmpeg"):
        return []
    dur = max(10, int(duration_sec or 60))
    step = max(2, dur // max(1, max_frames))
    targets = list(range(1, dur, step))[:max_frames]
    with tempfile.TemporaryDirectory() as tmpdir:
        vid_path = Path(tmpdir) / f"input{suffix or '.mp4'}"
        vid_path.write_bytes(video_bytes)
        return extract_frames_from_file(vid_path, targets)


async def extract_link_keyframes(url: str, target_secs: list[int], duration_sec: int = 0) -> tuple[list[tuple[int, bytes, str]], str]:
    """按需使用 yt-dlp + ffmpeg 下载低清视频并截取真实关键帧；工具缺失或超时时明确降级。"""
    max_dur = int(env("VIDEO_MAX_DURATION_SEC") or 1800)
    if duration_sec and duration_sec > max_dur:
        return [], f"视频时长 {duration_sec} 秒超出关键帧处理上限 {max_dur} 秒"
    yt_dlp = shutil.which("yt-dlp")
    ffmpeg = shutil.which("ffmpeg")
    if not yt_dlp or not ffmpeg:
        return [], "运行环境未安装 yt-dlp 或 ffmpeg，无法截取真实视频关键帧"
    if not target_secs:
        return [], "未规划出目标截图时间点"

    def _run() -> tuple[list[tuple[int, bytes, str]], str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            tpl = str(Path(tmpdir) / "video.%(ext)s")
            cmd = [
                yt_dlp, "--no-playlist", "-f", "best[height<=480]/bestvideo[height<=480]+bestaudio/best",
                "--merge-output-format", "mp4", "--max-filesize", "50M", "-o", tpl, url,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if proc.returncode != 0:
                return [], "yt-dlp 下载受限或失败"
            files = [p for p in Path(tmpdir).iterdir() if p.is_file()]
            if not files:
                return [], "未找到下载后的视频文件"
            frames = extract_frames_from_file(files[0], target_secs)
            return frames, ("" if frames else "ffmpeg 截帧失败")

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        return [], f"关键帧提取异常：{type(exc).__name__}"


async def analyze_instruction_video(client: httpx.AsyncClient, url: str, user_note: str,
                                    categories: list[dict]) -> dict | None:
    """#4 指令驱动视频工作流：先字幕/章节，再按需规划时间戳与真实关键帧截图。"""
    from gemini_processor import AnalysisResult, analyze_video_steps, match_category, plan_video_keyframes

    intent = classify_video_intent(user_note)
    if intent != "visual_steps":
        return None
    asset = await fetch_video_asset(client, url)
    transcript = asset.format_transcript()
    max_frames = int(env("VIDEO_MAX_KEYFRAMES") or 8)

    try:
        planned = await plan_video_keyframes(asset.title or url, asset.duration_sec, transcript, user_note, max_frames=max_frames)
        planned_steps = planned.get("steps") or []
    except Exception as exc:
        planned_steps = []
        log.warning("视频关键帧时间点规划失败：%s", type(exc).__name__)

    target_secs = [int(s["target_sec"]) for s in planned_steps if isinstance(s.get("target_sec"), (int, float))]
    if not target_secs:
        dur = max(20, asset.duration_sec or 120)
        step = max(5, dur // max_frames)
        target_secs = list(range(2, dur, step))[:max_frames]

    frames, frame_note = await extract_link_keyframes(url, target_secs, asset.duration_sec)
    status = asset.access_status
    notes = [n for n in (asset.note, frame_note) if n]
    if not frames and status == "PASS":
        status = "PARTIAL"

    if frames or transcript:
        try:
            vision = await analyze_video_steps(asset.title or url, url, transcript, user_note, frames, categories)
            rule = match_category(categories, vision.get("category", ""))
            result = AnalysisResult(
                title=str(vision.get("title") or asset.title or "视频分步笔记")[:40],
                category=rule["name"],
                tags=list(vision.get("tags") or ["视频教程"]),
                summary=str(vision.get("summary") or "已整理视频内容与时间轴。"),
                key_points=[str(x) for x in (vision.get("key_points") or [])[:8]],
                extracted_text=transcript[:12000],
                entity_type="视频教程/访谈",
            )
            steps = vision.get("steps") or []
            for step in steps:
                s_sec = step.get("start_sec")
                if s_sec is not None:
                    step["timestamp_url"] = timestamp_link(url, s_sec)
            return {
                "asset": asset,
                "result": result,
                "status": status,
                "note": "；".join(notes),
                "steps": steps,
                "frames": frames,
            }
        except Exception as exc:
            notes.append(f"视频分步分析失败：{type(exc).__name__}")
            if status == "PASS":
                status = "PARTIAL"

    fallback_cat = match_category(categories, "")["name"]
    result = AnalysisResult(
        title=(asset.title or url)[:40],
        category=fallback_cat,
        tags=["视频教程", "待整理"],
        summary="已保留视频元信息与可获取字幕，真实关键帧未完整提取。" if transcript else "视频信息受限，已留存原链接与元数据。",
        key_points=[f"[{format_timestamp(s['start_sec'])}] {s['text']}" for s in asset.transcript_segments[:6]],
        extracted_text=transcript[:12000],
        entity_type="视频教程/访谈",
    )
    return {
        "asset": asset,
        "result": result,
        "status": "FALLBACK" if not transcript and not asset.title else status,
        "note": "；".join(notes),
        "steps": [],
        "frames": frames,
    }


def render_video_blocks_with_uploads(steps: list[dict], upload_ids: dict[int, str]) -> list[dict]:
    return build_video_step_blocks(steps, upload_ids)
