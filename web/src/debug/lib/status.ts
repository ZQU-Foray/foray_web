/* 状态轮询（移植自旧版 static/app.js「状态轮询」段）：每 3 秒读一次当前设备状态，
 * 驱动服务连接指示、设备状态、目标芯片 / 调试器 / 串口候选、烧录与串口状态徽标。
 * 候选内容没变时不替换数组（保持引用稳定，下拉不重建、不打断正在进行的选择）。 */
import { reactive } from "vue";
import { dfetch } from "./api";
import { deviceCtx } from "./device";
import { sameOptions, type DdOption } from "./dropdown";

interface UsbEntry { label?: string; vid?: string; pid?: string; port?: string; serial?: string }
interface StatusBody {
  devices: { probes?: UsbEntry[]; ports?: UsbEntry[] };
  serial: { open?: boolean; port?: string; baudrate?: number };
  flash: { state: string };
  targets: { key: string; label: string }[];
  probes: { key: string; label: string }[];
  firmware_dir: string;
}

export interface DevRow { kind: string; label: string; detail: string }

export const station = reactive({
  /** 服务连接：null = 连接中（尚无结果） */
  online: null as boolean | null,
  /** 设备状态：tcp = 已登记地址文本；usb = 探针/串口清单；none = 未选设备；init = 扫描中 */
  devMode: "init" as "init" | "tcp" | "usb" | "none",
  devText: "",
  devRows: [] as DevRow[],
  targets: [] as DdOption[],
  probes: [] as DdOption[],
  ports: [{ value: "auto", label: "自动选择" }] as DdOption[],
  target: "",
  probe: "",
  port: "auto",
  flashState: "idle",
  serialOpen: false,
  serialText: "未打开",
  fwDir: "",
});

let targetGeneration = -1;
let lastPorts: UsbEntry[] = [];

const FLASH_MAP: Record<string, [string, string]> = {
  idle: ["neutral", "空闲"],
  running: ["yellow", "烧录中…"],
  success: ["ok", "烧录成功"],
  failed: ["danger", "烧录失败"],
};

/** 烧录状态徽标 [样式, 文案] */
export function flashBadge(): [string, string] {
  return FLASH_MAP[station.flashState] || ["neutral", station.flashState];
}

function setOptions(key: "targets" | "probes" | "ports", next: DdOption[]) {
  if (!sameOptions(station[key], next)) station[key] = next;
}

function renderDevices(d: StatusBody["devices"]) {
  if (deviceCtx.isTcp) {
    const cur = deviceCtx.current!;
    station.devMode = "tcp";
    station.devText = `单STA · ${cur.name || cur.id}
TCP-DAP：${cur.host}:${cur.dap_port || 4441}
TCP-UART：${cur.host}:${cur.uart_port || 20010}
已登记地址；实际连接状态见对应业务。`;
    return;
  }
  if (!deviceCtx.id) { station.devMode = "none"; return; }
  const rows: DevRow[] = [];
  for (const [kind, entries] of [["调试器", d.probes || []], ["串口", d.ports || []]] as [string, UsbEntry[]][]) {
    for (const p of entries) {
      rows.push({
        kind,
        label: `[${kind}] ${p.label}`,
        detail: `${kind === "调试器" ? `VID:PID=${p.vid}:${p.pid}` : p.port}  SN=${p.serial || "-"}`,
      });
    }
  }
  station.devMode = "usb";
  station.devRows = rows;
}

function renderTargets(targets: StatusBody["targets"]) {
  setOptions("targets", targets.map((t) => ({ value: t.key, label: t.label })));
  if (targetGeneration !== deviceCtx.generation) {
    if (deviceCtx.current?.target) station.target = deviceCtx.current.target;
    targetGeneration = deviceCtx.generation;
  }
}

function renderProbes(probes: StatusBody["probes"]) {
  setOptions("probes", probes.map((p) => ({ value: p.key, label: p.label })));
}

function renderSerialState(s: StatusBody["serial"]) {
  station.serialOpen = !!s.open;
  if (s.open) {
    const cur = deviceCtx.current;
    station.serialText = deviceCtx.isTcp && cur
      ? `TCP 串口已连接 · ${cur.host}:${cur.uart_port || 20010}`
      : `已打开: ${s.port} @ ${s.baudrate}`;
  } else {
    station.serialText = "未打开";
  }
  setOptions("ports", [{ value: "auto", label: "自动选择" }].concat(lastPorts.map((p) => ({
    value: String(p.port), label: `${p.port} (${p.label || "未知"})`,
  }))));
}

export async function refreshStatus(): Promise<void> {
  try {
    const generation = deviceCtx.generation;
    const r = await dfetch(deviceCtx.id ? deviceCtx.api("status") : "/api/status");
    const s = await r.json() as StatusBody;
    if (generation !== deviceCtx.generation) return;
    lastPorts = s.devices.ports || [];
    renderDevices(s.devices);
    renderTargets(s.targets);
    renderProbes(s.probes);
    station.flashState = s.flash.state;
    renderSerialState(s.serial);
    station.fwDir = s.firmware_dir;
    station.online = true;
  } catch {
    station.online = false;
  }
}

/** 切换设备时：目标芯片跟随新设备的登记值（旧版 onChange 里的同名逻辑） */
export function applyDeviceTarget(): void {
  if (deviceCtx.current?.target) station.target = deviceCtx.current.target;
}
