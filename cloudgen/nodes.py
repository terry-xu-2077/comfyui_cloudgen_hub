"""ComfyUI 节点：小云雀 / LibTV 图片、视频生成 + 积分查看。"""
from __future__ import annotations

import json
import re
import time

from . import flow, libtv, libtv_auth, lovart, plans, prices, util, xyq
from .util import CloudGenError

CAT = "☁️ 云端生成"
DEFAULT = "默认"

XYQ_RATIOS = [DEFAULT, "adaptive", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "2:1", "2.35:1", "1.85:1"]
XYQ_IMG_RES = [DEFAULT, "1K", "2K", "4K"]
XYQ_VID_RES = [DEFAULT, "480p", "720p", "1080p", "2k", "4k"]
XYQ_EFFORT = [DEFAULT, "low", "medium", "high"]
XYQ_PROMPT_MODES = ["原文直出", "智能体优化"]
XYQ_MODES = ["自动", "text_to_video", "reference_generation", "first_last_frame", "video_extend", "video_edit"]

LIBTV_QUALITY = ["自动", "高", "中", "低", "1K", "2K", "4K"]
LIBTV_QUALITY_MAP = {"自动": "auto", "高": "high", "中": "medium", "低": "low"}
LIBTV_RATIOS = ["auto", "1:1", "3:2", "2:3", "4:3", "3:4", "9:16", "16:9", "21:9",
                "1:1(2k)", "16:9(2k)", "9:16(2k)", "16:9(4k)", "9:16(4k)"]
LIBTV_VID_RES = [DEFAULT, "480p", "720p", "1080p", "2k", "4k"]
LIBTV_MODES = ["自动", "text2video", "singleImage2video", "frames2video", "image2video", "video2video", "mixed2video"]


def _opt(v: str) -> str:
    return "" if v in (DEFAULT, "自动", None) else v


def _balance_safe(fn):
    try:
        return float(fn()["balance"])
    except Exception:  # noqa: BLE001
        return None


def _cost_line(before, after) -> str:
    if before is None or after is None:
        return ""
    return f"本次消耗 {util.fmt_num(before - after)} 积分，剩余 {util.fmt_num(after)}"


def _record(provider, kind, model, dim, before, after, duration=None, count=None, job_id=None):
    if before is None or after is None:
        return
    if job_id:
        from . import jobs
        jobs.update(job_id, cost=before - after, cost_src="actual")
    try:
        prices.record_observed(provider, kind, model, dim, before - after, duration, count)
    except Exception as exc:  # noqa: BLE001
        print(f"[CloudGen] 记录实测消耗失败：{exc}")


def _libtv_charge(kind, det, dim, raw, duration=None, count=None, job_id=None) -> str:
    """LibTV 没有余额接口：优先用任务返回里的消耗，否则按配置价格估算，并从手动余额里扣。"""
    cost = None
    for path, key, v in util.walk(raw):
        k = key.lower()
        if any(x in k for x in ("credit", "cost", "consume", "积分", "point")) and not any(
                x in k for x in ("remain", "balance", "total", "limit", "max", "left")) and \
                isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            cost = v
            break
    src = "任务返回"
    if cost is None:
        dd = ((det.get("options") or {}).get("duration") or {}).get("default") or 5
        cost = prices.estimate("libtv", kind, det["name"], dim, duration, count, dd)
        src = "按配置价格估算"
    else:
        try:
            prices.record_observed("libtv", kind, det["name"], dim, cost, duration, count)
        except Exception:  # noqa: BLE001
            pass
    if cost is None:
        return "本次消耗未知（LibTV 接口不返回余额，可在 ⚙ 配置 → 积分价格 里填价格用于估算）"
    if job_id:
        from . import jobs
        jobs.update(job_id, cost=cost, cost_src="estimate" if src.startswith("按") else "actual")
    plans.deduct("libtv", cost)
    mb = plans.manual_balance("libtv")
    return f"本次消耗 {util.fmt_num(cost)} 积分（{src}）" + (f"，余额约 {util.fmt_num(mb)}" if mb is not None else "")


