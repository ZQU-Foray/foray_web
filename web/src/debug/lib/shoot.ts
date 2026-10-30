/* 发射机构三电机调试（移植自旧版 static/shoot.js，协议与交互逻辑逐项保持一致）：
 * 型号 / 模式 / PID / 目标滑杆（150ms 节流推送）/ 预设三级命名空间（槽位 × 型号 × ≤5 套）/
 * 使能流（急停锁定、离线槽位、Disable 模式引导）/ 急停 / RTT / 离线渲染 /
 * 链路恢复（遥测中断 >2.5s 或 link 0→1）后重推默认预设。
 *
 * 分工：Vue 渲染电机卡结构与慢变化的界面态（shootUi：折叠、故障标签、使能态、目标行、
 * 预设与型号选项、链路 / RTT 徽标）；50Hz 遥测按 rAF 合帧后命令式写入遥测数值节点
 * （tr/td/tc/tq/tt/to/ten/freq），原始遥测帧不进响应式。表单输入框是非受控的，
 * 和旧版一样按 id 直接读写 DOM（模板不绑定 value，Vue 不会覆盖用户正在输入的内容）；
 * 型号 / 模式 / 预设下拉写值走自绘下拉的 value 拦截器（交互期间暂存，结束后落盘）。 */
import { nextTick, reactive } from "vue";
import { dfetch, jsonInit, noop, wsHref } from "./api";
import { deviceCtx } from "./device";
import { toast } from "./toast";
import { detail, type TargetSample } from "./chart";
import type { DdOption } from "./dropdown";
import type { CmdResult, MotorPresetsDoc, PresetCell, Profile, ShootStatus, Telemetry } from "./shoot-types";

export const MODES = ["停止（无输出）", "转速 (rpm)", "位置 (deg)", "力矩 (N·m)"];
const FAULTS: [number, string][] = [
  [0, "离线"], [1, "过温警告"], [2, "过温"], [3, "过流"], [4, "参数被拒"], [5, "链路丢失"], [6, "急停"],
];
export const PARAMS: [string, string][] = [
  ["kp_speed", "Kp 速度"], ["ki_speed", "Ki 速度"], ["kd_speed", "Kd 速度"],
  ["kf_speed", "Kf 速度"], ["il_speed", "积分限幅"], ["dz_speed", "死区(rpm)"],
  ["kp_angle", "Kp 角度"], ["ki_angle", "Ki 角度"], ["kd_angle", "Kd 角度"],
  ["kf_angle", "Kf 角度"], ["il_angle", "积分限幅A"], ["dz_angle", "死区(deg)"],
];
const TELEM_KEYS = ["tr", "td", "tc", "tq", "tt", "to", "ten"];

type Kind = "rpm" | "deg" | "torque";
const KIND: Record<number, Kind | undefined> = { 1: "rpm", 2: "deg", 3: "torque" };
const META: Record<Kind, [string, string, number]> = {
  rpm: ["目标转速", "rpm", 1],
  deg: ["目标角度", "deg", 5],
  torque: ["目标力矩", "N·m", 0.01],
};
const DEG_LIMIT = 3600;   // 与固件 kMaxTargetDegLimit 一致

export interface FaultTag { text: string; cls: string }

function makeSlotUi() {
  return {
    open: false,                      // 电机卡折叠（一级）
    ctrlOpen: false,                  // 控制参数折叠（二级）
    mechOpen: false,                  // 机械参数折叠（二级）
    faults: [] as FaultTag[],
    offline: false,
    enRunning: false,                 // 按钮真值 = 遥测 enabled 位
    tgtShow: false,                   // 停止模式无目标行
    tgtLabel: "目标",
    tgtUnit: "",
    tgtMin: "-",
    tgtMax: "+",
    is2006: true,                     // 槽数 / 射击频率仅对 M2006 开放
    presetOpts: [] as DdOption[],
  };
}

/** 模板绑定的界面态（慢变化；遥测数值不在这里） */
export const shootUi = reactive({
  link: { text: "链路: 查询中", cls: "neutral", title: "" },
  rtt: { text: "RTT: -", cls: "neutral" },
  openBusy: false,
  modeOpts: MODES.map((m, i) => ({ value: String(i), label: m })) as DdOption[],
  modelOpts: [] as DdOption[],
  slots: [makeSlotUi(), makeSlotUi(), makeSlotUi()],
});

let profiles: Profile[] = [];
let defaults: Record<string, number> = {};
let ws: WebSocket | null = null;
let wsWant = false;
let serverOpen = false;
let shootTelem: Telemetry | null = null;

/* ---------- DOM 读写（与旧版 $id / val 同义；元素缺失时安全返回） ---------- */
const $id = (i: string) => document.getElementById(i);
function val(id: string): string {
  const el = $id(id) as HTMLInputElement | HTMLSelectElement | null;
  return el ? el.value : "";
}
function setVal(id: string, v: string | number): void {
  const el = $id(id) as HTMLInputElement | HTMLSelectElement | null;
  if (el) el.value = String(v);
}
function setText(id: string, text: string): void {
  const el = $id(id);
  if (el) el.textContent = text;
}
function isFiniteNum(v: number): boolean {
  return Number.isFinite(v);
}

