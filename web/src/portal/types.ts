/* 门户接口数据形状（与 app/portal/api.py、compute.py、normalize.py 对应）。
 * 只声明前端用到的字段；时间统一为 { ts: 秒 }。 */

export interface TimePoint {
  ts: number;
  raw_ms?: number;
  precision?: "date" | "datetime" | null;
}

export interface UserRef {
  id: string;
  name?: string;
}

/** 每个数据接口都带的同步信封 */
export interface Envelope {
  snapshot_version: string | number | null;
  last_success_at: number | null;
  server_time: number;
  stale: boolean;
  last_error: string | null;
  has_snapshot?: boolean;
}

/** 本地缓存库概况（sync-status.cache）：写入模式、推送队列积压与未确认冲突 */
export interface CacheStatus {
  write_mode: "direct" | "cache" | string;
  cache_tables: string[];
  pusher_alive: boolean;
  pending?: number;
  pushing?: number;
  failed?: number;
  open_conflicts?: number;
  oldest_open_at?: number | null;
  backlog_error?: boolean;
}

export interface SyncStatus extends Envelope {
  has_snapshot: boolean;
  last_attempt_at?: number | null;
  syncing?: boolean;
  counts?: Record<string, number>;
  cache?: CacheStatus;
}

/** 记录上的同步标记（快照生成时算好）：待同步 / 同步失败 / 冲突 */
export interface SyncMark {
  state: "pending" | "failed" | "conflict";
  label: string;
  text: string;
}

/** 写入接口的成功响应：queued=true 表示已存本地、正在推送到飞书 */
export interface WriteResult {
  ok?: boolean;
  record_id?: string;
  queued?: boolean;
  hint?: string;
  detail?: string;
}

/* ---------- 推送队列与冲突（/api/portal/sync-queue） ---------- */
export interface QueueOp {
  id: number;
  table: string;
  record_id: string;
  op: "create" | "update" | "delete";
  op_text: string;
  state: string;
  name: string;
  columns: string[];
  error: string;
  attempts: number;
  created_by: string;
  created_at: number | null;
}

export interface QueueConflict {
  id: number;
  table: string;
  record_id: string;
  name: string;
  kind: string;
  kind_text: string;
  field: string;
  local: string;
  remote: string;
  detected_at: number | null;
}

export interface SyncQueue {
  cache: CacheStatus;
  can_manage: boolean;
  failed: QueueOp[];
  pending: QueueOp[];
  conflicts: QueueConflict[];
}

/** 列表接口：信封 + 分页 */
export interface ListBody<T> extends Envelope {
  data: T[] | null;
  total?: number;
  has_more?: boolean;
}

export interface DetailBody<T> extends Envelope {
  data: T | null;
}

export interface Me {
  user: string;
  display: string;
  role: "member" | "emergency" | string;
  is_leader: boolean;
  leader_groups: string[];
  auth_method?: string;
}

export interface RosterUser {
  id: string;
  name: string;
  groups?: string[];
  group?: string;
  leader?: boolean;
}

export interface Meta extends Envelope {
  groups: string[];
  source_links: Record<string, string>;
  roster_users: RosterUser[];
}

export interface Milestone {
  id: string;
  name: string;
  kind?: string | null;
  target: TimePoint;
  url?: string | null;
}

export interface Task {
  id: string;
  name: string;
  priority?: string | null;
  status?: string | null;
  owner_group?: string | null;
  collab_groups?: string[];
  owners?: UserRef[];
  project_id?: string | null;
  project_name?: string | null;
  start?: TimePoint | null;
  due?: TimePoint | null;
  due_precision?: "date" | "datetime" | null;
  done_time?: TimePoint | null;
  submit_time?: TimePoint | null;
  overdue?: boolean;
  due_soon?: boolean;
  description?: string | null;
  blocker_note?: string | null;
  invalid?: boolean;
  url?: string | null;
  sync?: SyncMark | null;
}

export interface UrgentGroup {
  group: string;
  overdue: number;
  items: Task[];
}

export interface ScheduleItem {
  id: string;
  name: string;
  kind?: string | null;
  start: TimePoint;
  end?: TimePoint | null;
  all_day?: boolean;
  ongoing?: boolean;
  status?: string | null;
  location?: string | null;
  groups?: string[];
  participants?: UserRef[];
  description?: string | null;
  invalid?: boolean;
  url?: string | null;
  sync?: SyncMark | null;
}

