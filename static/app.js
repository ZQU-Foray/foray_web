/* STM32 局域网烧录/调试台 前端逻辑 */
'use strict';

const $ = (s) => document.querySelector(s);
const te = new TextEncoder();
const td = new TextDecoder('utf-8', { fatal: false });

const state = {
  flashWs: null,
  serialWs: null,
  presets: null,
  statusTimer: null,
};

/* ================= 会话失效拦截（阶段 3 认证） ================= */
/* 统一在 window.fetch 层感知 401：会话过期/撤销时引导回登录页，
 * 不改变既有请求的构造方式（集中一处，覆盖 app.js/shoot.js 全部调用）。 */
(() => {
  const orig = window.fetch;
  if (!orig) return;
  window.fetch = function (input, init) {
    return orig.call(window, input, init).then((resp) => {
      if (resp.status === 401) {
        const next = encodeURIComponent(location.pathname + location.search);
        location.href = '/login?next=' + next;
      }
      return resp;
    });
  };
})();

/* ================= 设备上下文（阶段 4） ================= */
/* 模式只影响当前标签页的设备选择，不改变服务器默认设备。 */
var deviceCtx = window.deviceCtx = {
  id: null,
  mode: sessionStorage.getItem('connection-mode') || 'sta',
  list: [],
  generation: 0,
  _listeners: [],
  onChange(fn) { this._listeners.push(fn); },
  get current() { return this.list.find(d => d.id === this.id); },
  get isTcp() { return this.current?.transport === 'vllink_tcp'; },
  api(path) {
    if (!this.id) throw new Error('请先选择设备');
    return (this.id === 'legacy-default' ? '/api' : '/api/v2/devices/' + encodeURIComponent(this.id)) + '/' + path;
  },
  ws(path) {
    if (!this.id) throw new Error('请先选择设备');
    return (this.id === 'legacy-default' ? '/ws' : '/ws/v2/devices/' + encodeURIComponent(this.id)) + '/' + path;
  },
  set(id) {
    if (id === this.id) { renderDeviceSelector(); return; }
    const previous = this.id;
    this.id = id;
    this.generation++;
    if (id) sessionStorage.setItem('current-device', id);
    renderDeviceSelector();
    this._listeners.forEach(fn => { try { fn(id, previous); } catch (e) { console.error(e); } });
  },
  choose() {
    const candidates = this.list.filter(d => (d.transport === 'vllink_tcp') === (this.mode === 'sta'));
    const saved = sessionStorage.getItem('current-device');
    /* 保存值不可用（新标签页 / 设备已删 / USB↔STA 切换）时回退到第一台：
       否则多设备场景会停在 id=null——标签显示第一台但实际未选中，
       烧录/串口/发射按钮全禁用、也不建立 WS，用户感知为“多个设备时用不了”。 */
    const pick = candidates.find(d => d.id === saved)?.id
      || (candidates.length ? candidates[0].id : null);
    this.set(pick);
    renderDeviceSelector();
  },
  async refresh() {
    try {
      const r = await fetch('/api/v2/devices');
      if (!r.ok) throw new Error('设备列表读取失败');
      this.list = (await r.json()).devices || [];
      this.choose();
    } catch (e) { $('#device-hint').textContent = e.message; }
  },
};

/* 下拉候选是否与当前 DOM 一致：一致就不重建，避免每 3 秒刷新把用户
   正在交互的下拉清空（曾导致触发器标签变空白、原生值与显示脱节） */
function sameSelectOptions(sel, options) {
  if (!sel || sel.options.length !== options.length) return false;
  return options.every((o, i) => {
    const op = sel.options[i];
    return op.value === o.value && op.textContent === o.label
      && op.disabled === !!o.disabled;
  });
}

