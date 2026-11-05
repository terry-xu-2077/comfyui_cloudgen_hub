"""ComfyUI 云端生成节点包：小云雀（xyq.jianying.com）+ LibTV（mcp.liblib.tv）+ Lovart（lgw.lovart.ai）。"""
from __future__ import annotations

import asyncio
import json

from .cloudgen import flow, jobs, libtv, libtv_auth, lovart, lovart_auth, plans, prices, util, xyq
from .cloudgen.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, all_balances, balances_text

WEB_DIRECTORY = "./web"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


def _strip(m: dict) -> dict:
    return {k: v for k, v in m.items() if k not in ("raw", "raw_detail")}


def _register_routes():
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return
    routes = PromptServer.instance.routes

    async def run(fn, *a):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *a)

    def ok(data):
        return web.json_response(data, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))

    def err(exc, code=200):
        return web.json_response({"error": str(exc)}, status=code)

    @routes.get("/cloudgen/balances")
    async def balances(_req):
        items = await run(all_balances)
        return ok({"items": items, "text": balances_text(items)})

    @routes.get("/cloudgen/models")
    async def models(req):
        provider = req.query.get("provider", "xyq")
        kind = req.query.get("kind", "image")
        refresh = req.query.get("refresh") == "1"
        mod = {"xyq": xyq, "flow": flow, "lovart": lovart}.get(provider, libtv)
        try:
            items = await run(lambda: mod.get_models(kind, refresh))
            return ok({"models": [_strip(m) for m in items]})
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/model_detail")
    async def model_detail(req):
        try:
            d = await run(libtv.model_detail, req.query.get("kind", "image"), req.query.get("name", ""))
            return ok(_strip(d))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/config")
    async def get_config(_req):
        cfg = util.load_config()
        ak = xyq.access_key()
        return ok({"xyq_configured": bool(ak), "xyq_key_masked": (ak[:5] + "…" + ak[-4:]) if len(ak) > 10 else ("已设置" if ak else ""),
                   "xyq_team_id": cfg.get("xyq_team_id", ""), "libtv": libtv_auth.status(), "lovart": lovart_auth.status(),
                   "libtv_project_id": cfg.get("libtv_project_id", ""),
                   "flow_base_url": flow.base_url(), "flow_key_set": bool(flow.api_key())})

    @routes.post("/cloudgen/config")
    async def set_config(req):
        body = await req.json()
        patch = {}
        if "xyq_access_key" in body:
            v = str(body["xyq_access_key"] or "").strip()
            patch["xyq_access_key"] = v or None
        if "xyq_team_id" in body:
            patch["xyq_team_id"] = str(body["xyq_team_id"] or "").strip() or None
        for k in ("flow_base_url", "flow_api_key"):
            if k in body:
                patch[k] = str(body[k] or "").strip() or None
        if "libtv_project_id" in body:
            patch["libtv_project_id"] = str(body["libtv_project_id"] or "").strip() or None
        util.save_config(patch)
        xyq._cache.clear()
        flow._cache.clear()
        return ok({"saved": list(patch)})

    @routes.get("/cloudgen/plans")
    async def get_plans(_req):
        return ok(plans.load())

    @routes.post("/cloudgen/plans")
    async def set_plans(req):
        body = await req.json()
        try:
            return ok(plans.save(body.get("provider", ""), body))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/prices")
    async def get_prices(_req):
        return ok(prices.load())

    @routes.post("/cloudgen/prices")
    async def set_prices(req):
        body = await req.json()
        try:
            return ok({"rules": prices.save_rules(body.get("rules") or {})})
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/media_duration")
    async def media_duration(req):
        paths = [x for x in req.query.get("paths", "").split("\n") if x.strip()]
        items = await run(lambda: [{"path": x, "duration": util.media_duration(x)} for x in paths])
        return ok({"items": items})

    @routes.get("/cloudgen/xyq/thread")
    async def xyq_thread(req):
        """只读：查看小云雀任务的完整线程（智能体如何理解提示词和参考图），用于排查。"""
        try:
            d = await run(xyq._call, xyq.P_THREAD, {"thread_id": req.query.get("thread_id", ""),
                                                   "run_id": req.query.get("run_id", ""), "after_seq": 0}, 60)
            return ok(d)
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/flow/get")
    async def flow_get(req):
        """只读：转发 Flow2API 的 GET 请求（模型列表、健康状态、接口文档），用于查看与排查。"""
        path = req.query.get("path", "/v1/models")
        if not path.startswith("/") or path.startswith("/api/"):
            return err(util.CloudGenError("只允许读取公开接口"))
        try:
            return ok({"data": await run(flow.get_json, path, path != "/health", 30)})
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/jobs")
    async def jobs_list(_req):
        return ok({"jobs": jobs.listing()})

    @routes.post("/cloudgen/jobs/fetch")
    async def jobs_fetch(req):
        body = await req.json()
        try:
            return ok(await run(jobs.fetch, body.get("id", "")))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.post("/cloudgen/jobs/delete")
    async def jobs_delete(req):
        body = await req.json()
        jobs.remove(body.get("id", ""))
        return ok({"ok": True})

    @routes.get("/cloudgen/libtv/fetch_node")
    async def libtv_fetch_node(req):
        """把 LibTV 画布节点上的结果下载到 output/cloudgen，例 /cloudgen/libtv/fetch_node?node=i-xxxx"""
        try:
            return ok(await run(libtv.fetch_node, req.query.get("node", "")))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/libtv/call")
    async def libtv_call(req):
        """调试用：只读调用 LibTV MCP 工具，例 /cloudgen/libtv/call?tool=read_project&args={}"""
        try:
            args = json.loads(req.query.get("args") or "{}")
            return ok(await run(libtv.call_readonly, req.query.get("tool", ""), args))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/assets")
    async def assets_info(_req):
        return ok(util.asset_count())

    @routes.post("/cloudgen/assets/clear")
    async def assets_clear(req):
        body = await req.json()
        prov = body.get("provider") or ""
        return ok({"cleared": util.asset_clear(f"{prov}:" if prov else "")})

    @routes.post("/cloudgen/libtv/login")
    async def libtv_login(_req):
        try:
            url = await run(libtv_auth.start_login)
            return ok({"url": url})
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.post("/cloudgen/libtv/logout")
    async def libtv_logout(_req):
        libtv_auth.logout()
        libtv._model_cache.clear()
        return ok({"ok": True})

    @routes.post("/cloudgen/lovart/login")
    async def lovart_login(_req):
        try:
            url = await run(lovart_auth.start_login)
            return ok({"url": url})
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.post("/cloudgen/lovart/logout")
    async def lovart_logout(_req):
        lovart_auth.logout()
        lovart._model_cache.clear()
        return ok({"ok": True})

    @routes.get("/cloudgen/lovart/describe")
    async def lovart_describe(req):
        """只读：查看 Lovart 某个模型的完整参数 schema，例 /cloudgen/lovart/describe?model=openai/gpt-image-2"""
        try:
            return ok(await run(lovart.describe_model, req.query.get("model", "")))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/lovart/tools")
    async def lovart_tools(_req):
        try:
            tools = await run(lambda: lovart.client().tools(refresh=True))
            return ok({"count": len(tools), "path": lovart.TOOLS_DUMP,
                       "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:120]} for t in tools]})
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/libtv/probe")
    async def libtv_probe(_req):
        try:
            return ok(await run(libtv.probe))
        except Exception as exc:  # noqa: BLE001
            return err(exc)

    @routes.get("/cloudgen/libtv/tools")
    async def libtv_tools(_req):
        try:
            tools = await run(lambda: libtv.client().tools(refresh=True))
            return ok({"count": len(tools), "path": libtv.TOOLS_DUMP,
                       "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:120]} for t in tools]})
        except Exception as exc:  # noqa: BLE001
            return err(exc)


_register_routes()
