/* Foray 门户前端（阶段 5）——API 驱动版。
 * 数据全部来自 /api/portal/*（认证 Cookie）；不再引用 fixtures.js（生产禁混 fixture）。
 * 行为约束（计划 §3.3/§5.3）：
 * - 每 15s 检查本地快照版本，变化才局部刷新（不整页重载、不重置筛选/详情/滚动）；
 * - 倒计时浏览器本地每秒计算（以 server_time 校时一次），不向飞书轮询；
 * - 甘特只读（无拖拽/写入）；缺日期事项进"未排期"列表；
 * - 401 → 登录页；envelope 三态（无快照/空/陈旧失败）明确提示。 */
(function () {
  "use strict";

  var QS = new URLSearchParams(location.search);
  var POLL_MS = parseInt(QS.get("poll_ms") || "15000", 10);  // 测试可加速
  POLL_MS = Number.isFinite(POLL_MS) ? Math.max(1000, Math.min(60000, POLL_MS)) : 15000;
  var reloadGeneration = 0;
  var state = {
    version: null, lastStale: null, serverOffset: 0,
    page: "", pathId: "",
    filters: { group: "全部", priority: "全部", status: "全部",
               owner: "全部", project: "全部",
               /* 物资明细筛选（2026-10-12）：赛季 / 一二级分类 / 开票 / 关键词 */
               season: "全部", category1: "全部", category2: "全部",
               invoice: "全部", q: "" },
    projectFilters: { group: "全部", archived: false },
    scheduleView: "list",                     /* list | calendar */
    ownerOptions: null, projectOptions: null,
    procOptions: null,                        /* 物资明细下拉可选项（接口下发） */
    meta: null,
  };

  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  /* ---------- API ---------- */

  function api(path) {
    return fetch(path).then(function (r) {
      if (r.status === 401) {
        // 登录页本身不得再跳转：否则 401 → /login → 401 形成无限重定向环
        if (location.pathname !== "/login") {
          location.href = "/login?next=" + encodeURIComponent(
            location.pathname + location.search);
        }
        var e401 = new Error("未认证或会话已失效");
        e401.status = 401;
        throw e401;
      }
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          var err = new Error((b && b.detail) || ("HTTP " + r.status));
          err.status = r.status;
          throw err;
        });
      }
      return r.json();
    });
  }

  function pad2(n) { return n < 10 ? "0" + n : "" + n; }
  function fmtDT(ts) {
    if (!ts) return "未设置";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) +
      " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }
  function fmtD(ts) {
    if (!ts) return "未设置";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }
  function localNowSec() { return Date.now() / 1000 + state.serverOffset; }

  /* ---------- 通用 UI ---------- */

  function badge(text, cls) { return el("span", "p-badge" + (cls ? " " + cls : ""), text); }
  function prioClass(p) { return p === "P0" ? "p0" : (p === "P1" ? "p1" : ""); }
  function emptyBox(msg) { return el("div", "p-empty", msg); }

  function renderEnvelope(env) {
    var line = $("#sync-line");
    if (!line) return;
    if (env.has_snapshot === false) {
      line.className = "p-sync is-empty";
      line.textContent = "尚无同步数据";
      return;
    }
    line.className = "p-sync" + (env.stale ? " is-stale" : "")
      + (env.last_error ? " is-failed" : "");
    /* v1.2：状态条只展示最新同步时间；异常作为短后缀（不放大段说明文字） */
    var bits = [];
    bits.push(env.last_success_at ? ("同步于 " + fmtDT(env.last_success_at)) : "尚未成功同步");
    if (env.stale) bits.push("缓存陈旧");
    if (env.last_error) bits.push("上次失败：" + errText(env.last_error));
    line.textContent = bits.join(" · ");
  }

  function errText(kind) {
    var map = {"network": "网络/上游不可达", "rate_limited": "限流（429）",
               "api": "飞书接口返回错误", "config": "配置或字段映射问题",
               "parse": "数据解析/分页校验失败",
               "schema": "本地快照库版本不匹配（已停止读写，见运维手册）"};
    return map[kind] || (String(kind).indexOf("internal:") === 0
      ? "内部错误（详见服务端日志）" : kind);
  }

  function showDataNote(msg, isError) {
    var main = $("#p-main") || document.body;
    var old = $("#data-note");
    if (old) old.remove();
    if (!msg) return;
    var n = el("p", "p-note" + (isError ? " is-error" : ""), msg);
    n.id = "data-note";
    main.insertBefore(n, main.firstChild);
  }

  function ensureRefreshButton() {
    var line = $("#sync-line");
    if (!line) return;
    /* 服务端可能已预渲染刷新按钮：存在即复用，只补事件绑定 */
    var b = $("#sync-refresh");
    if (!b) {
      b = el("button", "p-refresh", "刷新");
      b.id = "sync-refresh";
      line.parentNode.insertBefore(b, line.nextSibling);
    }
    if (b._bound) return;
    b._bound = true;
    b.addEventListener("click", function () {
      b.disabled = true;
      b.textContent = "刷新中…";
      fetch("/api/portal/sync", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          b.textContent = res.accepted ? "已触发同步" : (res.detail || "已限频");
          if (res.accepted) setTimeout(function () { reload(true); }, 1500);
          setTimeout(function () { b.disabled = false; b.textContent = "刷新"; }, 2000);
        })
        .catch(function () {
          b.textContent = "刷新失败（网络或未认证）";
          b.disabled = false;
        });
    });
    line.parentNode.insertBefore(b, line.nextSibling);
  }

  function qualityNote(n) {
    if (!n) return null;
    var d = el("p", "p-note",
      "※ " + n + " 条源数据存在质量异常（未知枚举/失效关联等），已排除出统计。");
    return d;
  }

  function projectName(pid) {
    if (!pid) return "—";
    var opts = state.projectOptions;
    if (!opts) return pid;                     /* 选项未加载完：先显示原始值 */
    for (var i = 0; i < opts.length; i++) {
      if (opts[i].id === pid) return opts[i].label;
    }
    return "项目已移除";                        /* §5.1 残留关联不静默 */
  }

  function ownerNames(list) {
    return (list || []).map(function (u) { return u.name || "…" + u.id.slice(-4); })
      .join("、") || "待分配";
  }

  function dueBadge(t) {
    var txt = t.due
      ? (t.due_precision === "date" ? fmtD(t.due.ts) : fmtDT(t.due.ts))
      : "未设置";
    if (t.overdue) return badge(txt + " · 逾期", "overdue");
    if (t.due_soon) return badge(txt + " · 临期", "soon");
    return badge(txt);
  }

  /* 记录级源链接：后端 url 来自官方 batch_get(with_shared_url) —— 逐条直达，
   * 链接缺失时回退到表级链接（见 linkToSource）。 */
  function recordLink(item, label) {
    if (!item || !item.url) return null;
    var a = el("a", "p-src-link", label || "飞书 ↗");
    a.href = item.url;
    a.target = "_blank";
    a.rel = "noopener";
    return a;
  }

  function linkToSource(tableKey) {
    var m = state.meta && state.meta.source_links;
    if (!m || !m[tableKey]) return null;
    var a = el("a", "p-src-link", "在飞书中打开该表 ↗");
    a.href = m[tableKey];
    a.target = "_blank";
    a.rel = "noopener";
    return a;
  }

  /* ---------- 页面渲染（当前页码与路径取自 state，支持软导航重复装配） ---------- */

  function renderOverview(env, data) {
    renderEnvelope(env);
    var main = $("#p-main");
    var cbox = $("#count-box");
    cbox.textContent = "";
    (data.milestones || []).forEach(function (m) {
      var d = el("div", "p-count");
      d.setAttribute("data-target", m.target.ts);
      d.setAttribute("data-precision", (m.target && m.target.precision) || "date");
      d.appendChild(el("div", "t-name", (m.kind ? m.kind + " · " : "") + m.name));
      /* 天数为大数字、剩余时分秒单列：秒级刷新由 countdownTick 写这两个子节点 */
      var left = el("div", "t-left");
      left.appendChild(el("b", "t-days", "—"));
      left.appendChild(el("span", "t-rem", ""));
      d.appendChild(left);
      d.appendChild(el("div", "t-sub", fmtD(m.target.ts)));
      var mlink = recordLink(m);
      if (mlink) d.appendChild(mlink);
      cbox.appendChild(d);
    });
    if (!cbox.children.length) cbox.appendChild(emptyBox("暂无展示中的未来节点"));
    var mall = el("a", "p-src-link", "查看全部节点与日程 →");
    mall.href = "/schedule";
    cbox.appendChild(mall);
    var gbox = $("#urgent-box");
    gbox.textContent = "";
    (data.groups || []).forEach(function (g) {
      var card = el("div", "p-card");
      var h = el("h2");
      h.appendChild(badge(g.group, "group"));
      if (g.overdue) h.appendChild(el("span", "p-h-warn", " 逾期 " + g.overdue));
      card.appendChild(h);
      if (!g.items.length) card.appendChild(emptyBox("本组暂无未完成事项"));
      g.items.forEach(function (t) {
        var r = el("div", "p-row");
        r.appendChild(badge(t.priority, prioClass(t.priority)));
        var a = el("a", "grow", t.name);
        a.href = "/tasks/" + encodeURIComponent(t.id);
        r.appendChild(a);
        r.appendChild(el("span", "who", ownerNames(t.owners)));
        r.appendChild(dueBadge(t));
        var tlink = recordLink(t);
        if (tlink) r.appendChild(tlink);
        card.appendChild(r);
      });
      gbox.appendChild(card);
    });
    var sbox = $("#schedule-box");
    sbox.textContent = "";
    (data.schedule || []).forEach(function (s) {
      var r = el("div", "p-row");
      r.appendChild(badge(s.kind || "—"));
      var a = el("a", "grow", s.name + (s.location ? " · " + s.location : ""));
      a.href = "/schedule/" + encodeURIComponent(s.id);
      r.appendChild(a);
      var stxt = s.all_day ? ("全天 " + fmtD(s.start.ts)) : fmtDT(s.start.ts);
      r.appendChild(el("span", "due", stxt + (s.ongoing ? " · 进行中" : "")));
      if (s.status === "已取消") r.appendChild(badge("已取消", "overdue"));
      var slink = recordLink(s);
      if (slink) r.appendChild(slink);
      sbox.appendChild(r);
    });
    if (!sbox.children.length) sbox.appendChild(emptyBox("未来 7 天暂无日程"));
    /* 打卡工时排行：服务端已按有效时长降序，前端只按序渲染，不再排序 */
    mountAttendance();
    var rbox = $("#rank-box");
    if (rbox) {
      rbox.textContent = "";
      var rank = data.members || [];
      if (!rank.length) rbox.appendChild(emptyBox("暂无成员考勤数据"));
      rank.forEach(function (m, i) {
        var r = el("div", "p-row");
        var has = m.work_hours != null;
        r.appendChild(has ? badge("#" + (i + 1), i === 0 ? "p0" : null) : badge("无数据"));
        r.appendChild(el("span", "grow", (m.member && m.member.name) || "(未命名)"));
        r.appendChild(el("span", "due", has
          ? m.work_hours.toFixed(2) + " 小时" : "未统计到打卡"));
        if (m.last_check_in) {
          r.appendChild(el("span", "who", "最近 " + fmtDT(m.last_check_in.ts)));
        }
        var rlink = recordLink(m);
        if (rlink) r.appendChild(rlink);
        rbox.appendChild(r);
      });
    }
    var old = $("#ov-quality"); if (old) old.remove();
    var qn = qualityNote(data.quality_issues);
    if (qn) {
      qn.id = "ov-quality";
      qn.appendChild(document.createTextNode(" "));
      qn.appendChild(qualityLink());
      main.insertBefore(qn, main.firstChild);
    }
  }

  function renderProjects(env, data) {
    renderEnvelope(env);
    var box = $("#project-list");
    box.textContent = "";
    (data || []).forEach(function (p) {
      var card = el("div", "p-card");
      var h = el("h2");
      h.appendChild(badge(p.owner_group || "—", "group"));
      var a = el("a", null, " " + p.name);
      a.href = "/projects/" + encodeURIComponent(p.id);
      h.appendChild(a);
      if (p.invalid) h.appendChild(badge("数据异常", "overdue"));
      card.appendChild(h);
      var r = el("div", "p-row");
      r.appendChild(el("span", "grow", p.description || "（无说明）"));
      r.appendChild(badge(p.status || "—"));
      card.appendChild(r);
      var meta = el("div", "p-row");
      meta.appendChild(el("span", "who", "负责人：" + ownerNames(p.owners)));
      meta.appendChild(el("span", "due",
        fmtD(p.plan_start && p.plan_start.ts) + " → " + fmtD(p.plan_end && p.plan_end.ts)));
      var pr = p.progress;
      meta.appendChild(el("span", "due", pr.percent == null
        ? "暂无事项" : pr.done + "/" + pr.total + " · " + pr.percent + "%"));
      var plink2 = recordLink(p);
      if (plink2) meta.appendChild(plink2);
      card.appendChild(meta);
      box.appendChild(card);
    });
    if (!box.children.length) box.appendChild(emptyBox("暂无项目（源表为空）"));
  }

  function gantt(tasks) {
    /* 只读甘特：日期轴 + 事项条；缺 start/due → 未排期列表；无任何拖拽交互。 */
    var wrap = el("div", "p-gantt");
    var dated = [], unscheduled = [];
    tasks.forEach(function (t) {
      if (t.start && t.due && t.due.ts >= t.start.ts) dated.push(t);
      else unscheduled.push(t);
    });
    if (!dated.length && !unscheduled.length) {
      wrap.appendChild(emptyBox("项目暂无事项"));
      return wrap;
    }
    if (dated.length) {
      var min = Infinity, max = -Infinity;
      dated.forEach(function (t) {
        min = Math.min(min, t.start.ts); max = Math.max(max, t.due.ts);
      });
      var span = Math.max(max - min, 86400 * 7);
      var head = el("div", "p-gantt-head");
      head.appendChild(el("span", "p-gantt-cell p-gantt-name", "事项"));
      var axis = el("div", "p-gantt-axis");
      for (var i = 0; i <= 4; i++) {
        axis.appendChild(el("span", null, fmtD(min + span * i / 4)));
      }
      head.appendChild(axis);
      wrap.appendChild(head);
      dated.forEach(function (t) {
        var row = el("div", "p-gantt-row");
        var nm = el("a", "p-gantt-cell p-gantt-name");
        nm.appendChild(badge(t.priority || "—", prioClass(t.priority)));
        nm.appendChild(el("span", null, " " + t.name));
        nm.href = "/tasks/" + encodeURIComponent(t.id);
        row.appendChild(nm);
        var track = el("div", "p-gantt-track");
        var bar = el("div", "p-gantt-bar st-" + (t.status === "已完成" ? "done" :
          t.status === "受阻" ? "blocked" : t.status === "已取消" ? "cancel" : "open"));
        var l = (t.start.ts - min) / span * 100;
        var w = Math.max((t.due.ts - t.start.ts) / span * 100, 1.2);
        bar.style.left = l.toFixed(2) + "%";
        bar.style.width = w.toFixed(2) + "%";
        bar.title = t.name + " · " + fmtD(t.start.ts) + " → " + fmtD(t.due.ts) +
          " · " + (t.status || "");
        track.appendChild(bar);
        row.appendChild(track);
        wrap.appendChild(row);
      });
    }
    if (unscheduled.length) {
      var u = el("div", "p-unsched");
      u.appendChild(el("p", "p-note",
        "未排期（缺开始或截止日期，不伪造排期）：" + unscheduled.length + " 条"));
      unscheduled.forEach(function (t) {
        var r = el("div", "p-row");
        r.appendChild(badge(t.priority || "—", prioClass(t.priority)));
        var a = el("a", "grow", t.name);
        a.href = "/tasks/" + encodeURIComponent(t.id);
        r.appendChild(a);
        r.appendChild(badge(t.status || "—"));
        u.appendChild(r);
      });
      wrap.appendChild(u);
    }
    return wrap;
  }

  /* 详情路由下隐藏列表外壳：任务列表是静态写死的表头 + 空 tbody，
     不隐藏就会在详情上方留下一条没有任何数据的表头栏（筛选条同理）。 */
  function hideListChrome() {
    var card = $("#task-list-card");
    if (card) card.hidden = true;
    var pcard = $("#proc-list-card");
    if (pcard) pcard.hidden = true;
    document.querySelectorAll(".p-filters").forEach(function (f) { f.hidden = true; });
    var note = $("#task-note-slot");
    if (note) note.hidden = true;
    var pnote = $("#proc-note-slot");
    if (pnote) pnote.hidden = true;
    var pstats = $("#proc-stats");
    if (pstats) pstats.hidden = true;
  }

  function renderProjectDetail(env, p) {
    hideListChrome();
    renderEnvelope(env);
    var box = $("#project-detail");
    box.textContent = "";
    var h = el("h2");
    h.appendChild(badge(p.owner_group || "—", "group"));
    h.appendChild(el("span", null, " " + p.name));
    box.appendChild(h);
    if (p.invalid) box.appendChild(el("p", "p-note",
      "※ 该项目源数据存在质量异常，统计已做排除处理。"));
    var info = el("div", "p-row");
    info.appendChild(el("span", "who", "状态：" + (p.status || "—") +
      " · 负责人：" + ownerNames(p.owners)));
    info.appendChild(el("span", "due",
      fmtD(p.plan_start && p.plan_start.ts) + " → " + fmtD(p.plan_end && p.plan_end.ts)));
    var pr = p.progress;
    info.appendChild(el("span", "due", pr.percent == null
      ? "暂无事项 / —" : "完成率 " + pr.percent + "%（" + pr.done + "/" + pr.total + "）"));
    box.appendChild(info);
    if (state.me && state.me.is_leader) box.appendChild(deleteControl(p.id, "projects", "项目"));
    if (p.description) box.appendChild(el("p", "p-empty", p.description));
    var rlink = recordLink(p, "在飞书中打开该记录 ↗");
    if (rlink) box.appendChild(rlink);
    var link = linkToSource("projects");
    if (link && !rlink) box.appendChild(link);
    box.appendChild(el("h2", "p-sec", "事项甘特（只读 · 禁止拖拽写入）"));
    box.appendChild(gantt(p.tasks || []));
    box.appendChild(el("h2", "p-sec", "事项列表"));
    var list = el("div", "p-card");
    (p.tasks || []).forEach(function (t) {
      var r = el("div", "p-row");
      r.appendChild(badge(t.priority || "—", prioClass(t.priority)));
      var a = el("a", "grow",
        t.name + (t.blocker_note ? "（受阻：" + t.blocker_note + "）" : ""));
      a.href = "/tasks/" + encodeURIComponent(t.id);
      r.appendChild(a);
      r.appendChild(badge(t.status || "—", t.status === "已完成" ? "done" : ""));
      r.appendChild(dueBadge(t));
      list.appendChild(r);
    });
    if (!(p.tasks || []).length) list.appendChild(emptyBox("暂无事项"));
    box.appendChild(list);
    if ((p.milestones || []).length) {
      box.appendChild(el("h2", "p-sec", "相关节点"));
      var ml = el("div", "p-card");
      p.milestones.forEach(function (m) {
        var r = el("div", "p-row");
        r.appendChild(badge(m.kind || "—"));
        r.appendChild(el("span", "grow", m.name));
        r.appendChild(el("span", "due", fmtDT(m.target && m.target.ts)));
        ml.appendChild(r);
      });
      box.appendChild(ml);
    }
  }

  function renderTasks(env, body) {
    renderEnvelope(env);
    var tbody = $("#task-body");
    tbody.textContent = "";
    (body.data || []).forEach(function (t) {
      var tr = document.createElement("tr");
      function td(node) {
        var c = document.createElement("td");
        c.dataset.label = ["优先级", "事项", "主责组", "负责人", "状态", "项目", "截止", "源记录"][tr.children.length];
        if (node) c.appendChild(node);
        tr.appendChild(c);
      }
      td(badge(t.priority || "—", prioClass(t.priority)));
      var a = el("a", null, t.name + (t.invalid ? "（数据异常）" : ""));
      a.href = "/tasks/" + encodeURIComponent(t.id);
      td(a);
      td(document.createTextNode(t.owner_group || "—"));
      td(document.createTextNode(ownerNames(t.owners)));
      td(badge(t.status || "—", t.status === "已完成" ? "done" : ""));
      td(document.createTextNode(t.project_name || projectName(t.project_id)));
      td(dueBadge(t));
      td(recordLink(t, "飞书 ↗"));
      tbody.appendChild(tr);
    });
    if (!tbody.children.length) {
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 8;
      td.appendChild(emptyBox("本筛选下暂无事项（源表可能为空）"));
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    var slot = $("#task-note-slot");
    var old = $("#tq-note"); if (old) old.remove();
    var qn = qualityNote((body.data || []).filter(function (t) { return t.invalid; }).length);
    if (qn && slot) {
      qn.id = "tq-note";
      qn.insertBefore(document.createTextNode("（本筛选结果内计数）"),
                      qn.firstChild ? qn.firstChild.nextSibling : null);
      qn.appendChild(document.createTextNode(" "));
      qn.appendChild(qualityLink());
      slot.appendChild(qn);
    }
  }

  function renderTaskDetail(env, t) {
    hideListChrome();
    renderEnvelope(env);
    var box = $("#task-detail");
    box.textContent = "";
    var h = el("h2");
    h.appendChild(badge(t.priority || "—", prioClass(t.priority)));
    h.appendChild(el("span", null, " " + t.name));
    box.appendChild(h);
    if (t.invalid) box.appendChild(el("p", "p-note",
      "※ 该事项源数据存在质量异常（未知枚举/失效关联），已排除出统计。"));
    var rows = [
      ["状态", t.status || "—"],
      ["主责组", t.owner_group || "—"],
      ["协作组", (t.collab_groups || []).join("、") || "—"],
      ["负责人", ownerNames(t.owners)],
      ["所属项目", t.project_name || t.project_id || "—"],
      ["开始", fmtDT(t.start && t.start.ts)],
      ["截止", t.due ? fmtDT(t.due.ts) + "（" +
        (t.due_precision === "date" ? "仅日期" : "精确时刻") + "）" : "未设置"],
      ["完成时间", fmtDT(t.done_time && t.done_time.ts)],
      ["提交验收", fmtDT(t.submit_time && t.submit_time.ts)],
    ];
    var card = el("div", "p-card");
    var grid = el("div", "p-kv");
    rows.forEach(function (pair) {
      var r = el("div", "p-row");
      r.appendChild(el("span", "who", pair[0]));
      r.appendChild(el("span", "grow", String(pair[1])));
      grid.appendChild(r);
    });
    card.appendChild(grid);
    box.appendChild(card);
    if (t.overdue) box.appendChild(el("p", "p-note", "※ 已逾期。"));
    else if (t.due_soon) box.appendChild(el("p", "p-note", "※ 即将到期（72 小时内）。"));
    if (t.description) box.appendChild(el("p", "p-empty", "说明：" + t.description));
    if (t.blocker_note) box.appendChild(el("p", "p-empty", "阻塞：" + t.blocker_note));
    box.appendChild(taskEditCard(t));
    var rlink = recordLink(t, "在飞书中打开该记录 ↗");
    if (rlink) box.appendChild(rlink);
    var link = linkToSource("tasks");
    if (link && !rlink) box.appendChild(link);
  }

  function renderSchedule(env, data) {
    renderEnvelope(env);
    var box = $("#schedule-list");
    box.textContent = "";
    (data || []).forEach(function (s) {
      var r = el("div", "p-row");
      r.appendChild(badge(s.kind || "—"));
      var a = el("a", "grow",
        s.name + (s.groups && s.groups.length ? " · " + s.groups.join("/") : "") +
        (s.location ? " · " + s.location : ""));
      a.href = "/schedule/" + encodeURIComponent(s.id);
      r.appendChild(a);
      var spanTxt;
      if (s.all_day) {
        var d1 = fmtD(s.start.ts), d2 = s.end ? fmtD(s.end.ts) : null;
        spanTxt = "全天 " + d1 + (d2 && d2 !== d1 ? " – " + d2 : "");
      } else {
        spanTxt = fmtDT(s.start.ts) + (s.end ? " → " + fmtDT(s.end.ts) : "");
      }
      if (s.ongoing) spanTxt += " · 进行中";
      r.appendChild(el("span", "due", spanTxt));
      if (s.status === "已取消") r.appendChild(badge("已取消", "overdue"));
      var ssrc = recordLink(s);
      if (ssrc) r.appendChild(ssrc);
      sbox_row(r, box);
    });
    if (!box.children.length) box.appendChild(emptyBox("未来 7 天暂无日程"));
  }
  function sbox_row(r, box) { box.appendChild(r); }

  function renderScheduleDetail(env, s) {
    hideListChrome();
    renderEnvelope(env);
    var box = $("#schedule-detail");
    box.textContent = "";
    var h = el("h2");
    h.appendChild(badge(s.kind || "—"));
    h.appendChild(el("span", null, " " + s.name));
    box.appendChild(h);
    if (s.invalid) box.appendChild(el("p", "p-note", "※ 该日程源数据存在质量异常。"));
    var allDay = !!s.all_day;
    var rows = [
      ["时间", allDay
        ? "全天 " + fmtD(s.start && s.start.ts) +
          (s.end ? " – " + fmtD(s.end.ts) : "")
        : fmtDT(s.start && s.start.ts) +
          (s.end ? " → " + fmtDT(s.end && s.end.ts) : "")],
      ["状态", s.status || "—"],
      ["相关组", (s.groups || []).join("、") || "—"],
      ["地点", s.location || "—"],
      ["参与人", ownerNames(s.participants)],
    ];
    var card = el("div", "p-card");
    var grid = el("div", "p-kv");
    rows.forEach(function (pair) {
      var r = el("div", "p-row");
      r.appendChild(el("span", "who", pair[0]));
      r.appendChild(el("span", "grow", String(pair[1])));
      grid.appendChild(r);
    });
    card.appendChild(grid);
    box.appendChild(card);
    if (state.me && state.me.is_leader) {
      var acts = el("div", "p-edit-actions");
      acts.appendChild(deleteControl(s.id, "schedule", "日程"));
      box.appendChild(acts);
    }
    if (s.description) box.appendChild(el("p", "p-empty", "说明：" + s.description));
    var rlink = recordLink(s, "在飞书中打开该记录 ↗");
    if (rlink) box.appendChild(rlink);
    var link = linkToSource("schedule");
    if (link && !rlink) box.appendChild(link);
  }

  /* ---------- 物资采购（全员可改；组长批复列仅组长，显隐按身份） ---------- */

  function isLeader() { return !!(state.me && state.me.is_leader); }

  /* 复选列按钮/徽章：组长看到可切换的按钮，组员看到只读徽章 */
  function procFlagControl(p, key, onText, offText) {
    if (key === "leader_approval" && !isLeader())
      return badge(p.leader_approval ? onText : offText,
                   p.leader_approval ? "done" : null);
    var b = el("button", "p-mini" + (p[key] ? " on" : ""), p[key] ? onText : offText);
    b.type = "button";
    b.addEventListener("click", function () {
      var fields = {};
      fields[key] = !p[key];
      patchProc(p.id, fields, b);
    });
    return b;
  }

  function procLinkCell(p) {
    if (!p.link) return document.createTextNode("—");
    var a = el("a", null, "打开链接 ↗");
    a.href = p.link;
    a.target = "_blank";
    a.rel = "noopener";
    a.title = p.link;
    return a;
  }

  /* 金额/数量展示：空值不伪装成 0（不把「没填」显示成「0 元」） */
  function fmtMoney(v) { return v == null ? "—" : "¥" + Number(v).toFixed(2); }

  function fmtNum(v) { return v == null ? "—" : String(v); }

  /* 毫秒时间戳 → <input type="date"> 需要的 YYYY-MM-DD（北京时间） */
  function msToDateInput(ms) {
    if (!ms) return "";
    var d = new Date(ms + 8 * 3600 * 1000);   /* 平移后取 UTC 年月日即北京日期 */
    return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" +
      pad2(d.getUTCDate());
  }

  function fmtDate(ts) { return ts ? msToDateInput(ts * 1000) : "—"; }

  /* 附件列（图片补充 / 发票文件）：有临时链接就点得开，否则退化为文件名提示 */
  function attachCell(list) {
    list = list || [];
    if (!list.length) return document.createTextNode("—");
    var first = list[0];
    var txt = list.length + " 个" + (first.name ? "·" + first.name : "");
    var title = list.map(function (x) { return x.name; }).join("\n");
    var node;
    if (first.url) {
      node = el("a", "p-clip", txt);
      node.href = first.url;
      node.target = "_blank";
      node.rel = "noopener";
    } else {
      node = el("span", "p-clip", txt);
      title += "（临时链接未随快照下发，请在飞书中查看）";
    }
    node.title = title;
    return node;
  }

  /* 发票列：开票开关（全员可切）+ 发票文件（点开第一份，悬停看全部文件名） */
  function procInvoiceCell(p) {
    var box = el("span", "p-invoice");
    box.appendChild(procFlagControl(p, "invoice_done", "已开", "未开"));
    if ((p.invoice_files || []).length) box.appendChild(attachCell(p.invoice_files));
    return box;
  }

  function procCategoryCell(p) {
    var s = el("span", "p-clip",
      (p.category1 || "未分类") + (p.category2 ? " / " + p.category2 : ""));
    s.title = s.textContent;
    return s;
  }

  function procPurchaserCell(p) {
    var names = (p.purchaser || []).map(function (u) {
      return u.name || "…" + String(u.id || "").slice(-4);
    });
    return document.createTextNode(names.join("、") || "—");
  }

  function renderProcurement(env, body) {
    renderEnvelope(env);
    fillProcFilters(body.options);
    var stat = $("#proc-stats");
    if (stat) {
      stat.textContent = "";
      var st = body.stats || {};
      stat.appendChild(el("p", "p-note",
        "共 " + (st.count || 0) + " 条 · 合计 " + fmtMoney(st.amount) +
        "（当前筛选口径；「总价」是飞书公式列＝单价×数量，门户只读）"));
    }
    var tbody = $("#proc-body");
    tbody.textContent = "";
    var labels = ["物资", "采购编号", "分类", "申请组别", "数量", "单价",
                  "总价", "采购日期", "采购人", "发票", "组长批复", "已采购", "源"];
    (body.data || []).forEach(function (p) {
      var tr = document.createElement("tr");
      function td(node) {
        var c = document.createElement("td");
        c.dataset.label = labels[tr.children.length] || "";
        if (node) c.appendChild(node);
        tr.appendChild(c);
      }
      var a = el("a", null, p.name + (p.invalid ? "（数据异常）" : ""));
      a.href = "/procurement/" + encodeURIComponent(p.id);
      td(a);
      td(el("span", "p-clip", p.code || "—"));
      td(procCategoryCell(p));
      td(document.createTextNode((p.groups || []).join("、") || "—"));
      td(el("span", "p-num", fmtNum(p.quantity)));
      td(el("span", "p-num", fmtMoney(p.unit_price)));
      td(el("span", "p-num", fmtMoney(p.total_price)));
      td(el("span", "p-num", fmtDate(p.purchase_date && p.purchase_date.ts)));
      td(procPurchaserCell(p));
      td(procInvoiceCell(p));
      td(procFlagControl(p, "leader_approval", "已批复", "未批复"));
      td(procFlagControl(p, "purchased", "已采购", "未采购"));
      td(recordLink(p, "飞书 ↗"));
      tbody.appendChild(tr);
    });
    if (!tbody.children.length) {
      var tr2 = document.createElement("tr");
      var td2 = document.createElement("td");
      td2.colSpan = labels.length;
      td2.appendChild(emptyBox("暂无采购记录（源表为空或被筛选）"));
      tr2.appendChild(td2);
      tbody.appendChild(tr2);
    }
  }

  function renderProcurementDetail(env, p) {
    hideListChrome();
    renderEnvelope(env);
    var box = $("#proc-detail");
    box.textContent = "";
    var h = el("h2");
    h.appendChild(badge((p.groups || []).join("/") || "采购", "group"));
    h.appendChild(el("span", null, " " + p.name));
    box.appendChild(h);
    if (p.invalid) box.appendChild(el("p", "p-note",
      "※ 该记录源数据存在质量异常（如缺物资名称），已排除出统计。"));
    var rows = [
      ["赛季", p.season || "—"],
      ["采购编号", p.code || "—"],
      ["一级分类", p.category1 || "—"],
      ["二级分类", p.category2 || "—"],
      ["申请组别", (p.groups || []).join("、") || "—"],
      ["数量", p.quantity == null ? "—" : String(p.quantity)],
      ["单价（元）", fmtMoney(p.unit_price)],
      ["总价（元）", fmtMoney(p.total_price) + "（飞书公式＝单价×数量）"],
      ["采购日期", fmtDate(p.purchase_date && p.purchase_date.ts)],
      ["采购人", (p.purchaser || []).map(function (u) {
        return u.name || "…" + String(u.id || "").slice(-4);
      }).join("、") || "—"],
      ["购物链接", procLinkCell(p)],
      ["用途说明", p.purpose || "—"],
      ["图片补充", (p.attachments || []).length
        ? attachCell(p.attachments) : "无（可在下方上传）"],
      ["发票已开", p.invoice_done ? "已开" : "未开"],
      ["发票文件", (p.invoice_files || []).length
        ? attachCell(p.invoice_files) : "无（可在下方上传）"],
      ["组长批复", p.leader_approval ? "已批复" : "未批复"],
      ["已采购", p.purchased ? "已采购" : "未采购"],
    ];
    var card = el("div", "p-card");
    var grid = el("div", "p-kv");
    rows.forEach(function (pair) {
      var r = el("div", "p-row");
      r.appendChild(el("span", "who", pair[0]));
      var val = el("span", "grow");
      if (typeof pair[1] === "object" && pair[1]) val.appendChild(pair[1]);
      else val.textContent = String(pair[1]);
      r.appendChild(val);
      grid.appendChild(r);
    });
    card.appendChild(grid);
    box.appendChild(card);
    box.appendChild(procEditCard(p));
    var rlink = recordLink(p, "在飞书中打开该记录 ↗");
    if (rlink) box.appendChild(rlink);
    var link = linkToSource("procurement");
    if (link && !rlink) box.appendChild(link);
  }

  /* 附件上传行：column=image（图片补充）/ invoice（发票文件）。多选时逐张顺序上传；
     服务端把新文件并入该列既有附件后整列写回，不会覆盖别人刚传的。 */
  function procUploadRow(p, column, label, accept, hint) {
    var row = el("div", "p-edit-row");
    row.appendChild(el("span", "who", label));
    var fi = document.createElement("input");
    fi.type = "file";
    fi.accept = accept;
    fi.multiple = true;
    fi.className = "p-file";
    var up = el("button", "p-mini", "上传");
    up.type = "button";
    up.addEventListener("click", function () {
      var files = Array.prototype.slice.call(fi.files || []);
      if (!files.length) { showDataNote("请先选择要上传的文件", true); return; }
      up.disabled = true;
      var i = 0;
      (function next() {
        if (i >= files.length) {
          up.disabled = false;
          up.textContent = "上传";
          fi.value = "";
          afterWrite();
          return;
        }
        var f = files[i++];
        up.textContent = "上传中 " + i + "/" + files.length + "…";
        var fd = new FormData();
        fd.append("file", f, f.name);
        fetch("/api/portal/write/procurement/" + encodeURIComponent(p.id) +
              "/attachments?column=" + encodeURIComponent(column),
              { method: "POST", body: fd })
          .then(function (r) {
            return r.json().then(function (j) { return { ok: r.ok, j: j }; });
          })
          .then(function (res) {
            if (!res.ok) {
              up.disabled = false;
              up.textContent = "上传";
              showDataNote("上传失败：" + (res.j.detail || "未知错误"), true);
              return;
            }
            next();
          })
          .catch(function () {
            up.disabled = false;
            up.textContent = "上传";
            showDataNote("网络错误，未上传", true);
          });
      })();
    });
    row.appendChild(fi);
    row.appendChild(up);
    row.appendChild(el("span", "who", hint));
    return row;
  }

  /* 详情：字段更新卡——已采购/发票已开全员可切；组长批复仅组长可切（服务端权威） */
  function procEditCard(p) {
    var card = el("div", "p-card");
    var h = el("h2");
    h.appendChild(el("span", "tag", "UPDATE / 01"));
    h.appendChild(document.createTextNode("字段更新"));
    card.appendChild(h);
    var r2 = el("div", "p-edit-row");
    r2.appendChild(el("span", "who", "已采购"));
    r2.appendChild(procFlagControl(p, "purchased", "已采购", "未采购"));
    r2.appendChild(el("span", "who", "发票已开"));
    r2.appendChild(procFlagControl(p, "invoice_done", "已开", "未开"));
    card.appendChild(r2);
    card.appendChild(procUploadRow(p, "image", "上传图片", "image/*",
      "（全员可传 · 图片 · ≤10MB/张）"));
    card.appendChild(procUploadRow(p, "invoice", "上传发票", "image/*,.pdf",
      "（全员可传 · PDF/图片 · ≤20MB/份）"));
    if (isLeader()) {
      var r1 = el("div", "p-edit-row");
      r1.appendChild(el("span", "who", "组长批复"));
      r1.appendChild(procFlagControl(p, "leader_approval", "已批复", "未批复"));
      r1.appendChild(el("span", "who",
        "（仅组长可改；其余字段全员可编辑）"));
      card.appendChild(r1);
    } else {
      card.appendChild(el("p", "p-note",
        "「组长批复」由组长勾选；其它字段全员可修改。"));
    }
    var acts = el("div", "p-edit-actions");
    var eb = el("button", "p-create", "编辑全部字段");
    eb.type = "button";
    eb.addEventListener("click", function () {
      openWriteForm("procurement", procPreset(p), "PATCH",
                    "/api/portal/write/procurement/" + encodeURIComponent(p.id));
    });
    acts.appendChild(eb);
    if (isLeader()) acts.appendChild(deleteControl(p.id, "procurement", "采购记录"));
    card.appendChild(acts);
    return card;
  }

  function procPreset(p) {
    return {
      name: p.name === "(未命名)" ? "" : p.name,
      season: p.season || "",
      category1: p.category1 || "",
      category2: p.category2 || "",
      code: p.code || "",
      purchase_date: msToDateInput(p.purchase_date && p.purchase_date.raw_ms),
      unit_price: p.unit_price == null ? "" : p.unit_price,
      quantity: p.quantity == null ? "" : p.quantity,
      purchaser: (p.purchaser || []).map(function (u) { return u.id; }),
      groups: p.groups || [],
      link: p.link || "",
      purpose: p.purpose || "",
      invoice_done: !!p.invoice_done,
      leader_approval: !!p.leader_approval,
    };
  }

  function patchProc(id, fields, btn) {
    var old = btn.textContent;
    btn.disabled = true; btn.textContent = "写入中…";
    fetch("/api/portal/write/procurement/" + encodeURIComponent(id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      btn.disabled = false; btn.textContent = old;
      if (res.ok) afterWrite();
      else showDataNote("写入失败：" + (res.j.detail || "未知错误"), true);
    }).catch(function () {
      btn.disabled = false; btn.textContent = old;
      showDataNote("网络错误，未写入", true);
    });
  }

  /* ---------- 数据质量明细（§5.2 异常与排除说明可见） ---------- */

  var TABLE_LABEL = {projects: "项目", tasks: "事项", milestones: "重要节点",
                     schedule: "公共日程", procurement: "物资采购"};

  function renderQuality(env, body) {
    renderEnvelope(env);
    var sum = $("#quality-summary");
    var box = $("#quality-list");
    box.textContent = "";
    var bt = body.by_table || {};
    var parts = Object.keys(bt).map(function (k) {
      return (TABLE_LABEL[k] || k) + " " + bt[k];
    });
    sum.textContent = "共 " + (body.total || 0) + " 条异常记录（" +
      (parts.join(" · ") || "无") + "）。这些记录已排除出统计与紧急列表。";
    if (!(body.data || []).length) {
      box.appendChild(emptyBox("没有质量异常记录"));
      return;
    }
    var table = el("table", "p-table");
    var thead = el("thead");
    var trh = el("tr");
    ["数据表", "记录 ID", "原因"].forEach(function (h) {
      trh.appendChild(el("th", null, h));
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    var tbody = el("tbody");
    (body.data || []).forEach(function (q) {
      var tr = el("tr");
      tr.appendChild(el("td", null, TABLE_LABEL[q.table] || q.table || "—"));
      tr.appendChild(el("td", null, q.record_id || "—"));
      tr.appendChild(el("td", null, q.issue || "—"));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    var wrap = el("div", "p-scroller");
    wrap.appendChild(table);
    box.appendChild(wrap);
  }

  function qualityLink() {
    var a = el("a", "p-src-link", "查看异常明细 →");
    a.href = "/quality";
    return a;
  }

  /* ---------- 日程：列表 / 日历 切换（§3.1） ---------- */

  function renderSchedulePage(env, body) {
    renderEnvelope(env);
    var listBox = $("#schedule-list");
    var calBox = $("#schedule-calendar");
    var useCal = state.scheduleView === "calendar";
    listBox.hidden = useCal;
    calBox.hidden = !useCal;
    if (useCal) renderScheduleCalendar(env, body.data || []);
    else renderSchedule(env, body.data || []);
    var note = $("#sched-note");
    if (note) note.remove();
    var tail = el("p", "p-note", useCal
      ? "日历视图覆盖未来 28 天；点日程进入详情。"
      : "列表视图覆盖未来 7 天（含正在进行）；点日程进入详情。");
    tail.id = "sched-note";
    listBox.parentNode.appendChild(tail);
  }

  function renderScheduleCalendar(env, data) {
    var box = $("#schedule-calendar");
    box.textContent = "";
    var now = new Date(localNowSec() * 1000);
    var start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var byDay = {};
    data.forEach(function (s) {
      var d = new Date(s.start.ts * 1000);
      var key = d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
      (byDay[key] = byDay[key] || []).push(s);
    });
    var grid = el("div", "p-cal");
    ["一", "二", "三", "四", "五", "六", "日"].forEach(function (w) {
      grid.appendChild(el("div", "p-cal-head", "周" + w));
    });
    for (var i = 0; i < 28; i++) {
      var day = new Date(start.getTime() + i * 86400000);
      var key = day.getFullYear() + "-" + pad2(day.getMonth() + 1) + "-" +
        pad2(day.getDate());
      var cell = el("div", "p-cal-cell" + (i === 0 ? " is-today" : ""));
      cell.appendChild(el("div", "p-cal-date",
        (day.getMonth() + 1) + "/" + day.getDate()));
      (byDay[key] || []).forEach(function (s) {
        var a = el("a", "p-cal-ev" + (s.status === "已取消" ? " is-cancel" : ""));
        a.href = "/schedule/" + encodeURIComponent(s.id);
        a.textContent = (s.all_day ? "全天 " : fmtDT(s.start.ts).slice(11) + " ") +
          s.name + (s.status === "已取消" ? "（已取消）" : "");
        a.title = s.name + (s.location ? " · " + s.location : "");
        cell.appendChild(a);
      });
      grid.appendChild(cell);
    }
    box.appendChild(grid);
  }

  /* ---------- 项目页筛选（主责组 + 含归档，§3.1/§5.1） ---------- */

  function buildProjectFilters() {
    var gbar = $("#proj-group-filter");
    if (gbar) {
      gbar.textContent = "";
      var groups = (state.meta && state.meta.groups) ||
        ["机械", "硬件", "电控", "算法", "运营"];
      ["全部"].concat(groups).forEach(function (g) {
        var b = el("button", null, g);
        b.setAttribute("aria-pressed",
          state.projectFilters.group === g ? "true" : "false");
        b.addEventListener("click", function () {
          state.projectFilters.group = g;
          gbar.querySelectorAll("button").forEach(function (x) {
            x.setAttribute("aria-pressed", x === b ? "true" : "false");
          });
          reload(true);
        });
        gbar.appendChild(b);
      });
    }
    var abar = $("#proj-archived-filter");
    if (abar) {
      abar.textContent = "";
      var on = el("button", null, state.projectFilters.archived
        ? "含归档：是" : "含归档：否");
      on.setAttribute("aria-pressed", state.projectFilters.archived ? "true" : "false");
      on.addEventListener("click", function () {
        state.projectFilters.archived = !state.projectFilters.archived;
        buildProjectFilters();
        reload(true);
      });
      abar.appendChild(on);
    }
  }

  function buildScheduleToggle() {
    var bar = $("#view-toggle");
    if (!bar) return;
    bar.textContent = "";
    [["list", "列表视图"], ["calendar", "日历视图"]].forEach(function (kv) {
      var b = el("button", null, kv[1]);
      b.setAttribute("aria-pressed", state.scheduleView === kv[0] ? "true" : "false");
      b.addEventListener("click", function () {
        state.scheduleView = kv[0];
        buildScheduleToggle();
        reload(true);
      });
      bar.appendChild(b);
    });
  }

  /* ---------- 数据加载与轮询 ---------- */

  var loaders = {
    overview: function () { return api("/api/portal/overview"); },
    /* 列表统一取后端上限 200 条；超出时由 has_more 提示（不静默截断） */
    projects: function () {
      if (state.pathId) return api("/api/portal/projects/" + encodeURIComponent(state.pathId));
      var pf = state.projectFilters, pq = ["page_size=200"];
      if (pf.group !== "全部") pq.push("group=" + encodeURIComponent(pf.group));
      if (pf.archived) pq.push("include_archived=true");
      return api("/api/portal/projects?" + pq.join("&"));
    },
    tasks: function () {
      if (state.pathId) return api("/api/portal/tasks/" + encodeURIComponent(state.pathId));
      var f = state.filters, q = [];
      if (f.group !== "全部") q.push("group=" + encodeURIComponent(f.group));
      if (f.priority !== "全部") q.push("priority=" + encodeURIComponent(f.priority));
      if (f.status !== "全部") q.push("status=" + encodeURIComponent(f.status));
      if (f.owner !== "全部") q.push("owner=" + encodeURIComponent(f.owner));
      if (f.project !== "全部") q.push("project_id=" + encodeURIComponent(f.project));
      q.push("page_size=200");
      return api("/api/portal/tasks?" + q.join("&"));
    },
    schedule: function () {
      if (state.pathId) return api("/api/portal/schedule/" + encodeURIComponent(state.pathId));
      /* 日历视图取 28 天窗口，列表视图保持默认 7 天 */
      var days = state.scheduleView === "calendar" ? 28 : 7;
      return api("/api/portal/schedule?days=" + days + "&page_size=200");
    },
    procurement: function () {
      if (state.pathId) return api("/api/portal/procurement/" + encodeURIComponent(state.pathId));
      var f = state.filters, q = [];
      ["group", "season", "category1", "category2", "invoice"].forEach(function (k) {
        if (f[k] && f[k] !== "全部") q.push(k + "=" + encodeURIComponent(f[k]));
      });
      if (f.q) q.push("q=" + encodeURIComponent(f.q));
      q.push("page_size=200");
      return api("/api/portal/procurement?" + q.join("&"));
    },
    quality: function () { return api("/api/portal/quality?page_size=200"); },
    login: function () { return Promise.resolve(null); },
  };

  function render(env, body) {
    try {
      if (state.page === "overview") renderOverview(env, body.data);
      else if (state.page === "projects")
        state.pathId ? renderProjectDetail(env, body.data) : renderProjects(env, body.data);
      else if (state.page === "tasks")
        state.pathId ? renderTaskDetail(env, body.data) : renderTasks(env, body);
      else if (state.page === "schedule")
        state.pathId ? renderScheduleDetail(env, body.data) : renderSchedulePage(env, body);
      else if (state.page === "procurement")
        state.pathId ? renderProcurementDetail(env, body.data) : renderProcurement(env, body);
      else if (state.page === "quality") renderQuality(env, body);
    } catch (e) {
      var box = $("#p-main") || document.body;
      box.insertBefore(el("p", "p-note",
        "※ 渲染异常：" + e.message + "（快照数据未受影响，可刷新重试）"),
        box.firstChild);
    }
  }

  /* 轮询重渲染会重建 DOM，原焦点控件随之消失（a11y 遗留项，阶段 8 修）：
     记录焦点元素的"稳定标识"（id 优先，其次 tag+type+name+文本），
     重渲染后按同一标识找回并恢复焦点与光标位置。 */
  function focusKey(node) {
    if (!node || node === document.body || !node.tagName) return null;
    if (node.id) return "#" + node.id;
    var sel = node.tagName.toLowerCase();
    if (node.type) sel += "[type=" + node.type + "]";
    if (node.name) sel += "[name=" + node.name + "]";
    var txt = (node.textContent || "").trim().slice(0, 24);
    return sel + (txt ? "::" + txt : "");
  }

  function captureFocus() {
    var a = document.activeElement;
    var key = focusKey(a);
    if (!key) return null;
    var snap = { key: key, start: null, end: null };
    try {
      if (typeof a.selectionStart === "number") {
        snap.start = a.selectionStart;
        snap.end = a.selectionEnd;
      }
    } catch (e) { /* 某些 input 类型不支持 selection */ }
    return snap;
  }

  function restoreFocus(snap) {
    if (!snap) return;
    var target = null;
    if (snap.key.charAt(0) === "#") target = document.getElementById(snap.key.slice(1));
    if (!target) {
      var parts = snap.key.split("::");
      var nodes = document.querySelectorAll(parts[0]);
      for (var i = 0; i < nodes.length; i++) {
        if (!parts[1] || (nodes[i].textContent || "").trim().slice(0, 24) === parts[1]) {
          target = nodes[i];
          break;
        }
      }
    }
    if (!target || typeof target.focus !== "function") return;
    try {
      target.focus({ preventScroll: true });
      if (snap.start != null && typeof target.setSelectionRange === "function") {
        target.setSelectionRange(snap.start, snap.end);
      }
    } catch (e) { /* 焦点恢复失败不影响数据展示 */ }
  }

  /* 后台刷新（轮询 / 手动刷新）会重建容器 DOM，新建的卡片会重放 `panelDeploy`
     入场动画——实测每次同步发布后有 5 个动画同时重播（组卡片），观感即
     "同步时抖一下"。首次绘制与页面跳转（软导航）保留动画，后台刷新关掉。 */
  function suppressEntrance() {
    document.querySelectorAll(".p-card").forEach(function (c) {
      c.style.animation = "none";
    });
  }

  function reload(keepScroll) {
    var generation = ++reloadGeneration;
    /* 滚动位置不显式恢复：DOM 同步替换后浏览器原生保持偏移（内容变短时
       自动收拢）。旧实现 scrollTo 回 fetch 开始时的快照，轮询重渲染会把
       用户刚滚过的位置拽回去——观感即"每次同步抖一下"。 */
    var focusSnap = keepScroll ? captureFocus() : null;
    return Promise.all([api("/api/portal/sync-status"), loaders[state.page]()])
      .then(function (rs) {
        if (generation !== reloadGeneration) return;
        var env = rs[0], body = rs[1] || {};
        state.serverOffset = (env.server_time || Date.now() / 1000) - Date.now() / 1000;
        state.version = env.snapshot_version;
        state.lastStale = env.stale;
        if (body.has_snapshot === false) {
          renderEnvelope({ has_snapshot: false });
          var box = $("#p-main");
          if (box && !$("#no-snap-note")) {
            var n = el("p", "p-note",
              "※ 尚无可用同步数据 —— 首次同步未完成或飞书未配置（与『源表为空』不同）。");
            n.id = "no-snap-note";
            box.insertBefore(n, box.firstChild);
          }
          return;
        }
        var old = $("#no-snap-note"); if (old) old.remove();
        showDataNote(body.has_more
          ? ("列表仅显示前 " + (body.data || []).length + " 条（共 " +
             body.total + " 条）——请用筛选缩小范围。")
          : "");
        render(env, body);
        if (keepScroll) suppressEntrance();     /* 后台刷新不重放入场动画 */
      })
      .catch(function (e) {
        if (generation !== reloadGeneration) return;
        var st = e && e.status;
        if (st === 401) return;                  /* 已由 api() 跳登录页 */
        if (st === 403) {
          showDataNote("暂时无法确认成员身份（上游复核不可用，非退出组织）", true);
        } else if (st === 404) {
          showDataNote("记录不存在：" + (e.message || "已从源表移除"), true);
        } else {
          showDataNote("页面数据加载失败（" + ((e && e.message) || "未知错误") +
                       "）——展示上次内容，可点『刷新』重试", true);
        }
      })
      .then(function () {
        if (generation === reloadGeneration) restoreFocus(focusSnap);  // 只恢复当前页面焦点
      });
  }

  function pollLoop() {
    var pending = false;
    setInterval(function () {
      if (pending || document.hidden) return;
      pending = true;
      api("/api/portal/sync-status").then(function (st) {
        if (st.snapshot_version !== state.version || st.stale !== state.lastStale) {
          return reload(true);            // 局部刷新：保筛选/详情/滚动
        }
      }).catch(function () {}).then(function () { pending = false; });
    }, POLL_MS);
  }

  /* ---------- 倒计时（本地 1s tick，不向飞书轮询） ---------- */

  function countdownTick() {
    setInterval(function () {
      var now = localNowSec();
      document.querySelectorAll(".p-count[data-target]").forEach(function (d) {
        var target = parseFloat(d.getAttribute("data-target"));
        var prec = d.getAttribute("data-precision");
        var days, rem = "";
        if (prec === "date") {
          /* 纯日期：只给天数/今天（§5.3 不虚构小时级精度） */
          var t0 = new Date(target * 1000); t0.setHours(0, 0, 0, 0);
          var n0 = new Date(now * 1000); n0.setHours(0, 0, 0, 0);
          var dd = Math.round((t0 - n0) / 86400000);
          days = dd > 0 ? dd + " 天" : (dd === 0 ? "今天" : "已过 " + (-dd) + " 天");
        } else {
          /* 精确时刻：天数 + 剩余时分秒（秒级刷新） */
          var sec = Math.max(target - now, 0);
          days = Math.floor(sec / 86400) + " 天";
          rem = "剩余 " + pad2(Math.floor(sec % 86400 / 3600)) + ":" +
            pad2(Math.floor(sec % 3600 / 60)) + ":" + pad2(Math.floor(sec % 60));
        }
        var big = $(".t-days", d);
        var small = $(".t-rem", d);
        if (!big || !small) return;
        big.textContent = days;
        small.textContent = rem;
      });
    }, 1000);
  }

  /* ---------- 筛选（tasks 页；状态在 state，跨刷新不重置） ---------- */

  /* 负责人/项目筛选项：一次性取全量后缓存（§3.1 要求按负责人、项目筛选） */
  function ensureFilterOptions() {
    if (state.ownerOptions && state.projectOptions) return Promise.resolve();
    return Promise.all([
      api("/api/portal/tasks?page_size=200"),
      api("/api/portal/projects?page_size=200&include_archived=true"),
    ]).then(function (rs) {
      var seen = {}, owners = [];
      (rs[0].data || []).forEach(function (t) {
        (t.owners || []).forEach(function (u) {
          if (u.id && !seen[u.id]) {
            seen[u.id] = 1;
            owners.push({ id: u.id, label: u.name || "…" + u.id.slice(-4) });
          }
        });
      });
      owners.sort(function (a, b) { return a.label.localeCompare(b.label, "zh"); });
      state.ownerOptions = owners;
      state.projectOptions = (rs[1].data || []).map(function (p) {
        return { id: p.id, label: p.name };
      });
    }).catch(function () { state.ownerOptions = []; state.projectOptions = []; });
  }

  function optionBar(sel, key, options) {
    var bar = $(sel);
    if (!bar) return;
    bar.textContent = "";
    [{ id: "全部", label: "全部" }].concat(options || []).forEach(function (o) {
      var b = el("button", null, o.label);
      b.setAttribute("aria-pressed", state.filters[key] === o.id ? "true" : "false");
      b.addEventListener("click", function () {
        state.filters[key] = o.id;
        bar.querySelectorAll("button").forEach(function (x) {
          x.setAttribute("aria-pressed", x === b ? "true" : "false");
        });
        reload(true);
      });
      bar.appendChild(b);
    });
  }

  function buildFilters() {
    var bar = $("#group-filter");
    if (!bar) return;
    bar.textContent = "";
    var groups = (state.meta && state.meta.groups) ||
      ["机械", "硬件", "电控", "算法", "运营"];
    ["全部"].concat(groups).forEach(function (g) {
      var b = el("button", null, g);
      b.setAttribute("aria-pressed", state.filters.group === g ? "true" : "false");
      b.addEventListener("click", function () {
        state.filters.group = g;
        bar.querySelectorAll("button").forEach(function (x) {
          x.setAttribute("aria-pressed", x === b ? "true" : "false");
        });
        reload(true);
      });
      bar.appendChild(b);
    });
    [["#pri-filter", "priority", ["全部", "P0", "P1", "P2", "P3"]],
     ["#status-filter", "status",
      ["全部", "未开始", "进行中", "受阻", "已完成", "已取消"]]].forEach(function (cfg) {
      var bar2 = $(cfg[0]);
      if (!bar2) return;
      bar2.textContent = "";
      cfg[2].forEach(function (v) {
        var b = el("button", null, v);
        b.setAttribute("aria-pressed", state.filters[cfg[1]] === v ? "true" : "false");
        b.addEventListener("click", function () {
          state.filters[cfg[1]] = v;
          bar2.querySelectorAll("button").forEach(function (x) {
            x.setAttribute("aria-pressed", x === b ? "true" : "false");
          });
          reload(true);
        });
        bar2.appendChild(b);
      });
    });
    optionBar("#owner-filter", "owner", state.ownerOptions);
    optionBar("#project-filter", "project", state.projectOptions);
  }

  /* ---------- 物资明细筛选（2026-10-12） ---------- */

  /* 二级分类 → 一级分类：只用于把下拉候选按一级分类收窄。
     飞书里新加的二级分类不在此表 → 归属未知，任何一级分类下都列出，
     不会因为维护者新增选项而在门户里选不到。 */
  var PROC_CATEGORY2_PARENT = {
    "直流电机": "电机", "无刷电机": "电机", "舵机": "电机",
    "电机驱动": "电机", "编码器": "电机", "减速箱": "电机",
    "螺丝": "标准件", "螺母": "标准件", "垫片垫圈": "标准件", "轴承": "标准件",
    "销与键": "标准件", "弹簧": "标准件", "卡簧": "标准件", "铆钉": "标准件",
    "板材": "加工件", "型材": "加工件", "管材": "加工件", "轴类": "加工件",
    "法兰": "加工件", "支架": "加工件", "3D打印件": "加工件", "定制机加": "加工件",
    "主控板": "电子元器件", "电源模块": "电子元器件", "传感器": "电子元器件",
    "电阻电容": "电子元器件", "连接器": "电子元器件", "芯片": "电子元器件",
    "显示屏": "电子元器件",
    "电源线": "线材接插件", "信号线": "线材接插件", "端子": "线材接插件",
    "插头插座": "线材接插件", "线束": "线材接插件",
    "螺丝刀": "工具耗材", "扳手": "工具耗材", "钻头": "工具耗材",
    "胶粘剂": "工具耗材", "扎带": "工具耗材", "砂纸": "工具耗材", "焊锡": "工具耗材",
    "其他": "其他",
  };

  var PROC_FILTER_KEYS = ["season", "category1", "category2", "invoice"];

  /* 下拉候选：常量清单 ∪ 快照实际值（接口 options 下发）；给了 parent 就按一级分类收窄 */
  function procOptionList(name, parent) {
    var o = state.procOptions || {};
    var list = (o[name] || []).slice();
    if (name === "category2" && parent && parent !== "全部") {
      list = list.filter(function (v) {
        var p = PROC_CATEGORY2_PARENT[v];
        return !p || p === parent;   /* 归属未知的新选项一律保留 */
      });
    }
    return list;
  }

  /* 筛选下拉填充：首项「全部」；当前值已不在候选里也保留，否则筛选会静默失效 */
  function fillFilterSelect(sel, values, cur) {
    sel.textContent = "";
    sel.appendChild(new Option("全部", "全部"));
    values.forEach(function (v) { sel.appendChild(new Option(v, v)); });
    if (cur && cur !== "全部" && values.indexOf(cur) < 0) {
      sel.appendChild(new Option(cur + "（已从飞书选项移除）", cur));
    }
    sel.value = cur || "全部";
  }

  /* 写表单选下拉：首项「不设置」；当前值不在候选里也保留，避免保存时被静默丢掉 */
  function fillSelect(sel, values, cur) {
    sel.textContent = "";
    sel.appendChild(new Option("（不设置）", ""));
    values.forEach(function (v) { sel.appendChild(new Option(v, v)); });
    if (cur && values.indexOf(cur) < 0) {
      sel.appendChild(new Option(cur + "（已从飞书选项移除）", cur));
    }
    sel.value = cur || "";
  }

  function buildProcFilters() {
    var bar = $("#proc-filters");
    if (!bar) return;
    bar.textContent = "";
    var LABELS = { season: "赛季", category1: "一级分类",
                   category2: "二级分类", invoice: "开票" };
    PROC_FILTER_KEYS.forEach(function (k) {
      var wrap = el("label", "p-filter");
      wrap.appendChild(el("span", "p-filter-label", LABELS[k]));
      var sel = document.createElement("select");
      sel.id = "proc-f-" + k;
      sel.addEventListener("change", function () {
        state.filters[k] = sel.value;
        /* 一级分类变了：二级分类候选跟着收窄并复位 */
        if (k === "category1") { state.filters.category2 = "全部"; fillProcFilters(); }
        reload(true);
      });
      wrap.appendChild(sel);
      bar.appendChild(wrap);
    });
    var sw = el("label", "p-filter");
    sw.appendChild(el("span", "p-filter-label", "搜索"));
    var si = document.createElement("input");
    si.type = "search";
    si.id = "proc-f-q";
    si.placeholder = "物资名称 / 采购编号";
    si.value = state.filters.q || "";
    function submitSearch() {
      state.filters.q = si.value.trim();
      reload(true);
    }
    si.addEventListener("keydown", function (e) { if (e.key === "Enter") submitSearch(); });
    si.addEventListener("search", submitSearch);
    sw.appendChild(si);
    bar.appendChild(sw);
    var rs = el("button", "p-mini", "重置");
    rs.type = "button";
    rs.addEventListener("click", function () {
      state.filters.group = "全部";
      PROC_FILTER_KEYS.forEach(function (k) { state.filters[k] = "全部"; });
      state.filters.q = "";
      buildFilters();        /* 组别 chips 复位 */
      buildProcFilters();    /* 本条的 select 与搜索框复位 */
      reload(true);
    });
    bar.appendChild(rs);
    fillProcFilters();
  }

  function fillProcFilters(opts) {
    if (opts) state.procOptions = opts;
    PROC_FILTER_KEYS.forEach(function (k) {
      var sel = $("#proc-f-" + k);
      if (!sel) return;
      var cur = state.filters[k] || "全部";
      if (k === "invoice") {
        sel.textContent = "";
        [["全部", "全部"], ["undone", "未开票"], ["done", "已开票"]].forEach(function (p) {
          sel.appendChild(new Option(p[1], p[0]));
        });
        sel.value = cur;
        return;
      }
      /* 接口下发的键是 seasons（复数），筛选键是 season，这里做一次映射 */
      var optKey = k === "season" ? "seasons" : k;
      fillFilterSelect(sel,
        procOptionList(optKey, k === "category2" ? state.filters.category1 : null), cur);
    });
    var si = $("#proc-f-q");
    if (si && si.value !== (state.filters.q || "")) si.value = state.filters.q || "";
  }

  /* ---------- 身份区（阶段 3） ---------- */

  function authArea() {
    var who = document.getElementById("who");
    if (!who) return;
    fetch("/api/auth/me").then(function (r) { return r.ok ? r.json() : null; })
      .then(function (me) {
        if (!me) { who.textContent = "未登录"; state.me = null; return; }
        state.me = me;                      /* is_leader / leader_groups：写入口显隐 */
        decorateWrite();
        /* 采购页的批复按钮按身份显隐：首帧身份未到时会按组员渲染，
           身份到达后局部重渲一次（其余页面由轮询自然覆盖） */
        if (state.page === "procurement") reload(true);
        var text = me.display + "（" +
          (me.role === "member" ? "成员" : "应急·仅调试") + "）";
        /* 与服务端预渲染一致时不动 DOM：避免每次进页文字闪一遍 */
        if (who.textContent !== text) who.textContent = text;
        var out = document.querySelector(".p-logout");
        if (!out) {
          out = document.createElement("button");
          out.className = "p-logout";
          out.textContent = "退出";
          who.parentNode.insertBefore(out, who.nextSibling);
        }
        if (out._bound) return;
        out._bound = true;
        out.addEventListener("click", function () {
          fetch("/api/auth/logout", { method: "POST" })
            .then(function () { location.href = "/login"; }).catch(function () {});
        });
      }).catch(function () { who.textContent = "身份未知（离线）"; });
  }


  /* ---------- 考勤打卡（排行卡头部；网段校验在服务端） ---------- */

  function mountAttendance() {
    var box = $("#attend-box");
    if (!box || box._mounted) return;
    box._mounted = true;
    var stEl = $("#attend-state");
    var btn = $("#attend-btn");
    function refresh() {
      fetch("/api/portal/attendance/status").then(function (r) { return r.json(); })
        .then(function (st) {
          var hrs = st.work_hours != null ? st.work_hours.toFixed(2) : "0.00";
          if (st.checked_in) {
            stEl.textContent = "已签到 " + fmtDT(st.since) + " · 我的工时 " + hrs + "h";
            btn.textContent = "签退";
          } else {
            stEl.textContent = "未签到 · 我的工时 " + hrs + "h";
            btn.textContent = "签到";
          }
          box._checkedIn = st.checked_in;
          btn.hidden = false;
        }).catch(function () { stEl.textContent = "考勤状态不可用"; });
    }
    btn.addEventListener("click", function () {
      btn.disabled = true;
      var act = box._checkedIn ? "check-out" : "check-in";
      fetch("/api/portal/attendance/" + act, { method: "POST" })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          btn.disabled = false;
          if (res.ok) {
            var msg = act === "check-out"
              ? "已签退 · 本次 +" + res.j.session_hours + "h（累计 " + res.j.work_hours + "h）"
              : "已签到";
            showDataNote(msg + "，正在同步…");
            fetch("/api/portal/sync", { method: "POST" }).catch(function () {});
            setTimeout(function () { reload(true); refresh(); }, 2400);
          } else {
            showDataNote("打卡失败：" + (res.j.detail || "未知错误"), true);
            refresh();
          }
        }).catch(function () {
          btn.disabled = false;
          showDataNote("网络错误，未打卡", true);
        });
    });
    refresh();
  }

  /* ---------- 写入（组长新建 / 组员白名单修改；权威校验在服务端） ---------- */

  var W_GROUPS = ["机械", "硬件", "电控", "算法", "运营"];

  var WRITE_FORMS = {
    task: {
      title: "新建事项", method: "POST", url: "/api/portal/write/tasks",
      rows: [
        { k: "name", label: "事项名称", t: "text", req: true },
        { k: "owner_group", label: "主责组（可多选）", t: "checks", opts: W_GROUPS, req: true },
        { k: "priority", label: "优先级", t: "radio",
          opts: ["P0", "P1", "P2", "P3"], def: "P2" },
        { k: "owners", label: "负责人（可多选）", t: "checks", dynOpts: "users" },
        { k: "project", label: "所属项目", t: "project" },
        { k: "start_time", label: "计划开始", t: "dt" },
        { k: "due_time", label: "计划截止", t: "dt" },
        { k: "status", label: "执行状态", t: "radio",
          opts: ["未开始", "进行中", "受阻", "待验收"], def: "未开始" },
        { k: "description", label: "具体事项", t: "text" },
      ],
    },
    schedule: {
      title: "新建日程", method: "POST", url: "/api/portal/write/schedule",
      rows: [
        { k: "name", label: "日程名称", t: "text", req: true },
        { k: "kind", label: "日程类型", t: "radio",
          opts: ["例会", "联调", "测试", "训练", "检查", "比赛", "运营活动", "其他"],
          def: "例会" },
        { k: "start_time", label: "开始时间", t: "dt", req: true },
        { k: "end_time", label: "结束时间", t: "dt" },
        { k: "groups", label: "涉及组别（可多选）", t: "checks", opts: W_GROUPS },
        { k: "participants", label: "参与人（可多选）", t: "checks", dynOpts: "users" },
        { k: "location", label: "地点", t: "text" },
        { k: "status", label: "日程状态", t: "radio",
          opts: ["待进行", "进行中", "已结束", "已取消"], def: "待进行" },
        { k: "description", label: "安排详情", t: "text" },
      ],
    },
    project: {
      title: "新建项目", method: "POST", url: "/api/portal/write/projects",
      rows: [
        { k: "name", label: "项目名称", t: "text", req: true },
        { k: "owner_group", label: "主责组", t: "radio", opts: W_GROUPS },
        { k: "collab_groups", label: "协作组（可多选）", t: "checks", opts: W_GROUPS },
        { k: "owners", label: "负责人（可多选）", t: "checks", dynOpts: "users" },
        { k: "plan_start", label: "计划开始", t: "dt" },
        { k: "plan_end", label: "计划结束", t: "dt" },
        { k: "status", label: "项目状态", t: "radio",
          opts: ["筹备中", "进行中", "已结束", "已归档"], def: "筹备中" },
        { k: "description", label: "项目说明", t: "text" },
      ],
    },
    procurement: {
      title: "新建采购申请", method: "POST", url: "/api/portal/write/procurement",
      uploadAfter: "procurement",      /* 创建成功后把所选图片传到新记录 */
      rows: [
        { k: "name", label: "物资名称", t: "text", req: true },
        { k: "season", label: "赛季", t: "select", optsFrom: "seasons" },
        { k: "category1", label: "一级分类", t: "select", optsFrom: "category1" },
        { k: "category2", label: "二级分类", t: "select", optsFrom: "category2",
          link: "category1" },
        { k: "code", label: "采购编号（留空按 赛季+分类 自动生成）", t: "text" },
        { k: "purchase_date", label: "采购日期", t: "date" },
        { k: "unit_price", label: "单价（元）", t: "number" },
        { k: "quantity", label: "数量", t: "number" },
        { k: "purchaser", label: "采购人（可多选）", t: "checks", dynOpts: "users" },
        { k: "groups", label: "申请组别（可多选）", t: "checks", opts: W_GROUPS },
        { k: "link", label: "购物链接", t: "text" },
        { k: "purpose", label: "用途说明", t: "text" },
        { k: "invoice_done", label: "发票已开", t: "check" },
        { k: "invoice_files", label: "发票文件（PDF/图片 · ≤20MB/份）", t: "files",
          col: "invoice", accept: "image/*,.pdf" },
        { k: "attach_files", label: "图片补充（图片 · ≤10MB/张）", t: "files" },
        { k: "leader_approval", label: "组长批复", t: "check", leaderOnly: true },
      ],
    },
  };

  function toLocalInput(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) +
      "T" + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  /* 花名册人员：同一人可能属多个组别（后端已按 open_id 聚合成 groups 数组），
     这里再兜一层，兼容只有单个 group 字段的旧形状 —— 此前按 id 去重只留第一条，
     多组别的人只会显示一个组。 */
  function userOptions() {
    var byId = {};
    ((state.meta && state.meta.roster_users) || []).forEach(function (u) {
      if (!u.id) return;
      var rec = byId[u.id] || (byId[u.id] = { id: u.id, name: u.name || "",
                                              groups: [], leader: false });
      var gs = u.groups || (u.group ? [u.group] : []);
      gs.forEach(function (g) {
        if (g && rec.groups.indexOf(g) < 0) rec.groups.push(g);
      });
      if (u.leader) rec.leader = true;
      if (!rec.name && u.name) rec.name = u.name;
    });
    return Object.keys(byId).map(function (k) { return byId[k]; })
      .sort(function (a, b) { return (a.name || "").localeCompare(b.name || "", "zh"); });
  }

  /* 勾选项标签：姓名（组别1/组别2·组长）——多组别全部列出 */
  function userLabel(u) {
    var tail = (u.groups || []).join("/");
    if (u.leader) tail = tail ? tail + "·组长" : "组长";
    return u.name + (tail ? "（" + tail + "）" : "");
  }

  /* 紧凑多选（人员）：控件本体仍是一个 select 形状的框，框内显示已选人员标签。
     点开后在浮层里勾选，勾选即写回标签；标签上的 × 可单个移除。
     真正的 <input type="checkbox"> 留在表单容器内（hidden），
     因此 collectForm 的 checks 分支、写接口与后端全部无需改动。
     浮层挂 body 并用 fixed：弹窗是 overflow:auto 且带动画（transform），
     留在文档流里会被裁掉或错位（调试台下拉踩过同一个坑）。 */
  var _multiPanels = [];               /* 关闭弹窗时统一清理，避免残留在 body */

  function withMultiSelect(wrap, row, selected) {
    var box = el("div", "p-multi");
    box.dataset.k = row.k; box.dataset.t = "checks";
    var people = userOptions();
    /* 编辑预置的人员可能已不在花名册：补进选项，否则保存时会被静默丢掉 */
    (selected || []).forEach(function (id) {
      if (!people.some(function (u) { return u.id === id; })) {
        people.push({ id: id, name: "…" + String(id).slice(-6), groups: [] });
      }
    });
    var picked = (selected || []).slice();
    var inputs = {};
    people.forEach(function (u) {
      var i = document.createElement("input");
      i.type = "checkbox"; i.value = u.id; i.hidden = true;
      i.checked = picked.indexOf(u.id) >= 0;
      box.appendChild(i);
      inputs[u.id] = i;
    });
    var trg = el("button", "p-multi-trigger");
    trg.type = "button";
    box.appendChild(trg);
    var panel = null;

    function personOf(id) {
      for (var i = 0; i < people.length; i++) {
        if (people[i].id === id) return people[i];
      }
      return { id: id, name: "…" + String(id).slice(-6), groups: [] };
    }

    function sync() {
      trg.textContent = "";
      if (!picked.length) {
        trg.appendChild(el("span", "p-multi-ph", "＋ 选择人员"));
      }
      picked.forEach(function (id) {
        var chip = el("span", "p-chip", userLabel(personOf(id)));
        var x = el("span", "p-chip-x", "×");
        x.title = "移除";
        x.addEventListener("click", function (e) {
          e.stopPropagation();
          picked = picked.filter(function (v) { return v !== id; });
          if (inputs[id]) inputs[id].checked = false;
          if (panel) {
            panel.querySelectorAll("input").forEach(function (c) {
              if (c.value === id) c.checked = false;
            });
          }
          sync();
        });
        chip.appendChild(x);
        trg.appendChild(chip);
      });
      trg.classList.toggle("is-empty", !picked.length);
    }

    function onDoc(e) {
      if (panel && !panel.contains(e.target) && !box.contains(e.target)) close();
    }

    /* 页面滚动会让浮层与触发器脱位，故滚动即关闭；但**浮层自身滚动不算**——
       浮层是 overflow:auto，人员多时必须在里面滚动查看，早期版本没排除这一点，
       结果一滚就关、排在下面的人永远选不到。 */
    function onScroll(e) {
      if (panel && (e.target === panel || panel.contains(e.target))) return;
      close();
    }

    function close() {
      if (panel) { panel.remove(); panel = null; }
      document.removeEventListener("click", onDoc, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", close);
    }

    function open() {
      close();
      panel = el("div", "p-multi-panel");
      if (!people.length) {
        panel.appendChild(el("div", "p-note", "花名册暂无人员（请先在飞书花名册里维护）"));
      }
      people.forEach(function (u) {
        var lb = el("label", "p-opt");
        var c = document.createElement("input");
        c.type = "checkbox"; c.value = u.id;
        c.checked = picked.indexOf(u.id) >= 0;
        c.addEventListener("change", function () {
          picked = c.checked ? picked.concat([u.id])
                             : picked.filter(function (v) { return v !== u.id; });
          if (inputs[u.id]) inputs[u.id].checked = c.checked;
          sync();
        });
        lb.appendChild(c);
        lb.appendChild(document.createTextNode(" " + userLabel(u)));
        panel.appendChild(lb);
      });
      document.body.appendChild(panel);
      _multiPanels.push(panel);
      /* 贴着触发器下方；下方空间不足则上翻（用 bottom 锚定） */
      var r = trg.getBoundingClientRect();
      var below = innerHeight - r.bottom - 12;
      panel.style.position = "fixed";
      panel.style.minWidth = r.width + "px";
      panel.style.maxWidth = Math.min(420, innerWidth - 24) + "px";
      panel.style.left = Math.max(8, Math.min(r.left, innerWidth - 24 - r.width)) + "px";
      if (below < 140 && r.top > below) {
        panel.style.bottom = (innerHeight - r.top + 4) + "px";
        panel.style.maxHeight = Math.min(240, Math.max(120, r.top - 12)) + "px";
      } else {
        panel.style.top = (r.bottom + 4) + "px";
        panel.style.maxHeight = Math.min(240, Math.max(120, below)) + "px";
      }
      /* 延迟注册：避免本次点击立刻触发"点外部关闭" */
      setTimeout(function () {
        document.addEventListener("click", onDoc, true);
        window.addEventListener("scroll", onScroll, true);   /* 面板内滚动不算，见 onScroll */
        window.addEventListener("resize", close);
      }, 0);
    }

    trg.addEventListener("click", function (e) {
      e.stopPropagation();
      if (panel) close(); else open();
    });
    sync();
    wrap.appendChild(box);
  }

  function withProjectOptions(cb) {
    if (state.projectOptions) return cb(state.projectOptions);
    api("/api/portal/projects?page_size=200").then(function (r) {
      state.projectOptions = (r.data || []).map(function (p) {
        return { id: p.id, label: p.name };
      });
      cb(state.projectOptions);
    }).catch(function () { cb([]); });
  }

  /* 组长显隐：身份到达（authArea）或软导航装配（bootPage）后调用 */
  function decorateWrite() {
    var main = $("#p-main");
    if (!main || state.pathId) return;
    var old = $("#write-bar");
    if (old) old.remove();
    /* 物资采购：全员可新建（不设限），按钮只要求已登录成员 */
    if (state.page === "procurement") {
      if (!state.me) return;
      var pbar = el("div", "p-filters");
      pbar.id = "write-bar";
      var pb = el("button", "p-create", "＋ 新建采购申请");
      pb.type = "button";
      pb.addEventListener("click", function () { openWriteForm("procurement"); });
      pbar.appendChild(pb);
      main.insertBefore(pbar, main.firstChild);
      return;
    }
    if (!(state.me && state.me.is_leader)) return;
    if (["tasks", "projects", "schedule"].indexOf(state.page) < 0) return;
    var bar = el("div", "p-filters");
    bar.id = "write-bar";
    var labels = { tasks: "事项", projects: "项目", schedule: "日程" };
    var b = el("button", "p-create", "＋ 新建" + labels[state.page]);
    b.type = "button";
    b.addEventListener("click", function () {
      openWriteForm(state.page === "tasks" ? "task"
                  : state.page === "schedule" ? "schedule" : "project");
    });
    bar.appendChild(b);
    main.insertBefore(bar, main.firstChild);
  }

  function formControl(row, preset) {
    var wrap = el("div", "p-form-row");
    var lab = el("span", "p-form-label", row.label + (row.req ? "（必填）" : ""));
    wrap.appendChild(lab);
    var v = preset != null ? preset : (row.def != null ? row.def : "");
    if (row.t === "text") {
      var i = document.createElement("input");
      i.type = "text"; i.value = v || ""; i.dataset.k = row.k;
      wrap.appendChild(i);
    } else if (row.t === "dt") {
      var d = document.createElement("input");
      d.type = "datetime-local"; d.value = v || ""; d.dataset.k = row.k;
      wrap.appendChild(d);
    } else if (row.t === "files") {
      /* 附件选择（仅新建时用；不设 data-k，值不进 JSON 载荷，由创建成功后的
         uploadAfter 流程逐份上传到 dataset.col 指定的附件列：image / invoice） */
      var fl = document.createElement("input");
      fl.type = "file";
      fl.accept = row.accept || "image/*";
      fl.multiple = true;
      fl.className = "p-file";
      fl.dataset.col = row.col || "image";
      wrap.appendChild(fl);
    } else if (row.t === "date") {
      /* 日期（YYYY-MM-DD）：服务端按北京时间转毫秒时间戳写飞书 */
      var dd = document.createElement("input");
      dd.type = "date";
      dd.dataset.k = row.k;
      dd.value = (v || "").slice(0, 10);
      wrap.appendChild(dd);
    } else if (row.t === "number") {
      /* 数字（单价/数量）：留空 = 不改/清空 */
      var nn = document.createElement("input");
      nn.type = "number";
      nn.step = "0.01";
      nn.min = "0";
      nn.dataset.k = row.k;
      nn.value = (v == null ? "" : v);
      wrap.appendChild(nn);
    } else if (row.t === "select") {
      /* 单选下拉：候选来自接口下发的 options（state.procOptions），
         飞书里增删选项后门户自动跟随 */
      var ss = document.createElement("select");
      ss.dataset.k = row.k;
      fillSelect(ss, procOptionList(row.optsFrom, null), v || "");
      wrap.appendChild(ss);
    } else if (row.t === "check") {
      /* 单个复选（物资采购的组长批复等布尔列）：值直接取 checked */
      var lb = el("label", "p-opt");
      var c = document.createElement("input");
      c.type = "checkbox"; c.dataset.k = row.k; c.dataset.t = "check";
      c.checked = !!v;
      lb.appendChild(c);
      lb.appendChild(document.createTextNode(" 勾选"));
      wrap.appendChild(lb);
    } else if (row.t === "radio" || row.t === "checks") {
      if (row.dynOpts === "users") {
        withMultiSelect(wrap, row, v || []);      /* 人员：紧凑多选下拉 */
        return wrap;
      }
      var box = el("div", null);
      box.dataset.k = row.k; box.dataset.t = row.t;
      (row.opts || []).forEach(function (o) {
        var val = (o && typeof o === "object") ? o.v : o;
        var text = (o && typeof o === "object") ? o.label : o;
        var lb = el("label", "p-opt");
        var c = document.createElement("input");
        c.type = row.t === "radio" ? "radio" : "checkbox";
        c.name = row.k; c.value = val;
        var on = row.t === "radio" ? v === val : (v || []).indexOf(val) >= 0;
        if (on) c.checked = true;
        lb.appendChild(c);
        lb.appendChild(document.createTextNode(" " + text));
        box.appendChild(lb);
      });
      wrap.appendChild(box);
    } else if (row.t === "project") {
      var sel = document.createElement("select");
      sel.dataset.k = row.k;
      sel.appendChild(new Option("（无）", ""));
      withProjectOptions(function (opts) {
        (opts || []).forEach(function (p) {
          sel.appendChild(new Option(p.label, p.id, false, p.id === v));
        });
      });
      wrap.appendChild(sel);
    }
    return wrap;
  }

  /* 二级分类联动：一级分类改动时重建二级分类候选并复位当前值。
     归属未知的新选项照常保留（procOptionList 的兜底），不会把维护者新加的选项藏起来。 */
  function wireLinkedSelects(form, spec) {
    (spec.rows || []).forEach(function (row) {
      if (row.t !== "select" || !row.link) return;
      var src = form.querySelector('[data-k="' + row.link + '"]');
      var dst = form.querySelector('[data-k="' + row.k + '"]');
      if (!src || !dst) return;
      var rebuild = function () {
        var list = procOptionList(row.optsFrom, src.value);
        var cur = dst.value;
        fillSelect(dst, list, list.indexOf(cur) >= 0 ? cur : "");
      };
      src.addEventListener("change", rebuild);
      rebuild();
    });
  }

  function collectForm(box) {
    var out = {};
    box.querySelectorAll("[data-k]").forEach(function (c) {
      var k = c.dataset.k, t = c.dataset.t;
      if (t === "radio") {
        var on = c.querySelector("input:checked");
        if (on) out[k] = on.value;
      } else if (t === "check") {
        out[k] = c.checked;
      } else if (t === "checks") {
        var vals = [];
        c.querySelectorAll("input:checked").forEach(function (x) { vals.push(x.value); });
        if (vals.length) out[k] = vals;
      } else if (c.value !== "" && c.value != null) {
        out[k] = c.multiple ? Array.prototype.slice.call(c.selectedOptions)
                            .map(function (o) { return o.value; }) : c.value;
      }
    });
    return out;
  }

  function taskPreset(t) {
    return {
      name: t.name,
      owner_group: t.owner_group ? [t.owner_group] : [],
      priority: t.priority,
      owners: (t.owners || []).map(function (u) { return u.id; }),
      project: t.project_id || "",
      start_time: toLocalInput(t.start && t.start.ts),
      due_time: toLocalInput(t.due && t.due.ts),
      status: ["未开始", "进行中", "受阻", "待验收"].indexOf(t.status) >= 0 ? t.status : "",
      description: t.description || "",
    };
  }

  function openWriteForm(kind, preset, method, url) {
    closeWriteForm();
    var spec = WRITE_FORMS[kind];
    var ov = el("div", "p-modal"); ov.id = "p-modal";
    var box = el("div", "p-modal-box");
    var head = el("div", "p-modal-head");
    var title = (method === "PATCH" ? "编辑" : "") + spec.title;
    head.appendChild(el("b", null, title));
    var x = el("button", "p-modal-close", "×");
    x.type = "button"; x.addEventListener("click", closeWriteForm);
    head.appendChild(x);
    box.appendChild(head);
    var form = el("div", null);
    spec.rows.forEach(function (row) {
      /* 组长专属行（组长批复）：非组长不渲染，payload 也就不会带该键；
         图片选择仅新建用（编辑的图片增删走详情页字段更新卡） */
      if (row.leaderOnly && !isLeader()) return;
      if (row.t === "files" && method === "PATCH") return;
      form.appendChild(formControl(row, (preset || {})[row.k]));
    });
    wireLinkedSelects(form, spec);
    box.appendChild(form);
    var acts = el("div", "p-modal-actions");
    var ok = el("button", "big", method === "PATCH" ? "保存修改" : "创建并写回飞书");
    ok.type = "button";
    var msg = el("p", "p-note p-form-msg", "");
    msg.hidden = true;
    ok.addEventListener("click", function () {
      var payload = collectForm(form);
      /* 必填校验（服务端还会再校验一遍；未渲染的组长专属行跳过） */
      for (var i = 0; i < spec.rows.length; i++) {
        var r = spec.rows[i];
        if (r.leaderOnly && !isLeader()) continue;
        if (r.req && (payload[r.k] == null || payload[r.k] === "" ||
                      (payload[r.k] && !payload[r.k].length))) {
          msg.textContent = "※ 缺必填项：" + r.label;
          msg.hidden = false;
          return;
        }
      }
      ok.disabled = true; ok.textContent = "写入中…";
      fetch(url || spec.url, {
        method: method || spec.method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (r) {
        return r.json().then(function (j) { return { ok: r.ok, j: j }; });
      }).then(function (res) {
        ok.disabled = false;
        ok.textContent = method === "PATCH" ? "保存修改" : "创建并写回飞书";
        if (res.ok) {
          /* 新建采购时选了附件：创建成功后按各自目标列（图片补充 / 发票文件）逐份上传，
             失败不回滚记录本身，提示去详情页续传 */
          var rid = res.j && res.j.record_id;
          var jobs = [];
          if (spec.uploadAfter === "procurement" && method !== "PATCH" && rid) {
            form.querySelectorAll("input.p-file").forEach(function (inp) {
              Array.prototype.slice.call(inp.files || []).forEach(function (f) {
                jobs.push({ file: f, col: inp.dataset.col || "image" });
              });
            });
          }
          if (!jobs.length) { closeWriteForm(); afterWrite(); return; }
          ok.disabled = true;
          var fi = 0;
          (function upNext() {
            if (fi >= jobs.length) { closeWriteForm(); afterWrite(); return; }
            var job = jobs[fi++];
            ok.textContent = "上传附件 " + fi + "/" + jobs.length + "…";
            var fd = new FormData();
            fd.append("file", job.file, job.file.name);
            fetch("/api/portal/write/procurement/" + encodeURIComponent(rid) +
                  "/attachments?column=" + encodeURIComponent(job.col),
                  { method: "POST", body: fd })
              .then(function (r2) {
                return r2.json().then(function (j2) { return { ok: r2.ok, j: j2 }; });
              })
              .then(function (res2) {
                if (!res2.ok) {
                  closeWriteForm();
                  showDataNote("记录已创建，但附件『" + job.file.name + "』上传失败：" +
                    (res2.j.detail || "未知错误") + "——可在详情页继续上传", true);
                  fetch("/api/portal/sync", { method: "POST" }).catch(function () {});
                  setTimeout(function () { reload(true); }, 2000);
                  return;
                }
                upNext();
              })
              .catch(function () {
                closeWriteForm();
                showDataNote("记录已创建，但附件上传中断（网络错误）——可在详情页继续上传", true);
              });
          })();
          return;
        }
        msg.textContent = "※ 写入失败：" + (res.j.detail || "未知错误");
        msg.hidden = false;
      }).catch(function () {
        ok.disabled = false;
        ok.textContent = method === "PATCH" ? "保存修改" : "创建并写回飞书";
        msg.textContent = "※ 网络错误，未写入";
        msg.hidden = false;
      });
    });
    acts.appendChild(ok);
    var cancel = el("button", "p-mini", "取消");
    cancel.type = "button"; cancel.addEventListener("click", closeWriteForm);
    acts.appendChild(cancel);
    box.appendChild(acts);
    box.appendChild(msg);
    ov.appendChild(box);
    ov.addEventListener("click", function (e) {
      if (e.target === ov) closeWriteForm();
    });
    document.body.appendChild(ov);
  }

  function closeWriteForm() {
    var m = $("#p-modal");
    if (m) m.remove();
    /* 多选浮层挂在 body 上，关弹窗时一并清掉，避免残留在页面上 */
    document.querySelectorAll("body > .p-multi-panel").forEach(function (p) { p.remove(); });
    _multiPanels = [];
  }

  /* 写入成功：触发手动同步 + 延迟局部刷新；轮询兜底（15s） */
  function afterWrite() {
    showDataNote("已写入飞书，正在同步…页面稍后自动更新");
    fetch("/api/portal/sync", { method: "POST" }).catch(function () {});
    setTimeout(function () { reload(true); }, 2500);
  }

  /* 事项详情：字段更新卡（所有人可改提交验收时间与两态执行状态；
     组长另有"编辑全部字段"）。字段能否改以服务端判定为准。 */
  function taskEditCard(t) {
    var card = el("div", "p-card");
    var h = el("h2");
    h.appendChild(el("span", "tag", "UPDATE / 01"));
    h.appendChild(document.createTextNode("字段更新"));
    card.appendChild(h);
    var r1 = el("div", "p-edit-row");
    r1.appendChild(el("span", "who", "提交验收时间"));
    var dt = document.createElement("input");
    dt.type = "datetime-local"; dt.className = "p-dt";
    dt.value = toLocalInput(t.submit_time && t.submit_time.ts);
    r1.appendChild(dt);
    var sv = el("button", "p-mini", "保存");
    sv.type = "button";
    sv.addEventListener("click", function () {
      if (!dt.value) { showDataNote("请先选择提交验收时间", true); return; }
      patchTask(t.id, { submit_time: dt.value }, sv);
    });
    r1.appendChild(sv);
    card.appendChild(r1);
    var r2 = el("div", "p-edit-row");
    r2.appendChild(el("span", "who", "执行状态"));
    ["未开始", "进行中"].forEach(function (st) {
      var b = el("button", "p-mini" + (t.status === st ? " on" : ""), st);
      b.type = "button";
      b.addEventListener("click", function () { patchTask(t.id, { status: st }, b); });
      r2.appendChild(b);
    });
    card.appendChild(r2);
    if (state.me && state.me.is_leader) {
      /* 组长动作行：编辑与删除拉开间距，危险操作（删除）右置远离主操作 */
      var acts = el("div", "p-edit-actions");
      var eb = el("button", "p-create", "编辑全部字段（组长）");
      eb.type = "button";
      eb.addEventListener("click", function () {
        openWriteForm("task", taskPreset(t), "PATCH",
                      "/api/portal/write/tasks/" + encodeURIComponent(t.id));
      });
      acts.appendChild(eb);
      acts.appendChild(deleteControl(t.id, "tasks", "事项"));
      card.appendChild(acts);
    }
    return card;
  }

  function patchTask(id, fields, btn) {
    var old = btn.textContent;
    btn.disabled = true; btn.textContent = "写入中…";
    fetch("/api/portal/write/tasks/" + encodeURIComponent(id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      btn.disabled = false; btn.textContent = old;
      if (res.ok) afterWrite();
      else showDataNote("写入失败：" + (res.j.detail || "未知错误"), true);
    }).catch(function () {
      btn.disabled = false; btn.textContent = old;
      showDataNote("网络错误，未写入", true);
    });
  }

  /* 组长删除（两步确认防误删）：table='tasks'|'projects'，成功后回列表 */
  function deleteControl(id, table, label) {
    var del = el("button", "p-danger", "删除" + label);
    del.type = "button";
    var armed = false, timer = null;
    del.addEventListener("click", function () {
      if (!armed) {
        armed = true;
        del.textContent = "再点一次确认删除（不可恢复）";
        del.classList.add("armed");
        timer = setTimeout(function () {
          armed = false;
          del.textContent = "删除" + label;
          del.classList.remove("armed");
        }, 3000);
        return;
      }
      clearTimeout(timer);
      del.disabled = true;
      del.textContent = "删除中…";
      fetch("/api/portal/write/" + table + "/" + encodeURIComponent(id),
            { method: "DELETE" })
        .then(function (r) {
          return r.json().then(function (j) { return { ok: r.ok, j: j }; });
        })
        .then(function (res) {
          if (res.ok) {
            showDataNote("已删除，正在同步…");
            fetch("/api/portal/sync", { method: "POST" }).catch(function () {});
            setTimeout(function () {
              var back = document.querySelector(
                '.side-nav-item[href="/' + table + '"]');
              if (back) back.click();          /* 软导航回列表 */
              else location.href = "/" + table;
            }, 2200);
          } else {
            del.disabled = false;
            del.textContent = "删除" + label;
            del.classList.remove("armed");
            showDataNote("删除失败：" + (res.j.detail || "未知错误"), true);
          }
        })
        .catch(function () {
          del.disabled = false;
          del.textContent = "删除" + label;
          showDataNote("网络错误，未删除", true);
        });
    });
    return del;
  }

  /* ---------- 启动 ---------- */

  authArea();
  ensureRefreshButton();

  /* 页面装配（首次加载与软导航共用）：读当前路径 → 重算页码/详情 ID →
     按页装配筛选条并拉取数据。nav.js 软导航替换主区后会再次调用。 */
  function bootPage() {
    state.page = document.body.getAttribute("data-page") || "";
    state.pathId = decodeURIComponent(location.pathname.split("/")[2] || "");
    if (state.page === "login") return;
    if (!state.meta) {
      api("/api/portal/meta").then(function (m) { state.meta = m; }).catch(function () {});
    }
    /* 筛选条首帧即建（组/P/状态为静态项；负责人/项目先只渲染"全部"），
       选项加载完成后重建补全——否则首帧是空条，事后撑开把表格推下去 */
    if (!state.pathId) buildFilters();
    if (state.page === "procurement" && !state.pathId) buildProcFilters();
    if (state.page === "tasks" && !state.pathId) {
      ensureFilterOptions().then(function () {
        if (state.page === "tasks") buildFilters();
      });
    }
    if (state.page === "projects" && !state.pathId) buildProjectFilters();
    if (state.page === "schedule" && !state.pathId) buildScheduleToggle();
    decorateWrite();
    reload(false);
  }

  window.__portal = { bootPage: bootPage };   /* 软导航入口（nav.js 用） */
  /* 登录页只做身份区：不轮询管理 API（否则 15s 一次无效 401） */
  if (document.body.getAttribute("data-page") === "login") return;
  bootPage();
  pollLoop();
  countdownTick();
})();
