"""Flow2API（Google Flow / VideoFX 的 OpenAI 兼容网关，用户自建）。

配置（config.json）：flow_base_url（自建服务地址，无默认值）、flow_api_key。
接口：
  GET  /v1/models                 模型列表（Bearer api_key）
  POST /v1/media/upload           上传参考图（原始字节），返回 id
  HEAD /v1/media/{id}             素材是否还在（200/404）
  POST /v1/chat/completions       生图/生视频，stream=true，参考图用 image_url 的 ref://id（旧服务端退回 base64 data URL）
  GET  /health                    服务与账号状态（公开）
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request

from . import util
from .util import CloudGenError

DEFAULT_BASE = ""


def base_url() -> str:
    return (util.load_config().get("flow_base_url") or DEFAULT_BASE).rstrip("/")


def _need_base() -> str:
    b = base_url()
    if not b:
        raise CloudGenError("未配置 Flow2API 服务地址：在侧边栏「⚙ 配置 → 账户 → Flow2API」填写")
    return b


def api_key() -> str:
    return str(util.load_config().get("flow_api_key") or "").strip()


def _headers(extra: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {api_key()}", "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h


def get_json(path: str, auth: bool = True, timeout: float = 20):
    status, _, data = util.http_request(_need_base() + path, headers=_headers() if auth else None, timeout=timeout)
    try:
        body = json.loads(data.decode("utf-8")) if data else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = data.decode("utf-8", "replace")[:400000]
    if status >= 400:
        raise CloudGenError(f"Flow2API {path} HTTP {status}: {str(body)[:300]}")
    return body


def health() -> dict:
    return get_json("/health", auth=False, timeout=8)


# ---------------------------------------------------------------- 模型目录
# /v1/models 返回的是展开后的具体模型名（比例、分辨率、时长都编码在名字里），这里归并成“模型族”
IMG_RE = re.compile(r"^(gemini-[\d.]+-[a-z-]*?image)-(landscape|portrait|square|four-three|three-four)(?:-(2k|4k))?$")
VID_RE = re.compile(r"^(veo-3\.1-(lite|fast|quality)|omni-1\.1-flash)(?:-(i2v|r2v|extend))?-(\d+)s-(landscape|portrait)$")
ASPECT = {"16:9": "landscape", "9:16": "portrait", "1:1": "square", "4:3": "four-three", "3:4": "three-four"}
ASPECT_R = {v: k for k, v in ASPECT.items()}
FALLBACK = {"image": ["Nano Banana 2.1", "Nano Banana Pro", "Nano Banana 2 Lite"],
            "video": ["Veo 3.1 Fast", "Veo 3.1 Quality", "Veo 3.1 Lite", "Omni 1.1 Flash"]}
_cache: dict = {}
_lock = threading.Lock()
CACHE_TTL = 600


def _catalog(refresh: bool = False) -> dict:
    with _lock:
        hit = _cache.get("cat")
        if hit and not refresh and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    data = get_json("/v1/models") or {}
    ids = {m.get("id"): m.get("description") or "" for m in data.get("data") or [] if m.get("id")}
    images: dict = {}
    videos: dict = {}
    for mid, desc in ids.items():
        m = IMG_RE.match(mid)
        if m:
            fam, aspect, size = m.group(1), m.group(2), (m.group(3) or "1k").upper()
            f = images.setdefault(fam, {"provider": "flow", "kind": "image", "key": fam,
                                        "name": desc.split("·")[0].strip() or fam, "aspects": set(), "sizes": set(), "ids": set()})
            f["aspects"].add(aspect); f["sizes"].add(size); f["ids"].add(mid)
            continue
        m = VID_RE.match(mid)
        if m:
            fam, tier, typ, dur, orient = m.group(1), m.group(2), m.group(3) or "t2v", int(m.group(4)), m.group(5)
            name = f"Veo 3.1 {tier.capitalize()}" if tier else "Omni 1.1 Flash"
            f = videos.setdefault(fam, {"provider": "flow", "kind": "video", "key": fam, "name": name,
                                        "types": {}, "orients": set(), "ids": set()})
            f["types"].setdefault(typ, set()).add(dur); f["orients"].add(orient); f["ids"].add(mid)
    out = {"image": [], "video": []}
    for f in images.values():
        out["image"].append({**f, "ids": sorted(f["ids"]), "aspects": sorted(f["aspects"]), "sizes": sorted(f["sizes"]),
                             "ratios": [ASPECT_R[a] for a in ASPECT.values() if a in f["aspects"]],
                             "resolutions": [s for s in ("1K", "2K", "4K") if s in f["sizes"]], "default_resolution": "1K"})
    order = {"veo-3.1-fast": 0, "veo-3.1-quality": 1, "veo-3.1-lite": 2, "omni-1.1-flash": 3}
    for f in sorted(videos.values(), key=lambda x: order.get(x["key"], 9)):
        types = {k: sorted(v) for k, v in f["types"].items()}
        t2v = types.get("t2v") or []
        allp = sorted({d for k, v in types.items() if k != "extend" for d in v})
        out["video"].append({**f, "ids": sorted(f["ids"]), "types": types, "orients": sorted(f["orients"]),
                             "ratios": [r for r, a in (("16:9", "landscape"), ("9:16", "portrait")) if a in f["orients"]],
                             "duration": {"options": allp, "default": 8 if 8 in t2v else (t2v[-1] if t2v else (allp[-1] if allp else 8))},
                             "modes": [m for m in ("t2v", "i2v", "r2v") if m in types]})
    # Nano Banana Pro（GEM_PIX_2）：新版 Flow2API 把它标成“不列出”的旧模型，/v1/models 里看不到，
    # 但生成接口只检查模型在不在 MODEL_CONFIG 里，按具体名字仍可调用。列表里有新版图片模型时补上它。
    pro = "gemini-3.0-pro-image"
    if not any(f["key"] == pro for f in out["image"]) and any(k.startswith("gemini-3.1-flash-image") for k in images):
        aspects = list(ASPECT.values())
        out["image"].append({"provider": "flow", "kind": "image", "key": pro, "name": "Nano Banana Pro", "hidden": True,
                             "aspects": aspects, "sizes": ["1K", "2K", "4K"],
                             "ids": sorted(f"{pro}-{a}{s}" for a in aspects for s in ("", "-2k", "-4k")),
                             "ratios": list(ASPECT.keys()), "resolutions": ["1K", "2K", "4K"], "default_resolution": "1K"})
    rank = {"gemini-3.1-flash-image": 0, "gemini-3.0-pro-image": 1}
    out["image"].sort(key=lambda x: (rank.get(x["key"], 2), x["name"]))
    with _lock:
        _cache["cat"] = (time.time(), out)
    return out


def get_models(kind: str, refresh: bool = False) -> list[dict]:
    if not api_key():
        raise CloudGenError("Flow2API 还没配置 API Key：在侧边栏「云端积分 → ⚙ 配置 → 账户」里填写")
    return _catalog(refresh)[kind]


def cached_model_names(kind: str) -> list[str]:
    hit = _cache.get("cat")
    if hit:
        return [m["name"] for m in hit[1][kind]] or FALLBACK[kind]
    if api_key():
        threading.Thread(target=lambda: _safe(lambda: _catalog()), daemon=True).start()
    return FALLBACK[kind]


def _safe(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        print(f"[CloudGen] Flow2API 模型列表获取失败：{exc}")


def find_model(kind: str, name: str) -> dict:
    for refresh in (False, True):
        for m in get_models(kind, refresh):
            if name in (m["name"], m["key"]):
                return m
    raise CloudGenError(f"Flow2API 没有模型「{name}」，可用：{', '.join(m['name'] for m in get_models(kind))}")


def swap_43() -> bool:
    """Flow2API（截至 2026-10）把 4:3 / 3:4 发给 Flow 的编号写反了：要 3:4 出 4:3，反之亦然。
    默认对调着发；等 Flow2API 修好后在配置里关掉（config.json: flow_swap_43=false）。"""
    v = util.load_config().get("flow_swap_43")
    return True if v is None else bool(v)


def image_model_id(m: dict, ratio: str, resolution: str) -> str:
    aspect = ASPECT.get(ratio or "16:9", "landscape")
    if swap_43() and aspect in ("four-three", "three-four"):
        aspect = "three-four" if aspect == "four-three" else "four-three"
    size = (resolution or "1K").upper()
    mid = m["key"] + f"-{aspect}" + ("" if size == "1K" else f"-{size.lower()}")
    if mid not in m["ids"]:
        raise CloudGenError(f"{m['name']} 不支持 {ratio} · {size}（可选比例 {'/'.join(m['ratios'])}，分辨率 {'/'.join(m['resolutions'])}）")
    return mid


def video_model_id(m: dict, typ: str, duration: int, ratio: str) -> str:
    orient = "portrait" if ratio == "9:16" else "landscape"
    durs = (m.get("types") or {}).get(typ) or []
    if not durs:
        names = {"t2v": "文生视频", "i2v": "首尾帧", "r2v": "参考图生成"}
        raise CloudGenError(f"{m['name']} 不支持{names.get(typ, typ)}，支持：{'、'.join(names.get(t, t) for t in m.get('modes') or [])}")
    if duration not in durs:
        raise CloudGenError(f"{m['name']} 的{ {'t2v': '文生视频', 'i2v': '首尾帧', 'r2v': '参考图生成'}.get(typ, typ)}只支持 {'/'.join(map(str, durs))} 秒")
    mid = f"{m['key']}{'' if typ == 't2v' else '-' + typ}-{duration}s-{orient}"
    if mid not in m["ids"]:
        raise CloudGenError(f"Flow2API 没有 {mid}")
    return mid


# ---------------------------------------------------------------- 生成
def data_url(png_bytes: bytes) -> str:
    """参考图转 data URL；太大的 PNG 转成 JPEG（质量 95）以免请求体过大。"""
    mime, data = "image/png", png_bytes
    if len(png_bytes) > 6 * 1024 * 1024:
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.open(io.BytesIO(png_bytes)).convert("RGB").save(buf, format="JPEG", quality=95)
        mime, data = "image/jpeg", buf.getvalue()
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def image_ref(png_bytes: bytes) -> str:
    """参考图先上传到 Flow2API 换成 ref://素材ID；相同内容只上传一次，服务端已删除则重传。
    服务端还没有 /v1/media/upload（未更新）时退回内嵌 data URL。"""
    key = f"flow:{base_url()}:{util.sha1(png_bytes)}"
    cached = util.asset_get(key)
    if cached:
        status, _, _ = util.http_request(f"{_need_base()}/v1/media/{cached}", method="HEAD", headers=_headers(), timeout=20)
        if status == 200:
            return f"ref://{cached}"
        util.asset_drop(key)
    status, _, data = util.http_request(_need_base() + "/v1/media/upload", method="POST",
                                        headers=_headers({"Content-Type": "application/octet-stream"}),
                                        body=png_bytes, timeout=300)
    if status in (404, 405):
        return data_url(png_bytes)
    if status >= 400:
        raise CloudGenError(f"Flow2API 上传参考图失败 HTTP {status}：{data.decode('utf-8', 'replace')[:300]}")
    upload_id = json.loads(data.decode("utf-8"))["id"]
    util.asset_put(key, upload_id, "flow-ref")
    return f"ref://{upload_id}"


URL_RE = re.compile(r"""(?:https?://[^\s"'<>()\]]+|data:(?:image|video)/[\w.+-]+;base64,[A-Za-z0-9+/=]+)""")


def _fix_url(u: str) -> str:
    """Flow2API 缓存的文件形如 {它自己配置的 base_url}/tmp/xxx；它的 base_url 可能是 localhost，换成我们连接用的地址。"""
    from urllib.parse import urlsplit
    p = urlsplit(u)
    if p.path.startswith("/tmp/"):
        return _need_base() + p.path + (f"?{p.query}" if p.query else "")
    return u


def _friendly_progress(line: str) -> str:
    """Flow2API 的进度文字里有打码/验证码等内部细节，节点上统一显示成「正在生成中…」。"""
    if re.search(r"打码|验证码|reCAPTCHA|captcha|recaptcha", line, re.I):
        return "正在生成中…"
    return line


def chat_generate(model_id: str, prompt: str, images: list[bytes], node_id=None, timeout: float = 1200,
                  quiet: bool = False) -> dict:
    """quiet=True：并发时由调用方统一显示进度，这里不刷节点进度。"""
    """POST /v1/chat/completions（stream），返回 {"text", "urls"}。进度文字转发到节点上。"""
    if not api_key():
        raise CloudGenError("Flow2API 还没配置 API Key")
    if images:
        content: list | str = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": image_ref(b)}} for b in images]
    else:
        content = prompt
    body = json.dumps({"model": model_id, "messages": [{"role": "user", "content": content}], "stream": True},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_need_base() + "/v1/chat/completions", data=body, method="POST",
                                 headers=_headers({"Content-Type": "application/json", "Accept": "text/event-stream",
                                                   "User-Agent": util.UA}))
    text, last_progress, errors = "", "", []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if obj.get("error"):
                    errors.append(str(obj["error"].get("message") if isinstance(obj["error"], dict) else obj["error"]))
                    continue
                for ch in obj.get("choices") or []:
                    delta = ch.get("delta") or ch.get("message") or {}
                    piece = (delta.get("content") or "") + (delta.get("reasoning_content") or "")
                    if not piece:
                        continue
                    text += piece
                    for ln in piece.splitlines():
                        ln = ln.strip()
                        if ln.startswith("错误") or ln.lower().startswith("error"):
                            errors.append(ln)
                        elif ln and not URL_RE.search(ln):
                            shown = _friendly_progress(ln)
                            if quiet or shown == last_progress:
                                continue
                            last_progress = shown
                            util.send_progress(node_id, f"Flow：{shown[:60]}")
                            m = re.search(r"(\d{1,3})\s*%", ln)
                            if m:
                                util.progress_bar(int(m.group(1)))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500] if exc.fp else ""
        try:
            j = json.loads(detail)
            detail = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else (j.get("detail") or j.get("error") or detail)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise CloudGenError(f"Flow2API HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise CloudGenError(f"连不上 Flow2API（{base_url()}）：{exc.reason}") from exc
    urls = [_fix_url(u.rstrip(".,;")) for u in URL_RE.findall(text)]
    if not urls:
        raise CloudGenError("Flow2API 没有返回结果：" + ("；".join(errors) or text.strip()[-300:] or "空响应"))
    return {"text": text, "urls": list(dict.fromkeys(urls)), "errors": errors}


def fetch(url: str, timeout: float = 600) -> bytes:
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    hdrs = _headers() if base_url() and url.startswith(base_url()) else None
    status, _, data = util.http_request(url, headers=hdrs, timeout=timeout)
    if status >= 400:
        raise CloudGenError(f"下载 Flow2API 结果失败 HTTP {status}: {url[:120]}")
    return data


def status() -> dict:
    """侧边栏用：服务是否在线、可用账号数。"""
    out = {"provider": "flow", "name": "Flow2API", "ok": False, "balance": "—", "expire": "", "extra": {}}
    try:
        h = health()
        active, total = h.get("active_tokens", 0), h.get("total_tokens", 0)
        out.update(ok=bool(h.get("backend_running")) and bool(api_key()), balance_source="none",
                   flow={"active": active, "total": total, "expiring_1h": h.get("tokens_expiring_within_1h", 0),
                         "expired": h.get("tokens_expired", 0), "banned": h.get("banned_429_tokens", 0),
                         "key": bool(api_key()), "base": base_url()})
        if not api_key():
            out["error"] = "未配置 API Key"
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out