/* quiet=true 时不弹 toast（供周期轮询静默失败，避免刷屏） */
async function api(path: string, body?: unknown, quiet?: boolean): Promise<CmdResult> {
  try {
    const opt = body !== undefined ? jsonInit("POST", body) : undefined;
    const generation = deviceCtx.generation;
    const base = deviceCtx.api("shoot");
    const r = await dfetch(base + "/" + path, opt);
    const j = await r.json() as CmdResult & { detail?: unknown };
    if (generation !== deviceCtx.generation) throw new Error("设备已切换，忽略旧响应");
    if (!r.ok) {
      const d = j.detail;
      throw new Error(typeof d === "string" && d ? d : d != null ? JSON.stringify(d) : String(r.status));
    }
    return j;
  } catch (e) {
    const names: Record<string, string> = { open: "连接串口", close: "断开连接", status: "读取状态" };
    if (!quiet) toast((names[path] || path) + "失败：" + (e as Error).message, true);
    throw e;
  }
}

function fillDefaults(): void {
  for (let s = 0; s < 3; s++) {
    for (const k in defaults) {
      const el = $id(k + s) as HTMLInputElement | null;
      if (el && document.activeElement !== el) el.value = String(defaults[k]);
    }
    const g = $id("gear" + s) as HTMLInputElement | null;
    const pr = profiles[+val("model" + s)];
    if (g && pr) g.value = String(pr.gear_ratio);
    /* 各模式目标初值（对应旧输入框的默认填充） */
    tgtState[s].rpm = defaults.target_rpm || 0;
    tgtState[s].deg = defaults.target_deg || 0;
    tgtState[s].torque = defaults.target_torque || 0;
    updateTargetUI(s);
  }
}

/* ---------- 目标滑杆：按模式显隐 + 按型号/固件上限自动限幅 ---------- */

const tgtState: Record<Kind, number>[] = [   // 每槽位保存三种目标的历史值，切模式互不丢失
  { rpm: 0, deg: 0, torque: 0 },
  { rpm: 0, deg: 0, torque: 0 },
  { rpm: 0, deg: 0, torque: 0 },
];
const uiCache: Record<number, string> = {};   // 槽位 → "mode-model"，遥测变化时才刷新滑杆 UI
let lastLinkSeen = -1;    // 上帧 link 位（-1 未知）
let lastTelemAt = 0;      // 上帧遥测到达时刻（检测遥测中断）

/* ---------- 电机调参预设（按 槽位×型号 命名空间，Web 侧存储） ---------- */

let motorPresets: MotorPresetsDoc | null = null;            // 服务器预设文档
const presetAutoApplied: Record<string, boolean> = {};     // "slot-model" → 本会话已自动应用过默认预设
let autoAppliedOnConnect = false;                           // 本次连接已执行过默认预设自动应用

async function loadMotorPresets(): Promise<void> {
  try {
    const generation = deviceCtx.generation;
    const r = await dfetch(deviceCtx.api("shoot/presets"));
    const doc = await r.json() as MotorPresetsDoc;
    if (r.ok && generation === deviceCtx.generation) motorPresets = doc;
  } catch { /* 静默，下次刷新再试 */ }
}

function pCell(s: number, model: string): PresetCell | null {
  return (motorPresets && motorPresets.motor_presets
    && motorPresets.motor_presets[s] && motorPresets.motor_presets[s][model]) || null;
}

function ensureCell(s: number, model: string): PresetCell {
  if (!motorPresets) motorPresets = { motor_presets: {} };
  const mp = motorPresets.motor_presets;
  if (!mp[s]) mp[s] = {};
  if (!mp[s][model]) mp[s][model] = { presets: [], default: -1 };
  return mp[s][model];
}

async function persistPresets(): Promise<boolean> {
  try {
    const generation = deviceCtx.generation;
    const r = await dfetch(deviceCtx.api("shoot/presets"), jsonInit("PUT", motorPresets));
    if (!r.ok) {
      let msg = String(r.status);
      try { const j = await r.json() as { detail?: unknown }; if (j.detail) msg = String(j.detail); } catch { /* 用状态码 */ }
      throw new Error(msg);
    }
    const doc = await r.json() as MotorPresetsDoc;
    if (generation !== deviceCtx.generation) return false;
    motorPresets = doc;   // 采纳服务器规整后的文档
    return true;
  } catch (e) {
    toast("预设保存失败: " + (e as Error).message, true);
    await loadMotorPresets();        // 失败则回读，避免本地与服务器漂移
    return false;
  }
}

/** 重建预设下拉（跟随当前型号；默认预设带 ★ 前缀）。
 *  旧版是整表重建、选中值随之回到「（选择预设）」；这里同样落到 select（默认 -1）。 */
