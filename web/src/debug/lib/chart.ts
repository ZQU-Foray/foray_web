/* 发射调试·波形区（移植自旧版 static/chart.js，录制与绘制逻辑逐项保持一致）：
 * 遥测录制 + 按数据类型分图的自绘 Canvas 示波器。每个勾选的数据类型一张图，图内三电机
 * 同量纲同尺度；线型区分电机（实线/长虚/点虚），目标值 = 同色半透明细线（随模式归属对应图）。
 * 由 shoot.ts 每帧遥测调用 detail.record()；环形缓冲 60s @50Hz。
 *
 * 50Hz 数据全部放在普通 TypedArray 里，不进 Vue 响应式；图框与数值总览是命令式 DOM，
 * 只有「开始/停止绘图」与列数这两个按钮态是响应式的。勾选框状态在用时直接读 DOM
 * （与旧版一致：脚本直接改 checked 而不派发事件时，导出 / 绘图仍以界面实际勾选为准）。 */
import { reactive } from "vue";
import { deviceCtx } from "./device";
import type { Telemetry } from "./shoot-types";

const N = 3000;                 // 60s × 50Hz 环形缓冲长度
const WINDOW_MS = 60000;
const DRAW_INTERVAL = 66;       // 绘制节流 ≈15fps（多图下控主线程占用）

export interface FieldDef { key: string; name: string; unit: string; color: string; on: boolean }

/* 曲线配色沿用旧版 chart.js 的数据色（深底示波器上的可辨识度已验证） */
export const FIELDS: FieldDef[] = [
  { key: "rpm",         name: "转速",     unit: "rpm", color: "#FAFA28", on: true },
  { key: "deg",         name: "角度",     unit: "°",   color: "#00D8D8", on: false },
  { key: "current",     name: "电流",     unit: "A",   color: "#FF2FA5", on: true },
  { key: "torque",      name: "力矩",     unit: "N·m", color: "#7FD9A0", on: false },
  { key: "temperature", name: "温度",     unit: "℃",  color: "#E0A458", on: false },
  { key: "freq",        name: "射击频率", unit: "Hz",  color: "#A78BFA", on: false },
];
const MOTOR_NAMES = ["M-1", "M-2", "M-3"];
const DASH = [[], [7, 5], [2, 4]];
const MODE_FIELD: Record<number, string> = { 1: "rpm", 2: "deg", 3: "torque" };   // 模式 → 目标归属图

export interface TargetSample { kind: string; value: number }

/** 按钮态（模板绑定） */
export const detailUi = reactive({ started: false, cols: "auto" });

/* ---------- 环形缓冲 ---------- */
const times = new Float64Array(N);
let head = 0, count = 0;
const series: Record<string, Float32Array> = {};
const seriesOf = (key: string) => series[key] || (series[key] = new Float32Array(N).fill(NaN));
const lastMode = [-1, -1, -1];

let started = false;          // 默认关闭：手动开启后才录制绘制
let latest: { t: Telemetry; targets: (TargetSample | null)[]; freqs: number[] } | null = null;   // 最近一帧（数值总览始终使用）
const zoomed = new Set<string>();   // 用户放大的字段图（图表重建后恢复）

function record(t: Telemetry, targets: (TargetSample | null)[], freqs: number[]): void {
  if (!t || !t.slots) return;
  latest = { t, targets, freqs };     // 数值总览始终刷新
  if (!started) return;               // 未开始绘图：不写缓冲
  times[head] = Date.now();
  for (let i = 0; i < 3; i++) {
    const s = t.slots[i] as unknown as Record<string, number> | undefined;
    if (!s) continue;
    for (const f of FIELDS) {
      if (f.key === "freq") continue;           // 频率由外部计算传入
      seriesOf("m" + i + "_" + f.key)[head] = +s[f.key] || 0;
    }
    seriesOf("m" + i + "_freq")[head] = (freqs && Number.isFinite(freqs[i])) ? freqs[i] : NaN;
    const tg = targets && targets[i];
    const tArr = seriesOf("tgt" + i);
    if (tg && s.mode >= 1 && s.mode <= 3) {
      tArr[head] = (lastMode[i] !== -1 && lastMode[i] !== s.mode) ? NaN : (+tg.value || 0);
      lastMode[i] = s.mode;
    } else {
      tArr[head] = NaN;
      lastMode[i] = -1;
    }
  }
  head = (head + 1) % N;
  if (count < N) count++;
}

/* ---------- DOM ---------- */
const $id = (i: string) => document.getElementById(i);
const isOn = (id: string) => !!($id(id) as HTMLInputElement | null)?.checked;
let grid: HTMLElement | null = null;
let numHost: HTMLElement | null = null;
const lastLegend = new WeakMap<HTMLElement, number>();

