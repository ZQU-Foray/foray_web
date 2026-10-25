/* 门户路由表与侧栏导航。新增页面路径必须同时登记 app/auth/guard.py 的
   PORTAL_PAGES 与 app/main.py 的页面路由，否则直接打开会 404 或落到默认公开。 */
import type { RouteDef } from "@/shared/router";
import Overview from "./pages/Overview.vue";
import Attendance from "./pages/Attendance.vue";
import Projects from "./pages/Projects.vue";
import ProjectDetail from "./pages/ProjectDetail.vue";
import Tasks from "./pages/Tasks.vue";
import TaskDetail from "./pages/TaskDetail.vue";
import Schedule from "./pages/Schedule.vue";
import ScheduleDetail from "./pages/ScheduleDetail.vue";
import Procurement from "./pages/Procurement.vue";
import ProcurementDetail from "./pages/ProcurementDetail.vue";
import Quality from "./pages/Quality.vue";

export const ROUTES: RouteDef[] = [
  { path: "/", name: "overview", component: Overview, meta: { title: "总览", crumb: "总览" } },
  { path: "/attendance", name: "attendance", component: Attendance, meta: { title: "考勤", crumb: "考勤" } },
  { path: "/procurement", name: "procurement", component: Procurement, meta: { title: "物资采购", crumb: "物资采购" } },
  { path: "/procurement/:id", name: "procurement-detail", component: ProcurementDetail, meta: { title: "采购详情", crumb: "物资采购 · 详情" } },
  { path: "/projects", name: "projects", component: Projects, meta: { title: "项目", crumb: "项目" } },
  { path: "/projects/:id", name: "project-detail", component: ProjectDetail, meta: { title: "项目详情", crumb: "项目 · 详情" } },
  { path: "/tasks", name: "tasks", component: Tasks, meta: { title: "事项", crumb: "事项" } },
  { path: "/tasks/:id", name: "task-detail", component: TaskDetail, meta: { title: "事项详情", crumb: "事项 · 详情" } },
  { path: "/schedule", name: "schedule", component: Schedule, meta: { title: "日程", crumb: "日程" } },
  { path: "/schedule/:id", name: "schedule-detail", component: ScheduleDetail, meta: { title: "日程详情", crumb: "日程 · 详情" } },
  { path: "/quality", name: "quality", component: Quality, meta: { title: "数据质量", crumb: "数据质量" } },
];

/* 侧栏导航与页面编号在 nav.ts（页面组件也要取编号，单独成模块以免与路由表互相引用） */
export { NAV, navNo, type NavItem } from "./nav";
