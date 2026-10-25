/* Foray 前端构建配置。
 * - 三个入口：portal（门户单页应用）/ debug（调试台）/ auth（登录、开通、重设密码）；
 * - 构建产物输出到 ../static/app（随代码入库；服务器上没有 node，不在服务器构建）；
 * - 页面 HTML 由 FastAPI 读取并注入首帧状态后返回，静态资源挂在 /app/assets/；
 * - 字体等大文件继续由 /ui/ 提供（不进打包），HTML 与 CSS 里的 /ui/ 绝对路径原样保留。 */
import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";
import { defineConfig, type Plugin } from "vite";
import vue from "@vitejs/plugin-vue";

const API_TARGET = process.env.FORAY_API || "http://127.0.0.1:8000";

/* 首帧脚本：必须在 <head> 里同步执行（侧栏收纳态、页间过场、开机引导遮底），
   打包后的模块脚本是延迟执行的，赶不上首帧，所以以内联普通脚本注入。 */
function headEarly(): Plugin {
  const src = readFileSync(new URL("./src/shared/head-early.js", import.meta.url), "utf-8");
  return {
    name: "foray-head-early",
    transformIndexHtml(html) {
      return html.replace("<!--HEAD_EARLY-->", "<script>" + src + "</script>");
    },
  };
}

/* 开发服务器：把门户/登录/调试台路径改写到对应入口（生产由 FastAPI 负责）。 */
function devRoutes(): Plugin {
  const portal = /^\/(projects|tasks|schedule|procurement)(\/[^/]+)?\/?$|^\/(quality|attendance)?$/;
  return {
    name: "foray-dev-routes",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        const url = req.url || "/";
        const path = url.split("?")[0];
        const qs = url.slice(path.length);
        if (portal.test(path)) req.url = "/portal.html" + qs;
        else if (/^\/(login|onboarding|reset)\/?$/.test(path)) req.url = "/auth.html" + qs;
        else if (/^\/debug\/?$/.test(path)) req.url = "/debug.html" + qs;
        next();
      });
    },
  };
}

export default defineConfig(({ command }) => ({
  base: command === "build" ? "/app/" : "/",
  plugins: [vue(), headEarly(), devRoutes()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    outDir: process.env.FORAY_OUT_DIR || "../static/app",
    emptyOutDir: true,
    assetsDir: "assets",
    /* CSS 压缩用 esbuild：不依赖 lightningcss 原生包，离线环境也能构建 */
    cssMinify: "esbuild",
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      input: {
        portal: fileURLToPath(new URL("./portal.html", import.meta.url)),
        debug: fileURLToPath(new URL("./debug.html", import.meta.url)),
        auth: fileURLToPath(new URL("./auth.html", import.meta.url)),
      },
      output: {
        /* 三个入口共用的部分单独成块：vendor（Vue 运行时）与 shared（设计系统与外壳） */
        advancedChunks: {
          groups: [
            { name: "vendor", test: /[\\/]node_modules[\\/]/, priority: 20 },
            { name: "shared", test: /[\\/]src[\\/]shared[\\/]/, priority: 10 },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API_TARGET },
      "/auth": { target: API_TARGET },
      "/ws": { target: API_TARGET, ws: true },
      "/ui": { target: API_TARGET },
    },
  },
}));