function refreshPresetUI(s: number, select = "-1"): void {
  const ui = shootUi.slots[s];
  if (!ui) return;
  const cell = pCell(s, val("model" + s));
  const opts: DdOption[] = [{ value: "-1", label: "（选择预设）" }];
  if (cell) cell.presets.forEach((p, i) =>
    opts.push({ value: String(i), label: (cell.default === i ? "★ " : "") + p.name }));
  ui.presetOpts = opts;
  void nextTick(() => setVal("preset" + s, select));
}

/* 应用预设：填表 + 逐项下发 + 生效 */
async function applyPreset(s: number, idx: number, quiet: boolean): Promise<boolean> {
  const cell = pCell(s, val("model" + s));
  if (!cell || idx < 0 || idx >= cell.presets.length) return false;
  const p = cell.presets[idx];
  const params = p.params || {};
  let n = 0;
  for (const k in params) {
    const el = $id(k + s) as HTMLInputElement | null;
    if (el) el.value = String(params[k]);
    const r = await api("param", { slot: s, key: k, value: params[k] }, true);
    if (r.ok) n++;
  }
  await api("apply", { slot: s }, true);
  if (!quiet) toast(`Motor${s + 1} 已应用预设「${p.name}」（${n} 项）`);
  return true;
}

/* 默认预设自动应用：连接时与换型号时触发一次 */
async function maybeAutoPreset(s: number, model: string | number, quiet: boolean): Promise<void> {
  const key = s + "-" + model;
  if (presetAutoApplied[key]) return;
  const cell = pCell(s, String(model));
  if (!cell || cell.default < 0 || cell.default >= cell.presets.length) return;
  presetAutoApplied[key] = true;
  await applyPreset(s, cell.default, quiet);
  toast(`Motor${s + 1} 已自动应用默认预设「${cell.presets[cell.default].name}」`);
}

async function savePreset(s: number): Promise<void> {
  const model = val("model" + s);
  const cell = ensureCell(s, model);
  if (cell.presets.length >= 5) {
    toast(`Motor${s + 1} 此型号预设已满 5 套，请先删除`, true);
    return;
  }
  const mname = (profiles[+model] || {} as Partial<Profile>).name || model;
  const name = prompt(`预设名称（Motor${s + 1} / ${mname}）：`, `预设${cell.presets.length + 1}`);
  if (name === null) return;                      // 取消
  const params: Record<string, number> = {};
  const gr = +val("gear" + s);
  if (isFiniteNum(gr) && val("gear" + s) !== "") params.gear_ratio = gr;
  for (const [k] of PARAMS) {
    const v = +val(k + s);
    if (isFiniteNum(v) && val(k + s) !== "") params[k] = v;
  }
  cell.presets.push({ name: (name.trim() || `预设${cell.presets.length + 1}`).slice(0, 24), params });
  if (await persistPresets()) {
    refreshPresetUI(s, String(cell.presets.length - 1));
    toast(`Motor${s + 1} 已保存预设「${cell.presets[cell.presets.length - 1].name}」`);
  }
}

async function deletePreset(s: number): Promise<void> {
  const idx = +val("preset" + s);
  const cell = pCell(s, val("model" + s));
  if (!cell || idx < 0 || idx >= cell.presets.length) {
    toast("请先选择要删除的预设", true);
    return;
  }
  const name = cell.presets[idx].name;
  if (!confirm(`删除 Motor${s + 1} 预设「${name}」？`)) return;
  cell.presets.splice(idx, 1);
  if (cell.default >= cell.presets.length) cell.default = -1;
  else if (cell.default > idx) cell.default -= 1;
  else if (cell.default === idx) cell.default = -1;
  if (await persistPresets()) {
    refreshPresetUI(s);
    toast(`已删除预设「${name}」`);
  }
}

async function toggleDefault(s: number): Promise<void> {
  const idx = +val("preset" + s);
  const cell = pCell(s, val("model" + s));
  if (!cell || idx < 0 || idx >= cell.presets.length) {
    toast("请先选择要设为默认的预设", true);
    return;
  }
  cell.default = (cell.default === idx) ? -1 : idx;
  if (await persistPresets()) {
    refreshPresetUI(s, String(idx));
    toast(cell.default === idx
      ? `「${cell.presets[idx].name}」已设为默认（连接/换型号自动应用）`
      : "已取消默认预设");
  }
}

function tgtLimit(s: number, kind: Kind): number {
  const pr = profiles[+val("model" + s)] || ({} as Partial<Profile>);
  if (kind === "rpm") return pr.max_output_rpm || 500;
  if (kind === "deg") return DEG_LIMIT;
  return pr.max_torque || 1.5;
}

/* 依据当前模式刷新滑杆行：显隐、标签、单位、上下限、步进。
   滑杆与数值框的 min/max/step/value 直接写 DOM（先限幅再写值，顺序不能被异步渲染打乱） */
