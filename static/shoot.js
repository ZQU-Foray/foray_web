/* 发射机构电机调试（原独立页 shoot.html 并入主页，协议与交互逻辑保持一致）
   新增：RTT 徽章周期调用 /api/shoot/ping；样式统一走主页 CSS 变量主题 */
'use strict';

(function () {
  const MODES = ["停止（无输出）", "转速 (rpm)", "位置 (deg)", "力矩 (N·m)"];
  const FAULTS = {0:"离线",1:"过温警告",2:"过温",3:"过流",4:"参数被拒",5:"链路丢失",6:"急停"};
  const LATCHED_FAULTS = 0x40 | 0x04 | 0x08; // 急停|过温|过流（锁定类，使能前必须清）
  const PARAMS = [
    ["kp_speed","Kp 速度"],["ki_speed","Ki 速度"],["kd_speed","Kd 速度"],
    ["kf_speed","Kf 速度"],["il_speed","积分限幅"],["dz_speed","死区(rpm)"],
    ["kp_angle","Kp 角度"],["ki_angle","Ki 角度"],["kd_angle","Kd 角度"],
    ["kf_angle","Kf 角度"],["il_angle","积分限幅A"],["dz_angle","死区(deg)"],
  ];
  let profiles = [], defaults = {}, ws = null, wsWant = false,
      serverOpen = false, shootTelem = null;

  const $id = (i) => document.getElementById(i);
  const val = (id) => $id(id).value;

  function toast(msg, err) {
    const d = document.createElement("div");
    d.className = "toast-item" + (err ? " err" : "");
    d.textContent = msg;
    $id("toast").appendChild(d);
    setTimeout(() => d.remove(), 4200);
  }

  /* quiet=true 时不弹 toast（供周期轮询静默失败，避免刷屏） */
  async function api(path, body, quiet) {
    try {
      const opt = body !== undefined ? {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)} : undefined;
      const generation = deviceCtx.generation;
      const base = deviceCtx.api("shoot");
      const r = await fetch(base + "/" + path, opt);
      const j = await r.json();
      if (generation !== deviceCtx.generation) throw new Error("设备已切换，忽略旧响应");
      if (!r.ok) throw new Error(j.detail || r.status);
      return j;
    } catch (e) {
      if (!quiet) toast((({open: "连接串口", close: "断开连接", status: "读取状态"})[path] || path) + "失败：" + e.message, true);
      throw e;
    }
  }

  /* ---------- 面板构建（事件用 data-act 委托，避免全局函数） ---------- */

  function buildPanels() {
    const host = $id("motor-panels");
    host.innerHTML = "";
    for (let s = 0; s < 3; s++) {
      const p = document.createElement("div");
      p.className = "motor-card mcollapse";
      p.dataset.slot = s;
      p.innerHTML = `
        <button class="mcol-head" type="button" data-act="mcol">
          <span class="mtag">M-${s + 1}</span><span class="mcol-title">Motor ${s + 1}</span>
          <span class="fault-tags" id="faults${s}"></span>
          <span class="badge dim mcol-en" id="headen${s}" title="使能状态">失能</span>
          <span class="chev" aria-hidden="true">▾</span>
        </button>
        <div class="mcol-body"><div class="mcol-inner">
          <div class="mrow">
            <span class="mgroup"><label>型号</label>
              <select id="model${s}" class="sel-model"></select></span>
            <span class="mgroup"><label>模式</label>
              <select id="mode${s}" class="sel-mode"></select></span>
          </div>
          <div class="mrow tgt-row" id="tgtrow${s}" style="display:none">
            <label id="tgtlabel${s}">目标</label>
            <span class="mgroup tgt-ctl">
              <span class="tgt-wrap">
                <input type="range" class="tgt-slider" id="tslider${s}" step="1"
                       title="拖动实时下发 · 双击回零">
                <span class="tgt-lims">
                  <span class="tgt-lim" id="tgtmin${s}">-</span>
                  <span class="tgt-lim" id="tgtmax${s}">+</span>
                </span>
              </span>
              <input type="number" class="tgt-val" id="tval${s}" step="any">
              <span class="tgt-unit" id="tgtunit${s}"></span>
              <button data-act="reset" class="tgt-reset" title="目标复位为 0 并下发">复位 0</button>
            </span>
          </div>
          <div class="mrow">
            <button data-act="apply">应用全部</button>
            <button data-act="enable" id="en${s}">使能电机</button>
            <button data-act="faultclear">清故障</button>
          </div>
          <div class="mrow">
            <label>预设</label>
            <span class="mgroup">
              <select id="preset${s}" class="sel-preset"></select>
              <button data-act="psave" title="把当前表单参数保存为预设（存于 Web 服务器）">保存</button>
              <button data-act="pdel" title="删除所选预设">删除</button>
              <button data-act="pdef" title="把所选预设设为默认（连接/换型号时自动应用）">默认</button>
            </span>
          </div>
          <div class="subcol">
            <button class="subcol-head" type="button" data-act="subcol">
              控制参数<span class="hint inline">（PID ${PARAMS.length} 项）</span>
              <span class="chev" aria-hidden="true">▸</span></button>
            <div class="subcol-body"><div class="subcol-inner">
              ${PARAMS.map(([k, n]) => `
              <div class="mrow"><label>${n}</label>
                <input type="number" id="${k}${s}" step="any">
                <button data-act="param" data-key="${k}" data-src="${k}${s}">→</button>
              </div>`).join("")}
            </div></div>
          </div>
          <div class="subcol">
            <button class="subcol-head" type="button" data-act="subcol">
              机械参数<span class="chev" aria-hidden="true">▸</span></button>
            <div class="subcol-body"><div class="subcol-inner">
              <div class="mrow">
                <span class="mgroup"><label>减速比</label>
                  <input type="number" id="gear${s}" step="0.1" min="1">
                  <button data-act="param" data-key="gear_ratio" data-src="gear${s}">→</button></span>
              </div>
              <div class="mrow" id="poleRow${s}">
                <span class="mgroup"><label>槽数</label>
                  <input type="number" id="pole${s}" step="1" min="1" value="8"
                         title="电机绕组槽数，用于计算电气频率"></span>
              </div>
              <div class="mrow" id="freqRow${s}">
                <label>射击频率</label>
                <b class="freq" id="freq${s}">-</b>
                <span class="hint inline">= 转速 × 槽数 / 60</span>
              </div>
            </div></div>
          </div>
          <div class="telem">
            <div>转速 <b id="tr${s}">-</b></div><div>角度 <b id="td${s}">-</b></div>
            <div>电流 <b id="tc${s}">-</b></div><div>力矩 <b id="tq${s}">-</b></div>
            <div>温度 <b id="tt${s}">-</b></div><div>在线 <b id="to${s}">-</b></div>
            <div>使能 <b id="ten${s}">-</b></div>
          </div>
          </div></div>`;
      host.appendChild(p);
      const ms = p.querySelector("#mode" + s);
      MODES.forEach((m, i) => ms.add(new Option(m, i)));
    }
    host.querySelectorAll('select[id^="model"]').forEach(
      sel => profiles.forEach(pr => sel.add(new Option(pr.name, pr.id))));
  }

  function fillDefaults() {
    for (let s = 0; s < 3; s++) {
      for (const k in defaults) {
        const el = $id(k + s);
        if (el && document.activeElement !== el) el.value = defaults[k];
      }
      const g = $id("gear" + s);
      const pr = profiles[+$id("model" + s).value];
      if (g && pr) g.value = pr.gear_ratio;
      /* 各模式目标初值（对应旧输入框的默认填充） */
      tgtState[s].rpm = defaults.target_rpm || 0;
      tgtState[s].deg = defaults.target_deg || 0;
      tgtState[s].torque = defaults.target_torque || 0;
      updateTargetUI(s);
    }
  }

  /* ---------- 目标滑杆：按模式显隐 + 按型号/固件上限自动限幅 ---------- */

  const tgtState = [   // 每槽位保存三种目标的历史值，切模式互不丢失
    {rpm: 0, deg: 0, torque: 0},
    {rpm: 0, deg: 0, torque: 0},
    {rpm: 0, deg: 0, torque: 0},
  ];
  const DEG_LIMIT = 3600;   // 与固件 kMaxTargetDegLimit 一致
  const uiCache = {};       // 槽位 → "mode-model" 缓存，遥测变化时才刷新滑杆 UI
  let lastLinkSeen = -1;    // 上帧 link 位（-1 未知）
  let lastTelemAt = 0;      // 上帧遥测到达时刻（检测遥测中断）

  /* ---------- 电机调参预设（按 槽位×型号 命名空间，Web 侧存储） ---------- */

  let motorPresets = null;            // 服务器预设文档
  const presetAutoApplied = {};       // "slot-model" → 本会话已自动应用过默认预设
  let autoAppliedOnConnect = false;   // 本次连接已执行过默认预设自动应用

  async function loadMotorPresets() {
    try {
      const generation = deviceCtx.generation;
      const r = await fetch(deviceCtx.api("shoot/presets"));
      const doc = await r.json();
      if (r.ok && generation === deviceCtx.generation) motorPresets = doc;
    } catch (e) { /* 静默，下次刷新再试 */ }
  }

  function pCell(s, model) {
    return (motorPresets && motorPresets.motor_presets
            && motorPresets.motor_presets[s]
            && motorPresets.motor_presets[s][model]) || null;
  }

  function ensureCell(s, model) {
    if (!motorPresets) motorPresets = {motor_presets: {}};
    const mp = motorPresets.motor_presets;
    if (!mp[s]) mp[s] = {};
    if (!mp[s][model]) mp[s][model] = {"presets": [], "default": -1};
    return mp[s][model];
  }

  async function persistPresets() {
    try {
      const generation = deviceCtx.generation;
      const r = await fetch(deviceCtx.api("shoot/presets"), {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(motorPresets),
      });
      if (!r.ok) throw new Error((await r.json()).detail || r.status);
      const doc = await r.json();
      if (generation !== deviceCtx.generation) return false;
      motorPresets = doc;   // 采纳服务器规整后的文档
      return true;
    } catch (e) {
      toast("预设保存失败: " + e.message, true);
      await loadMotorPresets();        // 失败则回读，避免本地与服务器漂移
      return false;
    }
  }

  /* 重建预设下拉（跟随当前型号；默认预设带 ★ 前缀） */
  function refreshPresetUI(s) {
    const sel = $id("preset" + s);
    if (!sel) return;
    const cell = pCell(s, $id("model" + s).value);
    sel.innerHTML = "";
    sel.add(new Option("（选择预设）", "-1"));
    if (cell) cell.presets.forEach((p, i) =>
      sel.add(new Option((cell.default === i ? "★ " : "") + p.name, String(i))));
  }

  /* 应用预设：填表 + 逐项下发 + 生效 */
  async function applyPreset(s, idx, quiet) {
    const cell = pCell(s, $id("model" + s).value);
    if (!cell || idx < 0 || idx >= cell.presets.length) return false;
    const p = cell.presets[idx];
    const params = p.params || {};
    let n = 0;
    for (const k in params) {
      const el = $id(k + s);
      if (el) el.value = params[k];
      const r = await api("param", {slot: s, key: k, value: params[k]}, true);
      if (r.ok) n++;
    }
    await api("apply", {slot: s}, true);
    if (!quiet) toast(`Motor${s + 1} 已应用预设「${p.name}」（${n} 项）`);
    return true;
  }

  /* 默认预设自动应用：连接时与换型号时触发一次 */
  async function maybeAutoPreset(s, model, quiet) {
    const key = s + "-" + model;
    if (presetAutoApplied[key]) return;
    const cell = pCell(s, String(model));
    if (!cell || cell.default < 0 || cell.default >= cell.presets.length) return;
    presetAutoApplied[key] = true;
    await applyPreset(s, cell.default, quiet);
    toast(`Motor${s + 1} 已自动应用默认预设「${cell.presets[cell.default].name}」`);
  }

  async function savePreset(s) {
    const model = $id("model" + s).value;
    const cell = ensureCell(s, model);
    if (cell.presets.length >= 5) {
      toast(`Motor${s + 1} 此型号预设已满 5 套，请先删除`, true);
      return;
    }
    const mname = (profiles[+model] || {}).name || model;
    const name = prompt(`预设名称（Motor${s + 1} / ${mname}）：`,
                        `预设${cell.presets.length + 1}`);
    if (name === null) return;                      // 取消
    const params = {};
    const gr = +val("gear" + s);
    if (isFinite(gr) && $id("gear" + s).value !== "") params.gear_ratio = gr;
    for (const [k] of PARAMS) {
      const v = +val(k + s);
      if (isFinite(v) && $id(k + s).value !== "") params[k] = v;
    }
    cell.presets.push({name: (name.trim() || `预设${cell.presets.length + 1}`).slice(0, 24),
                       params});
    if (await persistPresets()) {
      refreshPresetUI(s);
      $id("preset" + s).value = String(cell.presets.length - 1);
      toast(`Motor${s + 1} 已保存预设「${cell.presets[cell.presets.length - 1].name}」`);
    }
  }

  async function deletePreset(s) {
    const idx = +val("preset" + s);
    const cell = pCell(s, $id("model" + s).value);
    if (!cell || idx < 0 || idx >= cell.presets.length) {
      toast("请先选择要删除的预设", true);
      return;
    }
    const name = cell.presets[idx].name;
    if (!confirm(`删除 Motor${s + 1} 预设「${name}」？`)) return;
    cell.presets.splice(idx, 1);
    if (cell.default >= cell.presets.length) cell.default = -1;
    else if (cell.default > idx) cell.default -= 1;
    else if (cell.default === idx) cell.default = -1;
    if (await persistPresets()) {
      refreshPresetUI(s);
      toast(`已删除预设「${name}」`);
    }
  }

  async function toggleDefault(s) {
    const idx = +val("preset" + s);
    const cell = pCell(s, $id("model" + s).value);
    if (!cell || idx < 0 || idx >= cell.presets.length) {
      toast("请先选择要设为默认的预设", true);
      return;
    }
    cell.default = (cell.default === idx) ? -1 : idx;
    if (await persistPresets()) {
      refreshPresetUI(s);
      $id("preset" + s).value = String(idx);
      toast(cell.default === idx
        ? `「${cell.presets[idx].name}」已设为默认（连接/换型号自动应用）`
        : `已取消默认预设`);
    }
  }

  function tgtLimit(s, kind) {
    const pr = profiles[+$id("model" + s).value] || {};
    if (kind === "rpm") return pr.max_output_rpm || 500;
    if (kind === "deg") return DEG_LIMIT;
    return pr.max_torque || 1.5;
  }

  /* 依据当前模式刷新滑杆行：显隐、标签、单位、上下限、步进 */
  function updateTargetUI(s) {
    const mode = +val("mode" + s);
    const row = $id("tgtrow" + s);
    if (!row) return;
    if (mode === 0) { row.style.display = "none"; return; }   // 停止模式无目标
    row.style.display = "";
    const kind = {1: "rpm", 2: "deg", 3: "torque"}[mode];
    const meta = {
      rpm:  ["目标转速", "rpm",  1],
      deg:  ["目标角度", "deg",  5],
      torque: ["目标力矩", "N·m", 0.01],
    }[kind];
    const lim = tgtLimit(s, kind);
    $id("tgtlabel" + s).textContent = meta[0];
    $id("tgtunit" + s).textContent = meta[1];
    $id("tgtmin" + s).textContent = (kind === "torque" ? -lim : -lim);
    $id("tgtmax" + s).textContent = lim;
    const slider = $id("tslider" + s);
    slider.min = -lim; slider.max = lim; slider.step = meta[2];
    slider.value = tgtState[s][kind];
    const tv = $id("tval" + s);
    tv.step = meta[2];
    tv.value = tgtState[s][kind];
  }

  /* 机械参数区：槽数/电气频率仅对 M2006（model=0）开放 */
  function updateMechUI(s) {
    const is2006 = +val("model" + s) === 0;
    const poleRow = $id("poleRow" + s), freqRow = $id("freqRow" + s);
    if (poleRow) poleRow.style.display = is2006 ? "" : "none";
    if (freqRow) freqRow.style.display = is2006 ? "" : "none";
  }

  /* 下发当前滑杆目标：钳入固件合法范围（越界会被固件拒绝）。
     quiet=true 时不弹 toast（拖动过程中的节流推送静默，仅失败才提示） */
  async function sendTarget(s, quiet) {
    const mode = +val("mode" + s);
    const kind = {1: "rpm", 2: "deg", 3: "torque"}[mode];
    if (!kind) return;
    const lim = tgtLimit(s, kind);
    let v = +val("tval" + s);
    if (!isFinite(v)) v = 0;
    v = Math.max(-lim, Math.min(lim, Math.round(v * 1000) / 1000));
    tgtState[s][kind] = v;
    $id("tval" + s).value = v;
    $id("tslider" + s).value = v;
    const r = await api("param", {slot: s, key: "target_" + kind, value: v}, quiet);
    if (!r.ok) toast(`Motor${s + 1} 目标下发失败: ${r.status}`, true);
    else await api("apply", {slot: s}, quiet);
    return r.ok ? v : null;
  }

  /* 拖动节流推送：150ms 间隔（前沿立即发 + 尾沿兜底），
     115200 波特率下 ≈ 230B/s 下行，与 50Hz 遥测共存带宽充裕，无需固件改动 */
  const pushState = {};   // 槽位 → {last, timer}
  function pushThrottled(s) {
    const st = pushState[s] || (pushState[s] = { last: 0, timer: null });
    const now = Date.now();
    if (now - st.last >= 150) {
      st.last = now;
      if (st.timer) { clearTimeout(st.timer); st.timer = null; }
      sendTarget(s, true);
    } else if (!st.timer) {
      st.timer = setTimeout(() => {
        st.timer = null;
        st.last = Date.now();
        sendTarget(s, true);
      }, 150 - (now - st.last));
    }
  }

  /* 目标复位 0 并立即下发 */
  async function resetTarget(s) {
    $id("tslider" + s).value = 0;
    $id("tval" + s).value = 0;
    const v = await sendTarget(s, true);
    if (v !== null) toast(`Motor${s + 1} 目标已复位 0`);
  }

  const kindName = (kind) => ({rpm: "转速", deg: "角度", torque: "力矩"}[kind]);

  /* ---------- 状态渲染（按钮真值 = 遥测 enabled 位） ---------- */

  /* rAF 合帧：遥测 50Hz 到达时每帧只渲染一次屏幕刷新率，
     避免 WS 每消息直接操作 DOM 造成主线程过载（跑马灯等动画卡顿） */
  let pendingTelem = null;
  let paintQueued = false;
  function schedulePaint(t) {
    pendingTelem = t;
    /* 详细模式录制：每帧遥测全量入环形缓冲（未开启面板也在录，展开即有历史） */
    if (window.ShootDetail) {
      const tg = t.slots.map((sl, i) => {
        const kind = {1: "rpm", 2: "deg", 3: "torque"}[sl.mode];
        return kind ? {kind, value: tgtState[i][kind]} : null;
      });
      /* 射击频率 = |实际转速| × 槽数 / 60（仅 M2006 提供槽数） */
      const fq = t.slots.map((sl, i) => {
        if (sl.model !== 0) return NaN;
        const poles = +val("pole" + i);
        return (isFinite(poles) && poles > 0)
          ? Math.abs(sl.rpm) * poles / 60 : NaN;
      });
      ShootDetail.record(t, tg, fq);
    }
    if (!paintQueued) {
      paintQueued = true;
      requestAnimationFrame(() => {
        paintQueued = false;
        if (pendingTelem) paint(pendingTelem);
      });
    }
  }

  const faultCache = {};   // 槽位 → 上次故障位（无变化则跳过重建标签）

  function paint(t) {
    shootTelem = t;
    t.slots.forEach((s, i) => {
      $id("tr" + i).textContent = s.rpm.toFixed(1) + " rpm";
      $id("td" + i).textContent = s.deg.toFixed(1) + "°";
      $id("tc" + i).textContent = s.current.toFixed(2) + " A";
      $id("tq" + i).textContent = s.torque.toFixed(2) + " N·m";
      $id("tt" + i).textContent = s.temperature + " ℃";
      $id("to" + i).textContent = (s.fault & 1) ? "否" : "是";
      $id("ten" + i).textContent = s.enabled ? "是" : "否";
      if (faultCache[i] !== s.fault) {         // 故障位变化才重建标签
        faultCache[i] = s.fault;
        const f = $id("faults" + i);
        f.dataset.offline = "0";                // 遥测接管显示，清离线标记
        f.innerHTML = "";
        for (const [bit, name] of Object.entries(FAULTS))
          if (s.fault & (1 << bit)) {
            const sp = document.createElement("span");
            if (bit == 0) sp.className = "off";          // 离线 = 灰色，非报警
            else if (bit == 1 || bit == 3 || bit == 4) sp.className = "warn";
            sp.textContent = name;
            f.appendChild(sp);
          }
      }
      const modeSel = $id("mode" + i);
      if (document.activeElement !== modeSel) modeSel.value = s.mode;
      const modelSel = $id("model" + i);
      if (document.activeElement !== modelSel) modelSel.value = s.model;
      /* 模式/型号变化时刷新滑杆与机械参数区（显隐与上下限跟随），每帧仅比较缓存 */
      const key = s.mode + "-" + s.model;
      if (uiCache[i] !== key) { uiCache[i] = key; updateTargetUI(i); updateMechUI(i); }
      const en = $id("en" + i);
      const running = s.enabled == 1;                 // 按钮状态只看真实使能位
      en.textContent = running ? "失能电机" : "使能电机";
      en.classList.toggle("enable", running);
      /* 折叠头部徽章：使能状态一目了然（收起时也可见） */
      const headEn = $id("headen" + i);
      headEn.textContent = running ? "使能中" : "失能";
      headEn.className = "badge mcol-en " + (running ? "running" : "dim");
      /* 射击频率 = 实际转速 × 槽数 / 60（仅 M2006 提供槽数输入） */
      const freqEl = $id("freq" + i);
      if (freqEl) {
        const poles = +val("pole" + i);
        freqEl.textContent = (isFinite(poles) && poles > 0)
          ? (Math.abs(s.rpm) * poles / 60).toFixed(1) + " Hz" : "-";
      }
    });
    setLink(t.link === 1);

    /* 链路恢复检测：遥测中断 >2.5s 或 link 位 0→1 → MCU 可能重启过，
       清除自动应用标记并重推各槽默认预设（link_lost 期间固件已闭锁输出，安全） */
    const now = Date.now();
    const telemGap = lastTelemAt ? now - lastTelemAt : 0;
    const recovered = t.link === 1 && lastLinkSeen !== -1 &&
                      (lastLinkSeen === 0 || telemGap > 2500);
    lastTelemAt = now;
    lastLinkSeen = t.link;
    if (recovered && wsWant) {
      for (const k of Object.keys(presetAutoApplied)) delete presetAutoApplied[k];
      for (let i = 0; i < 3; i++) maybeAutoPreset(i, $id("model" + i).value, true);
    }
  }

  /* 无遥测（未连接/无数据）时：所有电机显式标记离线并复位展示，避免空白误读 */
  function paintOffline() {
    for (let i = 0; i < 3; i++) {
      const f = $id("faults" + i);
      if (f) {
        /* 幂等直写（不用跳过守卫：排队中的 rAF 遥测帧可能覆盖标签，
           守卫会造成"上线过的电机停留在空标签"的陈旧窗口） */
        f.dataset.offline = "1";
        f.innerHTML = "";
        const sp = document.createElement("span");
        sp.className = "off";
        sp.textContent = "离线";
        f.appendChild(sp);
      }
      delete faultCache[i];                 // 恢复遥测时强制重建故障标签
      const headEn = $id("headen" + i);
      if (headEn) { headEn.textContent = "失能"; headEn.className = "badge mcol-en dim"; }
      const en = $id("en" + i);
      if (en) { en.textContent = "使能电机"; en.classList.remove("enable"); }
      for (const k of ["tr", "td", "tc", "tq", "tt", "to", "ten"]) {
        const el = $id(k + i);
        if (el) el.textContent = "-";
      }
      const fr = $id("freq" + i);
      if (fr) fr.textContent = "-";
    }
    if (window.ShootDetail) ShootDetail.offline();   // 数值总览清空
  }

  /* 链路徽章四态：未连串口 / 已连串口但无遥测 / 遥测 link=1 / 遥测 link=0 */
  function setLink(up) {
    const b = $id("link-badge");
    b.textContent = "链路: " + (up ? "正常" : "断开");
    b.className = "badge " + (up ? "ok" : "err");
    b.title = "MCU 遥测的 link 位为 0：主机保活未在窗口内到达";
  }
  function setLinkOff() {
    const b = $id("link-badge");
    b.textContent = deviceCtx.isTcp ? "TCP 串口：未连接" : "串口：未连接";
    b.className = "badge dim";
    b.title = "服务器端串口会话未打开";
  }
  function setLinkWait() {
    const b = $id("link-badge");
    b.textContent = (deviceCtx.isTcp ? "TCP 串口已连接" : "串口已连接") + " · 等待 MCU 遥测…";
    b.className = "badge running";
    b.title = deviceCtx.isTcp
      ? "TCP 已连接，尚未收到有效 MCU 遥测。请检查 Basic2 的 TCP-UART 是否开启、实际波特率是否与 MCU 一致，以及 TXD/RXD/GND 接线和目标固件。"
      : "串口已连接，尚未收到有效 MCU 遥测。请检查 AP/STA 配对、UART 接线及目标固件。";
  }

  /* RTT 徽章：v=数值正常，false=Pong 超时，null=会话未开/已断开 */
  function setRtt(v) {
    const b = $id("rtt-badge");
    if (v === null) { b.textContent = "RTT: -"; b.className = "badge dim"; }
    else if (v === false) { b.textContent = "RTT: 超时"; b.className = "badge err"; }
    else { b.textContent = "RTT: " + (+v).toFixed(1) + " ms"; b.className = "badge ok"; }
  }

  /* ---------- 周期状态（含 WS 自动挂载/重连） ---------- */

  async function refresh() {
    if (!deviceCtx.id) return;
    try {
      const st = await api("status", undefined, true);
      serverOpen = st.open;
      if (st.profiles && !profiles.length) {
        profiles = st.profiles;
        defaults = st.defaults || {};
        $id("motor-panels").querySelectorAll('select[id^="model"]').forEach(
          sel => profiles.forEach(pr => sel.add(new Option(pr.name, pr.id))));
        fillDefaults();
        for (let i = 0; i < 3; i++) refreshPresetUI(i);   // 型号选项就位后重建预设列表
      }
      // 服务器会话已开 → 页面（含刷新后）自动挂载 WS，UI 与服务端一致
      if (serverOpen && wsWant && !ws) connectWs();
      if (!serverOpen) { wsWant = false; if (ws) { ws.close(); ws = null; } }
      if (!st.open) setLinkOff();
      else if (!st.telemetry) setLinkWait();   // 串口开但零遥测 = MCU/无线侧问题
      else setLink(st.telemetry.link === 1);
      /* 会话未开时无视 telemetry（服务端可能残留最后一帧陈旧数据），
         只有 open 且有遥测才交回实时渲染 */
      if (st.telemetry && st.open) {
        for (let i = 0; i < 3; i++) {          // 清离线标记，交回遥测渲染
          const f = $id("faults" + i);
          if (f) f.dataset.offline = "0";
        }
        schedulePaint(st.telemetry);
      } else {
        pendingTelem = null;                   // 掐断排队 rAF 帧的数据源（竞态源头）
        paintOffline();                        // 全部显式离线（幂等）
      }
      /* 连接建立且预设就绪后：各槽位按当前型号自动应用默认预设（每会话一次） */
      if (wsWant && st.open && st.telemetry && profiles.length && motorPresets
          && !autoAppliedOnConnect) {
        autoAppliedOnConnect = true;
        for (let i = 0; i < 3; i++)
          maybeAutoPreset(i, st.telemetry.slots[i].model, true);
      }
    } catch (e) { /* 静默 */ }
  }

  /* RTT 周期测量：仅链路正常（收到遥测且 link=1）时才 ping，
     避免链路断开时每 2s 打一次必然超时的请求（服务器日志刷 400） */
  async function rttLoop() {
    if (serverOpen) {
      const linkUp = !!(shootTelem && shootTelem.link === 1);
      if (linkUp) {
        try {
          const result = await api("ping", {}, true);
          setRtt(result.rtt_ms);
        } catch (e) {
          setRtt(false); // Pong 超时或请求失败
        }
      } else {
        setRtt(null);   // 链路未通：RTT 无意义，显示灰色占位
      }
    }
    setTimeout(rttLoop, 2000);
  }

  function connectWs() {
    if (!deviceCtx.id) return;
    if (ws) { ws.onclose = null; ws.close(); }
    const generation = deviceCtx.generation;
    const socket = ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://")
                       + location.host + deviceCtx.ws("shoot"));
    socket.onmessage = e => {
      if (generation !== deviceCtx.generation || ws !== socket) return;
      const m = JSON.parse(e.data);
      if (m.type === "telemetry") schedulePaint(m);
    };
    socket.onclose = () => {
      if (ws !== socket) return;
      ws = null;
      if (wsWant && serverOpen) setTimeout(() => {
        if (generation === deviceCtx.generation && wsWant) connectWs();
      }, 1000);
    };
  }

  /* ---------- 操作 ---------- */

  async function doOpen() {
    $id("shoot-open").disabled = true;
    try {
      const r = await api("open", {});
      wsWant = true;
      connectWs();
      toast(deviceCtx.isTcp ? "TCP 串口已连接，等待 MCU 遥测…" :
        "串口已打开: " + r.status.port + " @ " + r.status.baudrate + "（等待 MCU 遥测…）");
      refresh();
    } catch (e) { /* 接口函数已显示错误。 */ }
    finally { $id("shoot-open").disabled = !deviceCtx.id; }
  }
  async function doClose() {
    wsWant = false;
    if (ws) { ws.close(); ws = null; }
    await api("close", {});
    serverOpen = false;
    setLinkOff();
    setRtt(null);
    toast("已断开");
  }
  async function doEstop() {
    await api("estop", {});
    toast("急停已下发");
    refresh();
  }

  async function onMode(s) {
    // 固件 ModeSet 不自动失能：客户端先显式失能，使"已自动失能"承诺成立（防带电切模式）
    await api("enable", {slot: s, on: false});
    const r = await api("mode", {slot: s, mode: +val("mode" + s)});
    toast(`Motor${s + 1} 模式: ${r.status}（已自动失能）`, !r.ok);
    await api("apply", {slot: s});
  }
  async function onModel(s) {
    // 同上：切型号前先失能（增益/限幅换算随型号变化，带电切换有风险）
    await api("enable", {slot: s, on: false});
    const r = await api("model", {slot: s, model: +val("model" + s)});
    toast(`Motor${s + 1} 型号切换（已自动失能）: ${r.status}`, !r.ok);
    await api("apply", {slot: s});
    const pr = profiles[+val("model" + s)];
    if (pr) $id("gear" + s).value = pr.gear_ratio;
  }
  async function onApplyParam(s, key, v) {
    if (!isFinite(v)) { toast("非法数值", true); return; }
    const r = await api("param", {slot: s, key, value: v});
    toast(`Motor${s + 1} ${key}: ${r.status}`, !r.ok);
    if (r.ok) await api("apply", {slot: s});
  }
  async function onApply(s) {
    let n = 0;
    for (const [k] of PARAMS) {
      const v = +val(k + s);
      if (isFinite(v) && $id(k + s).value !== "") {
        const r = await api("param", {slot: s, key: k, value: v});
        if (r.ok) n++;
      }
    }
    await api("apply", {slot: s});
    toast(`Motor${s + 1} 已提交 ${n} 项参数`);
  }

  async function onToggleEnable(s) {
    const t = shootTelem && shootTelem.slots[s];
    const running = !!(t && t.enabled == 1);          // 只看真实使能位
    if (!running) {
      const latched = t ? (t.fault & ~1) : 0;         // 排除离线位
      let confirmed = false;                          // 各分支自行确认过则跳过通用确认
      if (latched & (0x04 | 0x08)) {                  // 过温/过流：必须人工确认后手动清
        toast(`Motor${s + 1} 存在过温/过流锁定，请检查后点击[清故障]`, true);
        return;
      }
      if (latched & 0x40) {                           // 仅急停锁定：确认后自动清+使能
        if (!confirm(`Motor${s + 1} 处于急停锁定。\n确认清除急停并使能？电机将开始跟踪目标。`)) return;
        await api("fault_clear", {slot: s});
        confirmed = true;
      } else if (t && (t.fault & 1)) {                // 离线槽位：使能无任何输出
        const online = shootTelem.slots
          .map((x, i) => (x.fault & 1) ? null : i)
          .filter((v) => v !== null);
        const tip = online.length
          ? `当前在线槽位: Motor ${online.map((v) => v + 1).join(" / ")}。` : "当前没有任何在线电机。";
        if (!confirm(`Motor${s + 1} 离线（总线上未检测到电机），使能不会有任何输出。\n` +
                     tip + `\n仍要使能吗？`)) return;
        confirmed = true;
      }

      const modeNow = +val("mode" + s);
      if (modeNow === 0) {
        /* Disable 模式下使能位不产生任何输出（固件 Step 将力矩钳 0）。
           引导一键切换 Rpm 模式（目标取滑杆保存的转速值）再使能 */
        const target = tgtState[s].rpm || 0;
        if (!confirm(`Motor${s + 1} 当前模式为 Disable（无输出），单独使能电机不会运转。\n` +
                     `切换到 Rpm 模式（目标 ${target} rpm）并使能？`)) return;
        const rm = await api("mode", {slot: s, mode: 1});
        if (!rm.ok) { toast(`Motor${s + 1} 模式切换失败: ${rm.status}`, true); return; }
        await api("param", {slot: s, key: "target_rpm", value: target});
        await api("apply", {slot: s});
      } else {
        if (!confirmed &&
            !confirm(`确认使能 Motor${s + 1}？电机将开始跟踪目标。`)) return;
        /* 使能前把滑杆当前目标一并提交并生效：
           避免"拖了滑杆但没提交"导致电机以旧目标（如 0）空转 */
        const key = {1: "target_rpm", 2: "target_deg",
                     3: "target_torque"}[modeNow];
        const kind = {1: "rpm", 2: "deg", 3: "torque"}[modeNow];
        const lim = tgtLimit(s, kind);
        const v = +val("tval" + s);
        if (isFinite(v)) {
          const clamped = Math.max(-lim, Math.min(lim, v));
          tgtState[s][kind] = clamped;
          await api("param", {slot: s, key, value: clamped});
        }
        await api("apply", {slot: s});
      }
    }
    const r = await api("enable", {slot: s, on: !running});
    toast(`Motor${s + 1} ${running ? "失能" : "使能"}: ${r.status}`, !r.ok);
  }

  async function onFaultClear(s) {
    const r = await api("fault_clear", {slot: s});
    toast(`Motor${s + 1} 清故障: ${r.status}`, !r.ok);
  }

  /* ---------- 事件绑定（委托到面板容器，避免内联全局函数） ---------- */

  $id("motor-panels").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const s = +btn.closest(".motor-card").dataset.slot;
    const act = btn.dataset.act;
    if (act === "mcol")                      // 电机卡折叠开合（class 驱动动画）
      btn.closest(".mcollapse").classList.toggle("open");
    else if (act === "subcol")               // 参数分类折叠开合
      btn.closest(".subcol").classList.toggle("open");
    else if (act === "param") onApplyParam(s, btn.dataset.key, +val(btn.dataset.src));
    else if (act === "reset") resetTarget(s);
    else if (act === "apply") onApply(s);
    else if (act === "enable") onToggleEnable(s);
    else if (act === "faultclear") onFaultClear(s);
    else if (act === "psave") savePreset(s);
    else if (act === "pdel") deletePreset(s);
    else if (act === "pdef") toggleDefault(s);
  });

  $id("motor-panels").addEventListener("change", (e) => {
    const id = e.target.id;
    if (/^mode\d$/.test(id)) { const s = +id.slice(-1); onMode(s); updateTargetUI(s); }
    else if (/^model\d$/.test(id)) {
      const s = +id.slice(-1);
      onModel(s);
      updateTargetUI(s);
      updateMechUI(s);                           // 槽数/频率行跟随型号
      refreshPresetUI(s);                       // 预设列表跟随型号切换
      maybeAutoPreset(s, $id("model" + s).value, false);  // 新型号默认预设自动应用
    }
    else if (/^preset\d$/.test(id)) {           // 选中预设 → 填表并下发
      const s = +id.slice(-1);
      if (+e.target.value >= 0) applyPreset(s, +e.target.value, false);
    }
    else if (/^tslider\d$/.test(id)) {          // 滑杆释放 → 确保终值立即落地
      const s = +id.slice(-1);
      $id("tval" + s).value = e.target.value;
      sendTarget(s, true);
    }
    else if (/^tval\d$/.test(id)) sendTarget(+id.slice(-1), false);  // 数值框精确输入 → 提交
  });

  /* 滑杆拖动：数值框实时同步 + 节流推送目标；双击 = 复位 0 */
  $id("motor-panels").addEventListener("input", (e) => {
    if (/^tslider\d$/.test(e.target.id)) {
      const s = +e.target.id.slice(-1);
      $id("tval" + s).value = e.target.value;
      pushThrottled(s);
    }
  });
  $id("motor-panels").addEventListener("dblclick", (e) => {
    if (/^tslider\d$/.test(e.target.id)) resetTarget(+e.target.id.slice(-1));
  });

  $id("shoot-open").addEventListener("click", doOpen);
  $id("shoot-close").addEventListener("click", doClose);
  $id("shoot-estop").addEventListener("click", doEstop);

  /* 设备切换时断开旧会话并重置自动应用标记 */
  if (window.deviceCtx) deviceCtx.onChange((id, previous) => {
    if (wsWant && previous) {
      const base = previous === "legacy-default" ? "/api" : "/api/v2/devices/" + encodeURIComponent(previous);
      fetch(base + "/shoot/close", {method: "POST"}).catch(() => toast("旧设备断开失败，请检查连接", true));
    }
    wsWant = false;
    if (ws) { ws.close(); ws = null; }
    serverOpen = false;
    shootTelem = null;
    for (const k of Object.keys(presetAutoApplied)) delete presetAutoApplied[k];
    autoAppliedOnConnect = false;
    pendingTelem = null;
    for (const st of Object.values(pushState)) { clearTimeout(st.timer); st.timer = null; st.last = 0; }
    $id("motor-panels").querySelectorAll('select[id^="model"]').forEach(sel => sel.innerHTML = "");
    profiles = [];
    motorPresets = null;
    shootTelem = null;
    lastTelemAt = 0;
    lastLinkSeen = -1;
    paintOffline();
    setRtt(null);
    $id("dc-clear").click();
    setLinkOff();
    loadMotorPresets().then(() => { for (let i = 0; i < 3; i++) refreshPresetUI(i); });
    refresh();
  });

  /* ---------- 初始化 ---------- */

  buildPanels();
  // 电机面板的 model/mode/preset 选择器挂载自绘下拉（选项稍后由 MutationObserver 补齐）
  if (window.ShootDropdown) ShootDropdown.attachAll($id("motor-panels"));
  loadMotorPresets();
  refresh();
  setInterval(refresh, 1000); // 兜底轮询：WS 未挂载/断开时页面仍与服务器同步
  rttLoop();
})();