/* 深底配色取自设计令牌（控制台色阶），只在首次绘制时读取一次 */
let palette: { bg: string; grid: string; axis: string; text: string; font: string } | null = null;
function colors() {
  if (!palette) {
    const cs = getComputedStyle(document.documentElement);
    const v = (name: string, fb: string) => cs.getPropertyValue(name).trim() || fb;
    palette = {
      bg: v("--c-console", "#141414"),
      grid: v("--c-console-line", "#2E2E2E"),
      axis: v("--c-log-dim", "#6B6B6B"),
      text: v("--c-console-text", "#D4D4D4"),
      /* 画布字体要用解析后的字族（令牌里含 var() 引用，canvas 不认） */
      font: getComputedStyle(document.body).fontFamily || "sans-serif",
    };
  }
  return palette;
}

/** 勾选状态快照（决定图集合，变化时重建） */
function layoutKey(): string {
  return FIELDS.filter((f) => isOn("dc-" + f.key)).map((f) => f.key).join(",") + "|" +
    [1, 2, 3].filter((m) => isOn("dc-m" + m)).join("") + "|" +
    (isOn("dc-target") ? "T" : "-") + "|" +
    (latest && latest.t ? latest.t.slots.map((s) => s && s.mode).join("") : "000");
}

function hintBox(text: string): HTMLElement {
  const d = document.createElement("div");
  d.className = "dchart-hint";
  d.textContent = text;
  return d;
}

function rebuildCharts(key: string): string {
  if (!grid) return key;
  grid.replaceChildren();
  for (const f of FIELDS) {
    if (!isOn("dc-" + f.key)) continue;
    const box = document.createElement("div");
    box.className = "dchart";
    box.dataset.field = f.key;
    const title = document.createElement("div");
    title.className = "dchart-title";
    const sw = document.createElement("span");
    sw.className = "sw";
    sw.style.setProperty("--c", f.color);
    const unit = document.createElement("i");
    unit.textContent = `(${f.unit})`;
    title.append(sw, f.name + " ", unit);
    const cv = document.createElement("canvas");
    cv.className = "dchart-cv";
    const lg = document.createElement("div");
    lg.className = "dchart-legend";
    box.append(title, cv, lg);
    if (zoomed.has(f.key)) box.classList.add("zoom");
    box.title = "单击放大 / 还原";
    box.addEventListener("click", () => flipZoom(box, f.key));
    grid.appendChild(box);
  }
  if (!grid.children.length) grid.appendChild(hintBox("勾选上方数据类型以显示波形图"));
  return key;
}

/* ---------- 通用 FLIP：任意布局变化（放大 / 布局切换 / 窗口 resize）统一补间 ---------- */

const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function snapRects(): Map<HTMLElement, DOMRect> {
  const m = new Map<HTMLElement, DOMRect>();
  if (grid) for (const b of grid.querySelectorAll<HTMLElement>(".dchart")) m.set(b, b.getBoundingClientRect());
  return m;
}

/* 把所有图框从快照位置/尺寸平滑过渡到当前位置：
   位移的兄弟框只做 translate，尺寸变化的主框额外带 scale */
function flipFrom(before: Map<HTMLElement, DOMRect> | null): void {
  if (!before || !grid || reduceMotion()) return;
  const moving: HTMLElement[] = [];
  for (const b of grid.querySelectorAll<HTMLElement>(".dchart")) {
    const prev = before.get(b);
    if (!prev) continue;
    const now = b.getBoundingClientRect();
    const dx = prev.left - now.left, dy = prev.top - now.top;
    const sx = prev.width / now.width, sy = prev.height / now.height;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && Math.abs(sx - 1) < 0.01 && Math.abs(sy - 1) < 0.01) continue;
    b.style.transition = "none";
    b.style.transformOrigin = "0 0";
    b.style.transform = "translate(" + dx + "px," + dy + "px) scale(" + sx + "," + sy + ")";
    moving.push(b);
  }
  if (!moving.length) return;
  requestAnimationFrame(() => {
    for (const b of moving) {
      b.style.transition = "transform .28s cubic-bezier(.4,0,.2,1)";
      b.style.transform = "";
      const done = () => {
        b.style.transition = ""; b.style.transform = "";
        b.removeEventListener("transitionend", done);
      };
      b.addEventListener("transitionend", done);
      window.setTimeout(done, 450);           // 兜底清理
    }
  });
}

