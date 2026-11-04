"""通用工具：配置、HTTP、递归查找、媒体转换。仅依赖标准库（torch/numpy/PIL 由 ComfyUI 提供）。"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Iterable

PACK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PACK_DIR, "config.json")
_cfg_lock = threading.Lock()

UA = "ComfyUI-LibTV-XYQ/0.1"


class CloudGenError(RuntimeError):
    pass


# ---------------------------------------------------------------- config
def load_config() -> dict:
    with _cfg_lock:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}


def save_config(patch: dict) -> dict:
    with _cfg_lock:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            data = {}
        for k, v in patch.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return data


# ---------------------------------------------------------------- http
def http_request(url: str, *, method: str = "GET", headers: dict | None = None, body: bytes | None = None,
                 timeout: float = 60) -> tuple[int, dict, bytes]:
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        data = exc.read() if exc.fp else b""
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, data
    except urllib.error.URLError as exc:
        raise CloudGenError(f"网络错误：{url} -> {exc.reason}") from exc


def post_json(url: str, payload: Any, headers: dict | None = None, timeout: float = 60) -> tuple[int, Any]:
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    status, _, data = http_request(url, method="POST", headers=hdrs,
                                   body=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=timeout)
    try:
        return status, json.loads(data.decode("utf-8")) if data else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, data.decode("utf-8", "replace")


def multipart(fields: dict[str, str], file_field: str, file_name: str, file_bytes: bytes,
              content_type: str) -> tuple[bytes, str]:
    boundary = f"----CloudGen{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for k, v in fields.items():
        parts += [f"--{boundary}\r\n".encode(),
                  f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode(), f"{v}\r\n".encode()]
    parts += [f"--{boundary}\r\n".encode(),
              f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'.encode(),
              f"Content-Type: {content_type}\r\n\r\n".encode(), file_bytes, b"\r\n",
              f"--{boundary}--\r\n".encode()]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def download(url: str, timeout: float = 300) -> bytes:
    status, _, data = http_request(url, timeout=timeout)
    if status >= 400:
        raise CloudGenError(f"下载失败 HTTP {status}: {url}")
    return data


# ---------------------------------------------------------------- json walking
def walk(value: Any, path: str = ""):
    """遍历嵌套结构，产出 (path, key, value)。"""
    if isinstance(value, dict):
        for k, v in value.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, str(k), v
            yield from walk(v, p)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            p = f"{path}[{i}]"
            yield p, "", v
            yield from walk(v, p)


def maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "{[":
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return value
    return value


def deep_decode(value: Any, depth: int = 0) -> Any:
    """把嵌套里的 JSON 字符串也解出来（服务端常把 data 放成字符串）。"""
    if depth > 6:
        return value
    value = maybe_json(value)
    if isinstance(value, dict):
        return {k: deep_decode(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [deep_decode(v, depth + 1) for v in value]
    return value


IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
VID_EXT = (".mp4", ".mov", ".webm", ".mkv", ".m4v")
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")


def classify_url(url: str, hint: str = "") -> str:
    low = url.lower().split("?")[0]
    h = hint.lower()
    if low.endswith(VID_EXT) or "video" in h:
        return "video"
    if low.endswith(IMG_EXT) or "image" in h or "img" in h or "cover" in h or "thumb" in h:
        return "image"
    return ""


def find_media_urls(value: Any) -> dict[str, list[str]]:
    """递归找出图片/视频 URL，过滤封面/缩略图/上传素材。"""
    out = {"image": [], "video": []}
    seen = set()
    for path, key, v in walk(value):
        if not isinstance(v, str) or not v.startswith(("http://", "https://")):
            continue
        lp = path.lower()
        if any(x in lp for x in ("cover", "thumb", "poster", "avatar", "icon", "upload", "input", "reference", "ref_")):
            continue
        kind = classify_url(v, lp)
        if kind and v not in seen:
            seen.add(v)
            out[kind].append(v)
    return out


def find_first(value: Any, keys: Iterable[str], types=(str, int, float)) -> Any:
    keys = [k.lower() for k in keys]
    for _, key, v in walk(value):
        if key.lower() in keys and isinstance(v, types) and v not in ("", None):
            return v
    return None


COST_KEYS = ("credit", "credits", "cost", "price", "point", "points", "consume", "consume_credit",
             "credit_cost", "cost_credit", "need_credit", "amount", "power", "算力", "积分")


def find_cost(value: Any) -> float | None:
    """在模型配置里找积分消耗字段（字段名未知时的启发式）。"""
    best = None
    for path, key, v in walk(value):
        k = key.lower()
        if not k:
            continue
        hit = any(c == k or k.endswith("_" + c) or k.startswith(c + "_") or c in k for c in COST_KEYS)
        if not hit or "balance" in k or "remain" in k or "limit" in k or "max" in k:
            continue
        num = None
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            num = float(v)
        elif isinstance(v, str) and re.fullmatch(r"\d+(\.\d+)?", v.strip()):
            num = float(v)
        if num is not None and num > 0:
            if best is None or num < best:
                best = num
    return best


def fmt_num(v: Any) -> str:
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:.2f}"
    except (TypeError, ValueError):
        return str(v)


def fmt_time(v: Any) -> str:
    """把秒/毫秒时间戳或 ISO 字符串转为本地时间文本。"""
    if v in (None, ""):
        return ""
    try:
        f = float(v)
        if f > 1e12:
            f /= 1000
        if f > 1e8:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(f))
    except (TypeError, ValueError):
        pass
    return str(v)


# ---------------------------------------------------------------- comfy helpers
def check_interrupt():
    try:
        import comfy.model_management as mm
        mm.throw_exception_if_processing_interrupted()
    except ImportError:
        pass


def send_progress(node_id: str | None, text: str):
    """在节点上显示进度文字（前端 cloudgen.progress 事件）。"""
    try:
        from server import PromptServer
        PromptServer.instance.send_sync("cloudgen.progress", {"node": node_id, "text": text})
    except Exception:
        pass
    print(f"[CloudGen] {text}")


def progress_bar(percent):
    """更新 ComfyUI 自带的节点进度条（队列面板里的百分比）。"""
    try:
        import comfy.utils
        pb = comfy.utils.ProgressBar(100)
        pb.update_absolute(max(0, min(100, int(percent))), 100)
    except Exception:
        pass


def tensor_to_png_bytes(image_tensor, index: int = 0) -> bytes:
    import numpy as np
    from PIL import Image
    arr = image_tensor[index].cpu().numpy()
    arr = (np.clip(arr, 0, 1) * 255).astype("uint8")
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def images_from_bytes(blobs: list[bytes]):
    """多张图转为 IMAGE batch；尺寸不同时按第一张缩放对齐。"""
    import numpy as np
    import torch
    from PIL import Image, ImageOps
    imgs = []
    for b in blobs:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(b))).convert("RGB")
        imgs.append(im)
    if not imgs:
        raise CloudGenError("没有得到图片结果")
    w, h = imgs[0].size
    tensors = []
    for im in imgs:
        if im.size != (w, h):
            im = im.resize((w, h), Image.LANCZOS)
        tensors.append(torch.from_numpy(np.asarray(im).astype("float32") / 255.0))
    return torch.stack(tensors, 0)


def output_dir(sub: str) -> str:
    try:
        import folder_paths
        base = folder_paths.get_output_directory()
    except ImportError:
        base = os.path.join(PACK_DIR, "output")
    d = os.path.join(base, sub)
    os.makedirs(d, exist_ok=True)
    return d


def temp_dir() -> str:
    try:
        import folder_paths
        base = folder_paths.get_temp_directory()
    except ImportError:
        base = os.path.join(PACK_DIR, "temp")
    d = os.path.join(base, "cloudgen")
    os.makedirs(d, exist_ok=True)
    return d


def save_blob(data: bytes, sub: str, prefix: str, ext: str) -> str:
    d = output_dir(sub)
    name = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
    path = os.path.join(d, name)
    with open(path, "wb") as fp:
        fp.write(data)
    return path


def video_object(path: str):
    try:
        from comfy_api.input_impl import VideoFromFile
        return VideoFromFile(path)
    except Exception:
        return None


# ---------------------------------------------------------------- 素材上传缓存（相同内容不重复上传）
ASSET_CACHE_PATH = os.path.join(PACK_DIR, "assets_cache.json")
_asset_lock = threading.Lock()


def _asset_load() -> dict:
    try:
        with open(ASSET_CACHE_PATH, "r", encoding="utf-8") as fp:
            d = json.load(fp)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def asset_get(key: str):
    with _asset_lock:
        v = _asset_load().get(key)
    return v.get("id") if isinstance(v, dict) else None


def asset_put(key: str, asset_id: str, name: str = ""):
    with _asset_lock:
        d = _asset_load()
        d[key] = {"id": asset_id, "name": name, "at": time.strftime("%Y-%m-%d %H:%M")}
        tmp = ASSET_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(d, fp, ensure_ascii=False, indent=1)
        os.replace(tmp, ASSET_CACHE_PATH)


def asset_drop(key: str):
    with _asset_lock:
        d = _asset_load()
        if d.pop(key, None) is not None:
            with open(ASSET_CACHE_PATH, "w", encoding="utf-8") as fp:
                json.dump(d, fp, ensure_ascii=False, indent=1)


def asset_clear(prefix: str = "") -> int:
    with _asset_lock:
        d = _asset_load()
        keep = {k: v for k, v in d.items() if prefix and not k.startswith(prefix)}
        n = len(d) - len(keep)
        with open(ASSET_CACHE_PATH, "w", encoding="utf-8") as fp:
            json.dump(keep, fp, ensure_ascii=False, indent=1)
        return n


def asset_count() -> dict:
    d = _asset_load()
    return {"libtv": sum(k.startswith("libtv:") for k in d), "xyq": sum(k.startswith("xyq:") for k in d)}


def sha1(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(data).hexdigest()


_dur_cache: dict = {}


def media_duration(path: str):
    """本地音视频时长（秒）。先用 PyAV（ComfyUI 自带），再退回 ffprobe；读不到返回 None。"""
    path = (path or "").strip().strip('"')
    if not path or not os.path.isfile(path):
        return None
    try:
        st = os.stat(path)
        ck = (path, st.st_mtime, st.st_size)
    except OSError:
        return None
    if ck in _dur_cache:
        return _dur_cache[ck]
    dur = None
    try:
        import av  # type: ignore
        with av.open(path) as c:
            if c.duration:
                dur = c.duration / 1_000_000
            else:
                vs = [s for s in c.streams if s.type in ("video", "audio") and s.duration and s.time_base]
                if vs:
                    dur = float(vs[0].duration * vs[0].time_base)
    except Exception:  # noqa: BLE001
        dur = None
    if dur is None:
        try:
            import subprocess
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                 capture_output=True, text=True, timeout=15)
            dur = float(out.stdout.strip())
        except Exception:  # noqa: BLE001
            dur = None
    if dur is not None:
        dur = round(dur, 2)
    _dur_cache[ck] = dur
    return dur
