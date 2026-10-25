/* 写入表单定义（组长新建 / 全员白名单修改；权威校验在服务端）。
 * 字段口径与旧版 portal.js 的 WRITE_FORMS 一一对应，改字段时两边对照 app/portal/api.py。 */
import { fetchProcurement, fetchProjects } from "./api";
import { store, type Option } from "./store";
import { msToDateInput, toLocalInput } from "@/shared/format";
import type { Procurement, Project, RosterUser, Task } from "./types";

export type FieldType =
  | "text" | "dt" | "date" | "number" | "select" | "check"
  | "radio" | "checks" | "people" | "project" | "files";

export interface FieldSpec {
  k: string;
  label: string;
  t: FieldType;
  req?: boolean;
  opts?: string[];
  def?: string;
  optsFrom?: "seasons" | "category1" | "category2";   // 下拉候选来自 /api/portal/procurement 的 options
  link?: string;                                     // 联动：本字段候选随该字段收窄
  leaderOnly?: boolean;                              // 仅组长可见可改
  col?: "image" | "invoice";                         // 附件目标列（files）
  accept?: string;
}

export interface FormSpec {
  title: string;
  table: "tasks" | "projects" | "schedule" | "procurement";
  rows: FieldSpec[];
  uploadAfter?: boolean;   // 新建成功后把所选附件逐份上传到新记录
}

export type FormKind = "task" | "schedule" | "project" | "procurement";

const W_GROUPS = ["机械", "硬件", "电控", "算法", "运营"];

export const FORMS: Record<FormKind, FormSpec> = {
  task: {
    title: "新建事项", table: "tasks",
    rows: [
      { k: "name", label: "事项名称", t: "text", req: true },
      { k: "owner_group", label: "主责组（可多选）", t: "checks", opts: W_GROUPS, req: true },
      { k: "priority", label: "优先级", t: "radio", opts: ["P0", "P1", "P2", "P3"], def: "P2" },
      { k: "owners", label: "负责人（可多选）", t: "people" },
      { k: "project", label: "所属项目", t: "project" },
      { k: "start_time", label: "计划开始", t: "dt" },
      { k: "due_time", label: "计划截止", t: "dt" },
      { k: "status", label: "执行状态", t: "radio", opts: ["未开始", "进行中", "受阻", "待验收"], def: "未开始" },
      { k: "description", label: "具体事项", t: "text" },
    ],
  },
  schedule: {
    title: "新建日程", table: "schedule",
    rows: [
      { k: "name", label: "日程名称", t: "text", req: true },
      { k: "kind", label: "日程类型", t: "radio",
        opts: ["例会", "联调", "测试", "训练", "检查", "比赛", "运营活动", "其他"], def: "例会" },
      { k: "start_time", label: "开始时间", t: "dt", req: true },
      { k: "end_time", label: "结束时间", t: "dt" },
      { k: "groups", label: "涉及组别（可多选）", t: "checks", opts: W_GROUPS },
      { k: "participants", label: "参与人（可多选）", t: "people" },
      { k: "location", label: "地点", t: "text" },
      { k: "status", label: "日程状态", t: "radio", opts: ["待进行", "进行中", "已结束", "已取消"], def: "待进行" },
      { k: "description", label: "安排详情", t: "text" },
    ],
  },
  project: {
    title: "新建项目", table: "projects",
    rows: [
      { k: "name", label: "项目名称", t: "text", req: true },
      { k: "owner_group", label: "主责组", t: "radio", opts: W_GROUPS },
      { k: "collab_groups", label: "协作组（可多选）", t: "checks", opts: W_GROUPS },
      { k: "owners", label: "负责人（可多选）", t: "people" },
      { k: "plan_start", label: "计划开始", t: "dt" },
      { k: "plan_end", label: "计划结束", t: "dt" },
      { k: "status", label: "项目状态", t: "radio", opts: ["筹备中", "进行中", "已结束", "已归档"], def: "筹备中" },
      { k: "description", label: "项目说明", t: "text" },
    ],
  },
  procurement: {
    title: "新建采购申请", table: "procurement", uploadAfter: true,
    rows: [
      { k: "name", label: "物资名称", t: "text", req: true },
      { k: "season", label: "赛季", t: "select", optsFrom: "seasons" },
      { k: "category1", label: "一级分类", t: "select", optsFrom: "category1" },
      { k: "category2", label: "二级分类", t: "select", optsFrom: "category2", link: "category1" },
      { k: "code", label: "采购编号（留空按 赛季+分类 自动生成）", t: "text" },
      { k: "purchase_date", label: "采购日期", t: "date" },
      { k: "unit_price", label: "单价（元）", t: "number" },
      { k: "quantity", label: "数量", t: "number" },
      { k: "purchaser", label: "采购人（可多选）", t: "people" },
      { k: "groups", label: "申请组别（可多选）", t: "checks", opts: W_GROUPS },
      { k: "link", label: "购物链接", t: "text" },
      { k: "purpose", label: "用途说明", t: "text" },
      { k: "invoice_done", label: "发票已开", t: "check" },
      { k: "invoice_files", label: "发票文件（PDF/图片 · ≤20MB/份）", t: "files", col: "invoice", accept: "image/*,.pdf" },
      { k: "attach_files", label: "图片补充（图片 · ≤10MB/张）", t: "files", col: "image", accept: "image/*" },
      { k: "leader_approval", label: "组长批复", t: "check", leaderOnly: true },
    ],
  },
};