function renderDeviceSelector() {
  const sel = $('#device-select');
  const candidates = deviceCtx.list.filter(d => (d.transport === 'vllink_tcp') === (deviceCtx.mode === 'sta'));
  const opts = candidates.length
    ? candidates.map(d => {
        // TCP 设备以 IP 末段为主标识（如 "142"），USB 用名称
        const lastOctet = d.host ? d.host.split('.').pop() : null;
        const label = d.transport === 'vllink_tcp'
          ? `STA · ${lastOctet}` : (d.name || d.id);
        return {value: d.id, label};
      })
    : [{value: '', label: '尚未添加 STA', disabled: true}];
  const keep = deviceCtx.id || '';
  if (!sameSelectOptions(sel, opts) || sel.value !== keep) {
    fillSelect(sel, opts, keep);
  }
  $('#connection-mode').value = deviceCtx.mode;
  const d = deviceCtx.current;
  const tcp = deviceCtx.isTcp;
  const hint = '';   // 冗长提示已移除（设备信息由下拉框和健康徽章承载）
  $('#device-hint').textContent = hint;
  $('#shoot-connection-hint').textContent = tcp
    ? `当前目标：${d.name || d.id} · TCP 串口 ${d.host}:${d.uart_port || 20010}。连接后等待 MCU 遥测。`
    : d ? '当前目标使用服务器 USB 串口；连接后等待 MCU 遥测。' : '请先在上方添加并选择设备。';
  $('#serial-connection-hint').textContent = tcp
    ? 'TCP 串口的实际波特率由 Basic2 配置决定，网页不会修改硬件参数。请确认已启用 TCP-UART，并与 MCU 波特率一致。'
    : '选择服务器上的串口与波特率。';
  $('#port-select').closest('label').hidden = tcp;
  $('#baud-input').closest('label').hidden = tcp;
  $('#probe-select').closest('label').hidden = tcp;
  $('#sta-add').hidden = deviceCtx.mode !== 'sta';
  $('#dev-scan').hidden = deviceCtx.mode !== 'sta';
  for (const id of ['flash-btn', 'serial-open', 'serial-close', 'serial-send', 'shoot-open', 'shoot-close', 'shoot-estop', 'presets-save']) {
    $('#' + id).disabled = !d;
  }
}


/* ===== 设备在线探测：周期检查当前设备可达性 ===== */
let _probeTimer = 0;

async function probeCurrentDevice() {
  const d = deviceCtx.current;
  const badge = document.getElementById('device-health');
  if (!badge) return;
  if (!d) { badge.style.display = 'none'; return; }
  if (d.transport !== 'vllink_tcp') {
    // USB 设备不做网络探测
    badge.style.display = 'none';
    return;
  }
  try {
    const r = await fetch(`/api/v2/devices/${encodeURIComponent(d.id)}/probe`, {method: 'POST'});
    const j = await r.json();
    if (deviceCtx.generation !== _probeTimer && _probeTimer) return;  // 设备已切换
    const ports = j.ports || {};
    const uartHint = ports.uart_session ? '串口会话已打开' : '串口会话未打开';
    if (j.reachable) {
      badge.textContent = '在线';
      badge.className = 'badge ok device-health';
      badge.title = `${d.host} TCP-DAP 可达 · ${uartHint}`;
    } else {
      badge.textContent = '离线';
      badge.className = 'badge err device-health';
      badge.title = `${d.host} TCP-DAP 不可达 — 请检查 STA 电源与网络`;
    }
    badge.style.display = '';
  } catch (e) {
    badge.textContent = '未知';
    badge.className = 'badge dim device-health';
    badge.style.display = '';
  }
}

function startDeviceProbe() {
  probeCurrentDevice();
  setInterval(() => {
    _probeTimer = deviceCtx.generation;
    probeCurrentDevice();
  }, 5000);
}

$('#connection-mode').addEventListener('change', e => {
  deviceCtx.mode = e.target.value;
  sessionStorage.setItem('connection-mode', deviceCtx.mode);
  deviceCtx.choose();
});
$('#sta-add').addEventListener('submit', async e => {
  e.preventDefault();
  const host = $('#sta-host').value.trim();
  if (!/^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$/.test(host) || host.split('.').some(n => +n > 255)) {
    $('#device-hint').textContent = '请输入有效的 STA IPv4 地址。';
    return;
  }
  try {
    const existing = deviceCtx.list.find(d => d.transport === 'vllink_tcp' && d.host === host);
    if (existing) {
      // 已注册：不重复添加，提示用下拉框选择
      const last = host.split('.').pop();
      $('#device-hint').textContent = `STA · ${last} 已注册，请在上拉框选择。`;
      return;
    }
    const id = 'sta-' + host.replaceAll('.', '-');
    const r = await fetch('/api/v2/devices', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, name: `STA · ${host.split('.').pop()}`, transport: 'vllink_tcp', host, dap_port: 4441, uart_port: 20010})});
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    sessionStorage.setItem('current-device', id);
    await deviceCtx.refresh();
  } catch (e) { $('#device-hint').textContent = '添加失败：' + e.message; }
});

