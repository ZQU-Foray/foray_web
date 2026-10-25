/* 门户接口集中处：所有读写都从这里发出。
 * 后端写入有两种模式（直写飞书 / 先存本地再推送），写入响应的 queued 区分两者；
 * 不论哪种，响应返回时页面数据已经包含这次改动，store.afterWrite 立即刷新即可。 */
import { getJSON, sendJSON, upload } from "@/shared/http";
import type {
  AttendancePage, AttendanceStatus, DetailBody, ListBody, Me, Meta, Overview, Procurement,
  ProcurementList, Project, QualityBody, ScheduleItem, SyncQueue, SyncStatus, Task, WriteResult,
} from "./types";

const enc = encodeURIComponent;

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const out: string[] = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === "全部" || v === false) continue;
    out.push(k + "=" + enc(String(v)));
  }
  return out.join("&");
}

/* ---------- 身份 / 元数据 / 同步 ---------- */
export const fetchMe = () => getJSON<Me>("/api/auth/me");
export const logout = () => sendJSON("POST", "/api/auth/logout");
export const fetchMeta = () => getJSON<Meta>("/api/portal/meta");
export const fetchSyncStatus = () => getJSON<SyncStatus>("/api/portal/sync-status");
export const requestSync = () =>
  sendJSON<{ accepted?: boolean; detail?: string }>("POST", "/api/portal/sync");

/* ---------- 读：列表统一取后端上限 200 条（超出由 has_more 提示，不静默截断） ---------- */
export const fetchOverview = () => getJSON<DetailBody<Overview>>("/api/portal/overview");
export const fetchAttendancePage = () => getJSON<DetailBody<AttendancePage>>("/api/portal/attendance");

export function fetchProjects(f: { group: string; archived: boolean }) {
  return getJSON<ListBody<Project>>("/api/portal/projects?" +
    qs({ page_size: 200, group: f.group, include_archived: f.archived ? "true" : null }));
}
export const fetchProjectsAll = () =>
  getJSON<ListBody<Project>>("/api/portal/projects?page_size=200&include_archived=true");
export const fetchProject = (id: string) =>
  getJSON<DetailBody<Project>>("/api/portal/projects/" + enc(id));

export function fetchTasks(f: { group: string; priority: string; status: string; owner: string; project: string }) {
  return getJSON<ListBody<Task>>("/api/portal/tasks?" + qs({
    group: f.group, priority: f.priority, status: f.status, owner: f.owner,
    project_id: f.project, page_size: 200,
  }));
}
export const fetchTasksAll = () => getJSON<ListBody<Task>>("/api/portal/tasks?page_size=200");
export const fetchTask = (id: string) => getJSON<DetailBody<Task>>("/api/portal/tasks/" + enc(id));

/** 日程：列表视图 7 天、日历视图 28 天 */
export const fetchSchedule = (days: number) =>
  getJSON<ListBody<ScheduleItem>>("/api/portal/schedule?days=" + days + "&page_size=200");
export const fetchScheduleItem = (id: string) =>
  getJSON<DetailBody<ScheduleItem>>("/api/portal/schedule/" + enc(id));

export function fetchProcurement(f: Record<string, string>) {
  return getJSON<ProcurementList>("/api/portal/procurement?" + qs({
    group: f.group, season: f.season, category1: f.category1, category2: f.category2,
    invoice: f.invoice, q: f.q, page_size: 200,
  }));
}
export const fetchProcurementItem = (id: string) =>
  getJSON<DetailBody<Procurement>>("/api/portal/procurement/" + enc(id));

export const fetchQuality = () => getJSON<QualityBody>("/api/portal/quality?page_size=200");

/* ---------- 推送队列与冲突：全员可看，组长可处理 ---------- */
export const fetchSyncQueue = () => getJSON<DetailBody<SyncQueue>>("/api/portal/sync-queue");
export const retryQueueOp = (id: number) =>
  sendJSON<{ count?: number; detail?: string }>("POST", "/api/portal/sync-queue/" + id + "/retry");
export const discardQueueOp = (id: number) =>
  sendJSON<{ count?: number; detail?: string }>("POST", "/api/portal/sync-queue/" + id + "/discard");
export const ackConflict = (id: number) =>
  sendJSON<{ detail?: string }>("POST", "/api/portal/conflicts/" + id + "/ack");

/* ---------- 考勤 ---------- */
export const fetchAttendance = () => getJSON<AttendanceStatus>("/api/portal/attendance/status");
/** 组长给成员设 / 取消本周请假（record_id 是成员数据表记录） */
export const setLeave = (recordId: string, on: boolean) =>
  sendJSON<WriteResult>("POST", "/api/portal/attendance/leave", { record_id: recordId, on });
export const attendanceAction = (act: "check-in" | "check-out") =>
  sendJSON<{ session_hours?: number; work_hours?: number; queued?: boolean; detail?: string }>(
    "POST", "/api/portal/attendance/" + act);

/* ---------- 写：权威校验在服务端 ---------- */
export type WriteTable = "tasks" | "projects" | "schedule" | "procurement";

export const createRecord = (table: WriteTable, payload: Record<string, unknown>) =>
  sendJSON<WriteResult>("POST", "/api/portal/write/" + table, payload);

export const patchRecord = (table: WriteTable, id: string, payload: Record<string, unknown>) =>
  sendJSON<WriteResult>("PATCH", "/api/portal/write/" + table + "/" + enc(id), payload);

/** 组长：把事项设为已完成并归档，完成时间由服务端记为此刻 */
export const completeTask = (id: string) =>
  sendJSON<WriteResult>("POST", "/api/portal/write/tasks/" + enc(id) + "/complete");

export const deleteRecord = (table: WriteTable, id: string) =>
  sendJSON<WriteResult>("DELETE", "/api/portal/write/" + table + "/" + enc(id));

/** 采购附件：column=image（图片补充）/ invoice（发票文件）；服务端并入该列既有附件 */
export const uploadProcAttachment = (id: string, column: "image" | "invoice", file: File) =>
  upload<WriteResult>(
    "/api/portal/write/procurement/" + enc(id) + "/attachments?column=" + enc(column), file);