function updateTargetUI(s: number): void {
  const ui = shootUi.slots[s];
  if (!ui) return;
  const mode = +val("mode" + s);
  const kind = KIND[mode];
  if (mode === 0 || !kind) { ui.tgtShow = false; return; }   // 停止模式无目标
  ui.tgtShow = true;
  const meta = META[kind];
  const lim = tgtLimit(s, kind);
  ui.tgtLabel = meta[0];
  ui.tgtUnit = meta[1];
  ui.tgtMin = String(-lim);
  ui.tgtMax = String(lim);
  const slider = $id("tslider" + s) as HTMLInputElement | null;
  if (slider) {
    slider.min = String(-lim); slider.max = String(lim); slider.step = String(meta[2]);
    slider.value = String(tgtState[s][kind]);
  }
  const tv = $id("tval" + s) as HTMLInputElement | null;
  if (tv) {
    tv.step = String(meta[2]);
    tv.value = String(tgtState[s][kind]);
  }
}

/* 机械参数区：槽数 / 电气频率仅对 M2006（model=0）开放 */
function updateMechUI(s: number): void {
  const ui = shootUi.slots[s];
  if (ui) ui.is2006 = +val("model" + s) === 0;
}

/* 下发当前滑杆目标：钳入固件合法范围（越界会被固件拒绝）。
   quiet=true 时不弹 toast（拖动过程中的节流推送静默，仅失败才提示） */
async function sendTarget(s: number, quiet: boolean): Promise<number | null | undefined> {
  const mode = +val("mode" + s);
  const kind = KIND[mode];
  if (!kind) return undefined;
  const lim = tgtLimit(s, kind);
  let v = +val("tval" + s);
  if (!isFiniteNum(v)) v = 0;
  v = Math.max(-lim, Math.min(lim, Math.round(v * 1000) / 1000));
  tgtState[s][kind] = v;
  setVal("tval" + s, v);
  setVal("tslider" + s, v);
  const r = await api("param", { slot: s, key: "target_" + kind, value: v }, quiet);
  if (!r.ok) toast(`Motor${s + 1} 目标下发失败: ${r.status}`, true);
  else await api("apply", { slot: s }, quiet);
  return r.ok ? v : null;
}

/* 拖动节流推送：150ms 间隔（前沿立即发 + 尾沿兜底），
   115200 波特率下 ≈ 230B/s 下行，与 50Hz 遥测共存带宽充裕，无需固件改动 */
const pushState: Record<number, { last: number; timer: number }> = {};
function pushThrottled(s: number): void {
  const st = pushState[s] || (pushState[s] = { last: 0, timer: 0 });
  const now = Date.now();
  if (now - st.last >= 150) {
    st.last = now;
    if (st.timer) { clearTimeout(st.timer); st.timer = 0; }
    sendTarget(s, true).catch(noop);
  } else if (!st.timer) {
    st.timer = window.setTimeout(() => {
      st.timer = 0;
      st.last = Date.now();
      sendTarget(s, true).catch(noop);
    }, 150 - (now - st.last));
  }
}

/* 目标复位 0 并立即下发 */
async function resetTarget(s: number): Promise<void> {
  setVal("tslider" + s, 0);
  setVal("tval" + s, 0);
  const v = await sendTarget(s, true);
  if (v !== null) toast(`Motor${s + 1} 目标已复位 0`);
}

/* ---------- 状态渲染（按钮真值 = 遥测 enabled 位） ---------- */

/* rAF 合帧：遥测 50Hz 到达时每帧只渲染一次屏幕刷新率，
   避免 WS 每消息直接操作 DOM 造成主线程过载 */
let pendingTelem: Telemetry | null = null;
let paintQueued = false;
function schedulePaint(t: Telemetry): void {
  pendingTelem = t;
  /* 波形录制：每帧遥测全量入环形缓冲（未开启绘图时只刷新数值总览） */
  const tg: (TargetSample | null)[] = t.slots.map((sl, i) => {
    const kind = KIND[sl.mode];
    return kind && tgtState[i] ? { kind, value: tgtState[i][kind] } : null;
  });
  /* 射击频率 = |实际转速| × 槽数 / 60（仅 M2006 提供槽数） */
  const fq = t.slots.map((sl, i) => {
    if (sl.model !== 0) return NaN;
    const poles = +val("pole" + i);
    return (isFiniteNum(poles) && poles > 0) ? Math.abs(sl.rpm) * poles / 60 : NaN;
  });
  detail.record(t, tg, fq);
  if (!paintQueued) {
    paintQueued = true;
    requestAnimationFrame(() => {
      paintQueued = false;
      if (pendingTelem) paint(pendingTelem);
    });
  }
}

const faultCache: Record<number, number | undefined> = {};   // 槽位 → 上次故障位（无变化则跳过重建标签）

