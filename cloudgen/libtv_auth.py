"""LibTV Remote MCP 的 OAuth 2.1（授权码 + PKCE + 动态注册 + RFC 8707 resource）。

流程照搬 dsh-libtv 的 lib/oauth.mjs：
  元数据   https://mcp.liblib.tv/.well-known/oauth-authorization-server
  资源     https://mcp.liblib.tv/.well-known/oauth-protected-resource
  回调     http://127.0.0.1:<port>/callback （默认 8765）
凭据写在节点包目录 libtv_credentials.json（与 dsh-libtv 的 ~/.libtv/credentials.json 格式兼容，
若本包里没有而 ~/.libtv 下有，会直接复用）。
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser

from . import util
from .util import CloudGenError

ISSUER = os.environ.get("LIBTV_ISSUER", "https://mcp.liblib.tv")
MCP_URL = os.environ.get("LIBTV_MCP_URL", "https://mcp.liblib.tv/mcp")
SCOPES = ["libtv:account:read", "libtv:project:read", "libtv:project:write", "libtv:media:write",
          "libtv:task:write", "libtv:export:write"]
CRED_FILE = os.path.join(util.PACK_DIR, "libtv_credentials.json")
LEGACY_CRED = os.path.join(os.path.expanduser("~"), ".libtv", "credentials.json")
CLIENT_NAME = "ComfyUI LibTV nodes"

_lock = threading.Lock()
_login_state: dict = {"running": False, "url": "", "error": "", "done_at": 0}


def port() -> int:
    return int(os.environ.get("LIBTV_OAUTH_PORT") or util.load_config().get("libtv_oauth_port") or 8765)


def redirect_uri() -> str:
    return f"http://127.0.0.1:{port()}/callback"


def load_creds() -> dict:
    for p in (CRED_FILE, LEGACY_CRED):
        try:
            with open(p, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                if isinstance(data, dict) and (p == CRED_FILE or data.get("tokens")):
                    return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def save_creds(creds: dict):
    tmp = CRED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(creds, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, CRED_FILE)


def clear_creds():
    try:
        os.remove(CRED_FILE)
    except OSError:
        pass


_meta: dict | None = None
_resource: str | None = None


def discover() -> dict:
    global _meta
    if _meta:
        return _meta
    status, _, data = util.http_request(f"{ISSUER}/.well-known/oauth-authorization-server",
                                        headers={"Accept": "application/json"}, timeout=20)
    if status >= 400:
        raise CloudGenError(f"读取 LibTV 授权服务器元数据失败 HTTP {status}")
    _meta = json.loads(data.decode("utf-8"))
    return _meta


def resource() -> str:
    global _resource
    if _resource:
        return _resource
    try:
        status, _, data = util.http_request(f"{ISSUER}/.well-known/oauth-protected-resource",
                                            headers={"Accept": "application/json"}, timeout=20)
        if status < 400:
            _resource = json.loads(data.decode("utf-8")).get("resource") or MCP_URL
            return _resource
    except Exception:  # noqa: BLE001
        pass
    _resource = MCP_URL
    return _resource


def ensure_client() -> dict:
    meta = discover()
    creds = load_creds()
    if creds.get("client_id") and creds.get("redirect_uri") == redirect_uri() and creds.get("issuer") == meta.get("issuer"):
        return creds
    reg = meta.get("registration_endpoint")
    if not reg:
        raise CloudGenError("LibTV 授权服务器未提供动态注册端点")
    status, body = util.post_json(reg, {
        "client_name": CLIENT_NAME, "redirect_uris": [redirect_uri()],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": " ".join(SCOPES)}, timeout=30)
    if status >= 400 or not isinstance(body, dict):
        raise CloudGenError(f"LibTV 动态注册失败 HTTP {status}: {str(body)[:300]}")
    creds = {k: v for k, v in creds.items() if k == "tokens"} if creds.get("issuer") == meta.get("issuer") else {}
    creds.update({"client_id": body.get("client_id"), "client_secret": body.get("client_secret"),
                  "redirect_uri": redirect_uri(), "issuer": meta.get("issuer"), "service": MCP_URL})
    save_creds(creds)
    return creds


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _token_request(form: dict) -> dict:
    meta = discover()
    body = urllib.parse.urlencode({k: v for k, v in form.items() if v}).encode()
    status, _, data = util.http_request(meta["token_endpoint"], method="POST", body=body, timeout=30, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    if status >= 400:
        raise CloudGenError(f"LibTV token 请求失败 HTTP {status}: {data[:300]!r}")
    return json.loads(data.decode("utf-8"))


def _store_tokens(creds: dict, tok: dict) -> dict:
    exp = tok.get("expires_in")
    old = creds.get("tokens") or {}
    creds["tokens"] = {
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
        "token_type": tok.get("token_type") or "Bearer",
        "scope": tok.get("scope"),
        "expires_at": int((time.time() + float(exp)) * 1000) if exp else None,
        "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_creds(creds)
    return creds["tokens"]


def start_login(open_browser: bool = True) -> str:
    """启动授权：返回授权链接；回调在后台线程里处理。"""
    with _lock:
        if _login_state["running"] and _login_state["url"]:
            return _login_state["url"]
        meta = discover()
        creds = ensure_client()
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(secrets.token_bytes(16))
        q = {"response_type": "code", "client_id": creds["client_id"], "redirect_uri": redirect_uri(),
             "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256", "resource": resource()}
        url = meta["authorization_endpoint"] + ("&" if "?" in meta["authorization_endpoint"] else "?") + \
            urllib.parse.urlencode(q, quote_via=urllib.parse.quote)
        _login_state.update(running=True, url=url, error="")

    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静默
            pass

        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(u.query)
            err = (qs.get("error") or [""])[0]
            ok = not err and (qs.get("state") or [""])[0] == state and (qs.get("code") or [""])[0]
            result.update(code=(qs.get("code") or [""])[0], error=err or ("" if ok else "state 校验失败或缺少 code"))
            html = (f"<!doctype html><meta charset='utf-8'><title>LibTV 授权</title><body style='font:16px system-ui;"
                    f"padding:48px'><h2 style='color:{'#07B8DD' if ok else '#c33'}'>{'授权完成' if ok else '授权失败'}</h2>"
                    f"<p>{'可以关闭此页面，回到 ComfyUI。' if ok else result['error']}</p></body>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

    def worker():
        try:
            srv = http.server.HTTPServer(("127.0.0.1", port()), Handler)
        except OSError as exc:
            _login_state.update(running=False, error=f"无法监听 127.0.0.1:{port()}：{exc}（可在 config.json 设 libtv_oauth_port）")
            return
        srv.timeout = 1
        deadline = time.time() + 600
        try:
            while not result and time.time() < deadline:
                srv.handle_request()
        finally:
            srv.server_close()
        try:
            if not result:
                raise CloudGenError("授权超时（10 分钟未完成）")
            if result.get("error"):
                raise CloudGenError(f"LibTV 授权失败：{result['error']}")
            creds2 = load_creds() if os.path.exists(CRED_FILE) else creds
            tok = _token_request({"grant_type": "authorization_code", "code": result["code"],
                                  "redirect_uri": redirect_uri(), "client_id": creds2["client_id"],
                                  "code_verifier": verifier, "resource": resource(),
                                  "client_secret": creds2.get("client_secret")})
            _store_tokens(creds2, tok)
            _login_state.update(running=False, error="", done_at=time.time(), url="")
        except Exception as exc:  # noqa: BLE001
            _login_state.update(running=False, error=str(exc), url="")

    threading.Thread(target=worker, daemon=True, name="libtv-oauth").start()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    return url


def _refresh(creds: dict) -> str:
    rt = (creds.get("tokens") or {}).get("refresh_token")
    if not rt:
        raise CloudGenError("LibTV 尚未授权：请在侧边栏「云端积分」面板点击「LibTV 授权」并微信扫码")
    try:
        tok = _token_request({"grant_type": "refresh_token", "refresh_token": rt, "client_id": creds.get("client_id"),
                              "resource": resource(), "client_secret": creds.get("client_secret")})
    except CloudGenError as exc:
        raise CloudGenError(f"LibTV 授权已失效，请重新授权（{exc}）") from exc
    if creds.get("redirect_uri") and not os.path.exists(CRED_FILE):
        save_creds(creds)
    return _store_tokens(creds, tok)["access_token"]


_refresh_lock = threading.Lock()


def access_token(force: bool = False) -> str:
    creds = load_creds()
    tok = creds.get("tokens") or {}
    if not tok.get("access_token"):
        raise CloudGenError("LibTV 尚未授权：请在侧边栏「云端积分」面板点击「LibTV 授权」并微信扫码")
    exp = tok.get("expires_at")
    fresh = not isinstance(exp, (int, float)) or exp - 60_000 > time.time() * 1000
    if fresh and not force:
        return tok["access_token"]
    with _refresh_lock:
        creds = load_creds()
        tok = creds.get("tokens") or {}
        exp = tok.get("expires_at")
        if not force and isinstance(exp, (int, float)) and exp - 60_000 > time.time() * 1000:
            return tok["access_token"]
        return _refresh(creds)


def status() -> dict:
    creds = load_creds()
    tok = creds.get("tokens") or {}
    exp = tok.get("expires_at")
    return {
        "authorized": bool(tok.get("access_token")),
        "has_refresh": bool(tok.get("refresh_token")),
        "token_expires": util.fmt_time(exp) if exp else "",
        "login_running": _login_state["running"],
        "login_url": _login_state["url"],
        "login_error": _login_state["error"],
    }


def logout():
    clear_creds()
