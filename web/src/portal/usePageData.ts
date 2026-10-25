/* 页面数据加载：首次进入拉取一次；之后在「筛选变化 / 轮询发现新版本 / 写入后」重新拉取。
 * - 同时取 sync-status 与本页数据（与旧版一致），信封统一刷新侧栏同步状态；
 * - 快速连续触发时只认最后一次（generation 闸门）；
 * - 刷新失败保留上次内容，弹出通知说明原因；列表被截断时也弹通知；
 *   同一条提示不随每次刷新重复弹（连续失败只弹一次，成功后复位）；
 * - 数据原地替换，Vue 只修补变化的节点：滚动位置、键盘焦点、入场动画都不受刷新影响。 */
import { onMounted, ref, shallowRef, watch, type WatchSource } from "vue";
import { fetchSyncStatus } from "./api";
import { applyEnvelope, store } from "./store";
import type { Envelope } from "./types";
import { HttpError } from "@/shared/http";
import { toast } from "@/shared/toast";

interface Body extends Partial<Envelope> {
  has_snapshot?: boolean;
  has_more?: boolean;
  total?: number;
  data?: unknown;
}

export function usePageData<T extends Body>(loader: () => Promise<T>, deps?: WatchSource[]) {
  const body = shallowRef<T | null>(null);
  const loaded = ref(false);
  const notFound = ref(false);
  let gen = 0;
  let lastMore = "";               // 上次弹过的「列表截断」通知
  let lastErr = "";                // 上次弹过的加载失败通知

  function fail(text: string): void {
    if (text !== lastErr) toast(text, "error");
    lastErr = text;
  }

  async function reload(): Promise<void> {
    const my = ++gen;
    try {
      const [env, res] = await Promise.all([fetchSyncStatus(), loader()]);
      if (my !== gen) return;
      applyEnvelope(env);
      if (res.has_snapshot === false) {
        store.noSnapshot = true;
        store.sync.has_snapshot = false;
        return;
      }
      store.noSnapshot = false;
      notFound.value = false;
      lastErr = "";
      const n = Array.isArray(res.data) ? res.data.length : 0;
      const more = res.has_more
        ? "列表仅显示前 " + n + " 条（共 " + res.total + " 条）——请用筛选缩小范围。"
        : "";
      if (more && more !== lastMore) toast(more, "info");
      lastMore = more;
      body.value = res;
      loaded.value = true;
    } catch (e) {
      if (my !== gen) return;
      const st = e instanceof HttpError ? e.status : 0;
      const msg = e instanceof Error ? e.message : "未知错误";
      if (st === 401) return;                       // 已跳登录页
      if (st === 403) fail("暂时无法确认成员身份（上游复核不可用，非退出组织）");
      else if (st === 404) { notFound.value = true; fail("记录不存在：" + (msg || "已从源表移除")); }
      else fail("页面数据加载失败（" + msg + "）——展示上次内容，可点『刷新』重试");
    }
  }

  onMounted(reload);
  watch(() => store.refreshSeq, reload);
  if (deps && deps.length) watch(deps, reload, { deep: true });

  return { body, loaded, notFound, reload };
}