function flipZoom(box: HTMLElement, fieldKey: string): void {
  const before = snapRects();
  const willZoom = !box.classList.contains("zoom");
  box.classList.toggle("zoom", willZoom);
  if (willZoom) zoomed.add(fieldKey); else zoomed.delete(fieldKey);
  box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  flipFrom(before);
  if (started || count > 0) schedule();
}

/* ---------- 窗口 resize：首帧快照 + 停顿后统一补间净变化 ---------- */
let resizeSnap: Map<HTMLElement, DOMRect> | null = null, resizeTimer = 0;
function onResize() {
  if (!grid || !grid.querySelector(".dchart")) return;
  if (!resizeSnap) resizeSnap = snapRects();
  clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(() => {
    const snap = resizeSnap;
    resizeSnap = null;
    flipFrom(snap);
  }, 240);
}

/* ---------- 布局密度控制：自动 / 1 列 / 2 列（记忆用户选择） ---------- */
function applyLayout(cols: string): void {
  if (grid) grid.dataset.cols = cols;     // 同步写 DOM：FLIP 需要立刻量到新布局
  detailUi.cols = cols;
  try { localStorage.setItem("dchart-cols", cols); } catch { /* 隐私模式：本次有效 */ }
}

function setCols(cols: string): void {
  const before = snapRects();
  applyLayout(cols);
  flipFrom(before);                       // 列数切换同样带移动补间
}

let rafId = 0;
let lastDraw = 0;
let chartKey = "";

function draw(): void {
  rafId = 0;
  if (!grid) return;
  const key = layoutKey();
  if (key !== chartKey) chartKey = rebuildCharts(key);

  const now = performance.now();
  if (now - lastDraw < DRAW_INTERVAL) { schedule(); return; }
  lastDraw = now;

  for (const box of grid.querySelectorAll<HTMLElement>(".dchart")) drawOne(box, box.dataset.field || "");
  schedule();
}