/* ---------- 物资分类：二级 → 一级（只用于把下拉候选按一级分类收窄） ----------
   飞书里新加的二级分类不在此表 → 归属未知，任何一级分类下都列出，
   不会因为维护者新增选项而在门户里选不到。 */
export const PROC_CATEGORY2_PARENT: Record<string, string> = {
  "直流电机": "电机", "无刷电机": "电机", "舵机": "电机",
  "电机驱动": "电机", "编码器": "电机", "减速箱": "电机",
  "螺丝": "标准件", "螺母": "标准件", "垫片垫圈": "标准件", "轴承": "标准件",
  "销与键": "标准件", "弹簧": "标准件", "卡簧": "标准件", "铆钉": "标准件",
  "板材": "加工件", "型材": "加工件", "管材": "加工件", "轴类": "加工件",
  "法兰": "加工件", "支架": "加工件", "3D打印件": "加工件", "定制机加": "加工件",
  "主控板": "电子元器件", "电源模块": "电子元器件", "传感器": "电子元器件",
  "电阻电容": "电子元器件", "连接器": "电子元器件", "芯片": "电子元器件",
  "显示屏": "电子元器件",
  "电源线": "线材接插件", "信号线": "线材接插件", "端子": "线材接插件",
  "插头插座": "线材接插件", "线束": "线材接插件",
  "螺丝刀": "工具耗材", "扳手": "工具耗材", "钻头": "工具耗材",
  "胶粘剂": "工具耗材", "扎带": "工具耗材", "砂纸": "工具耗材", "焊锡": "工具耗材",
  "其他": "其他",
};

/** 下拉候选：接口下发的 options（常量清单 ∪ 快照实际值）；给了 parent 就按一级分类收窄 */
export function procOptionList(name: string, parent?: string | null): string[] {
  const o = store.procOptions || {};
  let list = (o[name] || []).slice();
  if (name === "category2" && parent && parent !== "全部") {
    list = list.filter((v) => {
      const p = PROC_CATEGORY2_PARENT[v];
      return !p || p === parent;
    });
  }
  return list;
}

/** 新建采购申请的预填：赛季默认取当前筛选的赛季；没筛选就取当前赛季（每年 9 月起算下一赛季，
 *  如 2026-10 → 2027赛季）；候选里没有就不预填。采购编号要 赛季 + 一级分类 都有才能自动生成。 */
export function procNewPreset(filterSeason?: string, now = new Date()): Record<string, unknown> {
  const seasons = procOptionList("seasons");
  if (filterSeason && filterSeason !== "全部" && seasons.indexOf(filterSeason) >= 0) return { season: filterSeason };
  const cur = (now.getFullYear() + (now.getMonth() >= 8 ? 1 : 0)) + "赛季";
  return seasons.indexOf(cur) >= 0 ? { season: cur } : {};
}

