/* 设备上下文（移植自旧版 static/app.js「设备上下文（阶段 4）」与「设备在线探测」两段）。
 * 当前设备同时决定烧录、串口、发射调试、RTT 与预设的操作目标；连接方式只影响
 * 当前标签页的设备选择，不改变服务器默认设备。行为约束（2026-10-19 线上修复）：
 * - 无有效保存值（新标签页 / 设备已删 / USB↔STA 切换）时自动选中第一台，绝不停在半选；
 * - 设备下拉候选未变化时不重建（Vue 按 key 比对，DOM 不动）；
 * - 每 5 秒只对当前 TCP 设备调 /probe（服务端只探 DAP 端口、设备忙时不新建连接）。
 * deviceCtx 同时挂到 window 上（回归用例按 window.deviceCtx.id 断言）。 */
import { reactive } from "vue";
import { dfetch, errorText, jsonInit, ssGet, ssSet } from "./api";
import type { DdOption } from "./dropdown";
import { toast } from "./toast";

export interface Device {
  id: string;
  name?: string;
  transport?: string;
  host?: string;
  dap_port?: number;
  uart_port?: number;
  target?: string;
}

type Listener = (id: string | null, previous: string | null) => void;
const listeners: Listener[] = [];

/** 设备条上的健康徽章与扫描状态（模板直接绑定；提示改由右侧通知框弹出） */
export const deviceUi = reactive({
  health: { show: false, text: "未知", cls: "neutral", title: "" },
  scanning: false,
});

export const deviceCtx = reactive({
  id: null as string | null,
  mode: ssGet("connection-mode") || "sta",
  list: [] as Device[],
  generation: 0,
  onChange(fn: Listener) { listeners.push(fn); },
  get current(): Device | undefined { return this.list.find((d) => d.id === this.id); },
  get isTcp(): boolean { return this.current?.transport === "vllink_tcp"; },
  api(path: string): string {
    if (!this.id) throw new Error("请先选择设备");
    return (this.id === "legacy-default" ? "/api" : "/api/v2/devices/" + encodeURIComponent(this.id)) + "/" + path;
  },
  ws(path: string): string {
    if (!this.id) throw new Error("请先选择设备");
    return (this.id === "legacy-default" ? "/ws" : "/ws/v2/devices/" + encodeURIComponent(this.id)) + "/" + path;
  },
  set(id: string | null) {
    if (id === this.id) return;
    const previous = this.id;
    this.id = id;
    this.generation++;
    if (id) ssSet("current-device", id);
    listeners.forEach((fn) => { try { fn(id, previous); } catch (e) { console.error(e); } });
  },
  choose() {
    const candidates = candidateDevices();
    const saved = ssGet("current-device");
    /* 保存值不可用时回退到第一台：否则多设备场景会停在 id=null——标签显示第一台
       但实际未选中，烧录/串口/发射按钮全禁用、也不建立 WS（10-19 根因②） */
    const pick = candidates.find((d) => d.id === saved)?.id
      || (candidates.length ? candidates[0].id : null);
    this.set(pick);
  },
  async refresh() {
    try {
      const r = await dfetch("/api/v2/devices");
      if (!r.ok) throw new Error("设备列表读取失败");
      this.list = ((await r.json()) as { devices?: Device[] }).devices || [];
      this.choose();
    } catch (e) { toast((e as Error).message, true); }
  },
});

export type DeviceContext = typeof deviceCtx;

/** 当前连接方式下的候选设备（单STA = TCP 设备；USB / AP = 其余） */
export function candidateDevices(): Device[] {
  return deviceCtx.list.filter((d) => (d.transport === "vllink_tcp") === (deviceCtx.mode === "sta"));
}

/** 设备下拉的选项（TCP 设备以 IP 末段为主标识，如「STA · 142」；USB 用名称） */
export function deviceOptions(): DdOption[] {
  const candidates = candidateDevices();
  if (!candidates.length) return [{ value: "", label: "尚未添加 STA", disabled: true }];
  return candidates.map((d) => {
    const lastOctet = d.host ? d.host.split(".").pop() : null;
    return { value: d.id, label: d.transport === "vllink_tcp" ? `STA · ${lastOctet}` : (d.name || d.id) };
  });
}

/** 连接方式切换 */
export function setConnectionMode(mode: string): void {
  deviceCtx.mode = mode;
  ssSet("connection-mode", mode);
  deviceCtx.choose();
}

/* ===== 设备在线探测：周期检查当前设备可达性 ===== */