function faultTags(fault: number): FaultTag[] {
  const tags: FaultTag[] = [];
  for (const [bit, name] of FAULTS) {
    if (fault & (1 << bit)) {
      /* 离线 = 灰色（非报警）；过温警告 / 过流 / 参数被拒 = 橙色；其余红色 */
      tags.push({ text: name, cls: bit === 0 ? "off" : (bit === 1 || bit === 3 || bit === 4) ? "warn" : "" });
    }
  }
  return tags;
}

function paint(t: Telemetry): void {
  shootTelem = t;
  t.slots.forEach((s, i) => {
    const ui = shootUi.slots[i];
    if (!ui) return;
    setText("tr" + i, Number(s.rpm).toFixed(1) + " rpm");
    setText("td" + i, Number(s.deg).toFixed(1) + "°");
    setText("tc" + i, Number(s.current).toFixed(2) + " A");
    setText("tq" + i, Number(s.torque).toFixed(2) + " N·m");
    setText("tt" + i, s.temperature + " ℃");
    setText("to" + i, (s.fault & 1) ? "否" : "是");
    setText("ten" + i, s.enabled ? "是" : "否");
    if (faultCache[i] !== s.fault) {         // 故障位变化才重建标签
      faultCache[i] = s.fault;
      ui.offline = false;                     // 遥测接管显示，清离线标记
      ui.faults = faultTags(s.fault);
    }
    const modeSel = $id("mode" + i) as HTMLSelectElement | null;
    if (modeSel && document.activeElement !== modeSel) modeSel.value = String(s.mode);
    const modelSel = $id("model" + i) as HTMLSelectElement | null;
    if (modelSel && document.activeElement !== modelSel) modelSel.value = String(s.model);
    /* 模式/型号变化时刷新滑杆与机械参数区（显隐与上下限跟随），每帧仅比较缓存 */
    const key = s.mode + "-" + s.model;
    if (uiCache[i] !== key) { uiCache[i] = key; updateTargetUI(i); updateMechUI(i); }
    ui.enRunning = s.enabled == 1;            // 按钮状态只看真实使能位
    /* 射击频率 = 实际转速 × 槽数 / 60（仅 M2006 提供槽数输入） */
    const poles = +val("pole" + i);
    setText("freq" + i, (isFiniteNum(poles) && poles > 0)
      ? (Math.abs(s.rpm) * poles / 60).toFixed(1) + " Hz" : "-");
  });
  setLink(t.link === 1);

  /* 链路恢复检测：遥测中断 >2.5s 或 link 位 0→1 → MCU 可能重启过，
     清除自动应用标记并重推各槽默认预设（link_lost 期间固件已闭锁输出，安全） */
  const now = Date.now();
  const telemGap = lastTelemAt ? now - lastTelemAt : 0;
  const recovered = t.link === 1 && lastLinkSeen !== -1 && (lastLinkSeen === 0 || telemGap > 2500);
  lastTelemAt = now;
  lastLinkSeen = t.link;
  if (recovered && wsWant) {
    for (const k of Object.keys(presetAutoApplied)) delete presetAutoApplied[k];
    for (let i = 0; i < 3; i++) maybeAutoPreset(i, val("model" + i), true).catch(noop);
  }
}

/** 遥测数值复位为占位「-」 */
function resetTelemTexts(): void {
  for (let i = 0; i < 3; i++) {
    for (const k of TELEM_KEYS) setText(k + i, "-");
    setText("freq" + i, "-");
  }
}

/* 无遥测（未连接 / 无数据）时：所有电机显式标记离线并复位展示，避免空白误读。
   幂等直写（不用跳过守卫：排队中的 rAF 遥测帧可能覆盖标签，守卫会造成
   “上线过的电机停留在空标签”的陈旧窗口） */
function paintOffline(): void {
  for (let i = 0; i < 3; i++) {
    const ui = shootUi.slots[i];
    ui.offline = true;
    ui.faults = [{ text: "离线", cls: "off" }];
    delete faultCache[i];                 // 恢复遥测时强制重建故障标签
    ui.enRunning = false;
  }
  resetTelemTexts();
  detail.offline();                       // 数值总览清空
}

/* 链路徽章四态：未连串口 / 已连串口但无遥测 / 遥测 link=1 / 遥测 link=0 */
function setLink(up: boolean): void {
  const b = shootUi.link;
  b.text = "链路: " + (up ? "正常" : "断开");
  b.cls = up ? "ok" : "danger";
  b.title = "MCU 遥测的 link 位为 0：主机保活未在窗口内到达";
}
function setLinkOff(): void {
  const b = shootUi.link;
  b.text = deviceCtx.isTcp ? "TCP 串口：未连接" : "串口：未连接";
  b.cls = "neutral";
  b.title = "服务器端串口会话未打开";
}
function setLinkWait(): void {
  const b = shootUi.link;
  b.text = (deviceCtx.isTcp ? "TCP 串口已连接" : "串口已连接") + " · 等待 MCU 遥测…";
  b.cls = "warn";
  b.title = deviceCtx.isTcp
    ? "TCP 已连接，尚未收到有效 MCU 遥测。请检查 Basic2 的 TCP-UART 是否开启、实际波特率是否与 MCU 一致，以及 TXD/RXD/GND 接线和目标固件。"
    : "串口已连接，尚未收到有效 MCU 遥测。请检查 AP/STA 配对、UART 接线及目标固件。";
}