/** 采购下拉候选还没加载（直接打开详情页时）→ 拉一次列表接口取 options */
export async function ensureProcOptions(): Promise<void> {
  if (store.procOptions) return;
  try {
    const r = await fetchProcurement({});
    if (r.options) store.procOptions = r.options;
  } catch { /* 候选缺失时下拉只显示当前值 */ }
}

/** 项目候选（事项表单的「所属项目」） */
export async function ensureProjectOptions(): Promise<Option[]> {
  if (store.projectOptions) return store.projectOptions;
  try {
    const r = await fetchProjects({ group: "全部", archived: false });
    store.projectOptions = (r.data || []).map((p: Project) => ({ id: p.id, label: p.name }));
  } catch {
    store.projectOptions = [];
  }
  return store.projectOptions;
}

/* ---------- 人员（花名册） ---------- */
export interface Person { id: string; name: string; groups: string[]; leader: boolean }

/** 花名册人员：同一人可能属多个组别（后端已按 open_id 聚合），这里再兜一层旧形状 */
export function rosterPeople(): Person[] {
  const byId: Record<string, Person> = {};
  ((store.meta && store.meta.roster_users) || []).forEach((u: RosterUser) => {
    if (!u.id) return;
    const rec = byId[u.id] || (byId[u.id] = { id: u.id, name: u.name || "", groups: [], leader: false });
    const gs = u.groups || (u.group ? [u.group] : []);
    gs.forEach((g) => { if (g && rec.groups.indexOf(g) < 0) rec.groups.push(g); });
    if (u.leader) rec.leader = true;
    if (!rec.name && u.name) rec.name = u.name;
  });
  return Object.values(byId).sort((a, b) => (a.name || "").localeCompare(b.name || "", "zh"));
}

/** 勾选项标签：姓名（组别1/组别2·组长） */
export function personLabel(u: Person): string {
  let tail = (u.groups || []).join("/");
  if (u.leader) tail = tail ? tail + "·组长" : "组长";
  return u.name + (tail ? "（" + tail + "）" : "");
}

/* ---------- 编辑预置值（详情 → 表单） ---------- */
export function taskPreset(t: Task): Record<string, unknown> {
  return {
    name: t.name,
    owner_group: t.owner_group ? [t.owner_group] : [],
    priority: t.priority,
    owners: (t.owners || []).map((u) => u.id),
    project: t.project_id || "",
    start_time: toLocalInput(t.start && t.start.ts),
    due_time: toLocalInput(t.due && t.due.ts),
    status: ["未开始", "进行中", "受阻", "待验收"].indexOf(t.status || "") >= 0 ? t.status : "",
    description: t.description || "",
  };
}

export function procPreset(p: Procurement): Record<string, unknown> {
  return {
    name: p.name === "(未命名)" ? "" : p.name,
    season: p.season || "",
    category1: p.category1 || "",
    category2: p.category2 || "",
    code: p.code || "",
    purchase_date: msToDateInput(p.purchase_date && p.purchase_date.raw_ms),
    unit_price: p.unit_price == null ? "" : p.unit_price,
    quantity: p.quantity == null ? "" : p.quantity,
    purchaser: (p.purchaser || []).map((u) => u.id),
    groups: p.groups || [],
    link: p.link || "",
    purpose: p.purpose || "",
    invoice_done: !!p.invoice_done,
    leader_approval: !!p.leader_approval,
  };
}

/** 表单值 → 提交载荷（口径同旧版 collectForm：空串不提交、多选为空不提交、复选总是提交） */
export function collectPayload(values: Record<string, unknown>, rows: FieldSpec[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const r of rows) {
    if (r.t === "files") continue;
    const v = values[r.k];
    if (r.t === "check") { out[r.k] = !!v; continue; }
    if (r.t === "checks" || r.t === "people") {
      if (Array.isArray(v) && v.length) out[r.k] = v.slice();
      continue;
    }
    if (v === "" || v == null) continue;
    out[r.k] = typeof v === "number" ? String(v) : v;
  }
  return out;
}

/** 必填校验：返回缺失字段的标签，全部齐全返回 null */
export function missingRequired(rows: FieldSpec[], payload: Record<string, unknown>): string | null {
  for (const r of rows) {
    if (!r.req) continue;
    const v = payload[r.k];
    if (v == null || v === "" || (Array.isArray(v) && !v.length)) return r.label;
  }
  return null;
}
