/* 侧栏导航表与页面编号：页面编号（页头「— 02」）统一从这里取，增删页面只改一处。
   新增页面路径还要登记 routes.ts 的路由、app/auth/guard.py 的 PORTAL_PAGES 与 app/main.py 的页面路由。 */

export interface NavItem { href: string; no: string; name: string; en: string }

export const NAV: Array<{ label: string; items: NavItem[] }> = [
  {
    label: "MANAGEMENT",
    items: [
      { href: "/", no: "01", name: "总览", en: "HOME" },
      { href: "/attendance", no: "02", name: "考勤", en: "ATTENDANCE" },
      { href: "/projects", no: "03", name: "项目", en: "PROJECT" },
      { href: "/tasks", no: "04", name: "事项", en: "TASKS" },
      { href: "/procurement", no: "05", name: "物资采购", en: "PROCUREMENT" },
      { href: "/schedule", no: "06", name: "日程", en: "SCHEDULE" },
    ],
  },
  {
    label: "DIAGNOSTICS",
    items: [{ href: "/quality", no: "07", name: "数据质量", en: "QUALITY" }],
  },
];

/** 页面编号（页头「— 02」）：统一从侧栏导航表取，增删页面只改上面一处 */
export function navNo(href: string): string {
  for (const sec of NAV) {
    const it = sec.items.find((x) => x.href === href);
    if (it) return it.no;
  }
  return "";
}
