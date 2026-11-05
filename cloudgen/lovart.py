"""Lovart Remote MCP（https://lgw.lovart.ai/v1/openapi/mcp/widgets）：生图 / 生视频 / 积分。

工具（tools/list）：list_models、describe_model、generate_image、generate_video、upload_asset、
get_task_status、get_credits 等。生成工具返回 status=pending + task_ids，之后用 get_task_status 轮询到 completed，
artifacts[].url 即成品。生成器只接受 Lovart 自家托管的素材，本地图先走 upload_asset。
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from typing import Any

from . import jobs, libtv, lovart_auth, util
from .util import CloudGenError

TOOLS_DUMP = os.path.join(util.PACK_DIR, "lovart_tools.json")
DEFAULT = "默认"
_client: libtv.MCPClient | None = None


def client() -> libtv.MCPClient:
    global _client
    if _client is None:
        _client = libtv.MCPClient(lovart_auth.MCP_URL, lovart_auth, "Lovart", TOOLS_DUMP)
    return _client


def configured() -> bool:
    return bool(lovart_auth.status()["authorized"])


# ---------------------------------------------------------------- account
def account() -> dict:
    d = client().call("get_credits", {}, timeout=60)
    if not isinstance(d, dict):
        raise CloudGenError(f"Lovart get_credits 返回异常：{str(d)[:200]}")
    out = {"provider": "lovart", "name": "Lovart", "ok": True, "balance": util.fmt_num(d.get("credits", "?")),
           "expire": "", "extra": {}, "member": "会员" if d.get("member") else "非会员"}
    if d.get("concurrency_limit") is not None:
        out["extra"]["并发上限"] = d["concurrency_limit"]
    return out


# ---------------------------------------------------------------- models
_model_cache: dict[str, tuple[float, dict]] = {}
FALLBACK = ["(请先完成 Lovart 授权)"]


def _label(m: dict) -> str:
    return f"{m.get('display_name') or m['name']}{'（会员）' if m.get('members_only') else ''}"


def get_models(kind: str, refresh: bool = False) -> list[dict]:
    """kind: image | video。返回 [{name(提交用), label(下拉显示), display_name, members_only, default, ...}]。"""
    hit = _model_cache.get("all")
    if not hit or refresh or time.time() - hit[0] > 600:
        data = client().call("list_models", {}, timeout=60)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise CloudGenError(f"Lovart list_models 返回异常：{str(data)[:300]}")
        _model_cache["all"] = hit = (time.time(), data)
    data = hit[1]
    default = data.get(f"default_{kind}_model")
    out = []
    for m in data["models"]:
        if m.get("kind") != kind:
            continue
        out.append({**m, "label": _label(m), "default": m["name"] == default})
    return out


def cached_model_names(kind: str) -> list[str]:
    hit = _model_cache.get("all")
    if hit:
        names = [m["label"] for m in get_models(kind)]
        return [DEFAULT] + names if names else [DEFAULT]
    if configured():
        def job():
            try:
                get_models(kind)
            except Exception as exc:  # noqa: BLE001
                print(f"[CloudGen] Lovart 模型列表获取失败：{exc}")
        threading.Thread(target=job, daemon=True).start()
    return [DEFAULT]


def resolve_model(kind: str, label: str) -> str | None:
    """下拉里的显示名 → 提交用的 model 名；「默认」返回 None（交给服务端用默认模型）。"""
    if not label or label == DEFAULT:
        return None
    for refresh in (False, True):
        for m in get_models(kind, refresh=refresh):
            if label in (m["label"], m["display_name"], m["name"]):
                return m["name"]
    raise CloudGenError(f"Lovart 没有找到模型「{label}」")


def describe_model(name: str) -> Any:
    return client().call("describe_model", {"model": name}, timeout=60)


# ---------------------------------------------------------------- upload
def upload_bytes(data: bytes, filename: str) -> str:
    if len(data) > 8 * 1024 * 1024:
        raise CloudGenError("Lovart 上传限 8MB，请缩小参考图")
    key = "lovart:" + util.sha1(data)
    hit = util.asset_get(key)
    if hit:
        return hit
    res = client().call("upload_asset", {"data_base64": base64.b64encode(data).decode(), "filename": filename}, timeout=180)
    url = util.find_first(res, ("url", "asset_url", "file_url"))
    if not url:
        urls = [u for _, _, u in util.walk(res) if isinstance(u, str) and u.startswith("http")]
        url = urls[0] if urls else None
    if not url:
        raise CloudGenError(f"Lovart 上传没有返回 URL：{json.dumps(res, ensure_ascii=False)[:300]}")
    util.asset_put(key, url, filename)
    return url


def refs_from_images(images, node_id=None, limit: int = 16) -> list[str]:
    urls = []
    if images is None:
        return urls
    seq = images if isinstance(images, (list, tuple)) else [images]
    n = 0
    for t in seq:
        if t is None:
            continue
        for i in range(t.shape[0]):
            if n >= limit:
                return urls
            n += 1
            util.send_progress(node_id, f"上传参考图 {n}…")
            urls.append(upload_bytes(util.tensor_to_png_bytes(t, i), f"ref{n}.png"))
    return urls


def upload_frame(image, name: str) -> str | None:
    if image is None:
        return None
    t = image[0] if isinstance(image, (list, tuple)) else image
    return upload_bytes(util.tensor_to_png_bytes(t, 0), name)


# ---------------------------------------------------------------- generate
FAILED = ("failed", "error", "canceled", "cancelled", "rejected", "expired")


def _task_ids(d: Any) -> list[str]:
    ids = []
    if isinstance(d, dict):
        if isinstance(d.get("task_ids"), list):
            ids += [str(x) for x in d["task_ids"] if x]
        if d.get("task_id"):
            ids.append(str(d["task_id"]))
    return list(dict.fromkeys(ids))


def _artifacts(d: Any) -> list[dict]:
    return [a for a in (d.get("artifacts") or []) if isinstance(a, dict) and a.get("url")] if isinstance(d, dict) else []


def _poll(task_ids: list[str], project_id: str | None, kind: str, node_id, jid: str, timeout: float, first_wait: float) -> dict:
    t0 = time.time()
    wait = first_wait
    while True:
        end = time.time() + max(wait, 3)
        while time.time() < end:
            util.check_interrupt()
            time.sleep(1)
        args: dict = {"task_ids": task_ids}
        if project_id:
            args["project_id"] = project_id
        st = client().call("get_task_status", args, timeout=120)
        status = str(st.get("status", "")).lower() if isinstance(st, dict) else ""
        util.send_progress(node_id, f"Lovart {kind}：{status or '等待'} · 已等 {int(time.time() - t0)} 秒")
        if status in FAILED:
            raise CloudGenError(f"Lovart 任务失败：{json.dumps(st, ensure_ascii=False)[:500]}")
        if status in ("completed", "done", "succeeded", "success") and _artifacts(st):
            return st
        if status in ("completed", "done", "succeeded", "success"):
            raise CloudGenError(f"Lovart 任务完成但没有产物：{json.dumps(st, ensure_ascii=False)[:500]}")
        if time.time() - t0 > timeout:
            raise CloudGenError(f"Lovart 等待超时（{int(timeout)} 秒），任务 {task_ids} 仍在进行；可稍后重新运行相同参数续接")
        wait = float(st.get("poll_after_seconds") or 10) if isinstance(st, dict) else 10


def generate(kind: str, model: str | None, args: dict, prompt: str, node_id=None, seed=None, timeout: float = 3600) -> dict:
    """提交并等待完成。相同参数（含 seed）中断后重跑会续接原任务，不重复提交、不重复扣费。"""
    tool = "generate_image" if kind == "image" else "generate_video"
    body = {k: v for k, v in args.items() if v not in (None, "", [], {})}
    if model:
        body["model"] = model
    key = jobs.make_key("lovart", kind, body, seed)
    pend = jobs.find_pending(key)
    first_wait = 6.0
    if pend and pend[1].get("task_ids"):
        jid, j = pend
        util.send_progress(node_id, f"续接 Lovart 任务 {j['task_ids']}")
        task_ids, project = j["task_ids"], j.get("project")
    else:
        jid = jobs.create("lovart", kind, key, model or DEFAULT, prompt, submit={"tool": tool, "args": body})
        util.send_progress(node_id, f"提交 Lovart {'图片' if kind == 'image' else '视频'}任务…")
        try:
            res = client().call(tool, body, timeout=300)
        except CloudGenError as exc:
            jobs.update(jid, status="failed", error=str(exc)[:300])
            raise
        task_ids, project = _task_ids(res), res.get("project_id") if isinstance(res, dict) else None
        arts = _artifacts(res)
        if arts and not task_ids:  # 同步直出
            jobs.update(jid, status="done", urls=[a["url"] for a in arts])
            return {"artifacts": arts, "raw": res, "job_id": jid, "project_id": project, "task_ids": []}
        if not task_ids:
            jobs.update(jid, status="failed", error="未返回任务 ID")
            raise CloudGenError(f"Lovart 没有返回 task_ids：{json.dumps(res, ensure_ascii=False)[:500]}")
        first_wait = float(res.get("poll_after_seconds") or 10)
        jobs.update(jid, task_id=",".join(task_ids), task_ids=task_ids, project=project, link=res.get("edit_url"))
    try:
        st = _poll(task_ids, project, kind, node_id, jid, timeout, first_wait)
    except CloudGenError as exc:
        if "超时" not in str(exc):
            jobs.update(jid, status="failed", error=str(exc)[:300])
        raise
    arts = _artifacts(st)
    jobs.update(jid, status="done", urls=[a["url"] for a in arts], link=st.get("edit_url"))
    return {"artifacts": arts, "raw": st, "job_id": jid, "project_id": project, "task_ids": task_ids}


def fetch_job(j: dict) -> list[str]:
    """jobs.fetch 用：查一次任务状态，完成则返回产物 URL，未完成返回空列表，失败抛错。"""
    ids = j.get("task_ids") or []
    if not ids:
        return []
    args: dict = {"task_ids": ids}
    if j.get("project"):
        args["project_id"] = j["project"]
    st = client().call("get_task_status", args, timeout=120)
    status = str(st.get("status", "")).lower() if isinstance(st, dict) else ""
    if status in FAILED:
        raise CloudGenError(json.dumps(st, ensure_ascii=False)[:300])
    return [a["url"] for a in _artifacts(st)] if status in ("completed", "done", "succeeded", "success") else []
