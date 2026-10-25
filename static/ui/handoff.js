/* 应用间切换过场（门户 ↔ 调试台）+ 登录后首启标记。
 * 放 <head> 同步加载（与 sidebar.js 同一首帧防闪模式：<body> 未解析时
 * 先把状态写到 <html>，首帧即按目标状态布局/绘制）。
 * - 入场：上一页写 foray.handoff=1 → 首帧整屏覆盖，再滑出揭幕；
 * - 出场：拦截 .side-portal-link（两个应用的跨应用入口同用此类）→
 *   覆盖层自左滑入 → 延迟跳转；旧页会保留到新页首绘，覆盖层正好遮住
 *   切换空窗（无白屏、品牌区不闪）；
 * - 登录首启：URL 带 boot=1（飞书 OAuth 回调由服务端追加）或本站账号
 *   登录成功时写入 foray.post-login-boot，供共享开机引导动画判断
 *   "登录成功后的首次进入"——日常门户↔调试台切换不再重放长动画。 */
(function () {
  "use strict";
  var reduced = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* boot=1 → 转存为会话标记并清理地址栏（head 阶段即可 replaceState）；
   * 同时写揭幕标记：飞书回调由服务端 302 直落目标页（无出场页可演），
   * 落地页以整屏揭幕开场，与本站账号登录路径观感一致 */
  if (/[?&]boot=1\b/.test(location.search)) {
    try {
      sessionStorage.setItem("foray.post-login-boot", "1");
      sessionStorage.setItem("foray.handoff", "1");
      history.replaceState(null, "",
        location.pathname +
        location.search.replace(/([?&])boot=1&?/, "$1").replace(/[?&]$/, "") +
        location.hash);
    } catch (e) { /* 隐私模式：忽略，本次不播开机引导 */ }
  }

  /* 入场揭幕：首帧整屏覆盖（CSS 键在 html[data-handoff="in"]） */
  try {
    if (sessionStorage.getItem("foray.handoff") === "1") {
      sessionStorage.removeItem("foray.handoff");
      if (!reduced) document.documentElement.setAttribute("data-handoff", "in");
    }
  } catch (e) { /* 忽略 */ }

  if (reduced) return;        /* 减少动效：跳过出场动画，保留普通导航 */

  /* 出场：两个方向（门户→调试台 / 调试台→门户）统一处理 */
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest("a.side-portal-link, a.sys-switch") : null;
    if (!a) return;
    e.preventDefault();
    try { sessionStorage.setItem("foray.handoff", "1"); } catch (err) { /* 忽略 */ }
    document.documentElement.setAttribute("data-handoff", "out");
    /* 等覆盖层完成滑入（.24s）再跳转：跳转后旧页保持覆盖态直至新页首绘 */
    setTimeout(function () { location.href = a.href; }, 240);
  });
})();
