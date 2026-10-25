/* 门户全局状态：身份、元数据、同步信封、跨页保留的筛选条件（通知框见 @/shared/toast）。
 * 行为约束（计划 §3.3/§5.3，与旧版 portal.js 一致）：
 * - 每 POLL_MS 检查一次本地快照版本，变化才通知当前页局部刷新（不整页重载、不重置筛选与滚动）；
 * - 倒计时在浏览器本地每秒计算（以 server_time 校时），不向飞书轮询；
 * - 写入成功后立即刷新（afterWrite）：后端在响应前已把改动并进页面数据，不必等同步；
 *   直写模式由后端顺带请求一轮同步，先存本地的模式由推送线程推完后自己请求。 */
import { reactive } from "vue";
import { fetchMe, fetchMeta, fetchSyncStatus } from "./api";
import type { CacheStatus, Envelope, Me, Meta, ProcOptions, SyncStatus, WriteResult } from "./types";
import { fmtDT } from "@/shared/format";
import { toast } from "@/shared/toast";

const qs = new URLSearchParams(location.search);
const pollRaw = parseInt(qs.get("poll_ms") || "15000", 10);   // 测试可加速
export const POLL_MS = Number.isFinite(pollRaw) ? Math.max(1000, Math.min(60000, pollRaw)) : 15000;

export const GROUPS_FALLBACK = ["机械", "硬件", "电控", "算法", "运营"];

const boot = window.__FORAY_BOOT__ || {};

export interface Option { id: string; label: string }

export const store = reactive({
  me: null as Me | null,
  meLoaded: false,
  /** 首帧身份（服务端注入），/api/auth/me 返回前先用它，避免文字闪烁 */
  bootWho: (boot.who || null) as { display: string; role: string } | null,
  meta: null as Meta | null,
  sync: {
    has_snapshot: boot.sync ? !!boot.sync.has_snapshot : true,
    last_success_at: boot.sync ? boot.sync.last_success_at : null,
    stale: boot.sync ? !!boot.sync.stale : false,
    last_error: boot.sync ? boot.sync.last_error : null,
    known: !!boot.sync,
  },
  version: null as string | number | null,
  lastStale: null as boolean | null,
  /** 本地缓存库概况（推送队列积压、失败、未确认冲突），随 sync-status 更新 */
  cache: null as CacheStatus | null,
  serverOffset: 0,
  /** 当前页确认「尚无可用同步数据」 */
  noSnapshot: boot.sync ? !boot.sync.has_snapshot : false,
  /** 递增即通知当前页局部刷新（轮询发现新版本 / 写入后 / 手动刷新后） */
  refreshSeq: 0,

  /* ---- 跨页保留的筛选（软导航往返不重置） ---- */
  taskFilters: { group: "全部", priority: "全部", status: "全部", owner: "全部", project: "全部" },
  procFilters: { group: "全部", season: "全部", category1: "全部", category2: "全部", invoice: "全部", q: "" },
  projectFilters: { group: "全部", archived: false },
  scheduleView: "list" as "list" | "calendar",

  /* ---- 下拉候选（按需加载后缓存） ---- */
  ownerOptions: null as Option[] | null,
  projectOptions: null as Option[] | null,
  procOptions: null as ProcOptions | null,

  /** 当前打开的写入弹窗（null = 未打开） */
  form: null as null | { kind: "task" | "schedule" | "project" | "procurement"; preset?: Record<string, unknown>; recordId?: string },
});

/** 打开写入弹窗：recordId 为空 = 新建（POST），否则编辑该记录（PATCH） */
export function openForm(kind: "task" | "schedule" | "project" | "procurement",
                         preset?: Record<string, unknown>, recordId?: string): void {
  store.form = { kind, preset, recordId };
}

export function closeForm(): void {
  store.form = null;
}

export function isLeader(): boolean {
  return !!(store.me && store.me.is_leader);
}

export function groups(): string[] {
  return (store.meta && store.meta.groups) || GROUPS_FALLBACK;
}

export function localNowSec(): number {
  return Date.now() / 1000 + store.serverOffset;
}

