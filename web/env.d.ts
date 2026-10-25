/* 单文件组件的类型声明：让 .ts 文件可以导入 .vue（完整的模板类型检查用 vue-tsc）。 */
declare module "*.vue" {
  import type { DefineComponent } from "vue";
  const component: DefineComponent<object, object, unknown>;
  export default component;
}

/* 服务端注入的首帧状态（见 app/main.py 的 _serve_app_page） */
interface Window {
  __FORAY_BOOT__?: {
    who?: { display: string; role: string } | null;
    sync?: {
      has_snapshot: boolean;
      last_success_at: number | null;
      stale: boolean;
      last_error: string | null;
    } | null;
  };
}
