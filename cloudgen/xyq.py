"""小云雀（xyq.jianying.com）Skill OpenAPI 客户端。

接口与参数照搬官方 pippit-tool-cli（github.com/Pippit-dev/cli）：
  /api/biz/v1/skill/get_credit_balance        积分余额
  /api/biz/v1/skill/get_available_model_list  模型与参数配置（scene 区分图片/视频）
  /api/biz/v1/skill/upload_file               上传参考素材 -> pippit_asset_id
  /api/biz/v1/skill/submit_run                提交生成
  /api/biz/v1/skill/get_thread                轮询结果
鉴权：Authorization: Bearer <Access Key>（小云雀官网顶部【CLI/API】-【API】新建秘钥）。
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from typing import Any

from . import util
from .util import CloudGenError

DEFAULT_BASE = "https://xyq.jianying.com"
P_BALANCE = "/api/biz/v1/skill/get_credit_balance"
P_MODELS = "/api/biz/v1/skill/get_available_model_list"
P_UPLOAD = "/api/biz/v1/skill/upload_file"
P_SUBMIT = "/api/biz/v1/skill/submit_run"
P_THREAD = "/api/biz/v1/skill/get_thread"
P_TEAMS = "/api/biz/v1/skill/get_lite_team_list"
P_RESULT = "/api/biz/v1/agent/query_generate_video_result"  # 官方文档的结果查询接口（视频/图片 URL 直出）

# 官方「传参&错误码说明」里的业务码
RET_HINT = {"2": "参数或 Access Key 无效", "5": "下游服务错误，可重试", "10": "服务器繁忙被限流，稍后再试",
            "15": "已达当日生成上限", "16010": "操作过于频繁，请一分钟后再试", "12001": "安全审核发起失败，可重试",
            "12004": "提示词未通过安审", "12005": "参考图片未通过安审", "12006": "参考视频未通过安审",
            "12015": "参考音频未通过安审", "11001": "积分不足"}

SCENE = {"image": "web_image_agent", "video": "web_turbo_video_generator"}
AGENT_IMAGE = "pippit_nest_agent"
AGENT_VIDEO = "pippit_video_part_agent"

RATIO_ENUM = {0: "adaptive", 2: "16:9", 3: "9:16", 4: "4:3", 5: "3:4", 6: "1:1", 7: "2:1", 8: "2.35:1",
              9: "1.85:1", 10: "1.125:2.436", 11: "3:2", 12: "2:3", 13: "21:9"}
RATIO_LABEL_TO_ENUM = {v: k for k, v in RATIO_ENUM.items()}

# 加载期没有网络/没配置 Key 时的兜底模型名（会被服务端真实列表覆盖）
FALLBACK_MODELS = {"image": ["(请先配置小云雀 Access Key)"], "video": ["(请先配置小云雀 Access Key)"]}

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 600


def access_key() -> str:
    return (os.environ.get("XYQ_ACCESS_KEY") or util.load_config().get("xyq_access_key") or "").strip()


def base_url() -> str:
    return (util.load_config().get("xyq_base_url") or DEFAULT_BASE).rstrip("/")


def _headers() -> dict:
    ak = access_key()
    if not ak:
        raise CloudGenError("未配置小云雀 Access Key：在侧边栏「云端积分」面板里填写，或设置环境变量 XYQ_ACCESS_KEY")
    h = {"Authorization": f"Bearer {ak}"}
    team = str(util.load_config().get("xyq_team_id") or "").strip()
    if team and team != "0":
        h["X-Team-Id"] = team
    return h


def _call(path: str, body: dict, timeout: float = 60) -> dict:
    status, data = util.post_json(base_url() + path, body, headers=_headers(), timeout=timeout)
    if status >= 400:
        raise CloudGenError(f"小云雀 HTTP {status}: {str(data)[:300]}")
    if not isinstance(data, dict):
        raise CloudGenError(f"小云雀返回无法解析：{str(data)[:300]}")
    if str(data.get("ret", "")) != "0":
        ret = str(data.get("ret"))
        hint = RET_HINT.get(ret, "")
        raise CloudGenError(f"小云雀接口失败 ret={ret} {hint + '：' if hint else ''}{data.get('errmsg', '')} (log_id={data.get('log_id', '')})")
    d = data.get("data")
    return d if isinstance(d, dict) else {"_raw": d}


# ---------------------------------------------------------------- account
def get_balance() -> dict:
    d = _call(P_BALANCE, {})
    remain = d.get("total_remain_amount")
    out = {"provider": "xyq", "name": "小云雀", "ok": True, "balance": util.fmt_num(remain) if remain is not None else "?",
           "expire": "", "extra": {}}
    # 服务端若附带到期/会员字段，一并展示
    exp = util.find_first(d, ("expire_time", "expired_at", "expire_at", "vip_expire_time", "end_time", "deadline"))
    if exp:
        out["expire"] = util.fmt_time(exp)
    for k, v in d.items():
        if k != "total_remain_amount" and isinstance(v, (str, int, float)):
            out["extra"][k] = v
    return out


def list_teams() -> list:
    try:
        d = _call(P_TEAMS, {})
    except CloudGenError:
        return []
    for _, _, v in util.walk(d):
        if isinstance(v, list) and v and isinstance(v[0], dict) and any("team" in k for k in v[0]):
            return v
    return []


# ---------------------------------------------------------------- models
def _dimension(dims: list, key: str) -> dict | None:
    for d in dims or []:
        if isinstance(d, dict) and d.get("key") == key:
            return d
    return None


def _dim_options(d: dict | None, upper=False) -> tuple[list, Any]:
    if not d:
        return [], None
    opts = []
    for o in d.get("option_list") or []:
        if isinstance(o, dict) and not o.get("disabled"):
            v = str(o.get("value", "")).strip()
            if v:
                opts.append(v.upper() if upper else v)
    default = d.get("default_value")
    if isinstance(default, str):
        default = default.upper() if upper else default
    return opts, default


def normalize_model(raw: dict, kind: str) -> dict:
    pc = raw.get("parameter_config") or {}
    dims = pc.get("dimensions") or []
    m = {
        "provider": "xyq", "kind": kind,
        "name": str(raw.get("name") or raw.get("key") or "").strip(),
        "key": str(raw.get("key") or "").strip(),
        "description": raw.get("description") or "",
        "cost": util.find_cost({k: v for k, v in raw.items() if k not in ("parameter_config",)}) or
                util.find_cost(pc.get("commerce_config_v2") or {}),
    }
    ratios = [RATIO_ENUM[r] for r in raw.get("supported_ratio_list") or [] if r in RATIO_ENUM and r != 1]
    rd = _dimension(dims, "ratio")
    if rd:
        vals, dflt = _dim_options(rd)
        conv = []
        for v in vals:
            try:
                lab = RATIO_ENUM.get(int(v))
            except ValueError:
                lab = v
            if lab and lab not in conv:
                conv.append(lab)
        ratios = conv or ratios
    m["ratios"] = ratios
    dr = raw.get("default_ratio")
    m["default_ratio"] = RATIO_ENUM.get(dr) if isinstance(dr, int) else None

    res, dres = _dim_options(_dimension(dims, "resolution"), upper=(kind == "image"))
    m["resolutions"], m["default_resolution"] = res, dres
    if kind == "image":
        eff, deff = _dim_options(_dimension(dims, "effort"))
        m["efforts"], m["default_effort"] = eff, deff
    else:
        dd = _dimension(dims, "duration")
        dur: dict = {}
        if dd:
            rc = dd.get("range_config") or {}
            if rc.get("min_value") is not None:
                dur = {"min": int(rc.get("min_value")), "max": int(rc.get("max_value") or rc.get("min_value")),
                       "step": int(rc.get("step") or 1)}
            else:
                opts, _ = _dim_options(dd)
                dur = {"options": [int(x) for x in opts if str(x).isdigit()]}
            if dd.get("default_value") not in (None, ""):
                try:
                    dur["default"] = int(dd.get("default_value"))
                except (TypeError, ValueError):
                    pass
        elif raw.get("supported_duration_list"):
            dur = {"options": [int(x) for x in raw["supported_duration_list"] if isinstance(x, (int, float))]}
        m["duration"] = dur
        modes = []
        for md in ((raw.get("creation_mode_config") or {}).get("modes") or []):
            if isinstance(md, dict) and md.get("key"):
                modes.append(md["key"])
        m["modes"] = modes
    limits = {}
    for k in ("total_limit", "image_total_limit", "video_total_limit", "audio_total_limit"):
        if k in raw:
            limits[k] = raw[k]
    m["limits"] = limits
    return m


def get_models(kind: str, refresh: bool = False) -> list[dict]:
    if kind not in SCENE:
        raise CloudGenError(f"未知类型 {kind}")
    ck = f"{kind}:{access_key()[-6:]}"
    with _cache_lock:
        hit = _cache.get(ck)
        if hit and not refresh and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    d = _call(P_MODELS, {"scene": SCENE[kind], "source_model_key": ""}, timeout=30)
    models = []
    for raw in ((d.get("config") or {}).get("models") or []):
        if isinstance(raw, dict) and (raw.get("kind") in (None, kind)):
            m = normalize_model(raw, kind)
            if m["key"]:
                models.append(m)
    with _cache_lock:
        _cache[ck] = (time.time(), models)
    return models


def cached_model_names(kind: str) -> list[str]:
    """INPUT_TYPES 用：只读缓存；无缓存时后台拉取并先返回兜底（前端会再刷新下拉框）。"""
    ck = f"{kind}:{access_key()[-6:]}"
    hit = _cache.get(ck)
    if hit:
        return [m["name"] for m in hit[1]] or FALLBACK_MODELS[kind]
    if access_key():
        prefetch(kind)
    return FALLBACK_MODELS[kind]


def prefetch(kind: str):
    """后台拉取模型列表，避免阻塞 ComfyUI 加载节点定义。"""
    def job():
        try:
            get_models(kind)
        except Exception as exc:  # noqa: BLE001
            print(f"[CloudGen] 小云雀模型列表获取失败：{exc}")
    threading.Thread(target=job, daemon=True).start()


def find_model(kind: str, name: str) -> dict:
    models = get_models(kind)
    for m in models:
        if name in (m["name"], m["key"]):
            return m
    models = get_models(kind, refresh=True)
    for m in models:
        if name in (m["name"], m["key"]):
            return m
    raise CloudGenError(f"小云雀没有找到模型「{name}」，可用：{', '.join(m['name'] for m in models)}")


# ---------------------------------------------------------------- upload
def upload_bytes(data: bytes, filename: str) -> str:
    """相同内容（SHA-1）只上传一次，复用之前拿到的 pippit_asset_id。"""
    team = str(util.load_config().get("xyq_team_id") or "0")
    key = f"xyq:{team}:{util.sha1(data)}"
    cached = util.asset_get(key)
    if cached:
        return cached
    aid = _upload_bytes(data, filename)
    util.asset_put(key, aid, filename)
    return aid


def _upload_bytes(data: bytes, filename: str) -> str:
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if filename.lower().endswith(".mp3"):
        ctype = "audio/mpeg"
    elif filename.lower().endswith(".wav"):
        ctype = "audio/wav"
    body, ct = util.multipart({}, "file", filename, data, ctype)
    hdrs = _headers()
    hdrs.update({"Content-Type": ct, "Accept": "application/json"})
    status, _, resp = util.http_request(base_url() + P_UPLOAD, method="POST", headers=hdrs, body=body, timeout=300)
    try:
        js = json.loads(resp.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise CloudGenError(f"小云雀上传返回无法解析 HTTP {status}: {resp[:200]!r}")
    if status >= 400 or str(js.get("ret")) != "0":
        raise CloudGenError(f"小云雀上传失败 {filename}: ret={js.get('ret')} {js.get('errmsg', '')} (HTTP {status})")
    d = js.get("data") or {}
    aid = d.get("pippit_asset_id") or d.get("asset_id")
    if not aid:
        raise CloudGenError("小云雀上传响应缺少 pippit_asset_id")
    return aid


def upload_path(path: str) -> str:
    with open(path, "rb") as fp:
        return upload_bytes(fp.read(), os.path.basename(path))


# ---------------------------------------------------------------- submit / poll
def submit_image(prompt: str, model: dict, ratio: str, resolution: str, effort: str, count: int,
                 asset_ids: list[str]) -> dict:
    settings: dict[str, Any] = {"image_model": model["key"]}
    # 只传模型在开放接口里声明支持的参数，没声明的传了会被拒（如 旗舰生图模型 不支持 image_effort）
    if ratio and ratio in RATIO_LABEL_TO_ENUM and model.get("ratios"):
        settings["ratio"] = RATIO_LABEL_TO_ENUM[ratio]
    if resolution and (not model.get("resolutions") or resolution.upper() in [str(r).upper() for r in model["resolutions"]]):
        settings["resolution"] = resolution.upper()
    if effort and effort.lower() in [str(e).lower() for e in model.get("efforts") or []]:
        settings["image_effort"] = effort.lower()
    if count and count > 0:
        settings["generate_image_count"] = int(count)
    body: dict[str, Any] = {"agent_name": AGENT_IMAGE, "message": prompt.strip(), "general_agent_settings": settings,
                            "platform": "comfyui"}
    if asset_ids:
        body["asset_ids"] = asset_ids
    return _submit(body)


def submit_video(prompt: str, model: dict, ratio: str, resolution: str, duration: int | None, mode: str,
                 seed: int | None, images: list[str], videos: list[str], audios: list[str]) -> dict:
    param: dict[str, Any] = {"prompt": prompt.strip(), "model": model["key"]}
    if images:
        param["images"] = [{"pippit_asset_id": a} for a in images]
    if videos:
        param["videos"] = [{"pippit_asset_id": a} for a in videos]
    if audios:
        param["audios"] = [{"pippit_asset_id": a} for a in audios]
    if not duration:  # 官方文档中 duration_sec 为必填：0 时取模型默认值
        du = model.get("duration") or {}
        duration = du.get("default") or (du.get("options") or [None])[0] or du.get("min") or 5
    param["duration_sec"] = int(duration)
    if ratio and (not model.get("ratios") or ratio in model["ratios"]):
        param["ratio"] = ratio
    if resolution and resolution.lower() in [str(r).lower() for r in model.get("resolutions") or []]:
        param["resolution"] = resolution.lower()
    if seed is not None and seed >= 0:
        param["seed"] = int(seed)
    if mode == "first_last_frame":
        param["generate_type"] = 1
    elif mode in ("text_to_video", "reference_generation"):
        param["generate_type"] = 0
    task_type = {"reference_generation": "reference", "video_edit": "edit", "video_extend": "extend"}.get(mode)
    if task_type:
        param["task_type"] = task_type
    body = {"agent_name": AGENT_VIDEO, "call_source": "pippit_cli", "message": prompt.strip(),
            "video_part_tool_param": param, "platform": "comfyui"}
    babi = json.dumps({"scene_lv1": "ai_agent", "scene_lv2": "front_tool", "tool_id": "instant_video",
                       "tab_name": "other", "edit_type": "instant_video", "enter_from": "skill"}, separators=(",", ":"))
    from urllib.parse import quote
    return _submit(body, path=P_SUBMIT + "?babi_param=" + quote(babi))


def _submit(body: dict, path: str = P_SUBMIT) -> dict:
    d = _call(path, body, timeout=120)
    run = d.get("run") or {}
    if not run.get("thread_id") or not run.get("run_id"):
        raise CloudGenError(f"小云雀提交响应缺少 thread_id/run_id：{json.dumps(d, ensure_ascii=False)[:300]}")
    return {"thread_id": run["thread_id"], "run_id": run["run_id"], "link": d.get("web_thread_link", "")}


def _state(v) -> str:
    s = str(v).strip().lower()
    if s in ("3", "completed", "success", "succeeded", "done"):
        return "done"
    if s in ("4", "failed", "error"):
        return "failed"
    if s in ("5", "canceled", "cancelled"):
        return "canceled"
    if s in ("9", "requires_action"):
        return "action"
    return "running"


def extract_results(run: dict) -> dict:
    run = util.deep_decode(run)
    images, videos, errors, texts = [], [], [], []
    for entry in run.get("entry_list") or []:
        for holder_key in ("artifact", "message"):
            holder = entry.get(holder_key) or {}
            for c in holder.get("content") or []:
                if not isinstance(c, dict):
                    continue
                st = c.get("sub_type") or c.get("subtype") or ""
                data = c.get("data")
                if st == "biz/x_data_video" and isinstance(data, dict) and isinstance(data.get("video"), dict):
                    u = data["video"].get("download_url") or data["video"].get("url")
                    if u:
                        videos.append(u)
                elif st == "biz/x_data_image" and isinstance(data, dict) and isinstance(data.get("image"), dict):
                    u = data["image"].get("url") or data["image"].get("download_url")
                    if u:
                        images.append(u)
                if isinstance(data, dict) and data.get("error_message"):
                    errors.append(str(data["error_message"]))
                if c.get("type") == "text" and isinstance(data, str) and holder.get("role") == "assistant":
                    texts.append(data.strip())
    if not images and not videos:
        assistant = [e for e in run.get("entry_list") or []
                     if ((e.get("artifact") or e.get("message") or {}).get("role") == "assistant")]
        found = util.find_media_urls(assistant)
        images, videos = found["image"], found["video"]
    return {"images": list(dict.fromkeys(images)), "videos": list(dict.fromkeys(videos)),
            "errors": errors, "texts": texts[-3:]}


def _fail_text(reason) -> str:
    if isinstance(reason, dict):
        code = str(reason.get("code", ""))
        msg = reason.get("message") or ""
        hint = RET_HINT.get(code, "")
        return f"{hint or msg}（code={code}{'，' + msg if hint and msg and msg != hint else ''}）" if code else str(msg)
    return str(reason or "")


def _poll_result(thread_id: str, run_id: str) -> dict | None:
    """官方结果查询接口；不可用时返回 None 交给 get_thread 兜底。"""
    try:
        d = _call(P_RESULT, {"thread_id": thread_id, "run_id": run_id}, timeout=60)
    except CloudGenError as exc:
        if "ret=" in str(exc) and any(f"ret={c} " in str(exc) for c in ("2", "15", "11001")):
            raise
        return None
    return d


def _poll_thread(thread_id: str, run_id: str) -> tuple[str, dict, dict]:
    d = _call(P_THREAD, {"thread_id": thread_id, "run_id": run_id, "after_seq": 0}, timeout=60)
    thread = util.deep_decode(d.get("thread") or {})
    run = next((r for r in thread.get("run_list") or [] if r.get("run_id") == run_id), None) or {}
    return _state(run.get("state")) if run else "running", extract_results(run) if run else {
        "images": [], "videos": [], "errors": [], "texts": []}, run


def wait_run(thread_id: str, run_id: str, node_id=None, timeout: float = 3600, interval: float = 8) -> dict:
    t0 = time.time()
    use_result_api = True
    while True:
        util.check_interrupt()
        st, res, run = "running", None, {}
        if use_result_api:
            d = _poll_result(thread_id, run_id)
            if d is None:
                use_result_api = False
            else:
                st = _state(d.get("run_state"))
                res = {"images": list(d.get("image_urls") or []), "videos": list(d.get("video_urls") or []),
                       "errors": [], "texts": []}
                if st in ("failed", "canceled"):
                    raise CloudGenError(f"小云雀任务{'失败' if st == 'failed' else '已取消'}：{_fail_text(d.get('fail_reason')) or '未知原因'}")
                if st == "done" and not (res["images"] or res["videos"]):
                    st, res, run = _poll_thread(thread_id, run_id)  # 结果接口没解析到产物时用会话详情补
        if not use_result_api:
            st, res, run = _poll_thread(thread_id, run_id)
            if st in ("failed", "canceled"):
                msg = run.get("fail_reason") or run.get("error_message") or run.get("errmsg") or "; ".join(res["errors"])
                raise CloudGenError(f"小云雀任务{'失败' if st == 'failed' else '已取消'}：{_fail_text(msg) or '未知原因'}")
        if st == "done":
            return res
        if st == "action" or (use_result_api and st == "running" and time.time() - t0 > 60 and _needs_action(thread_id, run_id)):
            _, r2, _ = _poll_thread(thread_id, run_id)
            raise CloudGenError(f"小云雀需要人工确认：{'; '.join(r2['texts']) or '请在网页端补充信息'}")
        elapsed = int(time.time() - t0)
        util.send_progress(node_id, f"小云雀生成中… {elapsed // 60}:{elapsed % 60:02d}")
        if elapsed > timeout:
            raise CloudGenError(f"小云雀任务超时（{timeout}s），可稍后在网页端查看 thread_id={thread_id}")
        for _ in range(int(interval * 2)):
            util.check_interrupt()
            time.sleep(0.5)


_action_checked: dict[str, float] = {}


def _needs_action(thread_id: str, run_id: str) -> bool:
    """结果接口不反映“等待用户确认”，每 60 秒用会话详情查一次。"""
    if time.time() - _action_checked.get(run_id, 0) < 60:
        return False
    _action_checked[run_id] = time.time()
    try:
        st, _, _ = _poll_thread(thread_id, run_id)
        return st == "action"
    except CloudGenError:
        return False


def run_job(kind: str, key: str, model_name: str, prompt: str, submit_fn, node_id=None) -> dict:
    """先查有没有相同参数、还没结束的任务：有就续接原 thread/run，不重新提交扣费。"""
    from . import jobs
    pend = jobs.find_pending(key)
    if pend:
        jid, j = pend
        job = {"thread_id": j["thread_id"], "run_id": j["run_id"], "link": j.get("link", "")}
        util.send_progress(node_id, f"续接未完成的小云雀任务（{j.get('created')} 提交）…")
    else:
        job = submit_fn()
        jid = jobs.create("xyq", kind, key, model_name, prompt, thread_id=job["thread_id"], run_id=job["run_id"],
                          link=job.get("link", ""))
    try:
        res = wait_run(job["thread_id"], job["run_id"], node_id)
    except CloudGenError as exc:
        jobs.update(jid, status="failed", error=str(exc)[:300])
        raise
    jobs.update(jid, status="done", urls=res["images"] + res["videos"])
    res["job"] = job
    res["job_id"] = jid
    return res
