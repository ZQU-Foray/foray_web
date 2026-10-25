/* 调试台入口：设计系统样式 → 调试台样式 → 挂载外壳与四个页签 → 首启动画 → 启动轮询与连接。
 * 设备上下文挂到 window.deviceCtx（与旧版同名，回归用例与现场排障都按它读当前设备）。 */
import "@/shared/styles/index.css";
import "./styles/debug.css";
import { createApp } from "vue";
import App from "./App.vue";
import { playBootIfPending } from "@/shared/bootCover";
import { deviceCtx } from "./lib/device";
import { startDebugConsole } from "./lib/start";

window.deviceCtx = deviceCtx;
createApp(App).mount("#app");
playBootIfPending();
startDebugConsole();