/* RTT 徽章：数值 = 正常，false = Pong 超时，null = 会话未开 / 已断开 */
function setRtt(v: number | false | null): void {
  const b = shootUi.rtt;
  if (v === null) { b.text = "RTT: -"; b.cls = "neutral"; }
  else if (v === false) { b.text = "RTT: 超时"; b.cls = "danger"; }
  else { b.text = "RTT: " + (+v).toFixed(1) + " ms"; b.cls = "ok"; }
}

/* ---------- 周期状态（含 WS 自动挂载 / 重连） ---------- */

async function refresh(): Promise<void> {
  if (!deviceCtx.id) return;
  try {
    const st = await api("status", undefined, true) as unknown as ShootStatus;
    serverOpen = st.open;
    if (st.profiles && !profiles.length) {
      const generation = deviceCtx.generation;
      profiles = st.profiles;
      defaults = st.defaults || {};
      shootUi.modelOpts = profiles.map((pr) => ({ value: String(pr.id), label: pr.name }));
      await nextTick();                   // 型号选项进入 DOM 后再填默认值、建预设列表
      if (generation !== deviceCtx.generation) return;
      fillDefaults();
      for (let i = 0; i < 3; i++) refreshPresetUI(i);
    }
    /* 服务器会话已开 → 页面（含刷新后）自动挂载 WS，界面与服务端一致 */
    if (serverOpen && wsWant && !ws) connectWs();
    if (!serverOpen) { wsWant = false; if (ws) { ws.close(); ws = null; } }
    if (!st.open) setLinkOff();
    else if (!st.telemetry) setLinkWait();   // 串口开但零遥测 = MCU / 无线侧问题
    else setLink(st.telemetry.link === 1);
    /* 会话未开时无视 telemetry（服务端可能残留最后一帧陈旧数据），
       只有 open 且有遥测才交回实时渲染 */
    if (st.telemetry && st.open) {
      for (let i = 0; i < 3; i++) shootUi.slots[i].offline = false;   // 清离线标记，交回遥测渲染
      schedulePaint(st.telemetry);
    } else {
      pendingTelem = null;                   // 掐断排队 rAF 帧的数据源（竞态源头）
      paintOffline();                        // 全部显式离线（幂等）
    }
    /* 连接建立且预设就绪后：各槽位按当前型号自动应用默认预设（每会话一次） */
    if (wsWant && st.open && st.telemetry && profiles.length && motorPresets && !autoAppliedOnConnect) {
      autoAppliedOnConnect = true;
      for (let i = 0; i < 3; i++) maybeAutoPreset(i, st.telemetry.slots[i].model, true).catch(noop);
    }
  } catch { /* 静默 */ }
}

/* RTT 周期测量：仅链路正常（收到遥测且 link=1）时才 ping，
   避免链路断开时每 2s 打一次必然超时的请求（服务器日志刷 400） */
async function rttLoop(): Promise<void> {
  if (serverOpen) {
    const linkUp = !!(shootTelem && shootTelem.link === 1);
    if (linkUp) {
      try {
        const result = await api("ping", {}, true);
        setRtt(Number(result.rtt_ms));
      } catch {
        setRtt(false);   // Pong 超时或请求失败
      }
    } else {
      setRtt(null);      // 链路未通：RTT 无意义，显示灰色占位
    }
  }
  window.setTimeout(() => { void rttLoop(); }, 2000);
}

function connectWs(): void {
  if (!deviceCtx.id) return;
  if (ws) { ws.onclose = null; ws.close(); }
  const generation = deviceCtx.generation;
  const socket = ws = new WebSocket(wsHref(deviceCtx.ws("shoot")));
  socket.onmessage = (e) => {
    if (generation !== deviceCtx.generation || ws !== socket) return;
    const m = JSON.parse(e.data) as Telemetry & { type?: string };
    if (m.type === "telemetry") schedulePaint(m);
  };
  socket.onclose = () => {
    if (ws !== socket) return;
    ws = null;
    if (wsWant && serverOpen) window.setTimeout(() => {
      if (generation === deviceCtx.generation && wsWant) connectWs();
    }, 1000);
  };
}

/* ---------- 操作 ---------- */

async function doOpen(): Promise<void> {
  shootUi.openBusy = true;
  try {
    const r = await api("open", {});
    wsWant = true;
    connectWs();
    const st = (r.status || {}) as { port?: string; baudrate?: number };
    toast(deviceCtx.isTcp ? "TCP 串口已连接，等待 MCU 遥测…"
      : "串口已打开: " + st.port + " @ " + st.baudrate + "（等待 MCU 遥测…）");
    void refresh();
  } catch { /* 接口函数已显示错误 */ }
  finally { shootUi.openBusy = false; }
}

