/* 发射调试的数据结构（与固件遥测 / 服务端 /shoot/status 的字段一一对应） */

export interface SlotTelem {
  rpm: number;
  deg: number;
  current: number;
  torque: number;
  temperature: number;
  fault: number;
  mode: number;
  model: number;
  enabled: number;
}

export interface Telemetry {
  slots: SlotTelem[];
  link: number;
}

export interface Profile {
  id: number;
  name: string;
  gear_ratio: number;
  max_output_rpm?: number;
  max_torque?: number;
}

export interface ShootStatus {
  open: boolean;
  port?: string;
  baudrate?: number;
  telemetry?: Telemetry | null;
  profiles?: Profile[];
  defaults?: Record<string, number>;
}

export interface MotorPreset {
  name: string;
  params?: Record<string, number>;
}

export interface PresetCell {
  presets: MotorPreset[];
  default: number;
}

export interface MotorPresetsDoc {
  motor_presets: Record<string, Record<string, PresetCell>>;
}

/** /shoot/<命令> 的应答（ok = 固件 ACK 成功，status = 固件返回的状态字；
 *  open/close 的 status 是会话状态对象） */
export interface CmdResult {
  ok?: boolean;
  status?: unknown;
  rtt_ms?: number;
  [k: string]: unknown;
}
