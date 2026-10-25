/* 应用间切换过场（门户 ↔ 调试台、登录成功后跳转）。
 * 出场：覆盖层自左滑入 240ms 后再跳转；旧页保持覆盖态直到新页首绘，遮住切换空窗。
 * 入场：新页由 head-early.js 读取 foray.handoff 标记，首帧整屏覆盖再滑出揭幕。 */

function reducedMotion(): boolean {
  return (typeof window.matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches);
}

/** 带过场跳转到另一个应用（整页导航） */
export function handoffTo(href: string): void {
  if (reducedMotion() || !document.querySelector(".app-handoff-cover")) {
    location.href = href;
    return;
  }
  try { sessionStorage.setItem("foray.handoff", "1"); } catch { /* 隐私模式：仍正常跳转 */ }
  document.documentElement.setAttribute("data-handoff", "out");
  window.setTimeout(() => { location.href = href; }, 240);
}

/** 点击处理：普通左键点击跨应用链接时走过场，修饰键 / 新标签保持原生行为 */
export function onHandoffClick(e: MouseEvent, href: string): void {
  if (e.defaultPrevented || e.button !== 0) return;
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  handoffTo(href);
}
