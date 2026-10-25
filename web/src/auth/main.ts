/* 登录入口：/login（登录）、/onboarding（开通本站账号）、/reset（重设密码）共用一个入口，按路径渲染 */
import "@/shared/styles/index.css";
import "./auth.css";
import { createApp, type Component } from "vue";
import Login from "./Login.vue";
import Onboarding from "./Onboarding.vue";
import Reset from "./Reset.vue";

const path = location.pathname.replace(/\/+$/, "") || "/login";
const pages: Record<string, { comp: Component; title: string }> = {
  "/login": { comp: Login, title: "登录" },
  "/onboarding": { comp: Onboarding, title: "设置本站账号" },
  "/reset": { comp: Reset, title: "重设密码" },
};
const page = pages[path] || pages["/login"];
document.title = "Foray 信息面板 · " + page.title;
document.documentElement.removeAttribute("data-boot");   // 登录页不播开机引导
createApp(page.comp).mount("#app");