/* 扫描局域网 Basic2 STA 设备 */
async function scanStaDevices() {
  const btn = document.getElementById('dev-scan');
  if (!btn) return;
  btn.textContent = '扫描中…';
  btn.disabled = true;
  try {
    const r = await fetch('/api/v2/devices/scan', {method: 'POST'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.status);
    if (!d.found || !d.found.length) {
      alert(`未在 ${d.subnet || '局域网'} 内发现 Basic2 STA 设备。\n` +
            '请确认 STA 已上电并接入路由器。');
      return;
    }
    // 构造确认信息
    let msg = `在 ${d.subnet} 发现 ${d.found.length} 台设备：\n\n`;
    for (const f of d.found) {
      msg += `  ${f.host}  ${f.registered ? '（已注册）' : '（新设备）'}\n`;
    }
    const toRegister = d.found.filter(f => !f.registered);
    if (toRegister.length) {
      msg += `\n是否注册 ${toRegister.length} 台新设备？`;
      if (!confirm(msg)) return;
      for (const f of toRegister) {
        const id = 'sta-' + f.host.split('.').slice(-2).join('-');
        const resp = await fetch('/api/v2/devices', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            id, transport: 'vllink_tcp', host: f.host,
            name: `STA · ${f.host.split('.').pop()}`, dap_port: 4441, uart_port: 20010,
            capabilities: {
              flash: {available: true, source: 'probed'},
              uart: {available: true, source: 'probed'},
            },
          }),
        });
        if (resp.ok) {
          console.log(`已注册: ${id} (${f.host})`);
        }
      }
      await deviceCtx.refresh();
    } else {
      alert(msg + '\n所有设备均已注册。');
    }
  } catch (e) {
    alert('扫描失败: ' + e.message);
  } finally {
    btn.textContent = '扫描 STA';
    btn.disabled = false;
  }
}

/* ================= 工具 ================= */

function bytesToB64(bytes) {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

function b64ToBytes(b64) {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

function b64ToStr(b64) {
  return td.decode(b64ToBytes(b64));
}

function ts() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function appendLog(el, cls, text) {
  const line = document.createElement('div');
  line.className = cls;
  line.textContent = `[${ts()}] ${text}`;
  el.appendChild(line);
  while (el.children.length > 2000) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

function fillSelect(sel, options, keepValue) {
  const cur = keepValue !== undefined ? keepValue : sel.value;
  sel.innerHTML = '';
  for (const o of options) {
    const op = document.createElement('option');
    op.value = o.value;
    op.textContent = o.label;
    if (o.disabled) op.disabled = true;   // 占位项不可选中
    sel.appendChild(op);
  }
  sel.value = cur;
  if (sel.selectedIndex < 0 && sel.options.length) sel.selectedIndex = 0;
}

function setBadge(el, state, text) {
  el.className = 'badge ' + state;
  el.textContent = text;
}

/* ================= 页签 ================= */

document.querySelectorAll('.tab').forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll('.tab').forEach((b) =>
      b.classList.toggle('active', b === btn));
    document.querySelectorAll('.panel').forEach((p) =>
      p.classList.toggle('active', p.id === 'tab-' + btn.dataset.tab));
  };
});

/* ================= 状态轮询 ================= */

async function refreshStatus() {
  try {
    const generation = deviceCtx.generation;
    const r = await fetch(deviceCtx.id ? deviceCtx.api('status') : '/api/status');
    const s = await r.json();
    if (generation !== deviceCtx.generation) return;
    $('#conn-dot').className = 'dot ok';
    $('#conn-text').textContent = '服务在线';
    $('#conn-text').parentElement.classList.add('online');
    state.lastPorts = s.devices.ports || [];
    renderDevices(s.devices);
    renderTargets(s.targets);
    renderProbes(s.probes);
    renderFlashState(s.flash);
    renderSerialState(s.serial);
    if (!deviceCtx.id) renderDeviceSelector();
    $('#fw-dir').textContent = s.firmware_dir;
  } catch (e) {
    $('#conn-dot').className = 'dot bad';
    $('#conn-text').textContent = '服务离线';
    $('#conn-text').parentElement.classList.remove('online');
  }
}

