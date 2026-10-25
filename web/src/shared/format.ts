/* 日期与数字格式化（v5 文案：日期用点分隔，如 2026.10.18 / 2026.10.18 15:12）。
 * 时间戳单位：秒（与 /api/portal/* 的 ts 字段一致）。 */

export function pad2(n: number): string {
  return n < 10 ? "0" + n : String(n);
}

/** 秒级时间戳 → 2026.10.18 15:12；空值 → 未设置 */
export function fmtDT(ts?: number | null, empty = "未设置"): string {
  if (!ts) return empty;
  const d = new Date(ts * 1000);
  return d.getFullYear() + "." + pad2(d.getMonth() + 1) + "." + pad2(d.getDate()) +
    " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

/** 秒级时间戳 → 2026.10.18；空值 → 未设置 */
export function fmtD(ts?: number | null, empty = "未设置"): string {
  if (!ts) return empty;
  const d = new Date(ts * 1000);
  return d.getFullYear() + "." + pad2(d.getMonth() + 1) + "." + pad2(d.getDate());
}

/** 秒级时间戳 → 15:12 */
export function fmtTime(ts?: number | null): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

/** 秒级时间戳 → <input type="datetime-local"> 的值（本地时间） */
export function toLocalInput(ts?: number | null): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) +
    "T" + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

/** 毫秒时间戳 → <input type="date"> 的值（北京时间的年月日） */
export function msToDateInput(ms?: number | null): string {
  if (!ms) return "";
  const d = new Date(ms + 8 * 3600 * 1000);
  return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate());
}

/** 秒级时间戳 → 北京时间日期 2026.10.18（采购日期等纯日期字段） */
export function fmtBeijingDate(ts?: number | null, empty = "—"): string {
  if (!ts) return empty;
  return msToDateInput(ts * 1000).replace(/-/g, ".");
}

/** 金额：空值不伪装成 0 */
export function fmtMoney(v?: number | null): string {
  return v == null ? "—" : "¥" + Number(v).toFixed(2);
}

export function fmtNum(v?: number | null): string {
  return v == null ? "—" : String(v);
}

/** 当前本地时钟文案：2026.10.18 15:12 */
export function clockText(d = new Date()): string {
  return d.getFullYear() + "." + pad2(d.getMonth() + 1) + "." + pad2(d.getDate()) +
    " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}
