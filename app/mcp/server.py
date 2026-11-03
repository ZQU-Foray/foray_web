# -*- coding: utf-8 -*-
"""MCP（Model Context Protocol）streamable HTTP 端点。

形态：`POST /mcp`，JSON-RPC 2.0，单次请求单次响应（不启用 SSE 流）。
鉴权：**只认** `Authorization: Bearer <agent 令牌>`；浏览器会话 cookie 不适用——
agent 令牌是独立凭据，绑定一个成员身份与一组 scope，可单独撤销。

支持的方法（2025-06-18 协议的最小只读子集）：
- initialize / notifications/initialized / ping
- tools/list（按 scope 过滤）
- tools/call（scope 校验 → 工具层 → 审计）

安全边界：
- 令牌只在 `Authorization` 头里，不落日志、不落审计；
- 浏览器带 Origin 头一律拒绝（MCP 客户端不是浏览器；杜绝 DNS rebinding 借道）；
- 每个令牌每分钟最多 `MCP_RATE_PER_MIN` 次 tools/call；
- 未认证统一 401，不区分"令牌不存在"与"令牌已撤销"。
"""
import json
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config
from ..auth import agents
from . import scopes, tools

router = APIRouter()

SERVER_NAME = "foray-portal-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"
RATE_PER_MIN = int(getattr(config, "MCP_RATE_PER_MIN", 120))

_rate_lock = threading.Lock()
_rate = {}          # agent_id -> [窗口起点, 计数]


def _rpc_error(msg_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _rpc_ok(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse({"error": "unauthorized", "detail": detail}, status_code=401,
                        headers={"WWW-Authenticate": 'Bearer realm="foray-portal-mcp"'})


def _rate_ok(agent_id: str) -> bool:
    now = time.time()
    with _rate_lock:
        slot = _rate.get(agent_id)
        if not slot or now - slot[0] >= 60:
            _rate[agent_id] = [now, 1]
            return True
        if slot[1] >= RATE_PER_MIN:
            return False
        slot[1] += 1
        return True


def _tool_list(agent):
    out = []
    for name in scopes.visible_tools(agent, tools.TOOLS):
        meta = tools.TOOLS[name]
        out.append({"name": name, "description": meta["description"],
                    "inputSchema": meta["inputSchema"]})
    out.sort(key=lambda t: t["name"])
    return out


def _handle(msg, agent):
    """处理单条 JSON-RPC 消息，返回 (response | None, http_status)。"""
    if not isinstance(msg, dict):
        return _rpc_error(None, -32600, "请求必须是 JSON-RPC 对象"), 400
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(msg_id, -32602, "params 必须是对象"), 400
    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _rpc_error(msg_id, -32600, "缺少 jsonrpc/method"), 400

    if method == "initialize":
        return _rpc_ok(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }), 200
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None, 202
    if method == "ping":
        return _rpc_ok(msg_id, {}), 200
    if method == "tools/list":
        return _rpc_ok(msg_id, {"tools": _tool_list(agent)}), 200
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        meta = tools.TOOLS.get(name or "")
        if meta is None or name not in scopes.visible_tools(agent, tools.TOOLS):
            agents.audit("mcp_tool_call", agent=agent.get("id"), tool=name,
                         open_id=agent.get("open_id"), ok=False, reason="denied")
            return _rpc_error(msg_id, -32001,
                              "没有权限调用该工具（scope 不足或工具不存在）",
                              {"tool": name}), 200
        if not _rate_ok(agent["id"]):
            return _rpc_error(msg_id, -32002, "调用过于频繁，请稍后重试"), 200
        started = time.time()
        try:
            result = tools.call(name, agent, args)
        except tools.ToolError as e:
            agents.audit("mcp_tool_call", agent=agent.get("id"), tool=name,
                         open_id=agent.get("open_id"), ok=False,
                         reason=str(e), ms=round((time.time() - started) * 1000))
            return _rpc_error(msg_id, e.code, str(e)), 200
        except Exception as e:                      # 未预期异常：审计留痕，回通用错误
            agents.audit("mcp_tool_call", agent=agent.get("id"), tool=name,
                         open_id=agent.get("open_id"), ok=False,
                         reason=type(e).__name__,
                         ms=round((time.time() - started) * 1000))
            return _rpc_error(msg_id, -32603, "工具执行失败：%s" % type(e).__name__), 200
        agents.touch(agent["id"])
        agents.audit("mcp_tool_call", agent=agent.get("id"), tool=name,
                     open_id=agent.get("open_id"), ok=True,
                     ms=round((time.time() - started) * 1000))
        return _rpc_ok(msg_id, {
            "content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False, default=str)}],
            "isError": False}), 200

    return _rpc_error(msg_id, -32601, "不支持的方法：%s" % method), 200


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    # 浏览器来源一律拒绝：MCP 客户端不是浏览器（防 DNS rebinding 借道）
    if request.headers.get("origin"):
        return JSONResponse({"error": "forbidden",
                             "detail": "浏览器来源不允许访问 MCP 端点"},
                            status_code=403)
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return _unauthorized("缺少 Authorization: Bearer <agent 令牌>")
    agent = agents.verify(auth[7:].strip())
    if not agent:
        return _unauthorized("令牌无效或已撤销")
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, -32700, "请求体不是合法 JSON"),
                            status_code=400)
    if isinstance(payload, list):
        return JSONResponse(_rpc_error(None, -32600, "不支持批量请求"), status_code=400)
    resp, status = _handle(payload, agent)
    if resp is None:
        return JSONResponse(None, status_code=status)
    return JSONResponse(resp, status_code=status)


@router.get("/mcp")
def mcp_get():
    """不提供 SSE 流：GET 用于探活时给出可读说明，客户端应改用 POST。"""
    return JSONResponse({"error": "method_not_allowed",
                         "detail": "本端点只支持 POST（JSON-RPC）"},
                        status_code=405, headers={"Allow": "POST"})
