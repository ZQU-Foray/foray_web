/* 门户软导航：把门户内页间跳转改为 fetch + 主区替换（不发生真实导航）。
 * 动机：多页应用每次真实导航，标签页都会短暂进入加载态（favicon 位置
 * 闪一下加载指示）——这是浏览器原生行为、无法抑制；软导航后与调试台
 * （单页应用）一致：无加载态、无闪烁，且不依赖任何浏览器新特性。
 * 范围：仅拦截门户路由（五个页面及其详情）；调试台、登录、站外、
 * 新标签打开（修饰键/中键/target=_blank）一律保留普通导航。
 * 兜底：装配过程中任何异常 → location.href 硬跳转，永远有退路。 */
(function () {
  "use strict";

  function isPortalPath(p) {
    if (p === "/" || p === "/quality") return true;
    if (p === "/projects" || p === "/tasks" || p === "/schedule") return true;
    if (p === "/procurement") return true;
    return /^\/(projects|tasks|schedule|procurement)\/[^\/]+\/?$/.test(p);
  }

  var seq = 0;   /* 请求序号：快速连续切换时只认最后一次 */

  function markCurrent(path) {
    document.querySelectorAll(".side-nav-item").forEach(function (el) {
      var base = (el.getAttribute("href") || "").split("?")[0];
      var on = (base === "/" && path === "/") ||
               (base !== "/" && (path === base || path.indexOf(base + "/") === 0));
      if (on) el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
    });
  }

  function go(dest, push) {
    var my = ++seq;
    fetch(dest, { credentials: "same-origin" })
      .then(function (r) {
        /* 会话失效被重定向到登录页：退回硬导航走完整登录流程 */
        if (r.redirected) {
          try {
            if (new URL(r.url).pathname === "/login") { location.href = dest; return null; }
          } catch (e) { /* fallthrough */ }
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        if (!html || my !== seq) return;
        var doc = new DOMParser().parseFromString(html, "text/html");
        var next = doc.querySelector("main#p-main");
        var cur = document.querySelector("main#p-main");
        if (!next || !cur) { location.href = dest; return; }
        /* 整节点替换：#p-main 是新元素，入场动画自然重放；侧栏/黑条不动 */
        cur.replaceWith(next);
        document.title = doc.title;
        var dp = doc.body.getAttribute("data-page") || "";
        document.body.setAttribute("data-page", dp);
        var path = new URL(dest, location.href).pathname;
        if (push) history.pushState(null, "", dest);
        markCurrent(path);
        window.scrollTo(0, 0);
        if (window.__portal && window.__portal.bootPage) window.__portal.bootPage();
      })
      .catch(function () { location.href = dest; });   /* 兜底硬跳转 */
  }

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest("a") : null;
    if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
    var u;
    try { u = new URL(a.href, location.href); } catch (err) { return; }
    if (u.origin !== location.origin || !isPortalPath(u.pathname)) return;
    e.preventDefault();
    go(u.pathname + u.search, true);
  });

  window.addEventListener("popstate", function () {
    if (isPortalPath(location.pathname)) go(location.pathname + location.search, false);
  });
})();
