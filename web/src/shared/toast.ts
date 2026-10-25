/* 右侧通知框（门户 / 登录类页面 / 调试台共用，容器组件见 components/ToastStack.vue）：
 * - toast(文案, 语气) 从画面右侧弹出一条，停留一段时间后自动消失；文案越长停留越久，错误停得更久；
 * - 鼠标悬停或键盘聚焦在通知上时暂停计时，离开后继续；也可点 × 立即关闭；
 * - 同一条文案正在显示时不重复堆叠，只重新计时；同时最多显示 4 条，超出时挤掉最早的一条；
 * - 文案里可能含服务端 detail 与用户输入，一律按文字渲染（模板插值，不用 v-html）。 */
import { reactive } from "vue";

export type ToastTone = "info" | "ok" | "error";

export interface ToastItem {
  id: number;
  text: string;
  tone: ToastTone;
  /** 弹出时刻（显示用 HH:MM:SS） */
  at: number;
  /** 本轮计时总时长（毫秒）：倒计时细条按它播放 */
  ttl: number;
  /** 重新计时的次数：变了倒计时细条就从头播放 */
  round: number;
  paused: boolean;
}

const MAX = 4;
const BASE_MS: Record<ToastTone, number> = { info: 4500, ok: 4000, error: 6500 };
const MAX_MS = 12000;

export const toasts = reactive<ToastItem[]>([]);

/* 计时器不进响应式：left = 剩余毫秒，started = 本段计时开始时刻 */
const timers = new Map<number, { handle: number; left: number; started: number }>();
let seq = 0;

/** 停留时长：基础时长 + 超出 24 字的部分每字 60ms，封顶 12 秒 */
function lifetime(text: string, tone: ToastTone): number {
  return Math.min(BASE_MS[tone] + Math.max(0, text.length - 24) * 60, MAX_MS);
}

function find(id: number): ToastItem | undefined {
  return toasts.find((t) => t.id === id);
}

function arm(id: number, ms: number): void {
  const old = timers.get(id);
  if (old) window.clearTimeout(old.handle);
  timers.set(id, { handle: window.setTimeout(() => dismissToast(id), ms), left: ms, started: Date.now() });
}

export function toast(text: string, tone: ToastTone = "info"): void {
  const s = String(text == null ? "" : text).trim();
  if (!s) return;
  const ms = lifetime(s, tone);
  const same = toasts.find((t) => t.text === s && t.tone === tone);
  if (same) {
    same.ttl = ms;
    same.round++;
    same.at = Date.now();
    const t = timers.get(same.id);
    if (same.paused && t) t.left = ms;     // 鼠标还停在上面：保持暂停，离开后按新时长计
    else arm(same.id, ms);
    return;
  }
  const id = ++seq;
  toasts.push({ id, text: s, tone, at: Date.now(), ttl: ms, round: 0, paused: false });
  arm(id, ms);
  while (toasts.length > MAX) dismissToast(toasts[0].id);
}

export function dismissToast(id: number): void {
  const t = timers.get(id);
  if (t) window.clearTimeout(t.handle);
  timers.delete(id);
  const i = toasts.findIndex((x) => x.id === id);
  if (i >= 0) toasts.splice(i, 1);
}

export function pauseToast(id: number): void {
  const t = timers.get(id);
  const item = find(id);
  if (!t || !item || item.paused) return;
  window.clearTimeout(t.handle);
  t.left = Math.max(0, t.left - (Date.now() - t.started));
  item.paused = true;
}

export function resumeToast(id: number): void {
  const t = timers.get(id);
  const item = find(id);
  if (!t || !item || !item.paused) return;
  item.paused = false;
  t.started = Date.now();
  t.handle = window.setTimeout(() => dismissToast(id), t.left);
}
