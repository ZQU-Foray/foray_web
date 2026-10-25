/* 调试台的通知入口：沿用旧版 shoot.js 的 toast(文案, 是否错误) 调用方式，
 * 实际由共享的右侧通知框（@/shared/toast + ToastStack）弹出。 */
import { toast as show } from "@/shared/toast";

export function toast(text: string, err = false): void {
  show(text, err ? "error" : "info");
}
