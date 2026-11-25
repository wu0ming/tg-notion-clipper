"""
Gemini 付费 Key 429 诊断（只读，不写任何数据）：python tests/gemini_quota.py
打印：每个模型调用的完整错误体（含配额名称、配额上限值、项目号），以及该 Key 能看到的模型列表，
用于判断 429 是“预付余额为 0 / 未生效”还是“该项目对该模型的配额被设为 0”
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import env  # noqa: E402

BASE = "https://generativelanguage.googleapis.com/v1beta"


def call(method: str, url: str, key: str, body: dict | None = None):
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json", "x-goog-api-key": key},
                                 data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"exception": f"{type(e).__name__}: {e}"}


for label, key in (("付费 GEMINI_API_KEY", env("GEMINI_API_KEY")), ("免费 GEMINI_API_KEY_FREE", env("GEMINI_API_KEY_FREE"))):
    if not key:
        print(f"\n### {label}: 未配置")
        continue
    print(f"\n### {label}（前缀 {key[:6]}…）")
    st, data = call("GET", f"{BASE}/models?pageSize=200", key)
    names = [m.get("name", "").replace("models/", "") for m in data.get("models", [])]
    gem = [n for n in names if n.startswith("gemini-3") or n.startswith("gemini-4")]
    print(f"可见模型 HTTP {st}，共 {len(names)} 个，其中 gemini-3/4：{', '.join(gem[:20])}")
    for model in ("gemini-3.8-flash", "gemini-3.5-flash-lite"):
        st, data = call("POST", f"{BASE}/models/{model}:generateContent", key,
                        {"contents": [{"parts": [{"text": "只回复两个字：收到"}]}]})
        if st == 200:
            txt = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            print(f"  [OK] {model} → {txt.strip()[:20]}  tokens={data.get('usageMetadata', {}).get('totalTokenCount')}")
            continue
        err = data.get("error", data)
        print(f"  [FAIL] {model} → HTTP {st} {err.get('status', '')}")
        print("    message:", (err.get("message") or "")[:400])
        for d in err.get("details", []) or []:
            t = d.get("@type", "").split("/")[-1]
            if t == "google.rpc.QuotaFailure":
                for v in d.get("violations", []):
                    print(f"    配额：{v.get('quotaMetric') or v.get('subject')} | id={v.get('quotaId')} | "
                          f"上限={v.get('quotaValue')} | 维度={v.get('quotaDimensions')}")
            elif t == "google.rpc.ErrorInfo":
                print(f"    ErrorInfo：reason={d.get('reason')} metadata={json.dumps(d.get('metadata', {}), ensure_ascii=False)}")
            elif t == "google.rpc.Help":
                for l in d.get("links", []):
                    print(f"    帮助链接：{l.get('url')}")
            elif t == "google.rpc.RetryInfo":
                print(f"    建议重试间隔：{d.get('retryDelay')}")