async function doClose(): Promise<void> {
  wsWant = false;
  if (ws) { ws.close(); ws = null; }
  await api("close", {});
  serverOpen = false;
  setLinkOff();
  setRtt(null);
  toast("已断开");
}

async function doEstop(): Promise<void> {
  await api("estop", {});
  toast("急停已下发");
  void refresh();
}

async function onMode(s: number): Promise<void> {
  /* 固件 ModeSet 不自动失能：客户端先显式失能，使“已自动失能”承诺成立（防带电切模式） */
  await api("enable", { slot: s, on: false });
  const r = await api("mode", { slot: s, mode: +val("mode" + s) });
  toast(`Motor${s + 1} 模式: ${r.status}（已自动失能）`, !r.ok);
  await api("apply", { slot: s });
}

async function onModel(s: number): Promise<void> {
  /* 同上：切型号前先失能（增益/限幅换算随型号变化，带电切换有风险） */
  await api("enable", { slot: s, on: false });
  const r = await api("model", { slot: s, model: +val("model" + s) });
  toast(`Motor${s + 1} 型号切换（已自动失能）: ${r.status}`, !r.ok);
  await api("apply", { slot: s });
  const pr = profiles[+val("model" + s)];
  if (pr) setVal("gear" + s, pr.gear_ratio);
}

async function onApplyParam(s: number, key: string, v: number): Promise<void> {
  if (!isFiniteNum(v)) { toast("非法数值", true); return; }
  const r = await api("param", { slot: s, key, value: v });
  toast(`Motor${s + 1} ${key}: ${r.status}`, !r.ok);
  if (r.ok) await api("apply", { slot: s });
}

async function onApply(s: number): Promise<void> {
  let n = 0;
  for (const [k] of PARAMS) {
    const v = +val(k + s);
    if (isFiniteNum(v) && val(k + s) !== "") {
      const r = await api("param", { slot: s, key: k, value: v });
      if (r.ok) n++;
    }
  }
  await api("apply", { slot: s });
  toast(`Motor${s + 1} 已提交 ${n} 项参数`);
}

async function onToggleEnable(s: number): Promise<void> {
  const t = shootTelem && shootTelem.slots[s];
  const running = !!(t && t.enabled == 1);          // 只看真实使能位
  if (!running) {
    const latched = t ? (t.fault & ~1) : 0;         // 排除离线位
    let confirmed = false;                          // 各分支自行确认过则跳过通用确认
    if (latched & (0x04 | 0x08)) {                  // 过温/过流：必须人工确认后手动清
      toast(`Motor${s + 1} 存在过温/过流锁定，请检查后点击[清故障]`, true);
      return;
    }
    if (latched & 0x40) {                           // 仅急停锁定：确认后自动清 + 使能
      if (!confirm(`Motor${s + 1} 处于急停锁定。\n确认清除急停并使能？电机将开始跟踪目标。`)) return;
      await api("fault_clear", { slot: s });
      confirmed = true;
    } else if (t && (t.fault & 1)) {                // 离线槽位：使能无任何输出
      const online = shootTelem!.slots
        .map((x, i) => (x.fault & 1) ? null : i)
        .filter((v): v is number => v !== null);
      const tip = online.length
        ? `当前在线槽位: Motor ${online.map((v) => v + 1).join(" / ")}。` : "当前没有任何在线电机。";
      if (!confirm(`Motor${s + 1} 离线（总线上未检测到电机），使能不会有任何输出。\n` +
                   tip + "\n仍要使能吗？")) return;
      confirmed = true;
    }

    const modeNow = +val("mode" + s);
    if (modeNow === 0) {
      /* Disable 模式下使能位不产生任何输出（固件 Step 将力矩钳 0）。
         引导一键切换 Rpm 模式（目标取滑杆保存的转速值）再使能 */
      const target = tgtState[s].rpm || 0;
      if (!confirm(`Motor${s + 1} 当前模式为 Disable（无输出），单独使能电机不会运转。\n` +
                   `切换到 Rpm 模式（目标 ${target} rpm）并使能？`)) return;
      const rm = await api("mode", { slot: s, mode: 1 });
      if (!rm.ok) { toast(`Motor${s + 1} 模式切换失败: ${rm.status}`, true); return; }
      await api("param", { slot: s, key: "target_rpm", value: target });
      await api("apply", { slot: s });
    } else {
      if (!confirmed && !confirm(`确认使能 Motor${s + 1}？电机将开始跟踪目标。`)) return;
      /* 使能前把滑杆当前目标一并提交并生效：
         避免“拖了滑杆但没提交”导致电机以旧目标（如 0）空转 */
      const key = ({ 1: "target_rpm", 2: "target_deg", 3: "target_torque" } as Record<number, string>)[modeNow];
      const kind = KIND[modeNow];
      if (kind) {
        const lim = tgtLimit(s, kind);
        const v = +val("tval" + s);
        if (isFiniteNum(v)) {
          const clamped = Math.max(-lim, Math.min(lim, v));
          tgtState[s][kind] = clamped;
          await api("param", { slot: s, key, value: clamped });
        }
      }
      await api("apply", { slot: s });
    }
  }
  const r = await api("enable", { slot: s, on: !running });
  toast(`Motor${s + 1} ${running ? "失能" : "使能"}: ${r.status}`, !r.ok);
}