function renderDevices(d) {
  const box = $('#device-list');
  if (deviceCtx.isTcp) {
    const d = deviceCtx.current;
    box.textContent = `单STA · ${d.name || d.id}
TCP-DAP：${d.host}:${d.dap_port || 4441}
TCP-UART：${d.host}:${d.uart_port || 20010}
已登记地址；实际连接状态见对应业务。`;
    return;
  }
  if (!deviceCtx.id) { box.textContent = '请选择设备后查看状态。'; return; }
  box.replaceChildren();
  for (const [kind, entries] of [['调试器', d.probes || []], ['串口', d.ports || []]]) {
    for (const p of entries) {
      const row = document.createElement('div');
      row.className = 'item';
      const label = document.createElement('span');
      label.className = 'kind';
      label.textContent = `[${kind}] ${p.label}`;
      row.append(label, document.createElement('br'), document.createTextNode(
        `${kind === '调试器' ? `VID:PID=${p.vid}:${p.pid}` : p.port}  SN=${p.serial || '-'}`));
      box.append(row);
    }
  }
  if (!box.childElementCount) box.textContent = '未检测到 USB 调试器/串口';
}

function renderTargets(targets) {
  fillSelect($('#target-select'), targets.map((t) => ({
    value: t.key, label: t.label,
  })));
  if (state.targetGeneration !== deviceCtx.generation) {
    if (deviceCtx.current?.target) $('#target-select').value = deviceCtx.current.target;
    state.targetGeneration = deviceCtx.generation;
  }
}

function renderProbes(probes) {
  fillSelect($('#probe-select'), probes.map((p) => ({
    value: p.key, label: p.label,
  })));
}

function renderFlashState(f) {
  const map = {
    idle: ['idle', '空闲'],
    running: ['running', '烧录中…'],
    success: ['success', '烧录成功'],
    failed: ['failed', '烧录失败'],
  };
  const [cls, text] = map[f.state] || ['idle', f.state];
  setBadge($('#flash-state'), cls, text);
  $('#flash-btn').disabled = f.state === 'running' || !deviceCtx.id;
}

function renderSerialState(s) {
  if (s.open) {
    setBadge($('#serial-state'), 'open', deviceCtx.isTcp ? `TCP 串口已连接 · ${deviceCtx.current.host}:${deviceCtx.current.uart_port || 20010}` : `已打开: ${s.port} @ ${s.baudrate}`);
  } else {
    setBadge($('#serial-state'), 'idle', '未打开');
  }
  const ports = state.lastPorts || [];
  fillSelect($('#port-select'),
    [{ value: 'auto', label: '自动选择' }].concat(ports.map((p) => ({
      value: p.port, label: `${p.port} (${p.label || '未知'})`,
    }))));
}

/* ================= 烧录 ================= */

function connectFlashWs() {
  if (!deviceCtx.id) return;
  const generation = deviceCtx.generation;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  state.flashWs = new WebSocket(`${proto}://${location.host}${deviceCtx.ws('flash')}`);
  state.flashWs.onmessage = (e) => {
    if (generation !== deviceCtx.generation) return;
    const m = JSON.parse(e.data);
    if (m.type === 'log') {
      const cls = m.stream === 'sys' ? 'line-sys' : 'line-out';
      appendLog($('#flash-log'), cls, m.line);
    }
  };
  state.flashWs.onclose = () => setTimeout(() => { if (generation === deviceCtx.generation) connectFlashWs(); }, 2000);
}

$('#flash-btn').onclick = async () => {
  const firmware = $('#firmware-select').value;
  const target = $('#target-select').value;
  const probe = deviceCtx.isTcp ? 'vllink' : $('#probe-select').value;
  if (!firmware) { alert('请先选择固件'); return; }
  try {
    const r = await fetch(deviceCtx.api('flash'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ firmware, target, probe }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      appendLog($('#flash-log'), 'line-sys', `请求失败: ${e.detail || r.status}`);
    }
  } catch (e) {
    appendLog($('#flash-log'), 'line-sys', `请求失败: ${e}`);
  }
};