/** 同步失败类别 → 文案（与服务端 _SYNC_ERR_TEXT 同构） */
export function errText(kind: string): string {
  const map: Record<string, string> = {
    network: "网络/上游不可达", rate_limited: "限流（429）",
    api: "飞书接口返回错误", config: "配置或字段映射问题",
    parse: "数据解析/分页校验失败",
    schema: "本地快照库版本不匹配（已停止读写，见运维手册）",
  };
  if (map[kind]) return map[kind];
  return String(kind).indexOf("internal:") === 0 ? "内部错误（详见服务端日志）" : kind;
}

/** 推送队列计数：待推送（含推送中）/ 推送失败 / 未确认冲突 */
export function queueCounts(): { open: number; failed: number; conflicts: number } {
  const c = store.cache;
  if (!c) return { open: 0, failed: 0, conflicts: 0 };
  return { open: (c.pending || 0) + (c.pushing || 0), failed: c.failed || 0, conflicts: c.open_conflicts || 0 };
}

/** 侧栏同步状态行：只展示最新同步时间，异常作为短后缀 */
export function syncLine(): { text: string; cls: string } {
  const s = store.sync;
  if (!s.has_snapshot) return { text: "尚无同步数据", cls: "p-sync is-empty" };
  const q = queueCounts();
  const bits = [s.last_success_at ? "同步于 " + fmtDT(s.last_success_at) : "尚未成功同步"];
  if (s.stale) bits.push("缓存陈旧");
  if (s.last_error) bits.push("上次失败：" + errText(s.last_error));
  if (q.open) bits.push(q.open + " 条改动待推送");
  if (q.failed) bits.push(q.failed + " 条推送失败");
  if (q.conflicts) bits.push(q.conflicts + " 条冲突待确认");
  return {
    text: bits.join(" · "),
    cls: "p-sync" + (s.stale ? " is-stale" : "") + (s.last_error || q.failed ? " is-failed" : "") +
      (q.conflicts ? " is-warn" : ""),
  };
}

/** 用任一接口返回的信封刷新同步状态 */
export function applyEnvelope(env: Envelope | SyncStatus): void {
  if (env.server_time) store.serverOffset = env.server_time - Date.now() / 1000;
  store.version = env.snapshot_version;
  store.lastStale = env.stale;
  if (typeof env.has_snapshot === "boolean") store.sync.has_snapshot = env.has_snapshot;
  store.sync.last_success_at = env.last_success_at;
  store.sync.stale = !!env.stale;
  store.sync.last_error = env.last_error || null;
  store.sync.known = true;
  if ("cache" in env && env.cache) store.cache = env.cache;
}

export function requestRefresh(): void {
  store.refreshSeq++;
}

export async function loadMe(): Promise<void> {
  try {
    store.me = await fetchMe();
  } catch {
    store.me = null;
  } finally {
    store.meLoaded = true;
  }
}

export async function loadMeta(): Promise<void> {
  if (store.meta) return;
  try { store.meta = await fetchMeta(); } catch { /* 元数据缺失时用默认五组 */ }
}

function queueKey(): string {
  const q = queueCounts();
  return q.open + "/" + q.failed + "/" + q.conflicts;
}

/** 轮询：比对快照版本、陈旧标记与推送队列计数，变化才通知当前页刷新 */
export function startPolling(): void {
  let pending = false;
  window.setInterval(async () => {
    if (pending || document.hidden) return;
    pending = true;
    try {
      const st = await fetchSyncStatus();
      const before = queueKey();
      const changed = st.snapshot_version !== store.version || st.stale !== store.lastStale;
      applyEnvelope(st);
      if (changed || queueKey() !== before) requestRefresh();
    } catch { /* 网络抖动：下一轮再试 */ }
    pending = false;
  }, POLL_MS);
}

/** 写入成功的提示：先存本地的说明正在推送，直写的说明已写入飞书（服务端给了文案就用它） */
export function writeNote(res?: WriteResult | null, fallback?: string): string {
  if (res && res.hint) return res.hint;
  if (res && res.queued) return "已保存，正在推送到飞书（通常几秒内完成）";
  return fallback || "已写入飞书";
}

/** 写入成功：弹出通知 + 立即刷新（响应返回时页面数据已含这次改动） */
export function afterWrite(res?: WriteResult | null, fallback?: string): void {
  toast(writeNote(res, fallback), "ok");
  requestRefresh();
}