async function onFaultClear(s: number): Promise<void> {
  const r = await api("fault_clear", { slot: s });
  toast(`Motor${s + 1} 清故障: ${r.status}`, !r.ok);
}

/* ---------- 模板入口（对应旧版委托到 #motor-panels 的 click / change / input / dblclick） ---------- */

/** 电机卡按钮（data-act）；param 需给出参数键与数值来源输入框 id */
export function shootAct(s: number, act: string, key?: string, src?: string): void {
  let p: Promise<unknown> | undefined;
  if (act === "param") p = onApplyParam(s, key || "", +val(src || ""));
  else if (act === "reset") p = resetTarget(s);
  else if (act === "apply") p = onApply(s);
  else if (act === "enable") p = onToggleEnable(s);
  else if (act === "faultclear") p = onFaultClear(s);
  else if (act === "psave") p = savePreset(s);
  else if (act === "pdel") p = deletePreset(s);
  else if (act === "pdef") p = toggleDefault(s);
  if (p) p.catch(noop);
}

/** 型号 / 模式 / 预设下拉变化 */
export function shootSelect(s: number, which: "mode" | "model" | "preset", value: string): void {
  if (which === "mode") {
    onMode(s).catch(noop);
    updateTargetUI(s);
  } else if (which === "model") {
    onModel(s).catch(noop);
    updateTargetUI(s);
    updateMechUI(s);                            // 槽数 / 频率行跟随型号
    refreshPresetUI(s);                         // 预设列表跟随型号切换
    maybeAutoPreset(s, val("model" + s), false).catch(noop);   // 新型号默认预设自动应用
  } else if (+value >= 0) {                     // 选中预设 → 填表并下发
    applyPreset(s, +value, false).catch(noop);
  }
}

/** 滑杆拖动：数值框实时同步 + 节流推送目标 */
export function sliderInput(s: number, value: string): void {
  setVal("tval" + s, value);
  pushThrottled(s);
}
/** 滑杆释放：确保终值立即落地 */
export function sliderChange(s: number, value: string): void {
  setVal("tval" + s, value);
  sendTarget(s, true).catch(noop);
}
/** 数值框精确输入 → 提交 */
export function tvalChange(s: number): void {
  sendTarget(s, false).catch(noop);
}
/** 双击滑杆 = 复位 0 */
export function sliderReset(s: number): void {
  resetTarget(s).catch(noop);
}

export function shootOpen(): void { void doOpen(); }
export function shootClose(): void { doClose().catch(noop); }
export function shootEstop(): void { doEstop().catch(noop); }

/* 设备切换时断开旧会话并重置自动应用标记 */
function onDeviceSwitch(_id: string | null, previous: string | null): void {
  if (wsWant && previous) {
    const base = previous === "legacy-default" ? "/api" : "/api/v2/devices/" + encodeURIComponent(previous);
    dfetch(base + "/shoot/close", { method: "POST" }).catch(() => toast("旧设备断开失败，请检查连接", true));
  }
  wsWant = false;
  if (ws) { ws.close(); ws = null; }
  serverOpen = false;
  shootTelem = null;
  for (const k of Object.keys(presetAutoApplied)) delete presetAutoApplied[k];
  autoAppliedOnConnect = false;
  pendingTelem = null;
  for (const st of Object.values(pushState)) { clearTimeout(st.timer); st.timer = 0; st.last = 0; }
  shootUi.modelOpts = [];
  profiles = [];
  motorPresets = null;
  lastTelemAt = 0;
  lastLinkSeen = -1;
  paintOffline();
  setRtt(null);
  detail.clear();                         // 切换设备清空旧波形
  setLinkOff();
  loadMotorPresets().then(() => { for (let i = 0; i < 3; i++) refreshPresetUI(i); }).catch(noop);
  void refresh();
}

/** 启动：注册设备切换处理、读预设、1 秒兜底轮询（WS 未挂载/断开时界面仍与服务器同步）、RTT 循环 */
let shootStarted = false;

export function startShoot(): void {
  if (shootStarted) return;
  shootStarted = true;
  deviceCtx.onChange(onDeviceSwitch);
  resetTelemTexts();
  void loadMotorPresets();
  void refresh();
  window.setInterval(() => { void refresh(); }, 1000);
  void rttLoop();
}
