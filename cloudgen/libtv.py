"""LibTV Remote MCP（https://mcp.liblib.tv/mcp，streamable HTTP）客户端 + 生成封装。

LibTV 的工具参数 schema 由服务端 tools/list 下发，这里不硬编码字段名：
按工具的 inputSchema 把节点参数映射过去（prompt/model/ratio/...的常见别名），
映射不到的字段可用节点上的 extra_json 直接补。
侧边栏「导出 LibTV 工具清单」会把完整 schema 写到 libtv_tools.json，便于对照。
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from typing import Any

from . import libtv_auth, util
from .util import CloudGenError

PROTOCOL = "2025-06-18"
TOOLS_DUMP = os.path.join(util.PACK_DIR, "libtv_tools.json")


class MCPClient:
    def __init__(self, url: str = libtv_auth.MCP_URL, auth=libtv_auth, label: str = "LibTV", dump: str | None = None):
        self.url = url
        self.auth = auth
        self.label = label
        self.dump = dump or TOOLS_DUMP
        self.session_id: str | None = None
        self.protocol = PROTOCOL
        self._lock = threading.Lock()
        self._tools: list[dict] | None = None
        self._tools_at = 0.0

    # -------------------------------------------------- transport
    def _post(self, payload: dict, timeout: float, retry_auth: bool = True) -> tuple[int, dict, bytes]:
        token = self.auth.access_token()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {token}", "MCP-Protocol-Version": self.protocol}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        status, hdrs, data = util.http_request(self.url, method="POST", headers=headers,
                                               body=json.dumps(payload).encode("utf-8"), timeout=timeout)
        if status == 401 and retry_auth:
            self.auth.access_token(force=True)
            return self._post(payload, timeout, retry_auth=False)
        return status, hdrs, data

    @staticmethod
    def _parse(hdrs: dict, data: bytes, want_id) -> dict | None:
        ctype = hdrs.get("content-type", "")
        text = data.decode("utf-8", "replace")
        msgs = []
        if "text/event-stream" in ctype or text.lstrip().startswith(("event:", "data:", "id:")):
            buf: list[str] = []
            for line in text.splitlines() + [""]:
                if line.startswith("data:"):
                    buf.append(line[5:].lstrip())
                elif line == "" and buf:
                    try:
                        msgs.append(json.loads("\n".join(buf)))
                    except json.JSONDecodeError:
                        pass
                    buf = []
        elif text.strip():
            js = json.loads(text)
            msgs = js if isinstance(js, list) else [js]
        for m in msgs:
            if isinstance(m, dict) and m.get("id") == want_id and ("result" in m or "error" in m):
                return m
        return None

    def _rpc(self, method: str, params: dict | None = None, timeout: float = 120, _reinit=True) -> Any:
        if self.session_id is None and method != "initialize":
            self._initialize()
        rid = uuid.uuid4().hex[:12]
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        status, hdrs, data = self._post(payload, timeout)
        if status == 404 and self.session_id and _reinit:  # 会话过期
            self.session_id = None
            return self._rpc(method, params, timeout, _reinit=False)
        if status >= 400:
            raise CloudGenError(f"{self.label} MCP {method} HTTP {status}: {data[:300]!r}")
        if method == "initialize" and hdrs.get("mcp-session-id"):
            self.session_id = hdrs["mcp-session-id"]
        msg = self._parse(hdrs, data, rid)
        if msg is None:
            raise CloudGenError(f"{self.label} MCP {method} 无响应体")
        if "error" in msg:
            err = msg["error"]
            raise CloudGenError(f"{self.label} MCP {method} 错误：{err.get('message')} {json.dumps(err.get('data'), ensure_ascii=False)[:300] if err.get('data') else ''}")
        return msg["result"]

    def _initialize(self):
        self.session_id = None
        res = self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                       "clientInfo": {"name": "comfyui-cloudgen-hub", "version": "0.1"}}, timeout=30)
        if isinstance(res, dict) and res.get("protocolVersion"):
            self.protocol = res["protocolVersion"]
        if self.session_id is None:
            self.session_id = ""  # 无状态服务端
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=15)
        except CloudGenError:
            pass

    # -------------------------------------------------- api
    def tools(self, refresh=False) -> list[dict]:
        with self._lock:
            if self._tools is not None and not refresh and time.time() - self._tools_at < 1800:
                return self._tools
            tools, cursor = [], None
            for _ in range(20):
                res = self._rpc("tools/list", {"cursor": cursor} if cursor else {}, timeout=60)
                tools += res.get("tools") or []
                cursor = res.get("nextCursor")
                if not cursor:
                    break
            self._tools, self._tools_at = tools, time.time()
            try:
                with open(self.dump, "w", encoding="utf-8") as fp:
                    json.dump(tools, fp, ensure_ascii=False, indent=2)
            except OSError:
                pass
            return tools

    def tool(self, name: str) -> dict | None:
        for t in self.tools():
            if t.get("name") == name:
                return t
        return None

    def call(self, name: str, args: dict, timeout: float = 600) -> Any:
        res = self._rpc("tools/call", {"name": name, "arguments": args}, timeout=timeout)
        parsed = parse_tool_result(res)
        if isinstance(res, dict) and res.get("isError"):
            raise CloudGenError(f"{self.label} 工具 {name} 报错：{json.dumps(parsed, ensure_ascii=False)[:500]}")
        return parsed


def parse_tool_result(res: Any) -> Any:
    if not isinstance(res, dict):
        return res
    if res.get("structuredContent") is not None:
        return util.deep_decode(res["structuredContent"])
    texts = [c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
    others = [c for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") != "text"]
    if len(texts) == 1 and not others:
        return util.deep_decode(texts[0])
    parts = [util.deep_decode(t) for t in texts] + others
    return parts


_client: MCPClient | None = None


def client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient()
    return _client


# ================================================================== schema mapping
ALIASES = {
    "prompt": ["prompt", "text", "positive_prompt", "content", "description", "query_text", "instruction"],
    "negative_prompt": ["negative_prompt", "negativeprompt", "negative"],
    "model": ["model", "model_id", "modelid", "model_key", "modelkey", "model_name", "modelname", "model_code"],
    "ratio": ["ratio", "aspect_ratio", "aspectratio", "size_ratio", "image_ratio", "video_ratio"],
    "width": ["width", "w", "image_width"],
    "height": ["height", "h", "image_height"],
    "size": ["size", "image_size"],
    "quality": ["quality", "image_quality", "effort", "clarity"],
    "count": ["count", "n", "num", "number", "num_images", "image_count", "batch_size", "batch", "quantity"],
    "duration": ["duration", "duration_sec", "duration_seconds", "seconds", "length", "video_duration"],
    "resolution": ["resolution", "video_resolution", "image_resolution"],
    "seed": ["seed"],
    "images": ["imagelist", "image_list", "images", "image_urls", "imageurls", "reference_images", "ref_images", "reference_image_urls",
               "input_images", "image_list", "refs", "references"],
    "image": ["image", "image_url", "imageurl", "reference_image", "ref_image", "init_image", "input_image"],
    "first_frame": ["first_frame", "firstframe", "first_frame_url", "start_frame", "start_image", "first_image"],
    "last_frame": ["last_frame", "lastframe", "last_frame_url", "end_frame", "end_image", "last_image", "tail_image"],
    "kind": ["type", "media_type", "mediatype", "kind", "category", "modality", "output_type", "generation_type"],
    "sound": ["enablesound", "enable_sound", "sound", "with_audio", "generate_audio", "enable_audio"],
    "mode": ["modetype", "mode_type", "mode", "generate_mode"],
}
NEST_KEYS = ("params", "parameters", "options", "settings", "config", "input", "inputs", "arguments", "payload")


def _norm(s: str) -> str:
    return s.lower().replace("-", "_")


def _enum_values(enum) -> list:
    return [e.get("value") if isinstance(e, dict) else e for e in (enum or [])]


def _coerce(value: Any, prop: dict) -> Any:
    t = prop.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    enum = _enum_values(prop.get("enum"))
    if isinstance(value, bool) and enum and {"on", "off"} & {str(e).lower() for e in enum}:
        value = "on" if value else "off"
    try:
        if t == "integer" and not isinstance(value, list):
            value = int(float(value))
        elif t == "number" and not isinstance(value, list):
            value = float(value)
        elif t == "string" and not isinstance(value, (str, list)):
            value = str(value)
        elif t == "boolean" and not isinstance(value, bool):
            value = str(value).lower() in ("1", "true", "yes", "on")
        elif t == "array" and not isinstance(value, list):
            value = [value]
        elif t == "string" and isinstance(value, list):
            value = value[0] if value else ""
    except (TypeError, ValueError):
        pass
    if enum and value not in enum:
        low = {str(e).lower(): e for e in enum}
        if str(value).lower() in low:
            value = low[str(value).lower()]
    if t == "array" and isinstance(value, list):
        item = prop.get("items") or {}
        if item.get("type") == "object" and value and isinstance(value[0], str):
            props = item.get("properties") or {}
            key = next((k for k in props if _norm(k) in ("url", "image_url", "uri", "src")), None) or "url"
            value = [{key: v} for v in value]
    return value


def map_args(schema: dict | None, canon: dict) -> dict:
    """按 inputSchema 把规范参数映射到真实字段名。schema 缺失时原样返回。"""
    canon = {k: v for k, v in canon.items() if v not in (None, "", [])}
    if not schema or not isinstance(schema.get("properties"), dict):
        return dict(canon)
    props: dict = schema["properties"]
    out: dict = {}
    used = set()
    for pname, pdef in props.items():
        n = _norm(pname)
        for ck, names in ALIASES.items():
            if ck in canon and ck not in used and n in names:
                out[pname] = _coerce(canon[ck], pdef if isinstance(pdef, dict) else {})
                used.add(ck)
                break
    # 嵌套对象（如 params / settings）
    rest = {k: v for k, v in canon.items() if k not in used}
    if rest:
        for pname, pdef in props.items():
            if _norm(pname) in NEST_KEYS and isinstance(pdef, dict) and pdef.get("type") in ("object", None) \
                    and isinstance(pdef.get("properties"), dict):
                sub = map_args(pdef, rest)
                if sub:
                    out[pname] = {**out.get(pname, {}), **sub}
                break
    return out


def missing_required(schema: dict | None, args: dict) -> list[str]:
    if not schema:
        return []
    return [r for r in schema.get("required") or [] if r not in args]


def schema_brief(schema: dict | None) -> str:
    if not schema:
        return "(无 schema)"
    props = schema.get("properties") or {}
    parts = []
    for k, v in props.items():
        t = v.get("type") if isinstance(v, dict) else ""
        e = v.get("enum") if isinstance(v, dict) else None
        parts.append(f"{k}:{t}{'=' + '/'.join(map(str, e[:8])) if e else ''}")
    return f"必填={schema.get('required') or []} 字段=[{', '.join(parts)}]"


def pick_tool(*names: str) -> dict:
    for n in names:
        t = client().tool(n)
        if t:
            return t
    have = [t.get("name") for t in client().tools()]
    raise CloudGenError(f"LibTV 没有工具 {names}；现有工具：{', '.join(have)}")


# ================================================================== 通用小工具
def _ikey(tag: str) -> str:
    return f"comfyui-{tag}-{uuid.uuid4().hex}"


HEX32 = __import__("re").compile(r"^[0-9a-fA-F]{32}$")


def _find_key(data: Any, names: tuple, pred=None):
    low = {n.lower() for n in names}
    for _, key, v in util.walk(data):
        if key.lower() in low and isinstance(v, (str, int)) and str(v) and (pred is None or pred(v)):
            return v
    return None


# ================================================================== account
BAL_KEYS = ("credit", "credits", "balance", "points", "point", "remain", "remaining", "available", "score",
            "power", "computing_power", "integral", "jifen", "coin", "coins", "amount", "total")
EXP_KEYS = ("expire", "expires", "expired", "expiry", "end_time", "endtime", "endat", "valid_until", "deadline",
            "due", "vip_end", "member_end")


def _numeric(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) or (
        isinstance(v, str) and v.replace(".", "", 1).isdigit())


def account() -> dict:
    """LibTV MCP 没有积分余额接口：余额用侧边栏手动录入的值（减去之后的扣减），会员信息来自 account_user。"""
    from . import plans
    out = {"provider": "libtv", "name": "LibTV", "ok": True, "balance": "?", "expire": "", "extra": {},
           "balance_source": "manual"}
    data = client().call("account_user", {}, timeout=60)
    out["raw"] = data
    member = util.find_first(data, ("memberName",))
    if member:
        out["member"] = member
    nick = util.find_first(data, ("nickname",))
    if nick:
        out["extra"]["昵称"] = nick
    mb = plans.manual_balance("libtv")
    if mb is not None:
        out["balance"] = util.fmt_num(mb)
    else:
        out["balance_source"] = "none"
    return out


# ================================================================== models
_model_cache: dict[str, tuple[float, list[dict]]] = {}
FALLBACK = {"image": ["(请先完成 LibTV 授权)"], "video": ["(请先完成 LibTV 授权)"]}
KEY_FIELDS = ("modelKey", "model_key", "modelId", "model_id", "key", "id", "model", "code")
NAME_FIELDS = ("modelName", "model_name", "displayName", "display_name", "name", "title", "label")


def _model_list_from(data: Any) -> list[dict]:
    best: list = []
    for _, _, v in util.walk({"_": data}):
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            keys = set().union(*[set(x) for x in v[:5]])
            if keys & set(KEY_FIELDS + NAME_FIELDS) and len(v) > len(best):
                best = v
    return best


def _norm_model(raw: dict, kind: str) -> dict:
    key = next((raw[k] for k in KEY_FIELDS if raw.get(k) not in (None, "") and not isinstance(raw.get(k), (dict, list))), "")
    name = next((raw[k] for k in NAME_FIELDS if isinstance(raw.get(k), str) and raw.get(k).strip()), str(key))
    return {"provider": "libtv", "kind": kind, "name": str(name).strip(), "key": str(key),
            "cost": None, "pricing_rule": raw.get("pricingRule") or "", "vip": raw.get("vip"),
            "auto": bool(raw.get("autoPick")), "description": raw.get("description") or "", "raw": raw}


def get_models(kind: str, refresh: bool = False) -> list[dict]:
    hit = _model_cache.get(kind)
    if hit and not refresh and time.time() - hit[0] < 600:
        return hit[1]
    data = client().call("search_models", {"action": "search", "nodeType": kind}, timeout=60)
    models = [_norm_model(r, kind) for r in _model_list_from(data)]
    seen, uniq = set(), []
    for m in models:
        if m["key"] and m["name"] not in seen:
            seen.add(m["name"])
            uniq.append(m)
    if not uniq:
        raise CloudGenError(f"LibTV search_models 没返回模型：{json.dumps(data, ensure_ascii=False)[:300]}")
    _model_cache[kind] = (time.time(), uniq)
    return uniq


def cached_model_names(kind: str) -> list[str]:
    hit = _model_cache.get(kind)
    if hit:
        return [m["name"] for m in hit[1]] or FALLBACK[kind]
    if libtv_auth.status()["authorized"]:
        def job():
            try:
                get_models(kind)
            except Exception as exc:  # noqa: BLE001
                print(f"[CloudGen] LibTV 模型列表获取失败：{exc}")
        threading.Thread(target=job, daemon=True).start()
    return FALLBACK[kind]


def find_model(kind: str, name: str) -> dict:
    for refresh in (False, True):
        for m in get_models(kind, refresh=refresh):
            if name in (m["name"], m["key"]):
                return m
    raise CloudGenError(f"LibTV 没有找到模型「{name}」")


_detail_cache: dict[str, dict] = {}


def model_detail(kind: str, name: str) -> dict:
    """get_model：inputContract.model（提交用标识）+ parameterSchemas（参数可选值）。"""
    m = find_model(kind, name)
    if m["key"] in _detail_cache:
        return _detail_cache[m["key"]]
    det = dict(m)
    data = None
    for q in (m["name"], m["key"]):
        try:
            data = client().call("get_model", {"model": q}, timeout=60)
            break
        except CloudGenError as exc:
            det["detail_error"] = str(exc)
    if data is None:  # 例：AutoTake 这类“自动选模型”查不到参数，按服务端默认参数提交
        det.update(submit_model=m["key"], options={})
        _detail_cache[m["key"]] = det
        return det
    det.pop("detail_error", None)
    det["raw_detail"] = data
    contract = util.find_first(data, ("inputContract",), types=(dict,)) or {}
    det["submit_model"] = contract.get("model") if isinstance(contract.get("model"), str) else m["key"]
    det["cost"] = None
    det["options"] = extract_options(data)
    _detail_cache[m["key"]] = det
    return det


def _schema_props(data: Any) -> dict | None:
    """优先 inputContract.parameterSchemas，其次任意 properties。"""
    ps = util.find_first(data, ("parameterSchemas", "parameter_schemas"), types=(dict, list))
    if isinstance(ps, list):  # [{key/name, ...schema}]
        ps = {str(x.get("key") or x.get("name")): x for x in ps if isinstance(x, dict) and (x.get("key") or x.get("name"))}
    if isinstance(ps, dict) and ps:
        if isinstance(ps.get("properties"), dict):
            return ps["properties"]
        # 可能是 {mode: {properties:{...}}} 的按模式分组
        merged: dict = {}
        for v in ps.values():
            if isinstance(v, dict) and isinstance(v.get("properties"), dict):
                for k, d in v["properties"].items():
                    merged.setdefault(k, d)
        if merged:
            return merged
        if all(isinstance(v, (dict, list)) for v in ps.values()):
            return ps
    best = None
    for _, key, v in util.walk({"_": data}):
        if key == "properties" and isinstance(v, dict) and v and (best is None or len(v) > len(best)):
            best = v
    return best


def _enum_of(d: dict) -> list | None:
    if d.get("enum"):
        return _enum_values(d["enum"])
    for k in ("oneOf", "anyOf"):
        if isinstance(d.get(k), list):
            vals = [x.get("const") for x in d[k] if isinstance(x, dict) and "const" in x]
            if vals:
                return vals
    if isinstance(d.get("items"), dict) and d["items"].get("enum"):
        return _enum_values(d["items"]["enum"])
    return None


def extract_options(data: Any) -> dict:
    out: dict = {}
    props = _schema_props(data) or {}
    for key, d in props.items():
        if key == "modeType" and isinstance(d, dict) and isinstance(d.get("items"), dict) and not d["items"].get("type"):
            out["modes"] = {"options": list(d["items"].keys()), "limits": d["items"], "default": d.get("default")}
            continue
        if isinstance(d, list) and d and all(isinstance(x, (int, float, str)) for x in d):
            out[key] = {"options": d, "default": d[0]}
            continue
        if not isinstance(d, dict):
            continue
        item = {"label": d.get("displayName") or d.get("title") or key, "default": d.get("default")}
        en = _enum_of(d)
        if en:
            item["options"] = en
        lo = d.get("minimum", d.get("min"))
        hi = d.get("maximum", d.get("max"))
        if lo is not None or hi is not None:
            item.update(min=lo, max=hi, step=d.get("multipleOf", d.get("step")))
        if d.get("originalField"):
            item["field"] = d["originalField"]
        out[key] = item
    return out


def canonical_option(options: dict, canon_key: str) -> dict | None:
    names = ALIASES.get(canon_key, [canon_key])
    for k, v in options.items():
        if _norm(k) in names or _norm(k) == canon_key:
            return v
    return None


CANON_TO_MODEL = {"prompt": "prompt", "ratio": "ratio", "quality": "quality", "resolution": "resolution",
                  "duration": "duration", "count": "count", "sound": "enableSound", "seed": "seed",
                  "negative_prompt": "negativePrompt"}


def fit_model_params(canon: dict, model_opts: dict) -> tuple[dict, list[str]]:
    """把节点参数换成模型 parameterSchemas 里的字段名并按 enum/范围校正；模型没有的参数不传。"""
    params, warns = {}, []
    for ck, v in canon.items():
        if v in (None, "", []) or ck not in CANON_TO_MODEL or ck == "prompt":
            continue
        if not model_opts and ck not in ("ratio", "duration", "count"):
            continue  # 查不到参数说明的模型（如 AutoTake）只传最通用的几项
        opt = canonical_option(model_opts, ck) if model_opts else None
        if model_opts and opt is None:
            continue
        key = CANON_TO_MODEL[ck]
        if opt:
            key = next((k for k, o in model_opts.items() if o is opt), key)
            opts = opt.get("options")
            if isinstance(v, bool) and opts:
                sopts = {str(x).lower() for x in opts}
                v = ("on" if v else "off") if {"on", "off"} & sopts else (int(v) if sopts <= {"0", "1"} else v)
            if opts and v not in opts and str(v) not in map(str, opts):
                low = {str(x).lower(): x for x in opts}
                if str(v).lower() in low:
                    v = low[str(v).lower()]
                else:
                    warns.append(f"{opt.get('label', ck)}={v} 不在可选值 {opts} 内，用默认值")
                    continue
            elif opts:
                v = next((x for x in opts if str(x) == str(v)), v)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if opt.get("min") is not None and v < opt["min"]:
                    v = opt["min"]
                if opt.get("max") is not None and v > opt["max"]:
                    v = opt["max"]
        params[key] = v
    return params, warns


def deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ================================================================== project
PROJECT_NAME = "ComfyUI 生成"


def _project_uuid(data: Any) -> str | None:
    v = _find_key(data, ("uuid", "projectUuid", "project_uuid", "projectId", "project_id", "id"),
                  pred=lambda x: isinstance(x, str) and bool(HEX32.match(x.replace("-", ""))))
    return str(v) if v else None


def ensure_project() -> str:
    cfg = util.load_config()
    if cfg.get("libtv_project_id"):
        return str(cfg["libtv_project_id"])
    c = client()
    try:
        data = c.call("list_projects", {"name": PROJECT_NAME, "pageSize": 20}, timeout=60)
        for item in _model_list_from(data) or []:
            if str(item.get("name") or item.get("title") or "") == PROJECT_NAME:
                pid = _project_uuid(item)
                if pid:
                    util.save_config({"libtv_project_id": pid})
                    return pid
    except CloudGenError:
        pass
    data = c.call("create_project", {"name": PROJECT_NAME, "idempotencyKey": "comfyui-project-" + uuid.uuid4().hex[:12],
                                     "description": "ComfyUI 节点生成的图片和视频"}, timeout=120)
    pid = _project_uuid(data)
    if not pid:
        raise CloudGenError(f"LibTV 创建项目后没拿到项目 UUID：{json.dumps(data, ensure_ascii=False)[:300]}")
    util.save_config({"libtv_project_id": pid})
    return pid


# ================================================================== upload
_verified_nodes: set = set()


def _node_alive(project: str, node: str) -> bool:
    """确认缓存的画布节点还在（用户可能在画布里删了）；同一次运行内只查一次。"""
    if (project, node) in _verified_nodes:
        return True
    try:
        client().call("read_node", {"node": node, "projectId": project}, timeout=60)
    except CloudGenError:
        return False  # 节点被删了（read_node 报错），需要重新上传
    _verified_nodes.add((project, node))
    return True


def upload_image_bytes(data: bytes, filename: str, mime: str = "image/png", project: str | None = None) -> str:
    """上传本地图片并挂到项目画布，返回 image 节点 ID（用于参考图连线）。
    相同内容（SHA-1）在同一项目里只上传一次，之后复用画布上已有的节点。"""
    project = project or ensure_project()
    key = f"libtv:{project}:{util.sha1(data)}"
    cached = util.asset_get(key)
    if cached:
        if _node_alive(project, cached):
            return cached
        util.asset_drop(key)
    node = _upload_image_bytes(data, filename, mime, project)
    util.asset_put(key, node, filename)
    _verified_nodes.add((project, node))
    return node


def _upload_image_bytes(data: bytes, filename: str, mime: str, project: str) -> str:
    import hashlib
    c = client()
    ticket = c.call("upload_ticket", {"file_name": filename, "file_size": len(data),
                                      "sha1_hex": hashlib.sha1(data).hexdigest(), "projectId": project}, timeout=60)
    upload_id = _find_key(ticket, ("upload_id", "uploadId"))
    path = _find_key(ticket, ("path", "object_key", "objectKey", "key"))
    parts = None
    for _, key, v in util.walk(ticket):
        if isinstance(v, list) and v and all(isinstance(x, (dict, str)) for x in v) and key.lower() in (
                "parts", "part_urls", "parturls", "urls", "upload_urls", "uploadurls", "presigned_urls"):
            parts = v
            break
    if parts is None:
        single = _find_key(ticket, ("url", "upload_url", "uploadUrl", "put_url"), pred=lambda x: str(x).startswith("http"))
        parts = [single] if single else []
    if not parts or not upload_id or not path:
        raise CloudGenError(f"LibTV upload_ticket 返回无法识别：{json.dumps(ticket, ensure_ascii=False)[:400]}")
    psize = int(_find_key(ticket, ("part_size", "partSize")) or 5242880)

    def part_info(p, i):
        if isinstance(p, str):
            return p, i + 1, {}
        url = p.get("url") or p.get("upload_url") or p.get("uploadUrl") or p.get("put_url")
        num = int(p.get("part_number") or p.get("partNumber") or p.get("part") or i + 1)
        return url, num, p.get("headers") or {}

    infos = sorted((part_info(p, i) for i, p in enumerate(parts)), key=lambda x: x[1])
    # 预签名地址按“签名时的 Content-Type”校验；urllib 默认会补 application/x-www-form-urlencoded，
    # 所以必须显式指定：优先用票据里给的类型，否则发空值，再依次尝试常见类型。
    ticket_ct = _find_key(ticket, ("content_type", "contentType", "mime_type", "mimeType"))
    ct_candidates = [str(ticket_ct)] if ticket_ct else []
    ct_candidates += [c for c in ("", "application/octet-stream", mime) if c not in ct_candidates]
    good_ct = None
    for url, num, hdrs in infos:
        chunk = data[(num - 1) * psize: num * psize]
        tries = [good_ct] if good_ct is not None else ct_candidates
        last = None
        for ct in tries:
            h = {str(k): str(v) for k, v in hdrs.items()}
            if not any(k.lower() == "content-type" for k in h):
                h["Content-Type"] = ct
            status, _, resp = util.http_request(url, method="PUT", body=chunk, timeout=300, headers=h)
            if status < 400:
                good_ct = h.get("Content-Type", ct)
                last = None
                break
            last = (status, resp)
            if b"SignatureDoesNotMatch" not in (resp or b""):
                break
        if last:
            raise CloudGenError(f"LibTV 上传分片 {num} 失败 HTTP {last[0]}: {last[1][:300]!r}")
    done = c.call("upload_complete", {"path": str(path), "upload_id": str(upload_id)}, timeout=120)
    file_ref = _find_key(done, ("file_ref", "fileRef"))
    if not file_ref:
        raise CloudGenError(f"LibTV upload_complete 没返回 file_ref：{json.dumps(done, ensure_ascii=False)[:300]}")
    res = c.call("upload_media", {"name": os.path.splitext(filename)[0] + "_" + uuid.uuid4().hex[:4],
                                  "idempotencyKey": _ikey("upload"), "projectId": project,
                                  "fileRef": str(file_ref), "mediaType": "image"}, timeout=300)
    node = _find_key(res, ("nodeId", "node_id", "nodeKey", "node_key"))
    if not node:
        st = util.find_first(res, ("status",))
        raise CloudGenError(f"LibTV 素材挂到画布失败（{st or '无节点 ID'}）：{json.dumps(res, ensure_ascii=False)[:400]}")
    return str(node)


# ================================================================== generate
DONE = {"success", "succeeded", "successful", "completed", "complete", "done", "finished", "finish", "succeed", "ok", "2"}
FAIL = {"failed", "fail", "failure", "error", "canceled", "cancelled", "rejected", "timeout", "expired", "3", "4"}
RUNNING = {"running", "pending", "queued", "processing", "submitted", "waiting", "in_progress", "created", "0", "1"}


def _task_id(data: Any) -> str | None:
    v = _find_key(data, ("task_id", "taskId", "generationTaskId", "generation_task_id"),
                  pred=lambda x: not str(x).startswith("cop_"))
    return str(v) if v else None


def _status(data: Any) -> str:
    v = util.find_first(data, ("status", "state", "task_status", "taskStatus"))
    return str(v).lower() if v is not None else ""


def _fail_msg(data: Any) -> str:
    return str(util.find_first(data, ("failedReason", "failed_reason", "fail_reason", "error_message", "errorMessage",
                                      "error", "reason", "message")) or "")


def _all_urls(data: Any) -> list[str]:
    out = []
    for path, key, v in util.walk(data):
        if isinstance(v, str) and v.startswith("http") and not any(
                x in path.lower() for x in ("cover", "thumb", "poster", "avatar", "icon", "reference", "input", "link")):
            if "libtv.com/canvas" not in v and v not in out:
                out.append(v)
    return out


def _progress(data: Any):
    v = util.find_first(data, ("progress", "percent", "progressPercent", "process"))
    try:
        f = float(v)
        return f * 100 if 0 < f <= 1 and "." in str(v) else f
    except (TypeError, ValueError):
        return None


def node_media(project: str, node: str) -> dict:
    """读画布节点：结果链接（data.url / taskInfo.taskResult）+ 进度（taskInfo.progressPercent）。"""
    res = client().call("read_node", {"node": node, "projectId": project}, timeout=60)
    data = (res.get("node") or {}).get("data") or {} if isinstance(res, dict) else {}
    info = data.get("taskInfo") or {}
    tr = info.get("taskResult") or {}
    imgs = [x.get("previewPath") or x.get("originalPath") for x in tr.get("images") or [] if isinstance(x, dict)]
    vids = [x.get("previewPath") or x.get("originalPath") or x.get("url") for x in tr.get("videos") or [] if isinstance(x, dict)]
    urls = [u for u in (data.get("url") or []) if isinstance(u, str)]
    if not imgs and not vids and urls:
        if data.get("type") == "video" or res.get("node", {}).get("type") == "video":
            vids = urls
        else:
            imgs = urls
    return {"images": [u for u in imgs if u], "videos": [u for u in vids if u],
            "progress": info.get("progressPercent"), "status": info.get("status"), "loading": info.get("loading"),
            "failed": info.get("failedReason") or "", "task_id": info.get("taskId"), "raw": res}


def _task_state(last: Any) -> str:
    """task_status 新格式：outcome=SUCCEEDED/FAILED/...，readiness=TERMINAL_READY/..."""
    if not isinstance(last, dict):
        return ""
    outcome = str(last.get("outcome") or "").upper()
    readiness = str(last.get("readiness") or "").upper()
    if outcome in ("SUCCEEDED", "SUCCESS", "COMPLETED"):
        return "done"
    if outcome in ("FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED", "REJECTED", "TIMEOUT", "EXPIRED"):
        return "failed"
    if last.get("error"):
        return "failed"
    if readiness.startswith("TERMINAL") and outcome:
        return "done" if "READY" in readiness else "failed"
    st = _status(last)
    if st in DONE:
        return "done"
    if st in FAIL:
        return "failed"
    return st or outcome.lower() or readiness.lower() or "running"


def _artifact_nodes(last: Any) -> list[str]:
    out = []
    for a in (last.get("artifacts") if isinstance(last, dict) else None) or []:
        if isinstance(a, dict) and a.get("node_key") and a["node_key"] not in out:
            out.append(a["node_key"])
    return out


def wait_task(project: str, task_id: str, node_id=None, timeout: float = 3600, job_id: str | None = None,
              kind: str = "image", canvas_node: str | None = None) -> dict:
    from . import jobs
    t0 = time.time()
    last_st = None
    while True:
        util.check_interrupt()
        last = client().call("task_status", {"project_id": project, "task_id": task_id}, timeout=120)
        st = _task_state(last)
        if st != last_st:
            print(f"[CloudGen] LibTV 任务 {task_id} 状态={st}：{json.dumps(last, ensure_ascii=False)[:400]}")
            last_st = st
        nodes = _artifact_nodes(last) or ([canvas_node] if canvas_node else [])
        if st == "failed":
            msg = last.get("error") if isinstance(last, dict) else None
            raise CloudGenError(f"LibTV 任务失败：{json.dumps(msg, ensure_ascii=False) if msg else _fail_msg(last) or last.get('outcome')}")
        if st == "done":
            images, videos = [], []
            for n in nodes:
                m = node_media(project, n)
                images += m["images"]
                videos += m["videos"]
            if not images and not videos:  # 兜底：返回里直接带了链接
                f = util.find_media_urls(last)
                images, videos = f["image"], f["video"]
            if not images and not videos:
                raise CloudGenError(f"LibTV 任务已完成，但没读到结果链接（节点 {nodes}），可在项目「{PROJECT_NAME}」里查看")
            if job_id:
                jobs.update(job_id, canvas_nodes=nodes)
            return {"images": images, "videos": videos, "task_id": task_id, "raw": last, "nodes": nodes}
        prog = None
        if nodes:
            try:
                m = node_media(project, nodes[0])
                prog = m["progress"]
            except CloudGenError:
                pass
        if prog is not None:
            util.progress_bar(prog)
        if job_id:
            jobs.update(job_id, progress=prog, remote_status=st)
        el = int(time.time() - t0)
        util.send_progress(node_id, f"LibTV 生成中{' ' + str(round(prog)) + '%' if prog else ''} · {el // 60}:{el % 60:02d}")
        if el > timeout:
            raise CloudGenError(f"LibTV 任务超时 task_id={task_id}，可在 LibTV 项目「{PROJECT_NAME}」里查看")
        for _ in range(10):
            util.check_interrupt()
            time.sleep(0.5)


def _finish(project: str, res: Any, node_id, job_id=None, kind="image") -> dict:
    from . import jobs
    tid = _task_id(res)
    canvas = _find_key(res, ("nodeId", "node_id", "nodeKey", "node_key"))
    if job_id and (tid or canvas):
        jobs.update(job_id, task_id=tid, canvas_node=canvas)
    st = _task_state(res)
    if st == "failed":
        raise CloudGenError(f"LibTV 生成失败：{_fail_msg(res) or json.dumps(res, ensure_ascii=False)[:300]}")
    if not tid:
        # generate_video 会等到结束再返回：直接从结果节点读链接
        nodes = _artifact_nodes(res) or ([str(canvas)] if canvas else [])
        images, videos = [], []
        for n in nodes:
            m = node_media(project, n)
            images += m["images"]
            videos += m["videos"]
        if images or videos:
            return {"images": images, "videos": videos, "task_id": None, "raw": res, "nodes": nodes}
        f = util.find_media_urls(res)
        if f["image"] or f["video"]:
            return {"images": f["image"], "videos": f["video"], "task_id": None, "raw": res}
        raise CloudGenError(f"LibTV 没返回任务 ID：{json.dumps(res, ensure_ascii=False)[:500]}")
    return wait_task(project, tid, node_id, job_id=job_id, kind=kind, canvas_node=str(canvas) if canvas else None)


def _run_job(kind: str, model: dict, prompt: str, tool: str, args: dict, key: str, node_id, timeout: float) -> dict:
    """提交前先记下 tool+args（含 idempotencyKey）。中断后再次运行相同参数时，用同一个 key 重放，
    LibTV 会返回原任务而不是新建扣费任务。"""
    from . import jobs
    pend = jobs.find_pending(key)
    if pend:
        jid, j = pend
        tool, args = j["submit"]["tool"], j["submit"]["args"]
        util.send_progress(node_id, f"续接未完成的 LibTV 任务（{j.get('created')} 提交）…")
    else:
        jid = jobs.create("libtv", kind, key, model["name"], prompt, submit={"tool": tool, "args": args},
                          project=args.get("project_id") or args.get("projectId"))
        util.send_progress(node_id, f"LibTV 提交 {model['name']} …")
    project = args.get("project_id") or args.get("projectId")
    try:
        res = client().call(tool, args, timeout=timeout)
        out = _finish(project, res, node_id, job_id=jid, kind=kind)
    except CloudGenError as exc:
        jobs.update(jid, status="failed", error=str(exc)[:300])
        raise
    jobs.update(jid, status="done", urls=out["images"] + out["videos"], task_id=out.get("task_id"))
    out["job_id"] = jid
    return out


def generate_image(model: dict, prompt: str, params: dict, ref_nodes: list[str], node_id=None, seed=None) -> dict:
    from . import jobs
    project = ensure_project()
    # seed 参与任务指纹：同 seed 同参数 = 同一任务（中断后续接、不重复扣费）；换 seed = 新任务
    key = jobs.make_key("libtv", "image", model["submit_model"], prompt, params, ref_nodes, seed)
    if ref_nodes:  # 参考图：建一个 image 节点连上参考图并运行
        tool, args = "create_node", {"name": f"ComfyUI 图片 {time.strftime('%m%d-%H%M%S')}", "nodeType": "image",
                                     "idempotencyKey": _ikey("img"), "model": model["submit_model"],
                                     "projectId": project, "prompt": prompt, "params": params,
                                     "left": ref_nodes, "run": True}
    else:
        tool, args = "generation_submit", {"project_id": project, "idempotency_key": _ikey("img"), "kind": "image",
                                           "request": {"model": model["submit_model"], "prompt": prompt,
                                                       "params": params}}
    return _run_job("image", model, prompt, tool, args, key, node_id, 300)


def generate_video(model: dict, prompt: str, params: dict, start: str | None, end: str | None,
                   refs: list[str], node_id=None, seed=None) -> dict:
    from . import jobs
    project = ensure_project()
    args: dict = {"name": f"ComfyUI 视频 {time.strftime('%m%d-%H%M%S')}", "prompt": prompt,
                  "model": model["submit_model"], "run": True, "idempotencyKey": _ikey("vid"),
                  "projectId": project, "params": params}
    if start and end:
        args.update(intent="interpolate", startFrameNode=start, endFrameNode=end)
    elif start:
        args.update(intent="generate", startFrameNode=start)
    elif refs:
        args["referenceImageNodes"] = refs
    key = jobs.make_key("libtv", "video", model["submit_model"], prompt, params, start, end, refs, seed)
    return _run_job("video", model, prompt, "generate_video", args, key, node_id, 3600)


READONLY_TOOLS = {"read_project", "read_node", "task_status", "get_project_snapshot", "browse_project_media",
                  "inspect_media", "list_projects", "project_info", "account_user", "doctor", "search_models",
                  "get_model", "read_project_context", "resource_info"}


def fetch_node(node: str) -> dict:
    """把画布节点上的生成结果下载到 output/cloudgen（取回中断的任务用）。"""
    project = ensure_project()
    m = node_media(project, node)
    paths = []
    for u in m["images"]:
        paths.append(util.save_blob(util.download(u), "cloudgen", "libtv", ".png"))
    for u in m["videos"]:
        paths.append(util.save_blob(util.download(u, timeout=900), "cloudgen", "libtv", ".mp4"))
    return {"outputs": paths, "progress": m["progress"], "status": m["status"]}


def call_readonly(tool: str, args: dict) -> Any:
    if tool not in READONLY_TOOLS:
        raise CloudGenError(f"调试接口只允许只读工具：{sorted(READONLY_TOOLS)}")
    if tool in ("read_project", "read_node", "get_project_snapshot", "browse_project_media", "inspect_media",
                "read_project_context") and "projectId" not in args:
        args["projectId"] = ensure_project()
    if tool == "task_status" and "project_id" not in args:
        args["project_id"] = ensure_project()
    return client().call(tool, args, timeout=120)


def probe() -> dict:
    """调试：返回各只读工具的原始响应（不会产生费用）。"""
    c = client()
    out: dict = {}
    for name, args in (("account_user", {}), ("doctor", {}),
                       ("search_models_image", {"action": "search", "nodeType": "image"}),
                       ("search_models_video", {"action": "search", "nodeType": "video"}),
                       ("list_projects", {"pageSize": 5})):
        tool = "search_models" if name.startswith("search_models") else name
        try:
            out[name] = c.call(tool, args, timeout=60)
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)}
    for kind in ("image", "video"):
        try:
            m = get_models(kind)[0]
            out[f"get_model_{kind}"] = c.call("get_model", {"model": m["name"]}, timeout=60)
        except Exception as exc:  # noqa: BLE001
            out[f"get_model_{kind}"] = {"error": str(exc)}
    path = os.path.join(util.PACK_DIR, "libtv_probe.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2, default=str)
    return {"path": path, "keys": list(out)}
