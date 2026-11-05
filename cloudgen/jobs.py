"""生成任务记录：提交前先落盘，ComfyUI 重启/中断后可以续接或取回，不会重复提交扣费。

jobs.json：{job_id: {provider, kind, key, status(pending|done|failed), model, prompt, created, updated,
                    submit:{tool, args}（LibTV，用同一个 idempotencyKey 重放即可找回原任务）,
                    thread_id/run_id（小云雀）, task_id, project, urls, outputs, error}}
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid

from . import util

PATH = os.path.join(util.PACK_DIR, "jobs.json")
_lock = threading.Lock()
KEEP = 60  # 最多保留的记录数


def _read() -> dict:
    try:
        with open(PATH, "r", encoding="utf-8") as fp:
            d = json.load(fp)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(d: dict):
    if len(d) > KEEP:
        for k in sorted(d, key=lambda k: d[k].get("created", ""))[: len(d) - KEEP]:
            if d[k].get("status") != "pending":
                d.pop(k, None)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(d, fp, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def make_key(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def find_pending(key: str) -> tuple[str, dict] | None:
    with _lock:
        for jid, j in _read().items():
            if j.get("key") == key and j.get("status") == "pending":
                return jid, j
    return None


def create(provider: str, kind: str, key: str, model: str, prompt: str, **extra) -> str:
    jid = time.strftime("%m%d%H%M%S") + "-" + uuid.uuid4().hex[:4]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        d = _read()
        d[jid] = {"provider": provider, "kind": kind, "key": key, "status": "pending", "model": model,
                  "prompt": (prompt or "")[:120], "created": now, "updated": now, **extra}
        _write(d)
    return jid


def update(jid: str, **fields):
    with _lock:
        d = _read()
        if jid in d:
            d[jid].update(fields)
            d[jid]["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _write(d)


def get(jid: str) -> dict | None:
    with _lock:
        return _read().get(jid)


def listing() -> list[dict]:
    with _lock:
        d = _read()
    items = [{"id": k, **{x: v.get(x) for x in ("provider", "kind", "status", "model", "prompt", "created",
                                                   "updated", "task_id", "thread_id", "error", "outputs",
                                                   "progress", "link", "cost", "cost_src")}} for k, v in d.items()]
    return sorted(items, key=lambda x: x.get("created") or "", reverse=True)


def remove(jid: str):
    with _lock:
        d = _read()
        d.pop(jid, None)
        _write(d)


def fetch(jid: str) -> dict:
    """取回任务结果：已完成就把结果下载到 output/cloudgen；未完成就查一次远端状态。"""
    from . import libtv, lovart, xyq
    j = get(jid)
    if not j:
        raise util.CloudGenError("没有这条任务记录")
    urls = j.get("urls") or []
    if j.get("status") == "pending":
        if j["provider"] == "libtv" and j.get("task_id"):
            project = j.get("project") or libtv.ensure_project()
            last = libtv.client().call("task_status", {"project_id": project, "task_id": j["task_id"]}, timeout=120)
            st = libtv._task_state(last)
            if st == "failed":
                err = json.dumps(last.get("error"), ensure_ascii=False) if last.get("error") else str(last.get("outcome"))
                update(jid, status="failed", error=err)
                return {"status": "failed", "error": err}
            nodes = libtv._artifact_nodes(last) or ([j["canvas_node"]] if j.get("canvas_node") else [])
            prog = None
            urls = []
            for n in nodes:
                m = libtv.node_media(project, n)
                prog = m["progress"]
                urls += m["images"] + m["videos"]
            if st != "done" or not urls:
                update(jid, remote_status=st, progress=prog)
                return {"status": "pending", "remote_status": st, "progress": prog}
        elif j["provider"] == "xyq":
            d = xyq._poll_result(j["thread_id"], j["run_id"]) or {}
            st = xyq._state(d.get("run_state"))
            urls = list(d.get("image_urls") or []) + list(d.get("video_urls") or [])
            if st in ("failed", "canceled"):
                update(jid, status="failed", error=xyq._fail_text(d.get("fail_reason")))
                return {"status": "failed", "error": xyq._fail_text(d.get("fail_reason"))}
            if st != "done" or not urls:
                return {"status": "pending", "remote_status": st}
        elif j["provider"] == "lovart":
            try:
                urls = lovart.fetch_job(j)
            except util.CloudGenError as exc:
                update(jid, status="failed", error=str(exc))
                return {"status": "failed", "error": str(exc)}
            if not urls:
                return {"status": "pending", "remote_status": "生成中"}
        else:
            return {"status": "pending", "remote_status": "未拿到任务 ID，重新运行相同参数的节点即可续接"}
    if not urls:
        return {"status": j.get("status"), "error": j.get("error")}
    outputs = j.get("outputs") or []
    if not outputs:
        for u in urls:
            data = util.download(u, timeout=900)
            ext = ".mp4" if (j.get("kind") == "video" or u.lower().split("?")[0].endswith(util.VID_EXT)) else ".png"
            outputs.append(util.save_blob(data, "cloudgen", j["provider"], ext))
    update(jid, status="done", urls=urls, outputs=outputs)
    return {"status": "done", "outputs": outputs}
