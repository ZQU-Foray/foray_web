/* 固件管理（移植自旧版 static/app.js「固件管理」段）：固件库是全局的（不随设备变化），
 * 只在启动、上传、删除后刷新。固件名是外部字符串，模板里一律按文字渲染。 */
import { reactive } from "vue";
import { dfetch, errorText } from "./api";
import type { DdOption } from "./dropdown";

export interface FirmwareItem { name: string; size: number; mtime: number }

export const firmware = reactive({
  items: [] as FirmwareItem[],
  loaded: false,
  /** 烧录参数里的固件选择（首次加载前保留旧版的「（无固件）」占位） */
  options: [{ value: "", label: "（无固件）" }] as DdOption[],
  selected: "",
});

export function fwSize(f: FirmwareItem): string {
  return `${(f.size / 1024).toFixed(1)} KB`;
}

export async function reloadFirmware(): Promise<void> {
  try {
    const r = await dfetch("/api/firmware");
    const d = await r.json() as { items?: FirmwareItem[] };
    const items = d.items || [];
    firmware.options = items.map((f) => ({ value: f.name, label: `${f.name} (${fwSize(f)})` }));
    firmware.items = items;
    firmware.loaded = true;
  } catch { /* 忽略：下次操作后再刷新 */ }
}

export async function deleteFirmware(name: string): Promise<void> {
  if (!confirm(`删除固件 ${name}？`)) return;
  try {
    await dfetch("/api/firmware/" + encodeURIComponent(name), { method: "DELETE" });
  } catch { /* 与旧版一致：失败也刷新列表 */ }
  await reloadFirmware();
}

/** 上传；成功返回 true（调用方据此清空文件框） */
export async function uploadFirmware(file: File | null | undefined): Promise<boolean> {
  if (!file) { alert("请选择文件"); return false; }
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await dfetch("/api/firmware/upload", { method: "POST", body: fd });
    if (!r.ok) throw new Error(await errorText(r));
    void reloadFirmware();
    return true;
  } catch (e) {
    alert("上传失败: " + e);
    return false;
  }
}
