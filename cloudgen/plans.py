"""手动填写的套餐信息：套餐名、到期时间、每月积分重置日与时间、每月额度。

存放在节点包目录 plans.json（已 gitignore），侧边栏「云端积分」里可编辑。
"""
from __future__ import annotations

import calendar
import json
import os
import threading
from datetime import datetime, timedelta

from . import util

PLANS_PATH = os.path.join(util.PACK_DIR, "plans.json")
_lock = threading.Lock()

FIELDS = ("plan_name", "plan_expire", "reset_day", "reset_time", "monthly_quota", "manual_balance",
          "plan_price", "plan_price_period", "yuan_per_credit", "daily_free", "topup")
EMPTY = {"plan_name": "", "plan_expire": "", "reset_day": 0, "reset_time": "00:00", "monthly_quota": 0,
         "manual_balance": None, "manual_balance_at": "", "deducted": 0,
         "plan_price": None, "plan_price_period": "year", "yuan_per_credit": None,
         "daily_free": 0, "topup": 0}


def credit_rate(p: dict):
    """1 积分折合多少元：手填优先，否则按 套餐价 ÷（每月额度 × 月数）。"""
    try:
        r = float(p.get("yuan_per_credit") or 0)
        if r > 0:
            return r
        price, quota = float(p.get("plan_price") or 0), float(p.get("monthly_quota") or 0)
        if price > 0 and quota > 0:
            return price / (quota * (12 if p.get("plan_price_period", "year") == "year" else 1))
    except (TypeError, ValueError):
        pass
    return None


def spent_today(provider: str, now: datetime | None = None) -> float:
    """今天经 ComfyUI 生成扣掉的积分（按任务完成时间算；网站上手动用掉的统计不到）。"""
    from . import jobs
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    total = 0.0
    for j in jobs.listing():
        if j.get("provider") == provider and (j.get("updated") or j.get("created") or "").startswith(today):
            try:
                c = float(j.get("cost") or 0)
            except (TypeError, ValueError):
                c = 0
            if c > 0:
                total += c
    return total


def free_left(p: dict, provider: str, balance=None) -> float:
    """今日赠送还剩多少。小云雀扣分顺序：每日赠送 → 套餐 → 单独充值。"""
    free = float(p.get("daily_free") or 0)
    if free <= 0:
        return 0.0
    left = max(free - spent_today(provider), 0.0)
    try:
        left = min(left, max(float(balance) - float(p.get("topup") or 0), 0.0))
    except (TypeError, ValueError):
        pass
    return left