$('#log-clear').onclick = () => { $('#flash-log').innerHTML = ''; };

/* ================= 串口 ================= */

/* 串口 RX 行缓冲：数据按“读块”到达（TCP 还会被拆成零碎小块），逐块成行会把
   一行文本撕成多条带时间戳的日志；这里合并数据块，遇换行输出整行，
   半行超过 350ms 没有后续数据再冲刷，兼顾实时性与可读性。 */
const serialRx = { text: '', timer: 0 };

function serialRxFlush(text) {
  const clean = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\n+$/, '');
  if (clean === '') return;      // 纯换行不产生空日志行
  appendLog($('#serial-log'), 'line-rx', clean);
}

function serialRxAppend(text) {
  if (!text) return;
  serialRx.text += text;
  const nl = Math.max(serialRx.text.lastIndexOf('\n'), serialRx.text.lastIndexOf('\r'));
  if (nl >= 0) {
    const done = serialRx.text.slice(0, nl + 1);
    serialRx.text = serialRx.text.slice(nl + 1);
    serialRxFlush(done);
  }
  if (serialRx.text) {
    if (serialRx.timer) clearTimeout(serialRx.timer);
    serialRx.timer = setTimeout(() => {
      serialRx.timer = 0;
      const rest = serialRx.text;
      serialRx.text = '';
      serialRxFlush(rest);
    }, 350);
  }
}

function serialRxReset() {
  if (serialRx.timer) clearTimeout(serialRx.timer);
  serialRx.timer = 0;
  serialRx.text = '';
}

function connectSerialWs() {
  if (!deviceCtx.id) return;
  const generation = deviceCtx.generation;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  state.serialWs = new WebSocket(`${proto}://${location.host}${deviceCtx.ws('serial')}`);
  state.serialWs.onmessage = (e) => {
    if (generation !== deviceCtx.generation) return;
    const m = JSON.parse(e.data);
    if (m.type === 'sys') appendLog($('#serial-log'), 'line-sys', m.data);
    else if (m.type === 'rx') serialRxAppend(b64ToStr(m.b64));
    else if (m.type === 'tx') appendLog($('#serial-log'), 'line-tx', b64ToStr(m.b64));
  };
  state.serialWs.onclose = () => setTimeout(() => { if (generation === deviceCtx.generation) connectSerialWs(); }, 2000);
}

function serialSend(payload) {
  if (state.serialWs && state.serialWs.readyState === 1) {
    state.serialWs.send(JSON.stringify(payload));
  } else {
    appendLog($('#serial-log'), 'line-sys', '串口通道未连接');
  }
}

$('#serial-close').onclick = () => {
  serialSend({ type: 'close' });
};

function sendData(text, newline = 'crlf') {
  const nl = { crlf: '\r\n', lf: '\n', cr: '\r', none: '' }[newline] ?? '\r\n';
  const data = te.encode(text + nl);
  serialSend({ type: 'tx', b64: bytesToB64(data) });
}

$('#serial-send').onclick = () => {
  const text = $('#serial-input').value;
  if (!text) return;
  if ($('#hex-mode').checked) {
    const clean = text.replace(/[^0-9a-fA-F]/g, '');
    if (clean.length % 2) { alert('HEX 长度必须为偶数'); return; }
    let bytes = new Uint8Array(clean.length / 2);
    for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(clean.substr(i * 2, 2), 16);
    serialSend({ type: 'tx', b64: bytesToB64(bytes) });
  } else {
    sendData(text, 'none');
  }
  $('#serial-input').value = '';
};

$('#serial-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('#serial-send').click();
});

function renderPresetButtons() {
  const box = $('#preset-buttons');
  const cmds = (state.presets && state.presets.commands) || [];
  box.innerHTML = '';
  for (const c of cmds) {
    const b = document.createElement('button');
    b.className = 'preset-btn';
    b.textContent = c.name || c.id || '?';
    b.title = `data=${c.data}  encoding=${c.encoding}  newline=${c.newline}`;
    b.onclick = () => sendData(c.data || '', c.newline || 'crlf');
    box.appendChild(b);
  }
}

/* ================= 预设配置 ================= */

