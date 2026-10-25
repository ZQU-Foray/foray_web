/* 开通页与找回密码页行为（D2，阶段 9）。不伪造成功：错误原样展示。 */
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }

  function show(id, text, isError) {
    var n = $(id);
    if (!n) return;
    n.textContent = text;
    n.hidden = !text;
    if (isError) { n.classList.add("is-error"); } else { n.classList.remove("is-error"); }
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; })
        .then(function (j) { return {ok: r.ok, status: r.status, body: j}; });
    });
  }

  function whoami() {
    fetch("/api/auth/me").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (me) { if (me && $("who")) { $("who").textContent = me.display; } })
      .catch(function () {});
  }
  whoami();

  var ob = $("ob-form");
  if (ob) {
    ob.addEventListener("submit", function (ev) {
      ev.preventDefault();
      show("ob-msg", "提交中…");
      post("/api/auth/account/onboard", {
        username: $("ob-user").value.trim(),
        password: $("ob-pass").value,
        password2: $("ob-pass2").value
      }).then(function (res) {
        if (res.ok) {
          show("ob-msg", "设置完成，正在进入门户…");
          location.href = "/";
          return;
        }
        show("ob-msg", "设置失败：" + (res.body.detail || "未知错误"), true);
      }).catch(function () { show("ob-msg", "网络错误，无法连接服务器。", true); });
    });
  }

  var rs = $("rs-form");
  if (rs) {
    rs.addEventListener("submit", function (ev) {
      ev.preventDefault();
      show("rs-msg", "提交中…");
      post("/api/auth/account/reset-password", {
        password: $("rs-pass").value,
        password2: $("rs-pass2").value
      }).then(function (res) {
        if (res.ok) {
          show("rs-msg", "密码已重设，正在返回登录页…");
          location.href = "/login?reset=1";
          return;
        }
        show("rs-msg", "重设失败：" + (res.body.detail || "未知错误"), true);
      }).catch(function () { show("rs-msg", "网络错误，无法连接服务器。", true); });
    });
  }
})();
