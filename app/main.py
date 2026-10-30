"""局域网 STM32 烧录/调试 Web 服务入口。

启动: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import base64
import logging
import time
import time as _time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import config, device_registry, devices, firmware, motor_presets, presets, sessions, shoot, vllink_config
from .auth import accounts as auth_accounts
from .auth import emergency as auth_emergency
from .auth import feishu as auth_feishu
from .auth import guard as auth_guard
from .auth import sessions as auth_sessions
from .portal import api as portal_api
from .portal import pusher as portal_pusher
from .portal import store as portal_store
from .portal import sync as portal_sync

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("webserver")

_devices_cache = {"probes": [], "ports": [], "at": 0.0}


async def _device_poller():
    """周期扫描 USB 设备（Linux 上可用 pyudev 事件驱动，轮询更简单通用）。"""
    while True:
        try:
            _devices_cache["probes"] = devices.list_probes()
            _devices_cache["ports"] = devices.list_serial_ports()
            _devices_cache["at"] = asyncio.get_event_loop().time()
        except Exception as e:
            log.warning("设备扫描失败: %s", e)
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    sessions.init(loop)          # 按设备会话（legacy-default 预热）
    presets.load()  # 首次启动生成默认配置文件
    device_registry.bootstrap_legacy()  # 首次启动生成 legacy-default 设备
    task = asyncio.create_task(_device_poller())
    recheck_task = asyncio.create_task(auth_guard.recheck_loop())  # 成员复核
    # 考勤：零点孤儿会话自动作废（守护线程含停机补跑）
    portal_api.start_midnight_clearer()
    # 门户同步：后台工作线程，启动不等待飞书（阶段 4）
    portal_sync.scheduler().start()
    # 本地缓存库推送线程：把排队的网页改动推到飞书（direct 模式下队列通常为空）
    portal_pusher.pusher().start()
    log.info("服务已启动: http://%s:%d（门户写入模式 %s）", config.HOST, config.PORT,
             config.PORTAL_WRITE_MODE)
    yield
    task.cancel()
    recheck_task.cancel()
    # 退出取消（§6.4.10）：join 放线程池，避免阻塞事件循环关闭
    await asyncio.to_thread(portal_pusher.pusher().stop)
    await asyncio.to_thread(portal_sync.scheduler().stop)
    portal_store.close()
    sessions.store().close_all()


app = FastAPI(title="STM32 局域网烧录/调试服务", lifespan=lifespan)


@app.exception_handler(portal_store.SchemaVersionError)
async def _schema_version_mismatch(request: Request, exc: portal_store.SchemaVersionError):
    """快照库版本不匹配 → 明确 503 + 可操作提示（阶段 7 故障关闭）。

    不返回空数据、不静默降级：宁可数据接口整体报"存储版本不匹配"，
    也不能把未知版本的行按当前 schema 解释（可能读错或写坏）。
    """
    from fastapi.responses import JSONResponse as _JSON
    log.error("门户快照库版本不匹配，数据接口拒绝服务: %s", exc)
    return _JSON({"detail": str(exc), "code": exc.code,
                  "schema_found": exc.found, "schema_expected": exc.expected,
                  "snapshot_path": exc.path,
                  "hint": "备份该文件后按 docs/portal-operations.md「快照库版本不匹配」处置"},
                 status_code=503)

# 认证中间件（D1 统一策略，后注册 = 外层先执行；详 app/auth/guard.py 矩阵）
auth_guard.install(app)


@app.middleware("http")
async def _static_no_heuristic_cache(request, call_next):
    """静态资源缓存分层（v2）：
    ① 字体/图片素材（文件名带内容哈希）：max-age=604800 强缓存——
       多页应用每次导航零往返，消除品牌 logo 弹入与字体 FOUT；
    ② 公开样式与脚本允许后台重验证；页面、接口、跳转及设置会话的
       响应禁止缓存，避免共享缓存混用身份或重放授权跳转。
    背景：v1 的全线 no-cache 让每次导航都在重验证往返完成后才首绘，
    白屏窗口与标签页加载态在页间切换时反复可见。"""
    response = await call_next(request)
    path = request.url.path
    if (path.startswith(("/api", "/auth/", "/ws"))
            or "text/html" in response.headers.get("content-type", "")
            or response.status_code >= 300
            or "set-cookie" in response.headers):
        response.headers["Cache-Control"] = "no-store"
        return response
    if path.startswith("/app/assets/"):
        # Vue 构建产物：文件名带内容哈希，内容变了文件名就变，可永久缓存
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
    if (path.startswith("/ui/fonts/")
            or path.startswith("/ui/assets/")
            or path == "/ui/foray-robomaster.svg"
            or path.endswith("favicon.png")):
        response.headers["Cache-Control"] = "public, max-age=604800"
    else:
        response.headers["Cache-Control"] = (
            "public, max-age=0, stale-while-revalidate=604800")
    return response


# ================= 设备注册表 v2（阶段 1） =================

def _device_or_400(device_id):
    try:
        return device_registry.get(device_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/v2/devices")
def v2_devices_list():
    doc = device_registry.load()
    return {"devices": list(doc["devices"].values())}


@app.post("/api/v2/devices")
def v2_devices_register(body: dict):
    try:
        dev = device_registry.register(body)
        return dev["devices"][body.get("id")]
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/v2/devices/{device_id}")
def v2_devices_get(device_id: str):
    return _device_or_400(device_id)


@app.patch("/api/v2/devices/{device_id}")
def v2_devices_patch(device_id: str, body: dict):
    _device_or_400(device_id)
    try:
        doc = device_registry.update(device_id, body)
        return doc["devices"][device_id]
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/v2/devices/{device_id}/probe")
def v2_probe_device(device_id: str):
    """探测设备可达性：TCP 设备只连 DAP 端口，稳定不碰 UART 端口。

    背景：VLLINK Basic2 的 TCP-UART 可能只允许单客户端；前端每 5s 的健康探测
    若去连 uart_port(20010)，会顶掉/抢占正在使用的串口会话，用户表现为
    “网页显示调试器在线，但串口收不到数据”。因此 UART 状态改由进程内会话
    状态回答；设备忙（串口/发射会话或烧录中）时也不发起新的 TCP 连接。
    """
    import socket as _socket
    dev = _device_or_400(device_id)
    if dev.get("transport") == "vllink_tcp":
        host = dev.get("host", "")
        sess = sessions.store().peek(device_id)      # 只读查看，不创建会话
        uart_session = bool(sess and (sess.serial.is_open or sess.shoot.is_open))
        busy = uart_session or bool(
            sess and sess.flash.status().get("state") == "running")
        if busy:
            # 已有链路使用中：不发起任何新连接，避免干扰串口/发射/烧录链路
            return {"reachable": True, "host": host,
                    "ports": {"dap": None, "uart_session": uart_session},
                    "busy": True, "checked": _time.time()}
        dap_ok = False
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect((host, dev.get("dap_port", 4441)))
            dap_ok = True
        except Exception:
            dap_ok = False
        finally:
            try:
                s.close()
            except Exception:
                pass
        return {"reachable": dap_ok, "host": host,
                "ports": {"dap": dap_ok, "uart_session": uart_session},
                "busy": False, "checked": _time.time()}
    else:
        # USB：检查是否有任何串口设备
        ports = devices.list_serial_ports()
        return {"reachable": len(ports) > 0, "ports": {"usb": len(ports)},
                "checked": _time.time()}


@app.delete("/api/v2/devices/{device_id}")
def v2_devices_delete(device_id: str):
    try:
        device_registry.remove(device_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---- v2 VLLINK 配置（阶段 5） ----

@app.get("/api/v2/devices/{device_id}/vllink-config")
def v2_vllink_config_get(device_id: str):
    """读取设备配置 + 返回表单 schema。"""
    dev = _device_or_400(device_id)
    serial = dev.get("probe_serial")
    cfg = vllink_config.VllinkUsbConfig(serial)
    if not cfg.find():
        raise HTTPException(400, "未找到 VLLINK 设备（需以 USB 有线模式连接服务器）")
    try:
        raw = cfg.read_config()
        parsed = vllink_config.parse_config(raw)
        # 密码脱敏：仅返回是否已设置
        safe = {k: v for k, v in parsed.items()
                if "password" not in k.lower()}
        safe["_password_set"] = any(
            "password" in k.lower() and v for k, v in parsed.items())
        return {"config": safe, "raw_length": len(raw),
                "schema": vllink_config.config_schema()}
    except Exception as e:
        raise HTTPException(500, f"读取配置失败: {e}")
    finally:
        cfg.close()


@app.patch("/api/v2/devices/{device_id}/vllink-config")
def v2_vllink_config_patch(device_id: str, body: dict):
    """字段级 patch：合并写入 + 回读验证。"""
    dev = _device_or_400(device_id)
    serial = dev.get("probe_serial")
    cfg = vllink_config.VllinkUsbConfig(serial)
    if not cfg.find():
        raise HTTPException(400, "未找到 VLLINK 设备")
    try:
        raw = cfg.read_config()
        patched_text = vllink_config.build_config(raw, body.get("patch", {}))
        # 字节数校验
        encoded = patched_text.encode("utf-8")
        if len(encoded) > 4096:
            raise HTTPException(400, f"配置超长（{len(encoded)} > 4096 字节）")
        # 写入 + 提交
        cfg.write_config(patched_text)
        cfg.commit_config()
        # 回读验证
        verify = cfg.read_config()
        v_parsed = vllink_config.parse_config(verify)
        result = {}
        for k in body.get("patch", {}):
            if k in v_parsed:
                result[k] = "verified" if str(v_parsed[k]) == str(body["patch"][k]) else "mismatch"
            else:
                result[k] = "not_found"
        return {"ok": True, "fields": result,
                "needs_restart": any(k.startswith(("Mode", "Wireless")) for k in body.get("patch", {}))}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"写入配置失败: {e}")
    finally:
        cfg.close()




# ================= v2 按设备业务端点（阶段 4） =================

def _sess_or_404(device_id):
    try:
        return sessions.store().get(device_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/v2/devices/{device_id}/flash")
def v2_flash(device_id: str, body: dict):
    sess = _sess_or_404(device_id)
    try:
        job_id = sess.flash.start(
            body.get("firmware"), body.get("target"),
            body.get("probe", "auto"), body.get("address"))
        return {"job": job_id, "device": device_id}
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/v2/devices/{device_id}/flash/log")
def v2_flash_log(device_id: str, since: int = 0):
    return _sess_or_404(device_id).flash.log_since(since)


@app.websocket("/ws/v2/devices/{device_id}/flash")
async def v2_ws_flash(ws: WebSocket, device_id: str):
    auth = await auth_guard.ws_auth(ws)
    if not auth:
        return
    _tok, _rec = auth
    fm = _sess_or_404(device_id).flash
    q = fm.subscribe()
    job = fm._job
    if job:
        for l in list(job.lines)[-200:]:
            await ws.send_json({"type": "log", "stream": l["s"],
                                "line": l["line"], "job": job.id, "seq": l["seq"]})

    async def sender():
        try:
            while True:
                await ws.send_json(await q.get())
        except Exception:
            pass

    send_task = asyncio.create_task(sender())
    checker = asyncio.create_task(auth_guard.ws_periodic_check(ws, _tok))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()
        checker.cancel()
        fm.unsubscribe(q)


# ---- v2 串口 ----

@app.post("/api/v2/devices/{device_id}/serial/open")
def v2_serial_open(device_id: str, body: dict):
    sess = _sess_or_404(device_id)
    if not sess.open_serial(body.get("port"), body.get("baudrate")):
        raise HTTPException(400, "打开串口失败")
    return {"ok": True, "status": sess.serial.status()}


@app.post("/api/v2/devices/{device_id}/serial/close")
def v2_serial_close(device_id: str):
    sess = _sess_or_404(device_id)
    sess.serial.close()
    return {"ok": True, "status": sess.serial.status()}


@app.post("/api/v2/devices/{device_id}/serial/send")
def v2_serial_send(device_id: str, body: dict):
    sess = _sess_or_404(device_id)
    if "b64" in body:
        data = base64.b64decode(body["b64"])
    elif "text" in body:
        data = str(body["text"]).encode("utf-8")
    else:
        raise HTTPException(400, "需要 b64 或 text 字段")
    return {"ok": sess.serial.write(data), "status": sess.serial.status()}


@app.get("/api/v2/devices/{device_id}/serial/history")
def v2_serial_history(device_id: str, since: float = 0):
    sess = _sess_or_404(device_id)
    events = [m for m in list(sess.serial._history) if m["ts"] > since]
    return {"events": events, "status": sess.serial.status()}


@app.websocket("/ws/v2/devices/{device_id}/serial")
async def v2_ws_serial(ws: WebSocket, device_id: str):
    auth = await auth_guard.ws_auth(ws)
    if not auth:
        return
    _tok, _rec = auth
    sess = _sess_or_404(device_id)
    q = sess.serial.subscribe()

    async def sender():
        try:
            while True:
                await ws.send_json(await q.get())
        except Exception:
            pass

    send_task = asyncio.create_task(sender())
    checker = asyncio.create_task(auth_guard.ws_periodic_check(ws, _tok))
    try:
        while True:
            raw = await ws.receive_json()
            t = raw.get("type")
            # 命令前本地撤销即时检查（阶段 3：失效连接不接受控制指令）
            if t in ("open", "close", "tx") and not auth_guard.ws_command_allowed(_tok):
                await ws.close(code=4403, reason="会话已失效，拒绝控制指令")
                return
            if t == "open":
                sess.open_serial(raw.get("port"))
            elif t == "close":
                sess.serial.close()
            elif t == "tx":
                try:
                    sess.serial.write(base64.b64decode(raw.get("b64", "")))
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()
        checker.cancel()
        sess.serial.unsubscribe(q)


# ---- v2 发射调试 ----

def _v2_shoot(device_id):
    return _sess_or_404(device_id).shoot


@app.post("/api/v2/devices/{device_id}/shoot/open")
def v2_shoot_open(device_id: str, body: dict):
    sess = _sess_or_404(device_id)
    if not sess.open_shoot(body.get("port"), body.get("baudrate")):
        raise HTTPException(400, "打开发射调试串口失败")
    return {"ok": True, "status": sess.shoot.status()}


@app.post("/api/v2/devices/{device_id}/shoot/close")
def v2_shoot_close(device_id: str):
    sess = _sess_or_404(device_id)
    sess.shoot.close()
    return {"ok": True, "status": sess.shoot.status()}


@app.get("/api/v2/devices/{device_id}/shoot/status")
def v2_shoot_status(device_id: str):
    return _v2_shoot(device_id).status()


@app.post("/api/v2/devices/{device_id}/shoot/ping")
def v2_shoot_ping(device_id: str):
    rtt = _v2_shoot(device_id).ping()
    if rtt is None:
        raise HTTPException(400, "Pong 超时")
    return {"ok": True, "rtt_ms": rtt}


@app.post("/api/v2/devices/{device_id}/shoot/{action}")
def v2_shoot_action(device_id: str, action: str, body: dict):
    m = _v2_shoot(device_id)
    slot = int(body.get("slot", 0))
    if action == "mode":
        return m.set_mode(slot, int(body.get("mode", 0)))
    if action == "model":
        return m.set_model(slot, int(body.get("model", 0)))
    if action == "enable":
        return m.set_enable(slot, bool(body.get("on")))
    if action == "param":
        return m.set_param(slot, body.get("key"), body.get("value"))
    if action == "apply":
        return m.apply(slot)
    if action == "estop":
        return m.estop()
    if action == "fault_clear":
        return m.fault_clear(slot)
    raise HTTPException(404, f"未知操作 {action}")


@app.websocket("/ws/v2/devices/{device_id}/shoot")
async def v2_ws_shoot(ws: WebSocket, device_id: str):
    auth = await auth_guard.ws_auth(ws)
    if not auth:
        return
    _tok, _rec = auth
    sm = _v2_shoot(device_id)
    q = sm.subscribe()
    if sm.telemetry:
        await ws.send_json({"type": "telemetry", **sm.telemetry, "ts": time.time()})

    async def sender():
        try:
            while True:
                await ws.send_json(await q.get())
        except Exception:
            pass

    send_task = asyncio.create_task(sender())
    checker = asyncio.create_task(auth_guard.ws_periodic_check(ws, _tok))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()
        checker.cancel()
        sm.unsubscribe(q)


# ---- v2 电机预设（设备×槽位×型号） ----

@app.get("/api/v2/devices/{device_id}/shoot/presets")
def v2_shoot_presets_get(device_id: str):
    _sess_or_404(device_id)  # 验证设备存在
    return motor_presets.load(device_id)


@app.put("/api/v2/devices/{device_id}/shoot/presets")
def v2_shoot_presets_put(device_id: str, body: dict):
    _sess_or_404(device_id)
    try:
        return motor_presets.save(body, device_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/v2/devices/{device_id}/presets")
def v2_presets_get(device_id: str):
    _device_or_400(device_id)
    return presets.load(device_id)


@app.put("/api/v2/devices/{device_id}/presets")
def v2_presets_put(device_id: str, body: dict):
    _device_or_400(device_id)
    return presets.save(body, device_id)


@app.get("/api/v2/devices/{device_id}/status")
def v2_status(device_id: str):
    sess = _sess_or_404(device_id)
    return {
        "devices": _devices_cache,
        "serial": sess.serial.status(),
        "flash": sess.flash.status(),
        "targets": [{"key": k, **v} for k, v in config.TARGETS.items()],
        "probes": [{"key": k, **v} for k, v in config.PROBES.items()],
        "firmware_dir": str(config.FIRMWARE_DIR),
    }


# ================= REST =================

@app.get("/api/status")
def api_status():
    return {
        "devices": _devices_cache,
        "serial": sessions.store().get("legacy-default").serial.status(),
        "flash": _legacy().flash.status(),
        "targets": [{"key": k, **v} for k, v in config.TARGETS.items()],
        "probes": [{"key": k, **v} for k, v in config.PROBES.items()],
        "firmware_dir": str(config.FIRMWARE_DIR),
    }


@app.get("/api/firmware")
def api_firmware_list():
    return {"items": firmware.list_firmware(), "dir": str(config.FIRMWARE_DIR)}


@app.post("/api/firmware/upload")
async def api_firmware_upload(file: UploadFile):
    name = file.filename or "unnamed"
    try:
        if file.size is not None and file.size > config.MAX_UPLOAD_BYTES:
            raise ValueError("文件超过 64MB 上限")
        return await asyncio.to_thread(firmware.save_upload_stream, name, file.file)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        await file.close()


@app.delete("/api/firmware/{name}")
def api_firmware_delete(name: str):
    try:
        firmware.delete_firmware(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/flash")
def api_flash_start(body: dict):
    try:
        job_id = _legacy().flash.start(
            body.get("firmware"), body.get("target"),
            body.get("probe", "auto"), body.get("address"))
        return {"job": job_id}
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/flash/log")
def api_flash_log(since: int = 0):
    """烧录日志轮询（绝对游标 seq，deque 满后不失效——阶段 3 修复）。"""
    return _legacy().flash.log_since(since)


@app.get("/api/presets")
def api_presets_get():
    return presets.load()


@app.put("/api/presets")
def api_presets_put(body: dict):
    return presets.save(body)


# ================= 串口 REST（命令行/agent 用，无需 WebSocket） =================

def _legacy():
    return sessions.store().get("legacy-default")


@app.post("/api/serial/open")
def api_serial_open(body: dict):
    sess = _legacy()
    if not sess.open_serial(body.get("port"), body.get("baudrate")):
        raise HTTPException(400, "打开串口失败，详见服务端日志")
    return {"ok": True, "status": sess.serial.status()}


@app.post("/api/serial/close")
def api_serial_close():
    sess = _legacy()
    sess.serial.close()
    return {"ok": True, "status": sess.serial.status()}


@app.post("/api/serial/send")
def api_serial_send(body: dict):
    if "b64" in body:
        data = base64.b64decode(body["b64"])
    elif "text" in body:
        data = str(body["text"]).encode("utf-8")
    else:
        raise HTTPException(400, "需要 b64 或 text 字段")
    sess = _legacy()
    return {"ok": sess.serial.write(data), "status": sess.serial.status()}


@app.get("/api/serial/history")
def api_serial_history(since: float = 0):
    """返回 since 时间戳之后的串口事件（轮询式）。"""
    sess = _legacy()
    events = [m for m in list(sess.serial._history) if m["ts"] > since]
    return {"events": events, "status": sess.serial.status()}


# ================= WebSocket =================

@app.websocket("/ws/flash")
async def ws_flash(ws: WebSocket):
    auth = await auth_guard.ws_auth(ws)
    if not auth:
        return
    _tok, _rec = auth
    fm = _legacy().flash
    q = fm.subscribe()
    job = fm._job
    if job:
        for l in list(job.lines)[-200:]:
            await ws.send_json({"type": "log", "stream": l["s"],
                                "line": l["line"], "job": job.id,
                                "seq": l["seq"]})

    async def sender():
        try:
            while True:
                await ws.send_json(await q.get())
        except Exception:
            pass

    send_task = asyncio.create_task(sender())
    checker = asyncio.create_task(auth_guard.ws_periodic_check(ws, _tok))
    try:
        while True:
            await ws.receive_text()  # 仅用于感知断连
    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()
        checker.cancel()
        fm.unsubscribe(q)


@app.websocket("/ws/serial")
async def ws_serial(ws: WebSocket):
    auth = await auth_guard.ws_auth(ws)
    if not auth:
        return
    _tok, _rec = auth
    sess = _legacy()
    q = sess.serial.subscribe()

    async def sender():
        try:
            while True:
                await ws.send_json(await q.get())
        except Exception:
            pass

    send_task = asyncio.create_task(sender())
    checker = asyncio.create_task(auth_guard.ws_periodic_check(ws, _tok))
    try:
        while True:
            raw = await ws.receive_json()
            t = raw.get("type")
            # 命令前本地撤销即时检查（阶段 3：失效连接不接受控制指令）
            if t in ("open", "close", "tx") and not auth_guard.ws_command_allowed(_tok):
                await ws.close(code=4403, reason="会话已失效，拒绝控制指令")
                return
            if t == "open":
                sess.open_serial(raw.get("port"))   # 统一互斥（会话层）
            elif t == "close":
                sess.serial.close()
            elif t == "tx":
                try:
                    sess.serial.write(base64.b64decode(raw.get("b64", "")))
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()
        checker.cancel()
        sess.serial.unsubscribe(q)


# ================= 发射机构电机调试 =================

@app.post("/api/shoot/open")
def api_shoot_open(body: dict):
    sess = _legacy()
    if not sess.open_shoot(body.get("port"), body.get("baudrate")):
        raise HTTPException(400, "打开发射调试串口失败，详见服务端日志")
    return {"ok": True, "status": sess.shoot.status()}


@app.post("/api/shoot/close")
def api_shoot_close():
    sess = _legacy()
    sess.shoot.close()
    return {"ok": True, "status": sess.shoot.status()}


@app.get("/api/shoot/status")
def api_shoot_status():
    return _legacy().shoot.status()


@app.get("/api/shoot/presets")
def api_shoot_presets_get():
    """电机调参预设（按 槽位×型号 命名空间，Web 侧存储）。"""
    return motor_presets.load()


@app.put("/api/shoot/presets")
def api_shoot_presets_put(body: dict):
    try:
        return motor_presets.save(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/shoot/ping")
def api_shoot_ping():
    rtt = _legacy().shoot.ping()
    if rtt is None:
        raise HTTPException(400, "Pong 超时")
    return {"ok": True, "rtt_ms": rtt}


@app.post("/api/shoot/mode")
def api_shoot_mode(body: dict):
    return _legacy().shoot.set_mode(int(body.get("slot", 0)), int(body.get("mode", 0)))


@app.post("/api/shoot/model")
def api_shoot_model(body: dict):
    return _legacy().shoot.set_model(int(body.get("slot", 0)), int(body.get("model", 0)))


@app.post("/api/shoot/enable")
def api_shoot_enable(body: dict):
    return _legacy().shoot.set_enable(int(body.get("slot", 0)), bool(body.get("on")))


@app.post("/api/shoot/param")
def api_shoot_param(body: dict):
    return _legacy().shoot.set_param(int(body.get("slot", 0)),
                                   body.get("key"), body.get("value"))


@app.post("/api/shoot/apply")
def api_shoot_apply(body: dict):
    return _legacy().shoot.apply(int(body.get("slot", 0)))


@app.post("/api/shoot/estop")
def api_shoot_estop():
    return _legacy().shoot.estop()


@app.post("/api/shoot/fault_clear")
def api_shoot_fault_clear(body: dict):
    return _legacy().shoot.fault_clear(int(body.get("slot", 0)))


@app.websocket("/ws/shoot")
async def ws_shoot(ws: WebSocket):
    auth = await auth_guard.ws_auth(ws)
    if not auth:
        return
    _tok, _rec = auth
    sess = _legacy()
    q = sess.shoot.subscribe()
    if sess.shoot.telemetry:
        await ws.send_json({"type": "telemetry", **sess.shoot.telemetry,
                            "ts": time.time()})

    async def sender():
        try:
            while True:
                await ws.send_json(await q.get())
        except Exception:
            pass

    send_task = asyncio.create_task(sender())
    checker = asyncio.create_task(auth_guard.ws_periodic_check(ws, _tok))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()
        checker.cancel()
        sess.shoot.unsubscribe(q)


# ================= STA 局域网扫描（阶段 4 补充） =================

@app.post("/api/v2/devices/scan")
def v2_scan_sta():
    """扫描本网段 4441/20010 端口，自动发现在线的 Basic2 STA。

    原理：Basic2 STA 固件 ≥V00.50 在 4441(TCP-DAP) 和 20010(TCP-UART)
    端口监听。通过 socket 探测子网内这两端口同时开放的 IP。
    扫描范围：服务器所在 /24 子网（可配置 SCAN_SUBNET）。
    """
    import socket
    import concurrent.futures
    import os

    subnet = os.environ.get("SCAN_SUBNET", "")
    if not subnet:
        # 自动推断：取服务器任一非 loopback IPv4 的 /24
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            subnet = ip.rsplit(".", 1)[0]
        except Exception:
            return {"found": [], "error": "无法推断子网"}

    found = []
    probe_ports = [4441, 20010]

    # 正在使用串口/发射会话的设备：只探 DAP 端口，不去碰 UART 端口
    # （Basic2 的 TCP-UART 可能单客户端，探测会顶掉正在使用的链路）
    active_hosts = set()
    for d in device_registry.load()["devices"].values():
        if d.get("transport") != "vllink_tcp":
            continue
        sess = sessions.store().peek(d["id"])
        if sess and (sess.serial.is_open or sess.shoot.is_open):
            active_hosts.add(d.get("host"))

    def check_ip(ip):
        """检查 IP 的 4441+20010 是否同时开放；活跃会话设备只探 4441。"""
        ports = [4441] if ip in active_hosts else probe_ports
        opens = []
        for port in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            try:
                s.connect((ip, port))
                opens.append(port)
                s.close()
            except Exception:
                pass
        return (ip, opens) if len(opens) == len(ports) else None

    # 并发扫描 1-254（0.5s 超时 × 254 IP ÷ 50 并发 ≈ 3 秒）
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        results = pool.map(check_ip, [f"{subnet}.{i}" for i in range(1, 255)])
        found = [r for r in results if r]

    # 排除已注册的 IP
    registered = set()
    for d in device_registry.load()["devices"].values():
        if d.get("transport") == "vllink_tcp":
            registered.add(d.get("host"))

    devices = []
    for ip, ports in found:
        is_new = ip not in registered
        devices.append({"host": ip, "dap_port": 4441, "uart_port": 20010,
                        "registered": not is_new})
    return {"found": devices, "subnet": subnet + ".0/24"}


# ================= 门户只读管理 API（阶段 4） =================
app.include_router(portal_api.router)


# ================= 认证与访问控制（阶段 3，D1） =================

_login_fail = {}   # username -> {"fails": n, "lock_until": ts}（应急登录限频，内存）


def _set_session_cookie(resp, token, ttl):
    """HttpOnly + SameSite=Lax；HTTPS 部署时由反向代理/运维阶段加固 Secure。"""
    resp.set_cookie(config.SESSION_COOKIE, token, max_age=ttl,
                    httponly=True, samesite="lax", path="/",
                    secure=config.SESSION_COOKIE_SECURE)
    return resp


@app.post("/api/auth/emergency/login")
def auth_emergency_login(body: dict):
    """本地应急登录（D1 遗留）：**D2 起默认关闭**；仅测试基建可开启。

    生产口径（维护者确认）：无应急账号 —— 离线改用"本站账号密码登录"。
    """
    if not config.AUTH_EMERGENCY_ENABLED:
        raise HTTPException(404, "应急登录已停用（请用本站账号密码登录）")
    import time as _t
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    now = _t.time()
    st = _login_fail.get(username)
    if st and now < st.get("lock_until", 0):
        remain = int(st["lock_until"] - now) + 1
        raise HTTPException(429, f"失败次数过多，已锁定（约 {remain}s 后重试）")
    ok, info = auth_emergency.authenticate(username, password)
    if not ok:
        st = _login_fail.setdefault(username, {"fails": 0, "lock_until": 0})
        st["fails"] += 1
        if st["fails"] >= config.EMERGENCY_LOGIN_MAX_FAIL:
            st["lock_until"] = now + config.EMERGENCY_LOGIN_LOCK_SEC
            st["fails"] = 0
        raise HTTPException(401, "用户名或密码错误")
    _login_fail.pop(username, None)
    sess = auth_sessions.create(
        user=f"emergency:{username}", display=f"{username}（应急·仅调试）",
        role="emergency",
        extra={"revoked_after": info.get("revoked_after", 0)})
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"user": sess["user"], "role": sess["role"],
                         "display": sess["display"], "expires": sess["expires"]})
    ttl = int(sess["expires"] - time.time()) + 1
    return _set_session_cookie(resp, sess["token"], ttl)


def _local_login_guard(username: str, now: float):
    """本地登录限频（与应急同一套参数，内存态）。"""
    st = _login_fail.get(username)
    if st and now < st.get("lock_until", 0):
        remain = int(st["lock_until"] - now) + 1
        raise HTTPException(429, f"失败次数过多，已锁定（约 {remain}s 后重试）")


def _local_login_fail(username: str, now: float):
    st = _login_fail.setdefault(username, {"fails": 0, "lock_until": 0})
    st["fails"] += 1
    if st["fails"] >= config.AUTH_LOCAL_MAX_FAIL:
        st["lock_until"] = now + config.AUTH_LOCAL_LOCK_SEC
        st["fails"] = 0


@app.post("/api/auth/local/login")
def auth_local_login(body: dict):
    """本站账号登录（D2）：离线可用；联网时门户+调试，断网仅调试。"""
    import time as _t
    from fastapi.responses import JSONResponse
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    now = _t.time()
    _local_login_guard(username, now)
    ok, info = auth_accounts.authenticate(username, password)
    if not ok:
        _local_login_fail(username, now)
        raise HTTPException(401, "用户名或密码错误")
    _login_fail.pop(username, None)
    open_id = info["open_id"]
    auth_accounts.touch_login(open_id)
    sess = auth_sessions.create(
        user="feishu:" + open_id, display=info.get("display") or info["username"],
        role="member",
        extra={"open_id": open_id, "account_id": open_id, "username": info["username"],
               "auth_method": "local", "onboarded": True,
               "revoked_after": info.get("revoked_after", 0)})
    resp = JSONResponse({"user": sess["user"], "role": sess["role"],
                         "display": sess["display"], "expires": sess["expires"],
                         "auth_method": "local", "session": sess["token"]})
    return _set_session_cookie(resp, sess["token"],
                               int(sess["expires"] - _t.time()) + 1)


@app.post("/api/auth/account/onboard")
def auth_account_onboard(request: Request, body: dict):
    """首次飞书登录后设置本站用户名与密码（无公开注册：必须是已核验的飞书会话）。"""
    rec = getattr(request.state, "session", None)
    if not rec or rec.get("role") != "member":
        raise HTTPException(401, "需要先用飞书登录")
    open_id = (rec.get("extra") or {}).get("open_id", "")
    if not open_id:
        raise HTTPException(400, "会话缺少飞书身份，请重新用飞书登录")
    username = str(body.get("username") or "").strip()
    pw = str(body.get("password") or "")
    pw2 = str(body.get("password2") or "")
    if pw != pw2:
        raise HTTPException(422, "两次输入的密码不一致")
    try:
        r = auth_accounts.create(open_id, username, pw, display=rec.get("display", ""),
                                 tenant_key=(rec.get("extra") or {}).get("tenant_key", ""))
    except ValueError as e:
        raise HTTPException(422, str(e))
    tok = getattr(request.state, "session_token", None)
    if tok:
        auth_sessions.mark_onboarded(tok)
    return {"ok": True, "username": r["username"], "created": r["created"]}


@app.post("/api/auth/account/reset-password")
def auth_account_reset_password(request: Request, body: dict):
    """忘记密码：飞书再验证通过后的限时改密窗口内设置新密码。"""
    rec = getattr(request.state, "session", None)
    tok = getattr(request.state, "session_token", None)
    if not rec or not auth_sessions.reset_allowed(rec):
        raise HTTPException(403, "改密窗口已过期，请重新通过飞书验证")
    open_id = (rec.get("extra") or {}).get("open_id", "")
    if not auth_accounts.get(open_id):
        raise HTTPException(404, "该飞书账号尚未开通本站账号")
    pw = str(body.get("password") or "")
    pw2 = str(body.get("password2") or "")
    if pw != pw2:
        raise HTTPException(422, "两次输入的密码不一致")
    try:
        auth_accounts.set_password(open_id, pw, by="feishu")
    except ValueError as e:
        raise HTTPException(422, str(e))
    auth_sessions.clear_reset(tok)
    return {"ok": True, "note": "密码已重设，其他设备上的旧会话将在下一次请求时失效"}


@app.get("/api/auth/me")
def auth_me(request: Request):
    """当前身份自查（应急/成员均可；导航栏使用）。"""
    rec = getattr(request.state, "session", None)
    if not rec:
        raise HTTPException(401, "未认证")
    state = auth_sessions.session_state(rec)
    extra = rec.get("extra") or {}
    _, leader_groups = portal_api.leader_ctx(request)
    return {"user": rec["user"], "display": rec["display"], "role": rec["role"],
            "expires": rec["expires"],
            "is_leader": bool(leader_groups), "leader_groups": leader_groups,
            "recheck": state if rec["role"] == "member" else "ok",
            "auth_method": extra.get("auth_method", "feishu"),
            "onboarded": bool(extra.get("onboarded", True)),
            "username": extra.get("username")}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    tok = getattr(request.state, "session_token", None)
    if tok:
        auth_sessions.revoke(tok)
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.SESSION_COOKIE, path="/")
    return resp


@app.get("/auth/feishu/start")
def auth_feishu_start(next: str = "/", purpose: str = "login"):
    """发起 OAuth：一次性 state + 重定向授权页。未配置 → 503 明确诊断。"""
    from fastapi.responses import RedirectResponse
    from urllib.parse import quote
    cfg = auth_feishu.feishu_config()
    if not cfg:
        raise HTTPException(503, "飞书登录未配置（缺少 app_id/secret/redirect_url；"
                                 "应急登录不受影响）")
    state = auth_feishu.new_state()
    url = auth_feishu.authorize_url(state, cfg)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("oauth_state", state, max_age=config.OAUTH_STATE_TTL_SEC,
                    httponly=True, samesite="lax", path="/",
                    secure=config.SESSION_COOKIE_SECURE)
    resp.set_cookie("oauth_next", quote(next, safe=""), max_age=600,
                    httponly=True, samesite="lax", path="/")
    if purpose in ("login", "reset"):
        resp.set_cookie("oauth_purpose", purpose, max_age=600,
                        httponly=True, samesite="lax", path="/")
    return resp


def _finish_feishu_login(info, purpose: str = "login"):
    """组织核验 + 稳定用户映射（open_id）+ 建 member 会话（含开通状态）。"""
    cfg = auth_feishu.feishu_config()
    if not auth_feishu.tenant_allowed(info, cfg):
        raise HTTPException(403, "组织不匹配：该飞书账号不属于该组织")
    open_id = info["open_id"]
    name = info.get("name") or open_id[-6:]
    acc = auth_accounts.get(open_id)
    onboarded = bool(acc and acc.get("hash") and acc.get("status") == "active")
    if onboarded:
        auth_accounts.touch_login(open_id)
    auth_accounts.audit("feishu_login", open_id=open_id,
                        username=(acc or {}).get("username"),
                        purpose=purpose, onboarded=onboarded)
    sess = auth_sessions.create(
        user=f"feishu:{open_id}", display=name, role="member",
        extra={"open_id": open_id, "account_id": open_id,
               "tenant_key": info.get("tenant_key", ""),
               "auth_method": "feishu", "onboarded": onboarded})
    return sess


@app.get("/auth/feishu/callback")
def auth_feishu_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """浏览器回调：state 一次性校验（重放拒绝）→ 换令牌 → user_info → 建会话。"""
    from fastapi.responses import RedirectResponse
    from urllib.parse import unquote
    if error:
        from urllib.parse import quote
        return RedirectResponse(f"/login?oauth_error={quote(error)}", status_code=302)
    import secrets
    browser_state = request.cookies.get("oauth_state", "")
    if not state or not browser_state or not secrets.compare_digest(state, browser_state):
        raise HTTPException(400, "登录校验与当前浏览器不匹配，请重新发起登录")
    if not code or not auth_feishu.consume_state(state):
        raise HTTPException(400, "state 无效或已使用（重放拒绝），请重新发起登录")
    info, err = auth_feishu.exchange_code(code)
    if not info:
        raise HTTPException(502, f"飞书登录失败: {err}")
    purpose = request.cookies.get("oauth_purpose") or "login"
    sess = _finish_feishu_login(info, purpose=purpose)
    if purpose == "reset":
        auth_sessions.mark_reset_allowed(sess["token"])
        resp = RedirectResponse("/reset", status_code=302)
    elif not (sess.get("extra") or {}).get("onboarded"):
        resp = RedirectResponse("/onboarding", status_code=302)
    else:
        next_url = unquote(request.cookies.get("oauth_next") or "/")
        if (not next_url.startswith("/") or next_url.startswith("//")
                or "\\" in next_url or any(ord(c) < 32 for c in next_url)):
            next_url = "/"
        # 登录后首启标记：前端（handoff.js）转为会话标记，
        # 调试台据此只在本轮登录后的首次进入播放开机引导动画
        next_url += ("&" if "?" in next_url else "?") + "boot=1"
        resp = RedirectResponse(next_url, status_code=302)
    ttl = int(sess["expires"] - time.time()) + 1
    for cookie in ("oauth_state", "oauth_next", "oauth_purpose"):
        resp.delete_cookie(cookie, path="/")
    return _set_session_cookie(resp, sess["token"], ttl)


@app.post("/api/auth/exchange")
def auth_feishu_exchange(body: dict):
    """CLI 换取会话：用户浏览器完成授权后，wsctl 用 code+state 在此换会话令牌。"""
    code = str(body.get("code") or "")
    state = str(body.get("state") or "")
    if not code or not auth_feishu.consume_state(state):
        raise HTTPException(400, "state 无效或已使用（重放拒绝）")
    info, err = auth_feishu.exchange_code(code)
    if not info:
        raise HTTPException(502, f"飞书登录失败: {err}")
    sess = _finish_feishu_login(info)
    from fastapi.responses import JSONResponse
    return JSONResponse({"user": sess["user"], "role": sess["role"],
                         "display": sess["display"],
                         "session": sess["token"], "expires": sess["expires"]})


# ================= 静态页面（须最后挂载） =================

# 阶段 2 起根路径改为门户：/ 为管理门户外壳（骨架页经显式路由返回），
# 原调试台整体迁移至 /debug/（StaticFiles 挂载原 static/ 目录，html=True
# 使 /debug/ 直达 index.html）。调试 HTML 内相对资源引用自动跟随前缀，
# 前端 API/WS 均为绝对路径 /api /ws 不受影响（基线 docs/portal-baseline.md §8）。
# 门户静态资源挂 /portal（不进 /debug 命名空间）。回退方式：删除本段门户
# 路由与 /portal 挂载，将 /debug 挂载改回 "/"（见 docs/portal-progress.md 阶段 2）。

from fastapi.responses import FileResponse, HTMLResponse  # noqa: E402  (靠近使用处)

import html as _html  # noqa: E402  (预渲染侧栏身份/同步状态用)
from datetime import datetime as _dt  # noqa: E402

_PORTAL_PAGES = config.BASE_DIR / "static" / "portal" / "pages"

# 骨架页按 mtime 缓存；"尚无同步数据"提示、侧栏同步状态行、身份区均由
# 服务端按当前状态预渲染进首帧——否则这些内容由前端在 API 返回后才写入，
# 每次页间导航都会先闪一遍占位文字（"读取中…"/"未登录"）再切换成真实值。
_PAGE_TPL_CACHE: dict = {}
_NO_SNAP_NOTE = ('<p class="p-note" id="no-snap-note">'
                 "※ 尚无可用同步数据 —— 首次同步未完成或飞书未配置"
                 "（与『源表为空』不同）。</p>")

# 与 portal.js errText 同构（改文案需两处同步）
_SYNC_ERR_TEXT = {
    "network": "网络/上游不可达", "rate_limited": "限流（429）",
    "api": "飞书接口返回错误", "config": "配置或字段映射问题",
    "parse": "数据解析/分页校验失败",
    "schema": "本地快照库版本不匹配（已停止读写，见运维手册）",
}


def _side_sync_html(st: dict) -> str:
    """侧栏同步状态行（含刷新按钮），文案与 portal.js renderEnvelope 同构。"""
    if not st["has_snapshot"]:
        cls, text = "p-sync is-empty", "尚无同步数据"
    else:
        cls = "p-sync" + (" is-stale" if st["stale"] else "") + \
              (" is-failed" if st["last_error"] else "")
        bits = ["同步于 " + _dt.fromtimestamp(st["last_success_at"]).strftime(
                    "%Y-%m-%d %H:%M") if st["last_success_at"] else "尚未成功同步"]
        if st["stale"]:
            bits.append("缓存陈旧")
        if st["last_error"]:
            kind = st["last_error"]
            bits.append("上次失败：" + ("内部错误（详见服务端日志）" if kind.startswith("internal:")
                        else _SYNC_ERR_TEXT.get(kind, kind)))
        text = " · ".join(bits)
    return (f'<span class="{cls}" id="sync-line">{_html.escape(text)}</span>'
            '<button class="p-refresh" id="sync-refresh" type="button">刷新</button>')


def _side_who_html(request) -> str:
    """侧栏身份区（姓名 + 退出按钮），与 portal.js authArea 输出同构。"""
    rec = getattr(request.state, "session", None) or {}
    display, role = rec.get("display"), rec.get("role")
    if not display:
        return '<span id="who">未登录</span>'
    label = "成员" if role == "member" else "应急·仅调试"
    return (f'<span id="who">{_html.escape(display)}（{label}）</span>'
            '<button class="p-logout" type="button">退出</button>')


# ---------------- Vue 前端（2026-10 起默认） ----------------
# 页面 HTML 是 web/ 构建出的三个入口（portal / debug / auth），由这里读取后注入
# 首帧状态（身份、同步状态）再返回——与旧版骨架页的服务端预渲染同一目的：
# 侧栏身份与同步行首帧即为真实值，不闪「读取中…」。静态资源挂在 /app/assets/。
import json as _json  # noqa: E402

_APP_TPL_CACHE: dict = {}
_APP_MISSING = ("<!DOCTYPE html><meta charset=\"utf-8\"><title>前端未构建</title>"
                "<p>前端构建产物缺失：请在 web/ 目录执行 npm run build（产物输出到 "
                "static/app/），或设置环境变量 PORTAL_FRONTEND=legacy 回退旧版前端。</p>")


def _use_vue() -> bool:
    return config.PORTAL_FRONTEND != "legacy"


def _boot_state(request, with_sync=True) -> dict:
    rec = getattr(request.state, "session", None) or {}
    who = ({"display": rec["display"], "role": rec.get("role")}
           if rec.get("display") else None)
    sync = None
    if with_sync:
        try:
            st = portal_store.status()
            sync = {"has_snapshot": bool(st["has_snapshot"]),
                    "last_success_at": st["last_success_at"],
                    "stale": bool(st["stale"]), "last_error": st["last_error"]}
        except Exception:              # 状态读取失败不注入，前端自行拉取
            sync = None
    return {"who": who, "sync": sync}


def _serve_app_page(entry: str, request: Request, with_sync: bool = True) -> HTMLResponse:
    """返回 Vue 入口页（按 mtime 缓存模板），注入 window.__FORAY_BOOT__。"""
    path = config.APP_DIR / f"{entry}.html"
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return HTMLResponse(_APP_MISSING, status_code=503)
    key = str(path)
    cached = _APP_TPL_CACHE.get(key)
    if not cached or cached[0] != mtime:
        cached = (mtime, path.read_text(encoding="utf-8"))
        _APP_TPL_CACHE[key] = cached
    # JSON 里的 "<" 转义，防止 "</script>" 之类内容提前闭合脚本
    boot = _json.dumps(_boot_state(request, with_sync), ensure_ascii=False).replace("<", "\\u003c")
    page = cached[1].replace(
        "</head>", f"<script>window.__FORAY_BOOT__={boot};</script>\n</head>", 1)
    return HTMLResponse(page)


def _portal(filename: str, request: Request) -> HTMLResponse:
    """门户页面：默认 Vue 单页应用（同一入口按路径渲染），legacy 回退旧骨架页。"""
    if _use_vue():
        return _serve_app_page("portal", request)
    return _serve_portal_page(filename, request)


def _serve_portal_page(filename: str, request: Request) -> HTMLResponse:
    """门户骨架页：注入数据提示与侧栏状态后返回（portal.js 对同 id 元素
    只更新不重建，快照出现后会自行移除预渲染的提示条）。"""
    path = _PORTAL_PAGES / filename
    mtime = path.stat().st_mtime
    cached = _PAGE_TPL_CACHE.get(filename)
    if not cached or cached[0] != mtime:
        cached = (mtime, path.read_text(encoding="utf-8"))
        _PAGE_TPL_CACHE[filename] = cached
    page = cached[1]
    try:
        st = portal_store.status()
        has_snapshot = bool(st["has_snapshot"])
    except Exception:                  # 状态读取失败不注入，前端另有兜底提示
        st, has_snapshot = None, True
    if not has_snapshot:
        page = page.replace("<!--DATA_NOTE_SLOT-->", _NO_SNAP_NOTE)
    if st is not None:
        page = page.replace("<!--SYNC_SLOT-->", _side_sync_html(st))
        page = page.replace("<!--WHO_SLOT-->", _side_who_html(request))
    return HTMLResponse(page)


@app.get("/", include_in_schema=False)
def portal_overview(request: Request):
    return _portal("overview.html", request)


@app.get("/projects", include_in_schema=False)
def portal_projects(request: Request):
    return _portal("projects.html", request)


@app.get("/tasks", include_in_schema=False)
def portal_tasks(request: Request):
    return _portal("tasks.html", request)


@app.get("/schedule", include_in_schema=False)
def portal_schedule(request: Request):
    return _portal("schedule.html", request)


@app.get("/procurement", include_in_schema=False)
def portal_procurement(request: Request):
    """物资采购面板（总览之下）：列表 + 详情复用同一外壳。"""
    return _portal("procurement.html", request)


@app.get("/attendance", include_in_schema=False)
def portal_attendance(request: Request):
    """考勤页（2026-10-20 新增，只读）。只有新版前端有这一页：回退旧前端时回到总览。"""
    if not _use_vue():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/", status_code=307)
    return _serve_app_page("portal", request)


@app.get("/quality", include_in_schema=False)
def portal_quality(request: Request):
    """数据质量异常明细页（阶段 5 补：§5.2 异常与排除说明可见）。"""
    return _portal("quality.html", request)


@app.get("/login", include_in_schema=False)
def portal_login(request: Request):
    if _use_vue():
        return _serve_app_page("auth", request, with_sync=False)
    return FileResponse(_PORTAL_PAGES / "login.html", media_type="text/html")


# D2（阶段 9）：开通页与找回密码页（未登录也可打开，页面自身引导）
@app.get("/onboarding", include_in_schema=False)
def page_onboarding(request: Request):
    """开通页（首次飞书登录后设置本站用户名与密码）。"""
    if _use_vue():
        return _serve_app_page("auth", request, with_sync=False)
    return FileResponse(_PORTAL_PAGES / "onboarding.html", media_type="text/html")


@app.get("/reset", include_in_schema=False)
def page_reset(request: Request):
    """找回密码页（须先经飞书再验证，改密窗口默认 10 分钟）。"""
    if _use_vue():
        return _serve_app_page("auth", request, with_sync=False)
    return FileResponse(_PORTAL_PAGES / "reset.html", media_type="text/html")


# 阶段 5：详情直达（复用列表页外壳，前端按路径渲染详情；均受认证中间件管控）
@app.get("/projects/{project_id}", include_in_schema=False)
def portal_project_detail(project_id: str, request: Request):
    return _portal("projects.html", request)


@app.get("/tasks/{task_id}", include_in_schema=False)
def portal_task_detail(task_id: str, request: Request):
    return _portal("tasks.html", request)


@app.get("/schedule/{schedule_id}", include_in_schema=False)
def portal_schedule_detail(schedule_id: str, request: Request):
    return _portal("schedule.html", request)


@app.get("/procurement/{record_id}", include_in_schema=False)
def portal_procurement_detail(record_id: str, request: Request):
    return _portal("procurement.html", request)


# 调试台入口页：显式路由使其受认证中间件管控（302 登录）；
# 静态子资源仍由下方 /debug mount 公开服务（无敏感信息）。
# 无尾斜杠必须 307 到 /debug/：index.html 内是相对资源引用，若在 /debug 直接
# 返回 HTML，浏览器会把 style.css 解析成 /style.css（无根挂载）→ 404 裸页。
@app.get("/debug", include_in_schema=False)
def debug_index_noslash():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/debug/", status_code=307)


@app.get("/debug/", include_in_schema=False)
def debug_index(request: Request):
    if _use_vue():
        return _serve_app_page("debug", request, with_sync=False)
    return FileResponse(config.BASE_DIR / "static" / "index.html",
                        media_type="text/html")


# 阶段 10：统一设计资源（门户与调试台共用；见 static/ui/ 与 docs/portal-operations.md）
app.mount("/ui", StaticFiles(directory=str(config.BASE_DIR / "static" / "ui")),
          name="ui")
# Vue 构建产物的静态资源（只挂 assets/ 子目录；入口 HTML 只经上面的受控路由返回）。
# 目录不存在时（尚未构建）也要能启动服务，故 check_dir=False。
app.mount("/app/assets", StaticFiles(directory=str(config.APP_DIR / "assets"), check_dir=False),
          name="app-assets")
app.mount("/portal", StaticFiles(directory=str(config.BASE_DIR / "static" / "portal")),
          name="portal-assets")

# 原调试台：static/ 目录整体迁至 /debug/（相对引用自动跟随，无文件搬迁）
app.mount("/debug", StaticFiles(directory=str(config.BASE_DIR / "static"), html=True),
          name="debug")