async function probeCurrentDevice(): Promise<void> {
  const d = deviceCtx.current;
  const badge = deviceUi.health;
  if (!d) { badge.show = false; return; }
  if (d.transport !== "vllink_tcp") {   // USB 设备不做网络探测
    badge.show = false;
    return;
  }
  const generation = deviceCtx.generation;
  try {
    const r = await dfetch(`/api/v2/devices/${encodeURIComponent(d.id)}/probe`, { method: "POST" });
    const j = await r.json() as { reachable?: boolean; ports?: { uart_session?: boolean } };
    if (generation !== deviceCtx.generation) return;   // 设备已切换
    const ports = j.ports || {};
    const uartHint = ports.uart_session ? "串口会话已打开" : "串口会话未打开";
    if (j.reachable) {
      badge.text = "在线";
      badge.cls = "ok";
      badge.title = `${d.host} TCP-DAP 可达 · ${uartHint}`;
    } else {
      badge.text = "离线";
      badge.cls = "danger";
      badge.title = `${d.host} TCP-DAP 不可达 — 请检查 STA 电源与网络`;
    }
    badge.show = true;
  } catch {
    badge.text = "未知";
    badge.cls = "neutral";
    badge.title = "";
    badge.show = true;
  }
}

export function startDeviceProbe(): void {
  void probeCurrentDevice();
  window.setInterval(() => { void probeCurrentDevice(); }, 5000);
}

/* ===== 手动添加 / 扫描 STA ===== */

const IPV4 = /^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$/;

export async function addSta(raw: string): Promise<void> {
  const host = raw.trim();
  if (!IPV4.test(host) || host.split(".").some((n) => +n > 255)) {
    toast("请输入有效的 STA IPv4 地址。", true);
    return;
  }
  try {
    const existing = deviceCtx.list.find((d) => d.transport === "vllink_tcp" && d.host === host);
    if (existing) {
      /* 已注册：不重复添加，提示用下拉框选择 */
      toast(`STA · ${host.split(".").pop()} 已注册，请在下拉框选择。`);
      return;
    }
    const id = "sta-" + host.split(".").join("-");
    const r = await dfetch("/api/v2/devices", jsonInit("POST", {
      id, name: `STA · ${host.split(".").pop()}`, transport: "vllink_tcp", host,
      dap_port: 4441, uart_port: 20010,
    }));
    if (!r.ok) throw new Error(await errorText(r));
    ssSet("current-device", id);
    await deviceCtx.refresh();
  } catch (e) { toast("添加失败：" + (e as Error).message, true); }
}

interface ScanResult {
  found?: { host: string; registered?: boolean }[];
  subnet?: string;
  detail?: unknown;
}

/** 扫描局域网 Basic2 STA 设备（结果用原生对话框确认，与旧版一致） */
export async function scanStaDevices(): Promise<void> {
  if (deviceUi.scanning) return;
  deviceUi.scanning = true;
  try {
    const r = await dfetch("/api/v2/devices/scan", { method: "POST" });
    const d = await r.json() as ScanResult;
    if (!r.ok) throw new Error(typeof d.detail === "string" && d.detail ? d.detail : String(r.status));
    if (!d.found || !d.found.length) {
      alert(`未在 ${d.subnet || "局域网"} 内发现 Basic2 STA 设备。\n` +
            "请确认 STA 已上电并接入路由器。");
      return;
    }
    let msg = `在 ${d.subnet} 发现 ${d.found.length} 台设备：\n\n`;
    for (const f of d.found) {
      msg += `  ${f.host}  ${f.registered ? "（已注册）" : "（新设备）"}\n`;
    }
    const toRegister = d.found.filter((f) => !f.registered);
    if (toRegister.length) {
      msg += `\n是否注册 ${toRegister.length} 台新设备？`;
      if (!confirm(msg)) return;
      for (const f of toRegister) {
        const id = "sta-" + f.host.split(".").slice(-2).join("-");
        const resp = await dfetch("/api/v2/devices", jsonInit("POST", {
          id, transport: "vllink_tcp", host: f.host,
          name: `STA · ${f.host.split(".").pop()}`, dap_port: 4441, uart_port: 20010,
          capabilities: {
            flash: { available: true, source: "probed" },
            uart: { available: true, source: "probed" },
          },
        }));
        if (resp.ok) console.log(`已注册: ${id} (${f.host})`);
      }
      await deviceCtx.refresh();
    } else {
      alert(msg + "\n所有设备均已注册。");
    }
  } catch (e) {
    alert("扫描失败: " + (e as Error).message);
  } finally {
    deviceUi.scanning = false;
  }
}
