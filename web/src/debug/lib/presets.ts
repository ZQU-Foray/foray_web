/* 串口指令预设（移植自旧版 static/app.js「预设配置」段）：按当前设备读写，
 * 设置页编辑 JSON，串口页按 commands 生成快捷按钮（名称等外部字符串按文字渲染）。 */
import { reactive } from "vue";
import { dfetch, jsonInit } from "./api";
import { deviceCtx } from "./device";
import { toast } from "./toast";

export interface PresetCommand {
  id?: string;
  name?: string;
  data?: string;
  encoding?: string;
  newline?: string;
}
export interface PresetsDoc {
  baudrate?: number;
  commands?: PresetCommand[];
  [k: string]: unknown;
}

export const presets = reactive({
  doc: null as PresetsDoc | null,
  /** 设置页 JSON 编辑框 */
  text: "",
  /** 串口页波特率输入框（读到预设时回填） */
  baud: "115200",
});

export async function loadPresets(): Promise<void> {
  const generation = deviceCtx.generation;
  if (!deviceCtx.id) return;
  try {
    const r = await dfetch(deviceCtx.api("presets"));
    const doc = await r.json() as PresetsDoc;
    if (generation !== deviceCtx.generation) return;
    presets.doc = doc;
    presets.text = JSON.stringify(doc, null, 2);
    presets.baud = String(doc.baudrate || 115200);
  } catch { /* 忽略 */ }
}

/** 设备切换时清空（旧版 onChange：state.presets = null、编辑框清空、按钮清空） */
export function resetPresets(): void {
  presets.doc = null;
  presets.text = "";
}

export async function savePresets(): Promise<void> {
  let data: unknown;
  try {
    data = JSON.parse(presets.text);
  } catch (e) {
    toast("JSON 格式错误: " + e, true);
    return;
  }
  try {
    const r = await dfetch(deviceCtx.api("presets"), jsonInit("PUT", data));
    if (!r.ok) throw new Error(await r.text());
    presets.doc = await r.json() as PresetsDoc;
    toast("预设已保存");
  } catch (e) {
    toast("保存失败: " + e, true);
  }
}