def _parse_extra(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        v = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CloudGenError(f"extra_json 不是合法 JSON：{exc}") from exc
    if not isinstance(v, dict):
        raise CloudGenError("extra_json 必须是 JSON 对象")
    return v


def _split_urls(text: str) -> list[str]:
    return [u.strip() for u in (text or "").replace(",", "\n").splitlines() if u.strip().startswith("http")]


def _batch(images) -> list:
    if images is None:
        return []
    return [i for i in range(images.shape[0])]


def _iter_images(images):
    """支持单个 IMAGE batch，也支持列表（CreateList 等），按顺序产出 (tensor, index)。"""
    if images is None:
        return
    seq = images if isinstance(images, (list, tuple)) else [images]
    for t in seq:
        if t is None:
            continue
        for i in range(t.shape[0]):
            yield t, i


def _first_image(v):
    if isinstance(v, (list, tuple)):
        v = next((x for x in v if x is not None), None)
    return v


class _ListInput:
    """INPUT_IS_LIST：让列表输入（多张不同尺寸的参考图）在一次生成里一起使用，而不是每项各跑一次。"""
    INPUT_IS_LIST = True
    LIST_IMAGE_KEYS = ("images", "ref_images")
    FUNCTION = "run_list"

    def run_list(self, **kw):
        args = {}
        for k, v in kw.items():
            if k in self.LIST_IMAGE_KEYS:
                args[k] = [x for x in v if x is not None] if isinstance(v, list) else v
                if not args[k]:
                    args[k] = None
            elif k in ("first_frame", "last_frame"):
                args[k] = _first_image(v)
            else:
                args[k] = v[0] if isinstance(v, list) and v else v
        return self.run(**args)


# ======================================================================= 小云雀
class XYQImage(_ListInput):
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (xyq.cached_model_names("image"),),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "ratio": (XYQ_RATIOS, {"default": DEFAULT}),
                "resolution": (XYQ_IMG_RES, {"default": DEFAULT}),
                "quality": (XYQ_EFFORT, {"default": DEFAULT, "tooltip": "推理强度/质量（部分模型支持）", "advanced": True}),
                "count": ("INT", {"default": 1, "min": 1, "max": 8}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0x7FFFFFFF,
                                 "tooltip": "不传给服务端。改 seed = 新任务（重新生成并扣费）；seed 和其它参数都不变时，中断后再运行会续接原任务、不重复扣费", "advanced": True}),
            },
            "optional": {"images": ("IMAGE", {"tooltip": "参考图（可多张 batch）"}),
                         "prompt_mode": (XYQ_PROMPT_MODES, {"default": XYQ_PROMPT_MODES[0], "advanced": True,
                                                            "tooltip": "小云雀生图由智能体先改写提示词再出图。原文直出：要求智能体把提示词原样交给生图模型；智能体优化：允许它自由改写"})},
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "info")
    CATEGORY = CAT

    @classmethod
    def VALIDATE_INPUTS(cls, model, ratio, resolution, quality):
        return True

    def run(self, model, prompt, ratio, resolution, quality, count, seed, images=None, prompt_mode=None, unique_id=None):
        if not prompt.strip():
            raise CloudGenError("prompt 不能为空")
        m = xyq.find_model("image", model)
        before = _balance_safe(xyq.get_balance)
        assets = []
        for n, (t, i) in enumerate(_iter_images(images), 1):
            util.send_progress(unique_id, f"上传参考图 {n}")
            assets.append(xyq.upload_bytes(util.tensor_to_png_bytes(t, i), f"ref_{n}.png"))
        from . import jobs
        key = jobs.make_key("xyq", "image", m["key"], prompt, ratio, resolution, quality, count, assets, seed, prompt_mode)
        send_prompt, send_ratio = _xyq_mentions(prompt), _opt(ratio)
        if send_ratio and not m.get("ratios"):
            # 部分模型（如 旗舰生图模型 V2-Pro / V2-Flash）在开放接口里没有下发比例参数，传了会报错；
            # 网页端能选比例，这里改成在提示词末尾注明画面比例
            send_prompt = f"{send_prompt.rstrip()}\n画面比例 {send_ratio}"
            send_ratio = None
            util.send_progress(unique_id, f"{m['name']} 的接口不接受比例参数，已在提示词末尾注明「画面比例 {ratio}」")
        if (prompt_mode or XYQ_PROMPT_MODES[0]) == XYQ_PROMPT_MODES[0]:
            send_prompt = _xyq_verbatim(send_prompt, len(assets))
        res = xyq.run_job("image", key, m["name"], prompt, lambda: (
            util.send_progress(unique_id, f"提交小云雀 {m['name']} …"),
            xyq.submit_image(send_prompt, m, send_ratio, _opt(resolution), _opt(quality), count, assets))[1], unique_id)
        job = res["job"]
        if not res["images"]:
            raise CloudGenError(f"小云雀任务完成但没有图片结果。{'; '.join(res['texts'])} 查看：{job['link']}")
        blobs = [util.download(u) for u in res["images"]]
        for b in blobs:
            util.save_blob(b, "cloudgen", "xyq", ".png")
        after = _balance_safe(xyq.get_balance)
        _record("xyq", "image", m["name"], resolution, before, after, None, count, res.get("job_id"))
        info = "\n".join(x for x in [f"小云雀 · {m['name']} · {len(blobs)} 张", _cost_line(before, after),
                                     f"thread_id={job['thread_id']}", job["link"]] if x)
        util.send_progress(unique_id, _cost_line(before, after) or "完成")
        return {"ui": {"cloudgen_info": [info]}, "result": (util.images_from_bytes(blobs), info)}


