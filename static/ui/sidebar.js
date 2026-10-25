/* 侧边栏收纳开关（门户与调试台共用）。默认展开；状态记在 localStorage。
 * 用法：页面只需有 .app-shell > .app-sidebar（内含 .side-nav），本脚本自动插入按钮。
 * 可用 data-nav-key 指定存储键（默认 foray.nav.collapsed）。 */
(function () {
  "use strict";
  var KEY = document.documentElement.getAttribute("data-nav-key") || "foray.nav.collapsed";
  var mobile = window.matchMedia("(max-width: 900px)");

  /* 首帧防闪：本脚本放在 <head> 同步加载（无 defer），此时 <body> 尚未解析，
     先把收纳态写到 <html data-nav>（收纳 CSS 的键），首帧即按收纳态布局——
     否则刷新时会先绘制一帧展开态再收起（可见闪烁 + 一次多余动画）。 */
  try {
    if (!mobile.matches && window.localStorage.getItem(KEY) === "1") {
      document.documentElement.setAttribute("data-nav", "collapsed");
    }
  } catch (e) { /* 隐私模式 */ }

  function init() {
    var shell = document.querySelector(".app-shell");
    if (!shell) return;
    var sidebar = shell.querySelector(".app-sidebar");
    if (!sidebar) return;
    if (!sidebar.id) sidebar.id = "app-sidebar";

    var btn = shell.querySelector(".side-toggle");   // 页面预渲染的按钮优先（避免切换时闪现）
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button";
      btn.className = "side-toggle";
      btn.setAttribute("aria-controls", sidebar.id);
      btn.setAttribute("aria-label", "收起 / 展开侧边栏");
      btn.title = "收起 / 展开侧边栏";
      shell.appendChild(btn);
    }

    var items = sidebar.querySelectorAll(".side-nav-item");
    btn.setAttribute("aria-controls", sidebar.id);
    var main = shell.querySelector(".app-main");
    var backdrop = document.createElement("button");
    backdrop.type = "button";
    backdrop.className = "side-backdrop";
    backdrop.setAttribute("aria-label", "关闭导航菜单");
    backdrop.tabIndex = -1;
    backdrop.hidden = true;
    shell.appendChild(backdrop);
    var mobileOpen = false;
    function openMobile(open, restoreFocus) {
      mobileOpen = mobile.matches && open;
      document.documentElement.toggleAttribute("data-mobile-nav", mobileOpen);
      sidebar.inert = mobile.matches && !mobileOpen;
      if (main) main.inert = mobileOpen;
      backdrop.hidden = !mobileOpen;
      btn.setAttribute("aria-expanded", String(mobileOpen));
      btn.setAttribute("aria-label", mobileOpen ? "关闭导航菜单" : "打开导航菜单");
      btn.title = mobileOpen ? "关闭导航菜单" : "打开导航菜单";
      if (mobileOpen) {
        var first = sidebar.querySelector(".side-nav-item");
        if (first) first.focus();
      } else if (restoreFocus) btn.focus();
    }
    function apply(collapsed) {
      shell.classList.toggle("is-collapsed", collapsed);
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      document.documentElement.setAttribute("data-nav", collapsed ? "collapsed" : "expanded");
      /* 收纳后标签隐藏 → 用原生 title 保留可读性 */
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        if (collapsed) {
          var label = it.querySelector("span:not(.nav-no)");
          if (label && !it.title) it.title = label.textContent.trim();
        } else if (it.title) {
          it.removeAttribute("title");
        }
      }
    }

    var stored = null;
    try { stored = window.localStorage.getItem(KEY); } catch (e) { /* 隐私模式 */ }
    function syncViewport() {
      openMobile(false, sidebar.contains(document.activeElement));
      apply(!mobile.matches && stored === "1");
      if (mobile.matches) openMobile(false, false);
      else {
        btn.setAttribute("aria-label", "收起 / 展开侧边栏");
        btn.title = "收起 / 展开侧边栏";
      }
    }
    syncViewport();
    mobile.addEventListener("change", syncViewport);
    backdrop.addEventListener("click", function () { openMobile(false, true); });
    sidebar.addEventListener("click", function (ev) {
      if (mobile.matches && ev.target.closest("a, .side-nav-item")) openMobile(false, true);
    });
    document.addEventListener("keydown", function (ev) {
      if (!mobileOpen) return;
      if (ev.key === "Escape") {
        ev.preventDefault();
        openMobile(false, true);
      } else if (ev.key === "Tab") {
        var focusable = Array.from(sidebar.querySelectorAll("a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex='0']"))
          .filter(function (el) { return el.getClientRects().length && getComputedStyle(el).visibility !== "hidden"; });
        focusable.push(btn);
        var index = focusable.indexOf(document.activeElement);
        ev.preventDefault();
        focusable[(index + (ev.shiftKey ? -1 : 1) + focusable.length) % focusable.length].focus();
      }
    });

    var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var timer = null;

    btn.addEventListener("click", function () {
      if (mobile.matches) {
        openMobile(!mobileOpen, true);
        return;
      }
      var next = !shell.classList.contains("is-collapsed");
      apply(next);
      stored = next ? "1" : "0";
      try { window.localStorage.setItem(KEY, stored); } catch (e) { /* 隐私模式 */ }
      /* 动画期间加提示类（will-change），结束后摘掉，避免长期占用合成层。
         主列不再钉宽：侧栏宽度过渡时 flex 布局逐帧联动，系统条 / 内容区
         平滑跟随同一曲线，不再出现"侧栏动完、黑条瞬间跳宽"的断层。
         （图表 canvas 仅在 window resize 防抖后重绘，逐帧重排无额外开销。） */
      if (!reduced) {
        shell.classList.add("is-nav-animating");
        if (timer) window.clearTimeout(timer);
        var done = function (ev) {
          /* 只认侧栏自身的 width 过渡结束；子元素的 opacity 等过渡
             会冒泡触发 transitionend，不能作为动画完成信号 */
          if (ev && ev.target !== sidebar) return;
          if (ev && ev.propertyName && ev.propertyName !== "width") return;
          shell.classList.remove("is-nav-animating");
          sidebar.removeEventListener("transitionend", done);
        };
        sidebar.addEventListener("transitionend", done);
        timer = window.setTimeout(function () {
          shell.classList.remove("is-nav-animating");
          sidebar.removeEventListener("transitionend", done);
        }, 500);       /* 兜底：动画被中断也能摘掉 */
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
