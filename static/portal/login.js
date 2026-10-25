/* 登录页行为（D2，阶段 9）：本站账号登录 + 飞书开通/重设入口 + 状态提示。
 * 不伪造成功：错误原样展示；未配置飞书时按钮禁用并说明。 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }
  var params = new URLSearchParams(location.search);
  /* 登录后跳转目标：仅接受站内绝对路径（缺省 "/"；拒绝 // 开头防协议相对跳转）。
     旧写法 (params.get("next") || "/").startsWith("/") 会把缺省时的 null 直接
     赋给 next，登录后跳到 /null——已修复。 */
  var rawNext = params.get("next");
  var next = rawNext && rawNext.startsWith("/") && !rawNext.startsWith("//")
    ? rawNext : "/";

  // URL 状态提示（recheck=1 复核不可用 / oauth_error / reset=1）
  var note = $("login-note");
  var notes = [];
  if (params.get("recheck") === "1") {
    notes.push("暂时无法确认成员身份（上游复核不可用，并非退出组织）。" +
               "此时可登录并进入调试台，管理数据需等复核恢复。");
  }
  if (params.get("oauth_error")) {
    notes.push("飞书授权失败：" + params.get("oauth_error"));
  }
  if (params.get("reset") === "1") {
    notes.push("密码已重设，请用新密码登录。");
  }
  if (note && notes.length) {
    note.textContent = "※ " + notes.join("　");
    note.hidden = false;
  }

  // 飞书按钮可用性（预检 start 端点；未配置 → 禁用说明）
  var fbtn = $("feishu-btn");
  if (fbtn) {
    fetch("/auth/feishu/start?next=" + encodeURIComponent(next), {redirect: "manual"})
      .then(function (r) {
        if (r.status === 503) {
          fbtn.removeAttribute("href");
          fbtn.setAttribute("aria-disabled", "true");
          fbtn.textContent = "飞书登录（未配置）";
        }
      }).catch(function () { /* 网络异常保持默认跳转 */ });
  }

  // 本站账号登录
  var form = $("local-form");
  var msg = $("lo-msg");
  if (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      msg.textContent = "登录中…";
      fetch("/api/auth/local/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({username: $("lo-user").value,
                              password: $("lo-pass").value})
      }).then(function (r) {
        return r.json().then(function (j) {
          return {ok: r.ok, status: r.status, body: j};
        });
      }).then(function (res) {
        if (res.ok) {
          msg.textContent = "登录成功，正在跳转…";
          /* 登录后首启标记：调试台开机引导动画只在这之后的首次进入播放；
           * 同时写 handoff 标记，让落地页（门户/调试台）整屏揭幕入场 */
          try {
            sessionStorage.setItem("foray.post-login-boot", "1");
            sessionStorage.setItem("foray.handoff", "1");
          } catch (e) { /* 隐私模式：跳过动画 */ }
          /* 出场：与门户↔调试台切换同一套覆盖层滑入；减少动效时直接跳转 */
          var reduced = window.matchMedia &&
            window.matchMedia("(prefers-reduced-motion: reduce)").matches;
          if (reduced || !document.querySelector(".app-handoff-cover")) {
            location.href = next;
            return;
          }
          document.documentElement.setAttribute("data-handoff", "out");
          setTimeout(function () { location.href = next; }, 240);
          return;
        }
        var detail = res.body && res.body.detail ? String(res.body.detail) : "未知错误";
        msg.textContent = (res.status === 429 ? "" : "登录失败：") + detail;
        if (res.status === 403 && res.body && res.body.code === "onboarding_required") {
          location.href = "/onboarding";
        }
      }).catch(function () {
        msg.textContent = "网络错误，无法连接服务器。";
      });
    });
  }

  // 已登录身份显示
  fetch("/api/auth/me").then(function (r) {
    if (!r.ok) return null;
    return r.json();
  }).then(function (me) {
    if (me && $("who")) {
      var how = me.auth_method === "local" ? "本站账号" : "飞书";
      $("who").textContent = me.display + "（成员 · " + how + "）";
    }
  }).catch(function () {});
})();