async function loadPresets() {
  const generation = deviceCtx.generation;
  if (!deviceCtx.id) return;
  try {
    const r = await fetch(deviceCtx.api('presets'));
    const doc = await r.json();
    if (generation !== deviceCtx.generation) return;
    state.presets = doc;
    $('#presets-json').value = JSON.stringify(state.presets, null, 2);
    $('#baud-input').value = state.presets.baudrate || 115200;
    renderPresetButtons();
  } catch (e) { /* ignore */ }
}

$('#presets-save').onclick = async () => {
  let data;
  try {
    data = JSON.parse($('#presets-json').value);
  } catch (e) {
    $('#presets-msg').textContent = 'JSON 格式错误: ' + e;
    return;
  }
  try {
    const r = await fetch(deviceCtx.api('presets'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (!r.ok) throw new Error(await r.text());
    state.presets = await r.json();
    renderPresetButtons();
    $('#presets-msg').textContent = '已保存';
    setTimeout(() => { $('#presets-msg').textContent = ''; }, 2000);
  } catch (e) {
    $('#presets-msg').textContent = '保存失败: ' + e;
  }
};

$('#serial-open').addEventListener('click', async () => {
  try {
    const r = await fetch(deviceCtx.api('serial/open'), {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(deviceCtx.isTcp ? {} : {port: $('#port-select').value, baudrate: +$('#baud-input').value || 115200})});
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    refreshStatus();
  } catch (e) { appendLog($('#serial-log'), 'line-sys', '连接失败：' + e.message); }
});

/* ================= 固件管理 ================= */

async function reloadFirmware() {
  try {
    const r = await fetch('/api/firmware');
    const d = await r.json();
    const items = d.items || [];
    fillSelect($('#firmware-select'), items.map((f) => ({
      value: f.name,
      label: `${f.name} (${(f.size / 1024).toFixed(1)} KB)`,
    })), $('#firmware-select').value || '');
    const box = $('#fw-list');
    box.replaceChildren();
    for (const f of items) {
      const row = document.createElement('div');
      row.className = 'item';
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = f.name;
      const size = document.createElement('span');
      size.className = 'size';
      size.textContent = `${(f.size / 1024).toFixed(1)} KB · ${new Date(f.mtime * 1000).toLocaleString()}`;
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'del';
      del.dataset.name = f.name;
      del.textContent = '删除';
      row.append(name, size, del);
      box.append(row);
    }
    if (!items.length) box.textContent = '（暂无固件，可在此上传）';
    box.querySelectorAll('.del').forEach((el) => {
      el.onclick = async () => {
        if (!confirm(`删除固件 ${el.dataset.name}？`)) return;
        await fetch('/api/firmware/' + encodeURIComponent(el.dataset.name),
                    { method: 'DELETE' });
        reloadFirmware();
      };
    });
  } catch (e) { /* ignore */ }
}

$('#fw-upload').onclick = async () => {
  const f = $('#fw-file').files[0];
  if (!f) { alert('请选择文件'); return; }
  const fd = new FormData();
  fd.append('file', f);
  try {
    const r = await fetch('/api/firmware/upload', { method: 'POST', body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    $('#fw-file').value = '';
    reloadFirmware();
  } catch (e) {
    alert('上传失败: ' + e);
  }
};

/* ================= 开机加载盖（终末地式引导序列） ================= */

/* ================= 初始化 ================= */

deviceCtx.onChange(() => {
  serialRxReset();
  for (const key of ['flashWs', 'serialWs']) {
    if (state[key]) { state[key].onclose = null; state[key].close(); state[key] = null; }
  }
  $('#flash-log').textContent = '';
  $('#serial-log').textContent = '';
  state.presets = null;
  $('#presets-json').value = '';
  renderPresetButtons();
  if (deviceCtx.current?.target) $('#target-select').value = deviceCtx.current.target;
  connectFlashWs(); connectSerialWs(); refreshStatus(); loadPresets();
});
startDeviceProbe();
deviceCtx.refresh().then(() => {
    const sel = document.getElementById('device-select');
    if (sel) sel.addEventListener('change', () => deviceCtx.set(sel.value));
    const scan = document.getElementById('dev-scan');
    if (scan) scan.addEventListener('click', scanStaDevices);
  });
connectFlashWs();
connectSerialWs();
refreshStatus();
loadPresets();
reloadFirmware();
state.statusTimer = setInterval(refreshStatus, 3000);