function drawOne(box: HTMLElement, fieldKey: string): void {
  const canvas = box.querySelector("canvas");
  if (!canvas) return;
  const pal = colors();
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  if (!W || !H) return;
  /* 背板尺寸用整数比较：分数缩放（如 125%）下 W*dpr 为小数，
     直接比较永不相等 → 每帧重设尺寸清屏 → 频闪 */
  const bw = Math.round(W * dpr), bh = Math.round(H * dpr);
  if (canvas.width !== bw || canvas.height !== bh) {
    canvas.width = bw; canvas.height = bh;
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.globalAlpha = 1;
  ctx.fillStyle = pal.bg;
  ctx.fillRect(0, 0, W, H);

  if (count < 2) {
    ctx.fillStyle = pal.axis;
    ctx.font = "400 12px " + pal.font;
    ctx.textAlign = "center";
    ctx.fillText(started ? "等待遥测数据…" : "已停止（数据已冻结/为空）", W / 2, H / 2);
    return;
  }

  const tEnd = times[(head - 1 + N) % N];
  const tStart = tEnd - WINDOW_MS;
  const padL = 6, padR = 6, padT = 6, padB = 20;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const xOf = (t: number) => padL + (t - tStart) / WINDOW_MS * plotW;

  /* 水平网格 */
  ctx.strokeStyle = pal.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (const fy of [0.25, 0.5, 0.75]) {
    ctx.moveTo(0, (padT + plotH) * fy); ctx.lineTo(W, (padT + plotH) * fy);
  }
  ctx.stroke();

  /* 时间轴：每 10s 竖网格 + 底轴线 + 刻度标签 */
  const endSec = Math.floor(tEnd / 1000);
  ctx.beginPath();
  for (let s = Math.ceil(tStart / 10000) * 10; s <= endSec; s += 10) {
    const x = Math.round(xOf(s * 1000)) + 0.5;
    ctx.moveTo(x, padT); ctx.lineTo(x, H - padB);
  }
  ctx.stroke();
  ctx.strokeStyle = pal.axis;
  ctx.beginPath();
  ctx.moveTo(0, H - padB + 0.5); ctx.lineTo(W, H - padB + 0.5);
  ctx.stroke();
  ctx.fillStyle = pal.text;
  ctx.globalAlpha = 0.75;
  ctx.font = "400 10px " + pal.font;
  ctx.textAlign = "center";
  for (let s = Math.ceil(tStart / 10000) * 10; s <= endSec; s += 10) {
    const rel = s - endSec;
    ctx.fillText(rel === 0 ? "0s" : rel + "s", xOf(s * 1000), H - 6);
  }
  ctx.globalAlpha = 1;

  /* 该图的序列：勾选电机的该字段 + 目标（模式匹配时） */
  const items: { key: string; motor: number; isTarget: boolean }[] = [];
  for (let m = 0; m < 3; m++) {
    if (!isOn("dc-m" + (m + 1))) continue;
    items.push({ key: "m" + m + "_" + fieldKey, motor: m, isTarget: false });
    if (isOn("dc-target") && latest && latest.t.slots[m] &&
        MODE_FIELD[latest.t.slots[m].mode] === fieldKey)
      items.push({ key: "tgt" + m, motor: m, isTarget: true });
  }
  if (!items.length) return;

  /* 图内公共 min/max（同量纲可比） */
  let mn = Infinity, mx = -Infinity;
  const field = FIELDS.find((f) => f.key === fieldKey);
  if (!field) return;
  for (const it of items) {
    const arr = series[it.key];
    if (!arr) continue;
    for (let k = 0; k < count; k++) {
      const idx = (head - count + k + N * 2) % N;
      if (times[idx] < tStart) continue;
      const v = arr[idx];
      if (Number.isFinite(v)) {
        if (v < mn) mn = v;
        if (v > mx) mx = v;
      }
    }
  }
  if (!Number.isFinite(mn)) return;
  if (mx - mn < 1e-9) { mn -= 1; mx += 1; }
  const pad = (mx - mn) * 0.08;
  mn -= pad; mx += pad;
  const yOf = (v: number) => padT + (1 - (v - mn) / (mx - mn)) * plotH;

  /* 抽点步长：每像素最多一个采样点 */
  const samples = Math.min(count, Math.round(WINDOW_MS / 20));
  const stride = Math.max(1, Math.floor(samples / plotW));

  for (const it of items) {
    const arr = series[it.key];
    if (!arr) continue;
    ctx.strokeStyle = field.color;
    ctx.globalAlpha = it.isTarget ? 0.45 : 1;
    ctx.lineWidth = it.isTarget ? 1.2 : 1.8;
    ctx.setLineDash(DASH[it.motor]);
    ctx.beginPath();
    let pen = false;
    for (let k = 0; k < count; k += stride) {
      const idx = (head - count + k + N * 2) % N;
      if (times[idx] < tStart) continue;
      const v = arr[idx];
      if (!Number.isFinite(v)) { pen = false; continue; }
      const x = xOf(times[idx]), y = yOf(v);
      if (pen) ctx.lineTo(x, y); else { ctx.moveTo(x, y); pen = true; }
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
  }

  /* 图例：实时数值（每图独立 5Hz 节流） */
  const now = Date.now();
  if (now - (lastLegend.get(box) || 0) > 200) {
    lastLegend.set(box, now);
    const lg = box.querySelector(".dchart-legend");
    if (lg) {
      lg.replaceChildren(...items.map((it) => {
        const arr = series[it.key];
        let v = NaN;
        for (let k = 1; k <= count; k++) {
          const x = arr[(head - k + N * 2) % N];
          if (Number.isFinite(x)) { v = x; break; }
        }
        const chip = document.createElement("span");
        chip.className = "dchart-chip";
        const b = document.createElement("b");
        b.textContent = MOTOR_NAMES[it.motor] + (it.isTarget ? " 目标" : "");
        const u = document.createElement("i");
        u.textContent = field.unit;
        chip.append(b, " " + (Number.isFinite(v) ? (+v).toFixed(fieldKey === "temperature" ? 0 : 2) : "-") + " ", u);
        return chip;
      }));
    }
  }
}

function schedule(): void {
  if (!rafId) rafId = requestAnimationFrame(draw);
}

/* ---------- CSV 导出（仅勾选序列） ---------- */
function exportCsv(): void {
  const vis: string[] = [];
  for (let m = 0; m < 3; m++) {
    if (!isOn("dc-m" + (m + 1))) continue;
    for (const f of FIELDS) if (isOn("dc-" + f.key)) vis.push("m" + m + "_" + f.key);
    if (isOn("dc-target")) vis.push("tgt" + m);
  }
  if (!vis.length || count < 2) { alert("没有可导出的勾选数据"); return; }
  const tEnd = times[(head - 1 + N) % N];
  const colName = (k: string) => {
    const motor = +k.slice(k.search(/\d/), k.search(/\d/) + 1);
    if (k.startsWith("tgt")) return `Motor${motor + 1}_target`;
    const f = FIELDS.find((x) => k.endsWith("_" + x.key))!;
    return `Motor${motor + 1}_${f.name}(${f.unit})`;
  };
  const esc = (s: string) => '"' + String(s).replace(/"/g, '""') + '"';
  const lines = [["t_s"].concat(vis.map(colName)).map(esc).join(",")];
  for (let k = 0; k < count; k++) {
    const idx = (head - count + k + N * 2) % N;
    const row = [((times[idx] - tEnd) / 1000).toFixed(3)];
    for (const key of vis) {
      const v = series[key] ? series[key][idx] : NaN;
      row.push(Number.isFinite(v) ? String(v) : "");
    }
    lines.push(row.join(","));
  }
  const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  const d = new Date(), p = (n: number) => String(n).padStart(2, "0");
  a.href = URL.createObjectURL(blob);
  /* 文件名带设备 ID：多设备时导出的数据不会混淆 */
  a.download = `shoot_${deviceCtx.id || "legacy-default"}_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}` +
               `_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

/* ---------- 数值总览（始终显示全部字段，不受勾选影响） ---------- */
const numCells: { el: HTMLElement; f: string; m: number }[] = [];

function buildNumGrid(): void {
  if (!numHost) return;
  numHost.replaceChildren();
  numCells.length = 0;
  const row = (cls: string) => { const r = document.createElement("div"); r.className = cls; return r; };
  const cell = (cls: string) => { const c = document.createElement("span"); c.className = cls; return c; };
  const headRow = row("numrow numhead");
  headRow.appendChild(cell("numcell"));
  for (const f of FIELDS) {
    const c = cell("numcell");
    const sw = cell("sw");
    sw.style.setProperty("--c", f.color);
    c.append(sw, f.name);
    headRow.appendChild(c);
  }
  numHost.appendChild(headRow);
  for (let m = 0; m < 3; m++) {
    const r = row("numrow");
    const name = cell("numcell nummotor");
    name.textContent = "M-" + (m + 1);
    r.appendChild(name);
    for (const f of FIELDS) {
      const c = cell("numcell numval");
      c.dataset.f = f.key;
      c.dataset.m = String(m);
      const v = cell("v");
      v.textContent = "-";
      const u = cell("u");
      u.textContent = f.unit;
      c.append(v, u);
      r.appendChild(c);
      numCells.push({ el: v, f: f.key, m });
    }
    numHost.appendChild(r);
  }
}

function fmtNum(fkey: string, v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "-";
  if (fkey === "temperature") return (+v).toFixed(0);
  if (fkey === "freq") return (+v).toFixed(1);
  return (+v).toFixed(2);
}

function refreshNumGrid(): void {
  for (const c of numCells) {
    let v = NaN;
    if (latest && latest.t && latest.t.slots) {
      v = c.f === "freq"
        ? (latest.freqs ? latest.freqs[c.m] : NaN)
        : (latest.t.slots[c.m] as unknown as Record<string, number>)[c.f];
    }
    c.el.textContent = fmtNum(c.f, v);
  }
}

/** 离线（会话断开）：清空数值总览 */
function offline(): void {
  latest = null;
  refreshNumGrid();
}

/** 开始 / 停止绘图；停止后保留冻结画面（仍可导出） */
function toggleStart(): void {
  started = !started;
  detailUi.started = started;
  if (started) schedule();
  else if (count > 0) draw();
}

/** 清空缓冲（切换设备时也调用：旧设备的波形不得混入新设备） */
function clear(): void {
  head = 0; count = 0; lastMode.fill(-1);
  for (const k in series) series[k].fill(NaN);
  if (grid) for (const box of grid.querySelectorAll<HTMLElement>(".dchart")) drawOne(box, box.dataset.field || "");
}

/** 勾选变化：重新排程绘制（图集合在下一帧按勾选重建） */
function onPanelChange(): void {
  if (started || count > 0) schedule();
}

let numTimer = 0;
/** 挂载：图网格与数值总览容器由组件提供 */
function attach(gridEl: HTMLElement, numEl: HTMLElement): void {
  grid = gridEl;
  numHost = numEl;
  buildNumGrid();
  let saved = "auto";
  try { saved = localStorage.getItem("dchart-cols") || "auto"; } catch { saved = "auto"; }
  applyLayout(saved);
  window.clearInterval(numTimer);
  numTimer = window.setInterval(refreshNumGrid, 200);   // 数值总览独立于绘图开关
  window.addEventListener("resize", onResize);
  chartKey = "";
  if (count === 0) {
    grid.replaceChildren(hintBox("点击「开始绘图」开始录制并显示波形（默认关闭）"));
  }
}

function detach(): void {
  window.clearInterval(numTimer);
  window.removeEventListener("resize", onResize);
  if (rafId) cancelAnimationFrame(rafId);
  rafId = 0;
  grid = null;
  numHost = null;
}

export const detail = { attach, detach, record, offline, toggleStart, clear, exportCsv, setCols, onPanelChange };
