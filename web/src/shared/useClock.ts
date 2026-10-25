/* 顶栏时钟：30 秒刷新一次（只显示到分钟） */
import { onBeforeUnmount, onMounted, ref } from "vue";
import { clockText } from "./format";

export function useClock() {
  const text = ref(clockText());
  let timer = 0;
  onMounted(() => { timer = window.setInterval(() => { text.value = clockText(); }, 30000); });
  onBeforeUnmount(() => window.clearInterval(timer));
  return text;
}