def load() -> dict:
    with _lock:
        try:
            with open(PLANS_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            data = {}
    out = {p: {**EMPTY, **(data.get(p) or {})} for p in ("xyq", "libtv")}
    for prov, v in out.items():
        v["credit_rate"] = credit_rate(v)
        if v.get("daily_free"):
            try:
                v["free_left"] = free_left(v, prov)
            except Exception:  # noqa: BLE001
                v["free_left"] = None
    return out


def save(provider: str, patch: dict) -> dict:
    if provider not in ("xyq", "libtv"):
        raise util.CloudGenError(f"未知平台 {provider}")
    with _lock:
        try:
            with open(PLANS_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            data = {}
        cur = {**EMPTY, **(data.get(provider) or {})}
        for k in FIELDS:
            if k in patch:
                v = patch[k]
                if k == "manual_balance":
                    v = None if v in (None, "") else _num(v)
                    old = cur.get("manual_balance")
                    eff = None if old is None else _num(old - (cur.get("deducted") or 0))
                    if v == eff and old is not None:
                        continue  # 没改动
                    cur[k] = v
                    cur["manual_balance_at"] = datetime.now().strftime("%Y-%m-%d %H:%M") if v is not None else ""
                    cur["deducted"] = 0
                    continue
                if k in ("plan_price", "yuan_per_credit"):
                    try:
                        v = float(v) if v not in (None, "") else None
                    except (TypeError, ValueError):
                        v = None
                    cur[k] = v if v and v > 0 else None
                    continue
                if k == "plan_price_period":
                    cur[k] = "month" if v == "month" else "year"
                    continue
                if k in ("reset_day", "monthly_quota", "daily_free", "topup"):
                    try:
                        v = int(float(v or 0))
                    except (TypeError, ValueError):
                        v = 0
                    if k == "reset_day":
                        v = max(0, min(31, v))
                elif k == "reset_time":
                    v = _norm_time(str(v or "00:00"))
                else:
                    v = str(v or "").strip()
                cur[k] = v
        data[provider] = cur
        tmp = PLANS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, PLANS_PATH)
        return cur


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() else round(f, 2)
    except (TypeError, ValueError):
        return None


def deduct(provider: str, amount) -> None:
    """生成后从手动录入的余额里扣掉本次消耗。"""
    a = _num(amount)
    if not a or a <= 0:
        return
    with _lock:
        try:
            with open(PLANS_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            return
        cur = data.get(provider) or {}
        if cur.get("manual_balance") is None:
            return
        cur["deducted"] = _num((cur.get("deducted") or 0) + a)
        data[provider] = cur
        tmp = PLANS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, PLANS_PATH)


def manual_balance(provider: str, now: datetime | None = None):
    """手动余额 - 之后的扣减；若录入后已经过了一次积分重置，则视为失效。"""
    p = load()[provider]
    if p.get("manual_balance") is None:
        return None
    at = _parse_dt(p.get("manual_balance_at") or "")
    if at and p.get("reset_day"):
        nr = next_reset(p["reset_day"], p["reset_time"], at)
        if nr and nr <= (now or datetime.now()):
            return None
    return _num(p["manual_balance"] - (p.get("deducted") or 0))


def _norm_time(s: str) -> str:
    try:
        h, m = (s.replace("：", ":").split(":") + ["0"])[:2]
        return f"{max(0, min(23, int(h))):02d}:{max(0, min(59, int(m))):02d}"
    except ValueError:
        return "00:00"


def _parse_dt(s: str) -> datetime | None:
    s = (s or "").strip().replace("/", "-").replace(".", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(hour=23, minute=59, second=59) if fmt == "%Y-%m-%d" else d
        except ValueError:
            continue
    return None


def _month_moment(year: int, month: int, day: int, hh: int, mm: int) -> datetime:
    last = calendar.monthrange(year, month)[1]
    return datetime(year, month, min(day, last), hh, mm)


def next_reset(day: int, time_s: str, now: datetime | None = None) -> datetime | None:
    if not day:
        return None
    now = now or datetime.now()
    hh, mm = map(int, _norm_time(time_s).split(":"))
    cand = _month_moment(now.year, now.month, day, hh, mm)
    if cand <= now:
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        cand = _month_moment(y, m, day, hh, mm)
    return cand


def _span(td: timedelta) -> str:
    secs = max(0, int(td.total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}天{h}小时"
    if h:
        return f"{h}小时{m}分"
    return f"{m}分钟"


def status(provider: str, balance=None, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    p = load()[provider]
    out = {"plan_name": p["plan_name"], "plan_expire": p["plan_expire"], "reset_day": p["reset_day"],
           "reset_time": p["reset_time"], "monthly_quota": p["monthly_quota"],
           "manual_balance": p.get("manual_balance"), "manual_balance_at": p.get("manual_balance_at"),
           "deducted": p.get("deducted") or 0, "daily_free": p.get("daily_free") or 0, "topup": p.get("topup") or 0}
    exp = _parse_dt(p["plan_expire"])
    if exp:
        out["plan_expire_text"] = exp.strftime("%Y-%m-%d %H:%M")
        out["plan_left"] = "已到期" if exp <= now else _span(exp - now)
        out["plan_days_left"] = (exp - now).total_seconds() / 86400
    nr = next_reset(p["reset_day"], p["reset_time"], now)
    if nr:
        if exp and exp < nr:  # 套餐先于下次重置到期
            out["reset_note"] = "套餐会先到期"
        out["next_reset"] = nr.strftime("%m-%d %H:%M")
        out["reset_left"] = _span(nr - now)
        hours = (nr - now).total_seconds() / 3600
        out["reset_hours_left"] = hours
        try:
            bal = float(balance)
        except (TypeError, ValueError):
            bal = None
        if bal is not None and bal > 0:
            # 每日赠送积分当天有效、不叠加：月度积分 = 余额 - 今日赠送（按全额估算）
            # 单独充值的积分不随月重置，也不计入“将被清零”
            # 扣分顺序：每日赠送 → 套餐 → 单独充值
            free = float(p.get("daily_free") or 0)
            fl = free_left(p, provider, bal)
            topup = min(float(p.get("topup") or 0), max(bal - fl, 0))
            paid = max(bal - fl - topup, 0)
            days = max(hours / 24, 1 / 24)
            per_day = paid / max(days, 1) if days >= 1 else paid
            out["daily_budget"] = util.fmt_num(round(per_day + free, 0))
            out["expiring"] = util.fmt_num(round(paid, 0))
            out["free_left"] = util.fmt_num(round(fl, 0))
            out["topup_left"] = util.fmt_num(round(topup, 0))
            if hours <= 72 and paid > 0:
                out["warn"] = f"还有 {util.fmt_num(paid)} 积分将在 {out['reset_left']} 后重置"
    return out


def text_line(name: str, st: dict) -> str:
    parts = []
    if st.get("plan_name") or st.get("plan_expire_text"):
        parts.append(f"{st.get('plan_name') or '套餐'} 到期 {st.get('plan_expire_text', '—')}"
                     + (f"（剩 {st['plan_left']}）" if st.get("plan_left") else ""))
    if st.get("next_reset"):
        parts.append(f"积分 {st['next_reset']} 重置（剩 {st['reset_left']}）")
    return f"  {name}：" + "；".join(parts) if parts else ""
