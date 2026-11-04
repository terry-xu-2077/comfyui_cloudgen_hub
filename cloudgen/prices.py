"""手动维护的模型积分价格 + 每次运行的实测消耗。

prices.json 结构：
{
  "rules": {
    "xyq:video:Seedance 2.0": {"unit": "per_sec", "default": 10, "by_res": {"720p": 8, "1080p": 20}},
    "libtv:image:GPT Image 2": {"unit": "per_image", "default": 15, "by_res": {"2K": 20}}
  },
  "observed": {"xyq:video:Seedance 2.0|720p": {"cost": 40, "duration": 5, "count": 1, "at": "..."}}
}
unit: per_run（每次）/ per_image（每张）/ per_sec（每秒）
ref_by_res / ref_default：带参考视频时的每秒单价（可选），计费秒数 = 生成时长 + 参考视频总时长
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import util

PATH = os.path.join(util.PACK_DIR, "prices.json")
_lock = threading.Lock()
UNITS = ("per_run", "per_image", "per_sec")


def _read() -> dict:
    try:
        with open(PATH, "r", encoding="utf-8") as fp:
            d = json.load(fp)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(d: dict):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(d, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)


def load() -> dict:
    with _lock:
        d = _read()
    return {"rules": d.get("rules") or {}, "observed": d.get("observed") or {}}


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def save_rules(rules: dict) -> dict:
    """整体替换若干条规则；值为 null 或空的规则会被删除。"""
    with _lock:
        d = _read()
        cur = d.get("rules") or {}
        for key, r in (rules or {}).items():
            if not r:
                cur.pop(key, None)
                continue
            unit = r.get("unit") if r.get("unit") in UNITS else "per_run"
            by_res = {str(k): _num(v) for k, v in (r.get("by_res") or {}).items() if _num(v) is not None}
            default = _num(r.get("default"))
            if default is None and not by_res:
                cur.pop(key, None)
                continue
            item = {"unit": unit, "default": default, "by_res": by_res}
            # 带参考视频时的单价（小云雀：计费秒数 = 生成时长 + 参考视频时长）
            ref = {str(k): _num(v) for k, v in (r.get("ref_by_res") or {}).items() if _num(v) is not None}
            if ref:
                item["ref_by_res"] = ref
            if _num(r.get("ref_default")) is not None:
                item["ref_default"] = _num(r.get("ref_default"))
            cur[key] = item
        d["rules"] = cur
        _write(d)
        return cur


def record_observed(provider: str, kind: str, model: str, dim: str, cost, duration=None, count=None):
    c = _num(cost)
    if c is None or c < 0:
        return
    with _lock:
        d = _read()
        obs = d.get("observed") or {}
        obs[f"{provider}:{kind}:{model}|{dim or '默认'}"] = {
            "cost": c, "duration": duration, "count": count, "at": time.strftime("%Y-%m-%d %H:%M")}
        d["observed"] = obs
        _write(d)


def estimate(provider: str, kind: str, model: str, dim: str, duration=None, count=None, default_duration=5):
    """按配置的价格估算一次消耗；没配置返回 None。"""
    r = load()["rules"].get(f"{provider}:{kind}:{model}")
    if not r:
        return None
    base = (r.get("by_res") or {}).get(str(dim))
    if base is None:
        base = r.get("default")
    if base is None:
        return None
    unit = r.get("unit") or "per_run"
    if unit == "per_sec":
        return base * (duration or default_duration)
    if unit == "per_image":
        return base * (count or 1)
    return base
