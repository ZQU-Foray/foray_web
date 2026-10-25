/* 原始串口（移植自旧版 static/app.js「串口」段）：串口 WebSocket 收发 + RX 按行合并。
 * 与发射调试互斥由服务端负责（打开一方会断开另一方，仅影响当前设备）。 */
import { appendLog, b64ToStr, bytesToB64, dfetch, encodeText, errorText, jsonInit, wsHref } from "./api";
import { deviceCtx } from "./device";
import { refreshStatus, station } from "./status";

let serialWs: WebSocket | null = null;
let logEl: HTMLElement | null = null;

export function bindSerialLog(el: HTMLElement | null): void {
  logEl = el;
}

export function clearSerialLog(): void {
  if (logEl) logEl.textContent = "";
}

/* 串口 RX 行缓冲：数据按“读块”到达（TCP 还会被拆成零碎小块），逐块成行会把一行文本
   撕成多条带时间戳的日志；这里合并数据块，遇换行输出整行，半行超过 350ms 没有后续
   数据再冲刷，兼顾实时性与可读性。 */
const serialRx = { text: "", timer: 0 };

function serialRxFlush(text: string) {
  const clean = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(/\n+$/, "");
  if (clean === "") return;           // 纯换行不产生空日志行
  appendLog(logEl, "line-rx", clean);
}

function serialRxAppend(text: string) {
  if (!text) return;
  serialRx.text += text;
  const nl = Math.max(serialRx.text.lastIndexOf("\n"), serialRx.text.lastIndexOf("\r"));
  if (nl >= 0) {
    const done = serialRx.text.slice(0, nl + 1);
    serialRx.text = serialRx.text.slice(nl + 1);
    serialRxFlush(done);
  }
  if (serialRx.text) {
    if (serialRx.timer) clearTimeout(serialRx.timer);
    serialRx.timer = window.setTimeout(() => {
      serialRx.timer = 0;
      const rest = serialRx.text;
      serialRx.text = "";
      serialRxFlush(rest);
    }, 350);
  }
}

export function serialRxReset(): void {
  if (serialRx.timer) clearTimeout(serialRx.timer);
  serialRx.timer = 0;
  serialRx.text = "";
}

export function connectSerialWs(): void {
  if (!deviceCtx.id) return;
  const generation = deviceCtx.generation;
  const ws = serialWs = new WebSocket(wsHref(deviceCtx.ws("serial")));
  ws.onmessage = (e) => {
    if (generation !== deviceCtx.generation) return;
    const m = JSON.parse(e.data) as { type?: string; data?: string; b64?: string };
    if (m.type === "sys") appendLog(logEl, "line-sys", String(m.data ?? ""));
    else if (m.type === "rx") serialRxAppend(b64ToStr(m.b64 || ""));
    else if (m.type === "tx") appendLog(logEl, "line-tx", b64ToStr(m.b64 || ""));
  };
  ws.onclose = () => window.setTimeout(() => {
    if (generation === deviceCtx.generation) connectSerialWs();
  }, 2000);
}

/** 设备切换：断开旧连接且不再重连 */
export function closeSerialWs(): void {
  if (serialWs) { serialWs.onclose = null; serialWs.close(); serialWs = null; }
}

function serialSend(payload: unknown) {
  if (serialWs && serialWs.readyState === 1) {
    serialWs.send(JSON.stringify(payload));
  } else {
    appendLog(logEl, "line-sys", "串口通道未连接");
  }
}

export function closeSerial(): void {
  serialSend({ type: "close" });
}

/** 发送文本（预设指令按其 newline 追加换行） */
export function sendData(text: string, newline = "crlf"): void {
  const nl = ({ crlf: "\r\n", lf: "\n", cr: "\r", none: "" } as Record<string, string>)[newline] ?? "\r\n";
  serialSend({ type: "tx", b64: bytesToB64(encodeText(text + nl)) });
}

/** 输入框发送；返回 true 表示已发出、应清空输入框 */
export function sendInput(text: string, hex: boolean): boolean {
  if (!text) return false;
  if (hex) {
    const clean = text.replace(/[^0-9a-fA-F]/g, "");
    if (clean.length % 2) { alert("HEX 长度必须为偶数"); return false; }
    const bytes = new Uint8Array(clean.length / 2);
    for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(clean.substr(i * 2, 2), 16);
    serialSend({ type: "tx", b64: bytesToB64(bytes) });
  } else {
    sendData(text, "none");
  }
  return true;
}

export async function openSerial(baud: string): Promise<void> {
  try {
    const body = deviceCtx.isTcp ? {} : { port: station.port, baudrate: +baud || 115200 };
    const r = await dfetch(deviceCtx.api("serial/open"), jsonInit("POST", body));
    if (!r.ok) throw new Error(await errorText(r));
    void refreshStatus();
  } catch (e) { appendLog(logEl, "line-sys", "连接失败：" + (e as Error).message); }
}
