/* 发射调试·波形区：遥测录制 + 按数据类型分图的示波器（自绘 Canvas）。
   布局：右侧常显；每个勾选的数据类型一张图，图内三电机同量纲同尺度。
   线型区分电机（实线/长虚/点虚），目标值 = 同色半透明细线（随模式归属对应图）。
   由 shoot.js 每帧遥测调用 ShootDetail.record()；缓冲 60s @50Hz。 */
'use strict';

window.ShootDetail = (function () {

  const N = 3000;                 // 60s × 50Hz 环形缓冲长度
  const WINDOW_MS = 60000;
  const DRAW_INTERVAL = 66;       // 绘制节流 ≈15fps（多图下控主线程占用）

  const FIELDS = [
    {key: "rpm",         name: "转速", unit: "rpm", color: "#FAFA28"},
    {key: "deg",         name: "角度", unit: "°",   color: "#00D8D8"},
    {key: "current",     name: "电流", unit: "A",   color: "#FF2FA5"},
    {key: "torque",      name: "力矩", unit: "N·m", color: "#7FD9A0"},
    {key: "temperature", name: "温度", unit: "℃",  color: "#E0A458"},
    {key: "freq",        name: "射击频率", unit: "Hz", color: "#A78BFA"},
  ];
  const MOTOR_NAMES = ["M-1", "M-2", "M-3"];
  const DASH = [[], [7, 5], [2, 4]];
  const MODE_FIELD = {1: "rpm", 2: "deg", 3: "torque"};   // 模式 → 目标归属图

  /* ---------- 环形缓冲 ---------- */
  const times = new Float64Array(N);
  let head = 0, count = 0;
  const series = {};
  const seriesOf = (key) =>
    series[key] || (series[key] = new Float32Array(N).fill(NaN));
  const lastMode = [-1, -1, -1];

  let started = false;          // 默认关闭：手动开启后才录制绘制
  let latest = null;            // 最近一帧（数值总览始终使用，与录制开关无关）
  const zoomed = new Set();     // 用户放大的字段图（图表重建后恢复，不受联动影响）

  function record(t, targets, freqs) {
    if (!t || !t.slots) return;
    latest = {t, targets, freqs};           // 数值总览始终刷新
    if (!started) return;                   // 未开始绘图：不写缓冲
    times[head] = Date.now();
    for (let i = 0; i < 3; i++) {
      const s = t.slots[i];
      if (!s) continue;
      for (const f of FIELDS) {
        if (f.key === "freq") continue;             // 频率由外部计算传入
        seriesOf("m" + i + "_" + f.key)[head] = +s[f.key] || 0;
      }
      seriesOf("m" + i + "_freq")[head] =
        (freqs && Number.isFinite(freqs[i])) ? freqs[i] : NaN;
      const tg = targets && targets[i];
      const tArr = seriesOf("tgt" + i);
      if (tg && s.mode >= 1 && s.mode <= 3) {
        tArr[head] = (lastMode[i] !== -1 && lastMode[i] !== s.mode)
          ? NaN : (+tg.value || 0);
        lastMode[i] = s.mode;
      } else {
        tArr[head] = NaN;
        lastMode[i] = -1;
      }
    }
    head = (head + 1) % N;
    if (count < N) count++;
  }

  /* ---------- DOM ---------- */
  const $id = (i) => document.getElementById(i);
  let grid;

  /* 勾选状态快照（决定图集合，变化时重建） */
  function layoutKey() {
    return FIELDS.filter((f) => $id("dc-" + f.key) && $id("dc-" + f.key).checked)
                 .map((f) => f.key).join(",") + "|" +
           [1, 2, 3].filter((m) => $id("dc-m" + m).checked).join("") + "|" +
           ($id("dc-target").checked ? "T" : "-") + "|" +
           (latest && latest.t
             ? latest.t.slots.map((s) => s && s.mode).join("") : "000");
  }

  function rebuildCharts(key) {
    grid.innerHTML = "";
    for (const f of FIELDS) {
      if (!$id("dc-" + f.key).checked) continue;
      const box = document.createElement("div");
      box.className = "dchart";
      box.dataset.field = f.key;
      box.innerHTML =
        `<div class="dchart-title"><span class="sw" style="--c:${f.color}"></span>` +
        `${f.name} <i>(${f.unit})</i></div>` +
        `<canvas class="dchart-cv"></canvas>` +
        `<div class="dchart-legend"></div>`;
      if (zoomed.has(f.key)) box.classList.add("zoom");
      box.title = "单击放大 / 还原";
      box.addEventListener("click", () => {
        flipZoom(box, f.key);
      });
      grid.appendChild(box);
    }
    if (!grid.children.length) {
      grid.innerHTML = '<div class="hint inline" style="padding:24px 0">' +
        '勾选上方数据类型以显示波形图</div>';
    }
    return key;
  }

  /* ---------- 绘制 ---------- */
  /* ---------- 通用 FLIP：任意布局变化（放大/布局切换/窗口 resize）统一补间 ---------- */

  const REDUCE_MOTION = () =>
    matchMedia("(prefers-reduced-motion: reduce)").matches;

  function snapRects() {
    const m = new Map();
    for (const b of grid.querySelectorAll(".dchart")) m.set(b, b.getBoundingClientRect());
    return m;
  }

  /* 把所有图框从快照位置/尺寸平滑过渡到当前位置：
     位移的兄弟框只做 translate，尺寸变化的主框额外带 scale */
  function flipFrom(before) {
    if (!before || REDUCE_MOTION()) return;
    const moving = [];
    for (const b of grid.querySelectorAll(".dchart")) {
      const prev = before.get(b);
      if (!prev) continue;
      const now = b.getBoundingClientRect();
      const dx = prev.left - now.left, dy = prev.top - now.top;
      const sx = prev.width / now.width, sy = prev.height / now.height;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1 &&
          Math.abs(sx - 1) < 0.01 && Math.abs(sy - 1) < 0.01) continue;
      b.style.transition = "none";
      b.style.transformOrigin = "0 0";
      b.style.transform = "translate(" + dx + "px," + dy + "px) scale(" +
                          sx + "," + sy + ")";
      moving.push(b);
    }
    if (!moving.length) return;
    requestAnimationFrame(() => {
      for (const b of moving) {
        b.style.transition = "transform .28s cubic-bezier(.4,0,.2,1)";
        b.style.transform = "";
        const done = () => {
          b.style.transition = ""; b.style.transform = "";
          b.removeEventListener("transitionend", done);
        };
        b.addEventListener("transitionend", done);
        setTimeout(done, 450);              // 兜底清理
      }
    });
  }

  function flipZoom(box, fieldKey) {
    const before = snapRects();
    const willZoom = !box.classList.contains("zoom");
    box.classList.toggle("zoom", willZoom);
    if (willZoom) zoomed.add(fieldKey); else zoomed.delete(fieldKey);
    box.scrollIntoView({block: "nearest", behavior: "smooth"});
    flipFrom(before);
    if (started || count > 0) schedule();
  }

  /* ---------- 窗口 resize：首帧快照 + 停顿后统一补间净变化 ---------- */
  let resizeSnap = null, resizeTimer = 0;
  window.addEventListener("resize", () => {
    if (!grid || !grid.querySelector(".dchart")) return;
    if (!resizeSnap) resizeSnap = snapRects();
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const snap = resizeSnap;
      resizeSnap = null;
      flipFrom(snap);
    }, 240);
  });

  /* ---------- 布局密度控制：自动 / 1 列 / 2 列（记忆用户选择） ---------- */
  function applyLayout(cols) {
    grid.dataset.cols = cols;
    try { localStorage.setItem("dchart-cols", cols); } catch (e) {}
    for (const b of document.querySelectorAll(".seg-cols button"))
      b.classList.toggle("primary", b.dataset.cols === cols);
  }

  function initLayoutCtl() {
    const seg = document.querySelector(".seg-cols");
    if (!seg) return;
    let saved = "auto";
    try { saved = localStorage.getItem("dchart-cols") || "auto"; } catch (e) {}
    applyLayout(saved);
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-cols]");
      if (!b) return;
      const before = snapRects();
      applyLayout(b.dataset.cols);
      flipFrom(before);                     // 列数切换同样带移动补间
    });
  }

  let rafId = 0;
  let lastDraw = 0;
  let chartKey = "";

  function draw() {
    rafId = 0;
    if (!grid) return;
    const key = layoutKey();
    if (key !== chartKey) chartKey = rebuildCharts(key);

    const now = performance.now();
    if (now - lastDraw < DRAW_INTERVAL) { schedule(); return; }
    lastDraw = now;

    for (const box of grid.querySelectorAll(".dchart")) {
      drawOne(box, box.dataset.field);
    }
    schedule();
  }

  function drawOne(box, fieldKey) {
    const canvas = box.querySelector("canvas");
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    if (!W || !H) return;
    /* 背板尺寸用整数比较：分数缩放（如 125%）下 W*dpr 为小数，
       直接比较永不相等 → 每帧重设尺寸清屏 → 频闪 */
    const bw = Math.round(W * dpr), bh = Math.round(H * dpr);
    if (canvas.width !== bw || canvas.height !== bh) {
      canvas.width = bw; canvas.height = bh;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#0B0C0E";
    ctx.fillRect(0, 0, W, H);

    if (count < 2) {
      ctx.fillStyle = "rgba(255,255,255,0.3)";
      ctx.font = "400 12px " + getComputedStyle(canvas).fontFamily;
      ctx.textAlign = "center";
      ctx.fillText(started ? "等待遥测数据…" : "已停止（数据已冻结/为空）", W / 2, H / 2);
      return;
    }

    const tEnd = times[(head - 1 + N) % N];
    const tStart = tEnd - WINDOW_MS;
    const padL = 6, padR = 6, padT = 6, padB = 20;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const xOf0 = (t) => padL + (t - tStart) / WINDOW_MS * plotW;

    /* 水平网格 */
    ctx.strokeStyle = "rgba(255,255,255,0.07)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const fy of [0.25, 0.5, 0.75]) {
      ctx.moveTo(0, (padT + plotH) * fy); ctx.lineTo(W, (padT + plotH) * fy);
    }
    ctx.stroke();

    /* 时间轴：每 10s 竖网格 + 底轴线 + 刻度标签（明显加亮） */
    const endSec = Math.floor(tEnd / 1000);
    ctx.strokeStyle = "rgba(255,255,255,0.09)";
    ctx.beginPath();
    for (let s = Math.ceil(tStart / 10000) * 10; s <= endSec; s += 10) {
      const x = Math.round(xOf0(s * 1000)) + 0.5;
      ctx.moveTo(x, padT); ctx.lineTo(x, H - padB);
    }
    ctx.stroke();
    ctx.strokeStyle = "rgba(255,255,255,0.3)";
    ctx.beginPath();
    ctx.moveTo(0, H - padB + 0.5); ctx.lineTo(W, H - padB + 0.5);
    ctx.stroke();
    ctx.fillStyle = "rgba(255,255,255,0.6)";
    ctx.font = "400 10px " + getComputedStyle(canvas).fontFamily;
    ctx.textAlign = "center";
    for (let s = Math.ceil(tStart / 10000) * 10; s <= endSec; s += 10) {
      const rel = s - endSec;
      ctx.fillText(rel === 0 ? "0s" : rel + "s", xOf0(s * 1000), H - 6);
    }

    /* 该图的序列：勾选电机的该字段 + 目标（模式匹配时） */
    const items = [];
    for (let m = 0; m < 3; m++) {
      if (!$id("dc-m" + (m + 1)).checked) continue;
      items.push({key: "m" + m + "_" + fieldKey, motor: m, isTarget: false});
      if ($id("dc-target").checked && latest && latest.t.slots[m] &&
          MODE_FIELD[latest.t.slots[m].mode] === fieldKey)
        items.push({key: "tgt" + m, motor: m, isTarget: true});
    }
    if (!items.length) return;

    /* 图内公共 min/max（同量纲可比） */
    let mn = Infinity, mx = -Infinity;
    const field = FIELDS.find((f) => f.key === fieldKey);
    for (const it of items) {
      const arr = series[it.key];
      if (!arr) continue;
      for (let k = 0; k < count; k++) {
        const idx = (head - count + k + N * 2) % N;
        if (times[idx] < tStart) continue;
        const v = arr[idx];
        if (Number.isFinite(v)) {
          if (v < mn) mn = v;
          if (v > mx) mx = v;
        }
      }
    }
    if (!Number.isFinite(mn)) return;
    if (mx - mn < 1e-9) { mn -= 1; mx += 1; }
    const pad = (mx - mn) * 0.08;
    mn -= pad; mx += pad;
    const yOf = (v) => padT + (1 - (v - mn) / (mx - mn)) * plotH;
    const xOf = (t) => padL + (t - tStart) / WINDOW_MS * plotW;

    /* 抽点步长：每像素最多一个采样点 */
    const samples = Math.min(count, Math.round(WINDOW_MS / 20));
    const stride = Math.max(1, Math.floor(samples / plotW));

    for (const it of items) {
      const arr = series[it.key];
      if (!arr) continue;
      ctx.strokeStyle = field.color;
      ctx.globalAlpha = it.isTarget ? 0.45 : 1;
      ctx.lineWidth = it.isTarget ? 1.2 : 1.8;
      ctx.setLineDash(DASH[it.motor]);
      ctx.beginPath();
      let started = false;
      for (let k = 0; k < count; k += stride) {
        const idx = (head - count + k + N * 2) % N;
        if (times[idx] < tStart) continue;
        const v = arr[idx];
        if (!Number.isFinite(v)) { started = false; continue; }
        const x = xOf(times[idx]), y = yOf(v);
        started ? ctx.lineTo(x, y) : (ctx.moveTo(x, y), started = true);
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.setLineDash([]);
    }

    /* 图例：实时数值（每图独立 5Hz 节流） */
    const now = Date.now();
    if (!box._lastLegend) box._lastLegend = 0;
    if (now - box._lastLegend > 200) {
      box._lastLegend = now;
      const lg = box.querySelector(".dchart-legend");
      lg.innerHTML = items.map((it) => {
        const arr = series[it.key];
        let v = NaN;
        for (let k = 1; k <= count; k++) {
          const x = arr[(head - k + N * 2) % N];
          if (Number.isFinite(x)) { v = x; break; }
        }
        return `<span class="chip"><b>${MOTOR_NAMES[it.motor]}${it.isTarget ? " 目标" : ""}</b>` +
               ` ${Number.isFinite(v) ? (+v).toFixed(fieldKey === "temperature" ? 0 : 2) : "-"}` +
               ` <i>${field.unit}</i></span>`;
      }).join("");
    }
  }

  function schedule() {
    if (!rafId) rafId = requestAnimationFrame(draw);
  }

  /* ---------- CSV 导出（仅勾选序列） ---------- */
  function exportCsv() {
    const vis = [];
    for (let m = 0; m < 3; m++) {
      if (!$id("dc-m" + (m + 1)).checked) continue;
      for (const f of FIELDS)
        if ($id("dc-" + f.key).checked) vis.push("m" + m + "_" + f.key);
      if ($id("dc-target").checked) vis.push("tgt" + m);
    }
    if (!vis.length || count < 2) { alert("没有可导出的勾选数据"); return; }
    const tEnd = times[(head - 1 + N) % N];
    const colName = (k) => {
      const motor = +k.slice(k.search(/\d/), k.search(/\d/) + 1);
      if (k.startsWith("tgt")) return `Motor${motor + 1}_target`;
      const f = FIELDS.find((x) => k.endsWith("_" + x.key));
      return `Motor${motor + 1}_${f.name}(${f.unit})`;
    };
    const esc = (s) => '"' + String(s).replace(/"/g, '""') + '"';
    const lines = [["t_s"].concat(vis.map(colName)).map(esc).join(",")];
    for (let k = 0; k < count; k++) {
      const idx = (head - count + k + N * 2) % N;
      const row = [((times[idx] - tEnd) / 1000).toFixed(3)];
      for (const key of vis) {
        const v = series[key] ? series[key][idx] : NaN;
        row.push(Number.isFinite(v) ? String(v) : "");
      }
      lines.push(row.join(","));
    }
    const blob = new Blob(["\ufeff" + lines.join("\n")],
                          {type: "text/csv;charset=utf-8"});
    const a = document.createElement("a");
    const d = new Date(), p = (n) => String(n).padStart(2, "0");
    a.href = URL.createObjectURL(blob);
    a.download = `shoot_${window.deviceCtx?.id || "legacy-default"}_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}` +
                 `_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  /* ---------- 数值总览（始终显示全部字段，不受勾选影响） ---------- */
  const numWrap = {root: null, cells: {}};

  function buildNumGrid() {
    const host = document.getElementById("numgrid");
    if (!host) return;
    numWrap.root = host;
    let html = '<div class="numrow numhead"><span class="numcell"></span>' +
      FIELDS.map((f) => '<span class="numcell"><span class="sw" style="--c:' +
        f.color + '"></span>' + f.name + '</span>').join("") + '</div>';
    for (let m = 0; m < 3; m++) {
      html += '<div class="numrow"><span class="numcell nummotor">M-' + (m + 1) +
        '</span>' + FIELDS.map((f) =>
          '<span class="numcell numval" data-f="' + f.key + '" data-m="' + m +
          '"><span class="v">-</span><span class="u">' + f.unit + '</span></span>')
          .join("") + '</div>';
    }
    host.innerHTML = html;
    numWrap.cells = {};
    for (const el of host.querySelectorAll(".numval .v"))
      numWrap.cells[el.closest(".numval").dataset.f +
                    el.closest(".numval").dataset.m] = el;
  }

  function fmtNum(fkey, v) {
    if (v === null || v === undefined || !Number.isFinite(v)) return "-";
    if (fkey === "temperature") return (+v).toFixed(0);
    if (fkey === "freq") return (+v).toFixed(1);
    return (+v).toFixed(2);
  }

  function refreshNumGrid() {
    if (!numWrap.root) return;
    for (const key in numWrap.cells) {
      const el = numWrap.cells[key];          // el 是 .v 子 span，data 在父级 .numval 上
      const cell = el.closest(".numval");
      const fkey = cell.dataset.f, m = +cell.dataset.m;
      let v = NaN;
      if (latest && latest.t && latest.t.slots) {
        v = fkey === "freq"
          ? (latest.freqs ? latest.freqs[m] : NaN)
          : latest.t.slots[m][fkey];
      }
      el.textContent = fmtNum(fkey, v);
    }
  }

  /* 离线（会话断开）：清空数值总览 */
  function offline() {
    latest = null;
    refreshNumGrid();
  }

  /* ---------- 初始化 ---------- */
  function init() {
    grid = $id("detail-charts");
    if (!grid) return;
    const startBtn = $id("dc-start");
    startBtn.addEventListener("click", () => {
      started = !started;
      startBtn.textContent = started ? "停止绘图" : "开始绘图";
      startBtn.classList.toggle("primary", !started);
      if (started) schedule();
      else if (count > 0) draw();   // 停止后保留冻结画面（仍可导出）
    });
    $id("dc-clear").addEventListener("click", () => {
      head = 0; count = 0; lastMode.fill(-1);
      for (const k in series) series[k].fill(NaN);
      for (const box of grid.querySelectorAll(".dchart")) drawOne(box, box.dataset.field);
    });
    $id("dc-export").addEventListener("click", exportCsv);
    document.getElementById("detail-panel")
      .addEventListener("change", () => { if (started || count > 0) schedule(); });
    buildNumGrid();
    initLayoutCtl();
    setInterval(refreshNumGrid, 200);      // 数值总览独立于绘图开关
    if (count === 0)
      grid.innerHTML = '<div class="hint inline" style="padding:24px 0">' +
        '点击「开始绘图」开始录制并显示波形（默认关闭）</div>';
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else
    init();

  return {record, offline};
})();
