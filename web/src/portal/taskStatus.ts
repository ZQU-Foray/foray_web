/* 事项状态的显示口径：组长在详情页点「标记已完成」的事项（数据里写成 待验收 + 验收结论通过，
 * normalize.tasks 归一为「已完成」）即视为归档——事项页与项目详情默认把它们折叠到「已归档」里，
 * 用户展开才显示。 */
import type { Task } from "./types";

export const ACCEPTED = "已完成";

export function statusText(s?: string | null): string {
  return s || "—";
}

export function isAccepted(t: Pick<Task, "status">): boolean {
  return t.status === ACCEPTED;
}
