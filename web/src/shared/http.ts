/* 请求封装：所有接口调用统一走这里。
 * - 401（未登录 / 会话失效）→ 跳登录页并带回跳地址；登录页自身不跳，避免 401 重定向环；
 * - 读接口（getJSON）失败抛 HttpError，带服务端 detail；
 * - 写接口（sendJSON / upload）返回 { ok, status, body }，由调用方决定提示文案。 */

export class HttpError extends Error {
  status: number;
  body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export interface SendResult<T = Record<string, unknown>> {
  ok: boolean;
  status: number;
  body: T;
}

/** 跳转登录页（带回跳地址）；已在登录页时什么也不做。 */
export function toLogin(): void {
  if (location.pathname === "/login") return;
  location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
}

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string" && d) return d;
    if (d != null) return JSON.stringify(d);
  }
  return fallback;
}

async function parseBody(r: Response): Promise<unknown> {
  const text = await r.text();
  if (!text) return {};
  try { return JSON.parse(text); } catch { return { detail: text.slice(0, 200) }; }
}

/** GET 并解析 JSON；非 2xx 抛 HttpError。 */
export async function getJSON<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin", ...init });
  if (r.status === 401) {
    toLogin();
    throw new HttpError("未认证或会话已失效", 401, null);
  }
  const body = await parseBody(r);
  if (!r.ok) throw new HttpError(detailOf(body, "HTTP " + r.status), r.status, body);
  return body as T;
}

/** 发送 JSON（POST / PUT / PATCH / DELETE）；网络错误才抛异常。 */
export async function sendJSON<T = Record<string, unknown>>(
  method: string, path: string, payload?: unknown,
): Promise<SendResult<T>> {
  const init: RequestInit = { method, credentials: "same-origin" };
  if (payload !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(payload);
  }
  const r = await fetch(path, init);
  if (r.status === 401) toLogin();
  return { ok: r.ok, status: r.status, body: (await parseBody(r)) as T };
}

/** 上传单个文件（multipart，字段名默认 file）。 */
export async function upload<T = Record<string, unknown>>(
  path: string, file: File, field = "file",
): Promise<SendResult<T>> {
  const fd = new FormData();
  fd.append(field, file, file.name);
  const r = await fetch(path, { method: "POST", body: fd, credentials: "same-origin" });
  if (r.status === 401) toLogin();
  return { ok: r.ok, status: r.status, body: (await parseBody(r)) as T };
}

/** 从 SendResult 里取服务端错误说明。 */
export function errorDetail(res: SendResult<unknown>, fallback = "未知错误"): string {
  return detailOf(res.body, fallback);
}

/** 当前页面协议对应的 WebSocket 地址。 */
export function wsUrl(path: string): string {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;
}