export interface MemberRank {
  member?: UserRef | null;
  work_hours?: number | null;
  last_check_in?: TimePoint | null;
  url?: string | null;
}

/** 总览「需要关注」的分档：逾期 / 72 小时内到期 / 受阻 / P0 */
export type AttentionLevel = "overdue" | "due_soon" | "blocked" | "p0";

export interface AttentionTask extends Task {
  level: AttentionLevel;
}

export interface Overview {
  milestones: Milestone[];
  /** 旧版五组格子用（旧前端回退时仍要），新版总览用 attention */
  groups: UrgentGroup[];
  attention: AttentionTask[];
  kpi: { overdue: number; due_soon: number; attention: number; today_schedule: number; checked_in?: number };
  schedule: ScheduleItem[];
  members: MemberRank[];
  quality_issues: number;
}

export interface Progress {
  done: number;
  total: number;
  percent: number | null;
}

export interface Project {
  id: string;
  name: string;
  description?: string | null;
  owner_group?: string | null;
  collab_groups?: string[];
  owners?: UserRef[];
  status?: string | null;
  plan_start?: TimePoint | null;
  plan_end?: TimePoint | null;
  progress: Progress;
  invalid?: boolean;
  url?: string | null;
  sync?: SyncMark | null;
  tasks?: Task[];
  milestones?: Milestone[];
}

export interface Attachment {
  name?: string;
  url?: string | null;
}

export interface Procurement {
  id: string;
  name: string;
  season?: string | null;
  category1?: string | null;
  category2?: string | null;
  code?: string | null;
  groups?: string[];
  quantity?: number | null;
  unit_price?: number | null;
  total_price?: number | null;
  purchase_date?: TimePoint | null;
  purchaser?: UserRef[];
  link?: string | null;
  purpose?: string | null;
  attachments?: Attachment[];
  invoice_done?: boolean;
  invoice_files?: Attachment[];
  leader_approval?: boolean;
  purchased?: boolean;
  invalid?: boolean;
  url?: string | null;
  sync?: SyncMark | null;
}

export interface ProcOptions {
  seasons?: string[];
  category1?: string[];
  category2?: string[];
  [k: string]: string[] | undefined;
}

export interface ProcurementList extends ListBody<Procurement> {
  stats?: { count?: number; amount?: number | null };
  options?: ProcOptions;
}

export interface QualityIssue {
  table?: string;
  record_id?: string;
  issue?: string;
}

export interface QualityBody extends ListBody<QualityIssue> {
  by_table?: Record<string, number>;
}

export interface AttendanceStatus {
  checked_in: boolean;
  since?: number | null;
  work_hours?: number | null;
}

/* ---------- 考勤页（/api/portal/attendance，只读） ---------- */
export interface AttendanceRow {
  id: string;
  member: UserRef;
  work_hours?: number | null;
  /** 本周时长（飞书公式）；未接入或待结算时为 null */
  week_hours?: number | null;
  week_ok?: boolean | null;
  last_week_hours?: number | null;
  last_week_ok?: boolean | null;
  /** 结算周还不是本周（换周尚未同步到）：本周数值未知 */
  pending: boolean;
  checked_in: boolean;
  checked_in_since?: number | null;
  on_duty: boolean;
  /** 本周标了请假（组长设置）；leave = 请假且没达标，状态显示「请假」 */
  on_leave?: boolean;
  leave?: boolean;
  /** 上周请假且没达标：「上周」一栏显示「请假」 */
  last_week_leave?: boolean;
  url?: string | null;
}

export interface DutyPerson { id: string | null; name: string; in_members: boolean }

export interface AttendancePage {
  threshold: number;
  week: { start: number; end: number; key: string };
  today: { date: string; weekday: number; label: string };
  configured: { week: boolean; duty: boolean; leave?: boolean };
  duty: { source: "date" | "weekday" | null; people: DutyPerson[] };
  summary: { members: number; met: number; pending: number; checked_in: number; leave?: number; required?: number };
  members: AttendanceRow[];
  rank: MemberRank[];
}
