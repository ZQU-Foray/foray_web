/* 门户入口：样式 → 路由 → 身份/元数据 → 挂载 → 轮询 */
import "@/shared/styles/index.css";
import { createApp } from "vue";
import App from "./App.vue";
import { createRouter } from "@/shared/router";
import { playBootIfPending } from "@/shared/bootCover";
import NotFound from "./pages/NotFound.vue";
import { ROUTES } from "./routes";
import { loadMe, loadMeta, startPolling } from "./store";

createRouter(ROUTES, NotFound);
void loadMe();
void loadMeta();
createApp(App).mount("#app");
playBootIfPending();
startPolling();
