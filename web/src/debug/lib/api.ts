/* 调试台请求与通用工具（移植自旧版 static/app.js 的「会话失效拦截」与「工具」两段）。
 * - dfetch：所有调试台请求都走这里，401（会话过期/撤销）统一跳登录页并带回跳地址，
 *   与旧版在 window.fetch 层拦截 401 的语义一致，但不再改写全局 fetch；
 * - 其余为串口收发用到的 base64 / 文本编解码、日志时间戳与控制台追加行。 */
import { toLogin } from "@/shared/http";

/** 发请求；401 时引导回登录页（响应仍原样返回，由调用方按旧逻辑处理）。 */
export async function dfetch(input: string, init?: RequestInit): Promise<Response> {
  const r = await fetch(input, { credentials: "same-origin", ...init });
  if (r.status === 401) toLogin();
  return r;
}

/** JSON 请求体（POST / PUT） */
export function jsonInit(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

/** 失败响应的说明：优先服务端 detail，否则状态码（对应旧版 `(await r.json()).detail || r.status`） */
export async function errorText(r: Response): Promise<string> {
  try {
    const j = await r.json();
    const d = j && typeof j === "object" ? (j as { detail?: unknown }).detail : undefined;
    if (typeof d === "string" && d) return d;
    if (d != null) return JSON.stringify(d);
  } catch { /* 非 JSON 响应：退回状态码 */ }
  return String(r.status);
}

/** 当前页面协议对应的 WebSocket 完整地址 */
export function wsHref(path: string): string {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;
}

/** 空回调：用于吞掉已经自行提示过的异步失败，避免未处理的 Promise 拒绝 */
export function noop(): void { /* 有意为空 */ }

const te = new TextEncoder();
const td = new TextDecoder("utf-8", { fatal: false });

export function encodeText(text: string): Uint8Array {
  return te.encode(text);
}

export function bytesToB64(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

export function b64ToBytes(b64: string): Uint8Array {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

export function b64ToStr(b64: string): string {
  return td.decode(b64ToBytes(b64));
}

/** 日志时间戳 HH:MM:SS */
export function ts(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 控制台追加一行（按文字渲染；最多保留 2000 行；自动滚到底） */
export function appendLog(el: HTMLElement | null, cls: string, text: string): void {
  if (!el) return;
  const line = document.createElement("div");
  line.className = cls;
  line.textContent = `[${ts()}] ${text}`;
  el.appendChild(line);
  while (el.children.length > 2000) el.removeChild(el.firstChild as Node);
  el.scrollTop = el.scrollHeight;
}

/** sessionStorage 读写（隐私模式下可能抛错：读按无值处理，写静默跳过） */
export function ssGet(key: string): string | null {
  try { return sessionStorage.getItem(key); } catch { return null; }
}
export function ssSet(key: string, value: string): void {
  try { sessionStorage.setItem(key, value); } catch { /* 本次会话内不记忆 */ }
}
