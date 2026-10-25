/* 轻量路由（History 模式）。
 * 离线开发环境装不上 vue-router，这里只实现本项目用到的子集，接口刻意与
 * vue-router 对齐（RouterLink / RouterView / useRoute / useRouter.push），
 * 将来联网后可直接换成 vue-router，页面代码基本不用改。
 * 约定：路径参数只支持 "/xxx/:id" 这种单段形式；查询串原样保留在 route.query。 */
import { defineComponent, h, shallowRef, type Component, type PropType } from "vue";

export interface RouteDef {
  path: string;                  // 例："/tasks/:id"
  name: string;
  component: Component;
  meta?: Record<string, unknown>;
}

export interface Route {
  path: string;
  name: string;
  params: Record<string, string>;
  query: URLSearchParams;
  fullPath: string;
  def: RouteDef | null;
}

interface Compiled { def: RouteDef; re: RegExp; keys: string[] }

let compiled: Compiled[] = [];
const current = shallowRef<Route>(blank());
const listeners: Array<(to: Route, from: Route) => void> = [];

function blank(): Route {
  return { path: "/", name: "", params: {}, query: new URLSearchParams(), fullPath: "/", def: null };
}

function compile(def: RouteDef): Compiled {
  const keys: string[] = [];
  const src = def.path
    .replace(/\/+$/, "")
    .replace(/:([A-Za-z_]+)/g, (_m, k: string) => { keys.push(k); return "([^/]+)"; });
  return { def, keys, re: new RegExp("^" + (src || "") + "/?$") };
}

function resolve(fullPath: string): Route {
  const url = new URL(fullPath, location.origin);
  const path = url.pathname;
  for (const c of compiled) {
    const m = c.re.exec(path);
    if (!m) continue;
    const params: Record<string, string> = {};
    c.keys.forEach((k, i) => { params[k] = decodeURIComponent(m[i + 1]); });
    return { path, name: c.def.name, params, query: url.searchParams, fullPath: path + url.search, def: c.def };
  }
  return { path, name: "", params: {}, query: url.searchParams, fullPath: path + url.search, def: null };
}

function commit(to: Route): void {
  const from = current.value;
  current.value = to;
  listeners.forEach((fn) => { try { fn(to, from); } catch (e) { console.error(e); } });
}

/** 站内路径是否由本路由表接管（否则走普通导航） */
export function isRouted(fullPath: string): boolean {
  try {
    const url = new URL(fullPath, location.href);
    if (url.origin !== location.origin) return false;
    return !!resolve(url.pathname + url.search).def;
  } catch {
    return false;
  }
}

export const router = {
  push(to: string): void {
    const r = resolve(to);
    if (!r.def) { location.href = to; return; }
    if (r.fullPath !== current.value.fullPath) history.pushState(null, "", r.fullPath);
    commit(r);
    window.scrollTo(0, 0);
  },
  replace(to: string): void {
    const r = resolve(to);
    if (!r.def) { location.replace(to); return; }
    history.replaceState(null, "", r.fullPath);
    commit(r);
  },
  back(): void { history.back(); },
  afterEach(fn: (to: Route, from: Route) => void): void { listeners.push(fn); },
};

/** 安装路由表并解析当前地址；返回初始路由 */
export function createRouter(defs: RouteDef[]): Route {
  compiled = defs.map(compile);
  window.addEventListener("popstate", () => commit(resolve(location.pathname + location.search)));
  const first = resolve(location.pathname + location.search);
  current.value = first;
  return first;
}

export function useRoute() { return current; }
export function useRouter() { return router; }

/** <RouterLink to="/tasks/xx">：渲染成普通 <a href>，左键无修饰键点击时走站内导航 */
export const RouterLink = defineComponent({
  name: "RouterLink",
  props: { to: { type: String as PropType<string>, required: true } },
  setup(props, { slots, attrs }) {
    function onClick(e: MouseEvent) {
      if (e.defaultPrevented || e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const t = (attrs as Record<string, unknown>).target;
      if (t && t !== "_self") return;
      if (!isRouted(props.to)) return;
      e.preventDefault();
      router.push(props.to);
    }
    return () => h("a", { href: props.to, onClick }, slots.default ? slots.default() : props.to);
  },
});

/** <RouterView />：渲染当前路由组件；路径变化即换新实例（详情页互不串数据） */
export const RouterView = defineComponent({
  name: "RouterView",
  setup() {
    return () => {
      const r = current.value;
      return r.def ? h(r.def.component, { key: r.path }) : null;
    };
  },
});
