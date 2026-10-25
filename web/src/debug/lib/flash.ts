/* 固件烧录（移植自旧版 static/app.js「烧录」段）：烧录日志 WebSocket（断线 2 秒重连，
 * 设备切换后旧连接的消息一律丢弃）+ 发起烧录。日志控制台是命令式追加的（高频、上限 2000 行）。 */
import { appendLog, dfetch, errorText, jsonInit, wsHref } from "./api";
import { deviceCtx } from "./device";
import { station } from "./status";
import { firmware } from "./firmware";

let flashWs: WebSocket | null = null;
let logEl: HTMLElement | null = null;

export function bindFlashLog(el: HTMLElement | null): void {
  logEl = el;
}

export function clearFlashLog(): void {
  if (logEl) logEl.textContent = "";
}

export function connectFlashWs(): void {
  if (!deviceCtx.id) return;
  const generation = deviceCtx.generation;
  const ws = flashWs = new WebSocket(wsHref(deviceCtx.ws("flash")));
  ws.onmessage = (e) => {
    if (generation !== deviceCtx.generation) return;
    const m = JSON.parse(e.data) as { type?: string; stream?: string; line?: string };
    if (m.type === "log") appendLog(logEl, m.stream === "sys" ? "line-sys" : "line-out", String(m.line ?? ""));
  };
  ws.onclose = () => window.setTimeout(() => {
    if (generation === deviceCtx.generation) connectFlashWs();
  }, 2000);
}

/** 设备切换：断开旧连接且不再重连 */
export function closeFlashWs(): void {
  if (flashWs) { flashWs.onclose = null; flashWs.close(); flashWs = null; }
}

export async function startFlash(): Promise<void> {
  const fw = firmware.selected;
  const target = station.target;
  const probe = deviceCtx.isTcp ? "vllink" : station.probe;
  if (!fw) { alert("请先选择固件"); return; }
  try {
    const r = await dfetch(deviceCtx.api("flash"), jsonInit("POST", { firmware: fw, target, probe }));
    if (!r.ok) appendLog(logEl, "line-sys", `请求失败: ${await errorText(r)}`);
  } catch (e) {
    appendLog(logEl, "line-sys", `请求失败: ${e}`);
  }
}