class XYQVideo(_ListInput):
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (xyq.cached_model_names("video"),),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "mode": (XYQ_MODES, {"default": "自动", "tooltip": "自动：有首尾帧→首尾帧；有参考图/视频→参考生成；否则文生视频", "advanced": True}),
                "ratio": (XYQ_RATIOS, {"default": DEFAULT}),
                "resolution": (XYQ_VID_RES, {"default": DEFAULT}),
                "duration": ("INT", {"default": 0, "min": 0, "max": 120, "tooltip": "秒，0=模型默认"}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 0x7FFFFFFF, "tooltip": "-1=随机（不传）", "advanced": True}),
            },
            "optional": {
                "first_frame": ("IMAGE",),
                "last_frame": ("IMAGE",),
                "ref_images": ("IMAGE", {"tooltip": "参考图（可多张 batch）"}),
                "ref_video_path": ("STRING", {"default": "", "tooltip": "参考/待编辑视频的本地路径，多个用换行分隔", "advanced": True}),
                "audio_path": ("STRING", {"default": "", "tooltip": "参考音频 mp3/wav 本地路径", "advanced": True}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "video_path", "info")
    CATEGORY = CAT

    @classmethod
    def VALIDATE_INPUTS(cls, model, ratio, resolution, mode):
        return True

    def run(self, model, prompt, mode, ratio, resolution, duration, seed, first_frame=None, last_frame=None,
            ref_images=None, ref_video_path="", audio_path="", unique_id=None):
        m = xyq.find_model("video", model)
        before = _balance_safe(xyq.get_balance)
        imgs, vids, auds = [], [], []
        if first_frame is not None:
            imgs.append(xyq.upload_bytes(util.tensor_to_png_bytes(first_frame), "first.png"))
        if last_frame is not None:
            imgs.append(xyq.upload_bytes(util.tensor_to_png_bytes(last_frame), "last.png"))
        for n, (t, i) in enumerate(_iter_images(ref_images), 1):
            imgs.append(xyq.upload_bytes(util.tensor_to_png_bytes(t, i), f"ref_{n}.png"))
        for p in [x.strip().strip('"') for x in (ref_video_path or "").splitlines() if x.strip()]:
            util.send_progress(unique_id, f"上传参考视频 {p}")
            vids.append(xyq.upload_path(p))
        for p in [x.strip().strip('"') for x in (audio_path or "").splitlines() if x.strip()]:
            auds.append(xyq.upload_path(p))
        if mode == "自动":
            if first_frame is not None and last_frame is not None and ref_images is None:
                mode = "first_last_frame"
            elif imgs or vids or auds:
                mode = "reference_generation"
            else:
                mode = "text_to_video"
        from . import jobs
        key = jobs.make_key("xyq", "video", m["key"], prompt, mode, ratio, resolution, duration, seed, imgs, vids, auds)
        res = xyq.run_job("video", key, m["name"], prompt, lambda: (
            util.send_progress(unique_id, f"提交小云雀 {m['name']}（{mode}）…"),
            xyq.submit_video(_xyq_mentions(prompt), m, _opt(ratio), _opt(resolution), duration or None, mode,
                             seed if seed >= 0 else None, imgs, vids, auds))[1], unique_id)
        job = res["job"]
        if not res["videos"]:
            raise CloudGenError(f"小云雀任务完成但没有视频结果。{'; '.join(res['texts'])} 查看：{job['link']}")
        path = util.save_blob(util.download(res["videos"][0], timeout=900), "cloudgen", "xyq", ".mp4")
        after = _balance_safe(xyq.get_balance)
        _record("xyq", "video", m["name"], resolution, before, after, duration or None, 1, res.get("job_id"))
        info = "\n".join(x for x in [f"小云雀 · {m['name']} · {mode}", _cost_line(before, after), path,
                                     f"thread_id={job['thread_id']}", job["link"]] if x)
        util.send_progress(unique_id, _cost_line(before, after) or "完成")
        return {"ui": {"cloudgen_info": [info]}, "result": (util.video_object(path), path, info)}


# ======================================================================= LibTV
def _libtv_ratio(r: str) -> tuple[str, str]:
    if "(" in r:
        base, res = r.split("(", 1)
        return base, res.rstrip(")").upper()
    return r, ""


def _libtv_refs(images, urls_text, unique_id) -> list[str]:
    """参考图上传到 LibTV 项目画布，返回 image 节点 ID。"""
    nodes = []
    for n, (t, i) in enumerate(_iter_images(images), 1):
        util.send_progress(unique_id, f"上传参考图 {n} 到 LibTV")
        nodes.append(libtv.upload_image_bytes(util.tensor_to_png_bytes(t, i), f"ref_{n}.png"))
    for j, u in enumerate(_split_urls(urls_text)):
        util.send_progress(unique_id, f"下载并上传参考图链接 {j + 1}")
        nodes.append(libtv.upload_image_bytes(util.download(u), f"url_{j + 1}.png"))
    return nodes


def _mention_refs(prompt: str, refs: list) -> str:
    """提示词里的 <Picture N> 换成 LibTV 的节点引用 {{Node <id>}}（按参考图接入顺序）。"""
    import re
    if not refs:
        return prompt

    def rep(m):
        n = int(m.group(1))
        return "{{Node %s}}" % refs[n - 1] if 1 <= n <= len(refs) else m.group(0)
    return re.sub(r"<Picture\s+(\d+)>", rep, prompt, flags=re.I)


def _xyq_mentions(prompt: str) -> str:
    """<Picture N> / <Video N> / <Audio N> → 小云雀能理解的 图片N / 视频N / 音频N（按上传顺序）。"""
    import re
    names = {"picture": "图片", "video": "视频", "audio": "音频"}
    # 后面紧跟数字/字母时补一个空格，免得 "图片1" + "2个人" 连成 "图片12个人"
    return re.sub(r"<(Picture|Video|Audio)\s+(\d+)>(?=([0-9A-Za-z])?)",
                  lambda m: names[m.group(1).lower()] + m.group(2) + (" " if m.group(3) else ""), prompt, flags=re.I)


def _xyq_verbatim(prompt: str, n_refs: int) -> str:
    """小云雀生图走智能体（pippit_nest_agent）：它会先改写提示词再调用生图工具，常改掉主体和构图。
    这里把要求写在消息里，让它把原文原样作为生图 Prompt。"""
    refs = f"参考图按上传顺序依次是 图片1…图片{n_refs}，提示词里的「图片N」即第 N 张。" if n_refs else ""
    return ("【执行要求】请把下面【提示词】部分一字不改地作为生图工具的 Prompt 使用：不要改写、扩写、翻译或删减，"
            "不要自行设定主视觉、主体位置或构图，不要追加风格词。" + refs + "\n【提示词】\n" + prompt.strip())


def _fit(det: dict, canon: dict, extra: dict, unique_id) -> dict:
    params, warns = libtv.fit_model_params(canon, det.get("options") or {})
    for w in warns:
        util.send_progress(unique_id, "⚠ " + w)
    return libtv.deep_merge(params, extra.get("params", extra) if isinstance(extra, dict) else {})


class LibTVImage(_ListInput):
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (libtv.cached_model_names("image"),),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "quality": (LIBTV_QUALITY, {"default": "自动", "tooltip": "选项随模型变化（如 自动/高/中/低 或 1K/2K/4K）"}),
                "ratio": (LIBTV_RATIOS, {"default": "auto"}),
                "count": ("INT", {"default": 1, "min": 1, "max": 8}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0x7FFFFFFF,
                                 "tooltip": "不传给服务端。改 seed = 新任务（重新生成并扣费）；seed 和其它参数都不变时，中断后再运行会续接原任务、不重复扣费", "advanced": True}),
            },
            "optional": {
                "images": ("IMAGE", {"tooltip": "参考图（可多张 batch 或列表），提示词里输入 @ 引用"}),
                "ref_urls": ("STRING", {"default": "", "multiline": False, "tooltip": "参考图 URL，多个用逗号分隔", "advanced": True}),
                "extra_json": ("STRING", {"default": "", "multiline": True, "advanced": True,
                                          "tooltip": "额外的模型参数 JSON（合并进 params），如 {\"search_enabled\": 0}"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "info")
    CATEGORY = CAT

    @classmethod
    def VALIDATE_INPUTS(cls, model, quality, ratio):
        return True

    def run(self, model, prompt, quality, ratio, count, seed, images=None, ref_urls="", extra_json="", unique_id=None):
        if not prompt.strip():
            raise CloudGenError("prompt 不能为空")
        extra = _parse_extra(extra_json)
        det = libtv.model_detail("image", model)
        refs = _libtv_refs(images, ref_urls, unique_id)
        r, res = _libtv_ratio(ratio)
        q = LIBTV_QUALITY_MAP.get(quality, quality)
        opts = det.get("options") or {}
        qopt = libtv.canonical_option(opts, "quality")
        ropt = libtv.canonical_option(opts, "resolution")
        canon = {"ratio": r, "count": count}
        if qopt:
            canon["quality"] = res if res and res in map(str, qopt.get("options") or []) else q
            if res:
                canon["resolution"] = res
        elif ropt:  # 例：1K/2K/4K 在这类模型里叫 resolution
            canon["resolution"] = res or (q if q in map(str, ropt.get("options") or []) else None)
        params = _fit(det, canon, extra, unique_id)
        out = libtv.generate_image(det, _mention_refs(prompt.strip(), refs), params, refs, unique_id, seed=seed)
        if not out["images"]:
            raise CloudGenError(f"LibTV 没有返回图片：{json.dumps(out['raw'], ensure_ascii=False)[:400]}")
        blobs = [util.download(u) for u in out["images"]]
        for b in blobs:
            util.save_blob(b, "cloudgen", "libtv", ".png")
        charge = _libtv_charge("image", det, quality, out["raw"], None, count, out.get("job_id"))
        info = "\n".join(x for x in [f"LibTV · {det['name']} · {len(blobs)} 张", charge,
                                     f"params={json.dumps(params, ensure_ascii=False)}", f"task_id={out.get('task_id')}"] if x)
        util.send_progress(unique_id, charge)
        return {"ui": {"cloudgen_info": [info]}, "result": (util.images_from_bytes(blobs), info)}


class LibTVVideo(_ListInput):
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (libtv.cached_model_names("video"),),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "ratio": (["adaptive"] + LIBTV_RATIOS[1:9], {"default": "16:9"}),
                "resolution": (LIBTV_VID_RES, {"default": DEFAULT}),
                "duration": ("INT", {"default": 5, "min": 0, "max": 120, "tooltip": "秒，0=模型默认"}),
                "sound": ("BOOLEAN", {"default": True, "tooltip": "生成音频（模型支持时）"}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 0x7FFFFFFF, "tooltip": "-1=随机（不传）", "advanced": True}),
            },
            "optional": {
                "first_frame": ("IMAGE", {"tooltip": "只接首帧=图生视频；首尾帧都接=首尾帧"}),
                "last_frame": ("IMAGE",),
                "ref_images": ("IMAGE", {"tooltip": "多图参考（不能和首尾帧同时用）"}),
                "ref_urls": ("STRING", {"default": "", "tooltip": "参考图 URL，多个用逗号分隔", "advanced": True}),
                "extra_json": ("STRING", {"default": "", "multiline": True, "advanced": True}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "video_path", "info")
    CATEGORY = CAT

    @classmethod
    def VALIDATE_INPUTS(cls, model, ratio, resolution):
        return True

    def run(self, model, prompt, ratio, resolution, duration, sound, seed, first_frame=None, last_frame=None,
            ref_images=None, ref_urls="", extra_json="", unique_id=None):
        if not prompt.strip():
            raise CloudGenError("prompt 不能为空")
        if last_frame is not None and first_frame is None:
            raise CloudGenError("接了尾帧就必须同时接首帧")
        extra = _parse_extra(extra_json)
        det = libtv.model_detail("video", model)
        start = end = None
        if first_frame is not None:
            start = libtv.upload_image_bytes(util.tensor_to_png_bytes(first_frame), "first.png")
        if last_frame is not None:
            end = libtv.upload_image_bytes(util.tensor_to_png_bytes(last_frame), "last.png")
        refs = [] if start else _libtv_refs(ref_images, ref_urls, unique_id)
        canon = {"ratio": ratio, "resolution": _opt(resolution), "duration": duration or None, "sound": sound,
                 "seed": seed if seed >= 0 else None}
        params = _fit(det, canon, extra, unique_id)
        out = libtv.generate_video(det, _mention_refs(prompt.strip(), refs), params, start, end, refs, unique_id, seed=seed)
        if not out["videos"]:
            raise CloudGenError(f"LibTV 没有返回视频：{json.dumps(out['raw'], ensure_ascii=False)[:400]}")
        path = util.save_blob(util.download(out["videos"][0], timeout=900), "cloudgen", "libtv", ".mp4")
        charge = _libtv_charge("video", det, resolution, out["raw"], duration or None, 1, out.get("job_id"))
        info = "\n".join(x for x in [f"LibTV · {det['name']}", charge, path,
                                     f"params={json.dumps(params, ensure_ascii=False)}", f"task_id={out.get('task_id')}"] if x)
        util.send_progress(unique_id, charge)
        return {"ui": {"cloudgen_info": [info]}, "result": (util.video_object(path), path, info)}


# ======================================================================= Lovart
LOVART_RATIOS = [DEFAULT, "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"]
LOVART_VID_RATIOS = [DEFAULT, "16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]


def _lovart_balance():
    return _balance_safe(lovart.account)


class LovartImage(_ListInput):
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (lovart.cached_model_names("image"), {"default": DEFAULT, "tooltip": "默认=Lovart 当前的默认图片模型"}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "aspect_ratio": (LOVART_RATIOS, {"default": DEFAULT}),
                "count": ("INT", {"default": 1, "min": 1, "max": 8}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0x7FFFFFFF, "advanced": True,
                                 "tooltip": "不传给服务端。改 seed = 新任务（重新生成并扣费）；参数都不变时，中断后再运行会续接原任务、不重复扣费"}),
            },
            "optional": {
                "ref_images": ("IMAGE", {"tooltip": "参考图（可多张 batch 或列表），会先上传到 Lovart"}),
                "extra_json": ("STRING", {"default": "", "multiline": True, "advanced": True,
                                          "tooltip": "模型专属参数 JSON（作为 params 传入），字段名见 describe_model，如 {\"quality\": \"high\"}"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "info")
    CATEGORY = CAT

    @classmethod
    def VALIDATE_INPUTS(cls, model):
        return True

    def run(self, model, prompt, aspect_ratio, count, seed, ref_images=None, extra_json="", unique_id=None):
        if not prompt.strip():
            raise CloudGenError("prompt 不能为空")
        params = _parse_extra(extra_json)
        name = lovart.resolve_model("image", model)
        refs = lovart.refs_from_images(ref_images, unique_id)
        args = {"prompt": prompt.strip(), "aspect_ratio": _opt(aspect_ratio), "n": count, "ref_images": refs, "params": params}
        before = _lovart_balance()
        out = lovart.generate("image", name, args, prompt, unique_id, seed=seed)
        arts = [a for a in out["artifacts"] if a.get("type", "image") == "image"] or out["artifacts"]
        blobs = [util.download(a["url"]) for a in arts]
        for b in blobs:
            util.save_blob(b, "cloudgen", "lovart", ".png")
        after = _lovart_balance()
        _record("lovart", "image", name or DEFAULT, aspect_ratio, before, after, None, count, out.get("job_id"))
        cost = _cost_line(before, after)
        info = "\n".join(x for x in [f"Lovart · {name or DEFAULT} · {len(blobs)} 张", cost,
                                     f"task_ids={','.join(out['task_ids'])}", out["raw"].get("edit_url") or ""] if x)
        util.send_progress(unique_id, cost or "完成")
        return {"ui": {"cloudgen_info": [info]}, "result": (util.images_from_bytes(blobs), info)}


class LovartVideo(_ListInput):
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (lovart.cached_model_names("video"), {"default": DEFAULT, "tooltip": "默认=Lovart 当前的默认视频模型（视频模型多数需要 Lovart 会员）"}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "aspect_ratio": (LOVART_VID_RATIOS, {"default": DEFAULT}),
                "duration": ("INT", {"default": 5, "min": 0, "max": 60, "tooltip": "秒，0=模型默认；可选值因模型而异"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0x7FFFFFFF, "advanced": True,
                                 "tooltip": "不传给服务端。改 seed = 新任务（重新生成并扣费）"}),
            },
            "optional": {
                "first_frame": ("IMAGE", {"tooltip": "首帧（图生视频）"}),
                "last_frame": ("IMAGE", {"tooltip": "尾帧（需同时接首帧，模型需支持首尾帧）"}),
                "ref_images": ("IMAGE", {"tooltip": "主体/风格参考图"}),
                "extra_json": ("STRING", {"default": "", "multiline": True, "advanced": True,
                                          "tooltip": "模型专属参数 JSON（作为 params 传入），字段名见 describe_model"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "video_path", "info")
    CATEGORY = CAT

    @classmethod
    def VALIDATE_INPUTS(cls, model):
        return True

    def run(self, model, prompt, aspect_ratio, duration, seed, first_frame=None, last_frame=None,
            ref_images=None, extra_json="", unique_id=None):
        if not prompt.strip() and first_frame is None:
            raise CloudGenError("prompt 和首帧至少要有一个")
        if last_frame is not None and first_frame is None:
            raise CloudGenError("接了尾帧就必须同时接首帧")
        params = _parse_extra(extra_json)
        name = lovart.resolve_model("video", model)
        start = lovart.upload_frame(first_frame, "first.png")
        end = lovart.upload_frame(last_frame, "last.png")
        refs = [] if start else lovart.refs_from_images(ref_images, unique_id)
        args = {"prompt": prompt.strip(), "aspect_ratio": _opt(aspect_ratio), "duration": duration or None,
                "first_frame": start, "last_frame": end, "ref_images": refs, "params": params}
        before = _lovart_balance()
        out = lovart.generate("video", name, args, prompt, unique_id, seed=seed)
        vids = [a for a in out["artifacts"] if a.get("type") == "video"] or out["artifacts"]
        path = util.save_blob(util.download(vids[0]["url"], timeout=900), "cloudgen", "lovart", ".mp4")
        after = _lovart_balance()
        _record("lovart", "video", name or DEFAULT, aspect_ratio, before, after, duration or None, 1, out.get("job_id"))
        cost = _cost_line(before, after)
        info = "\n".join(x for x in [f"Lovart · {name or DEFAULT}", cost, path,
                                     f"task_ids={','.join(out['task_ids'])}", out["raw"].get("edit_url") or ""] if x)
        util.send_progress(unique_id, cost or "完成")
        return {"ui": {"cloudgen_info": [info]}, "result": (util.video_object(path), path, info)}



# ======================================================================= 积分
def _configured_providers() -> list[tuple[str, str, object]]:
    """只返回已配置的平台（小云雀有 Access Key、LibTV / Lovart 已授权），未配置的不在侧边栏出现。"""
    items = []
    if xyq.access_key():
        items.append(("xyq", "小云雀", xyq.get_balance))
    if libtv_auth.status()["authorized"]:
        items.append(("libtv", "LibTV", libtv.account))
    if lovart.configured():
        items.append(("lovart", "Lovart", lovart.account))
    return items


def all_balances() -> list[dict]:
    out = []
    for prov, name, fn in _configured_providers():
        try:
            d = fn()
            d.pop("raw", None)
            out.append(d)
        except Exception as exc:  # noqa: BLE001
            out.append({"provider": prov, "name": name, "ok": False,
                        "balance": "—", "expire": "", "error": str(exc), "extra": {}})
    flow_items = []
    if flow.api_key() or util.load_config().get("flow_base_url"):
        flow_items.append(flow.status())
    for d in out:
        if d["provider"] not in ("xyq", "libtv"):
            continue
        try:
            d["plan"] = plans.status(d["provider"], d.get("balance") if d.get("ok") else None)
        except Exception as exc:  # noqa: BLE001
            d["plan"] = {"error": str(exc)}
    return out + flow_items


def balances_text(items: list[dict]) -> str:
    lines = []
    for d in items:
        pl = d.get("plan") or {}
        if d.get("provider") == "flow":
            f = d.get("flow") or {}
            lines.append(f"Flow2API：{'在线' if d.get('ok') else d.get('error', '不可用')}" +
                         (f"，可用账号 {f.get('active', 0)}/{f.get('total', 0)}" if f else ""))
            continue
        if d.get("ok") and d.get("balance_source") == "none":
            s = f"{d['name']}：余额未录入（接口不提供，在 ⚙ 配置 → 套餐 里填）"
        elif d.get("ok"):
            s = f"{d['name']}：剩余 {d['balance']} 积分" + ("（手动录入后估算）" if d.get("balance_source") == "manual" else "")
            if pl.get("monthly_quota"):
                s += f" / {pl['monthly_quota']}"
            if d.get("expire") and not pl.get("plan_expire_text"):
                s += f"，到期 {d['expire']}"
        else:
            s = f"{d['name']}：{d.get('error', '不可用')}"
        lines.append(s)
        extra = plans.text_line(d["name"], pl)
        if extra:
            lines.append(extra)
        if pl.get("warn"):
            lines.append("  ⚠ " + pl["warn"])
    lines.append(time.strftime("更新于 %H:%M:%S"))
    return "\n".join(lines)


class CloudCredits:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"refresh": ("INT", {"default": 0, "min": 0, "max": 0x7FFFFFFF,
                                                 "tooltip": "改动数值即重新查询"})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "run"
    CATEGORY = CAT
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, refresh):
        return time.time()

    def run(self, refresh):
        items = all_balances()
        text = balances_text(items)
        return {"ui": {"cloudgen_info": [text], "cloudgen_balances": [json.dumps(items, ensure_ascii=False)]},
                "result": (text,)}


# ======================================================================= Flow2API
FLOW_IMG_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4"]
FLOW_VID_MODES = ["自动", "文生视频", "首尾帧", "参考图生成"]


def _flow_job(kind, model_name, prompt, key):
    from . import jobs
    return jobs.create("flow", kind, key, model_name, prompt)


class FlowImage(_ListInput):
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (flow.cached_model_names("image"),),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "ratio": (FLOW_IMG_RATIOS, {"default": "16:9"}),
                "resolution": (["1K", "2K", "4K"], {"default": "1K", "tooltip": "2K/4K 由 Flow 先出图再放大"}),
                "count": ("INT", {"default": 1, "min": 1, "max": 4, "tooltip": "Flow2API 每次请求出 1 张，多张会依次请求"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0x7FFFFFFF,
                                 "tooltip": "不传给服务端，只用于让 ComfyUI 重新执行", "advanced": True}),
            },
            "optional": {"images": ("IMAGE", {"tooltip": "参考图（可多张 batch 或列表），提示词里输入 @ 引用"})},
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "info")
    CATEGORY = CAT

    @classmethod
    def VALIDATE_INPUTS(cls, model, ratio, resolution):
        return True

    def run(self, model, prompt, ratio, resolution, count, seed, images=None, unique_id=None):
        if not prompt.strip():
            raise CloudGenError("prompt 不能为空")
        from . import jobs
        m = flow.find_model("image", model)
        mid = flow.image_model_id(m, ratio, resolution)
        refs = [util.tensor_to_png_bytes(t, i) for t, i in _iter_images(images)]
        text = _xyq_mentions(prompt.strip())
        jid = _flow_job("image", m["name"], prompt, jobs.make_key("flow", "image", mid, prompt, count, seed, len(refs)))
        blobs, urls = [], []
        try:
            ref_urls = flow.image_refs(refs, unique_id)
            for n in range(max(1, count)):
                util.send_progress(unique_id, "生成中" + (f"（第 {n + 1}/{count} 张）" if count > 1 else "") + "…")
                out = flow.chat_generate(mid, text, ref_urls, unique_id, timeout=900)
                u = out["urls"][-1]
                blobs.append(flow.fetch(u)); urls.append(u)
        except CloudGenError as exc:
            jobs.update(jid, status="failed", error=str(exc)[:300])
            raise
        for b in blobs:
            util.save_blob(b, "cloudgen", "flow", ".png")
        jobs.update(jid, status="done", urls=urls)
        info = "\n".join([f"Flow2API · {m['name']} · {mid} · {len(blobs)} 张"] + [u for u in urls if not u.startswith("data:")])
        util.send_progress(unique_id, f"完成 · {len(blobs)} 张")
        return {"ui": {"cloudgen_info": [f"完成 · {len(blobs)} 张"]}, "result": (util.images_from_bytes(blobs), info)}


class FlowVideo(_ListInput):
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (flow.cached_model_names("video"),),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "mode": (FLOW_VID_MODES, {"default": "自动", "advanced": True,
                                          "tooltip": "自动：接了首帧→首尾帧；接了参考图→参考图生成；否则文生视频"}),
                "ratio": (["16:9", "9:16"], {"default": "16:9"}),
                "duration": ([4, 6, 8, 10], {"default": 8, "tooltip": "可选时长随模型和模式变化（首尾帧 4/6 秒，参考图 8 秒）"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0x7FFFFFFF,
                                 "tooltip": "不传给服务端，只用于让 ComfyUI 重新执行", "advanced": True}),
            },
            "optional": {
                "first_frame": ("IMAGE",),
                "last_frame": ("IMAGE",),
                "ref_images": ("IMAGE", {"tooltip": "参考图（最多 3 张，可用 batch 或列表）"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "video_path", "info")
    CATEGORY = CAT

    @classmethod
    def VALIDATE_INPUTS(cls, model, ratio, duration, mode):
        return True

    def run(self, model, prompt, mode, ratio, duration, seed, first_frame=None, last_frame=None, ref_images=None,
            unique_id=None):
        if not prompt.strip():
            raise CloudGenError("prompt 不能为空")
        if last_frame is not None and first_frame is None:
            raise CloudGenError("接了尾帧就必须同时接首帧")
        from . import jobs
        m = flow.find_model("video", model)
        refs = [util.tensor_to_png_bytes(t, i) for t, i in _iter_images(ref_images)]
        typ = {"文生视频": "t2v", "首尾帧": "i2v", "参考图生成": "r2v"}.get(mode)
        if not typ:
            typ = "i2v" if first_frame is not None else ("r2v" if refs else "t2v")
        imgs: list[bytes] = []
        if typ == "i2v":
            if first_frame is None:
                raise CloudGenError("首尾帧模式需要接首帧")
            imgs = [util.tensor_to_png_bytes(first_frame)] + ([util.tensor_to_png_bytes(last_frame)] if last_frame is not None else [])
        elif typ == "r2v" or (typ == "t2v" and refs and m["key"].startswith("omni")):
            if not refs:
                raise CloudGenError("参考图生成需要接 ref_images")
            if len(refs) > 3:
                util.send_progress(unique_id, f"⚠ 参考图最多 3 张，只用前 3 张（共 {len(refs)} 张）")
            imgs = refs[:3]
        if typ == "t2v" and m["key"].startswith("omni"):
            mid = flow.video_model_id(m, "t2v", int(duration), ratio)  # Omni 同一个模型可带 0~3 张参考图
        else:
            mid = flow.video_model_id(m, typ, int(duration), ratio)
        jid = _flow_job("video", m["name"], prompt, jobs.make_key("flow", "video", mid, prompt, seed, len(imgs)))
        try:
            ref_urls = flow.image_refs(imgs, unique_id)
            util.send_progress(unique_id, "生成中…")
            out = flow.chat_generate(mid, _xyq_mentions(prompt.strip()), ref_urls, unique_id, timeout=1800)
            vids = [u for u in out["urls"] if re.search(r"\.(mp4|webm|mov)(\?|$)|video", u, re.I)] or out["urls"]
            data = flow.fetch(vids[-1], timeout=900)
        except CloudGenError as exc:
            jobs.update(jid, status="failed", error=str(exc)[:300])
            raise
        path = util.save_blob(data, "cloudgen", "flow", ".mp4")
        jobs.update(jid, status="done", urls=[vids[-1]])
        info = f"Flow2API · {m['name']} · {mid}\n{path}\n{vids[-1] if not vids[-1].startswith('data:') else ''}".strip()
        util.send_progress(unique_id, "完成")
        return {"ui": {"cloudgen_info": ["完成"]}, "result": (util.video_object(path), path, info)}


NODE_CLASS_MAPPINGS = {
    "CloudGen_XYQ_Image": XYQImage,
    "CloudGen_XYQ_Video": XYQVideo,
    "CloudGen_LibTV_Image": LibTVImage,
    "CloudGen_LibTV_Video": LibTVVideo,
    "CloudGen_Lovart_Image": LovartImage,
    "CloudGen_Lovart_Video": LovartVideo,
    "CloudGen_Credits": CloudCredits,
    "CloudGen_Flow_Image": FlowImage,
    "CloudGen_Flow_Video": FlowVideo,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "CloudGen_XYQ_Image": "小云雀 · 图片生成",
    "CloudGen_XYQ_Video": "小云雀 · 视频生成",
    "CloudGen_LibTV_Image": "LibTV · 图片生成",
    "CloudGen_LibTV_Video": "LibTV · 视频生成",
    "CloudGen_Lovart_Image": "Lovart · 图片生成",
    "CloudGen_Lovart_Video": "Lovart · 视频生成",
    "CloudGen_Credits": "云端积分 · 小云雀 / LibTV / Lovart",
    "CloudGen_Flow_Image": "Flow2API · 图片生成",
    "CloudGen_Flow_Video": "Flow2API · 视频生成",
}
