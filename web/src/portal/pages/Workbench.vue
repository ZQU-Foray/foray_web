<script setup lang="ts">
/* 工作台：卡片按「每个人自己」的布局排列，布局存在服务端（workbench_layouts），
 * 换浏览器 / 换设备进来看到的还是同一份；卡片数据仍由服务端按布局现算，口径同各页面。
 * 拖拽用浏览器原生 HTML5 拖放（不引依赖：前端装不上新包），键盘用户另有上移/下移按钮。 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import PortalPage from "../components/PortalPage.vue";
import ConfirmButton from "../components/ConfirmButton.vue";
import RankList from "../components/RankList.vue";
import CheckinButton from "../components/CheckinButton.vue";
import { navNo } from "../nav";
import { fetchAttendance, fetchWorkbench, resetWorkbench, saveWorkbench } from "../api";
import { requestRefresh, store } from "../store";
import { usePageData } from "../usePageData";
import { RouterLink } from "@/shared/router";
import { errorDetail } from "@/shared/http";
import { fmtDT } from "@/shared/format";
import { toast } from "@/shared/toast";
import type {
  CardWidth, MemberRank, Procurement, Task, WorkbenchAttendanceRow, WorkbenchCatalogItem,
  WorkbenchCard, WorkbenchDutyPerson, WorkbenchLayoutItem, WorkbenchMilestoneRow,
  WorkbenchProjectRow,
} from "../types";

const { body, loaded, reload } = usePageData(fetchWorkbench);

const catalog = computed<WorkbenchCatalogItem[]>(() => (body.value && body.value.catalog) || []);
const cards = computed<WorkbenchCard[]>(() => (body.value && body.value.cards) || []);
const savedOnce = computed(() => !!(body.value && body.value.saved));
const updatedAt = computed(() => (body.value && body.value.updated_at) || null);

/* 布局是本地可编辑副本：拖拽/增删先改它，保存成功后再由服务端返回的 cards 对齐 */
const layout = ref<WorkbenchLayoutItem[]>([]);
const dirty = ref(false);
watch(body, (b) => {
  if (!b || dirty.value) return;
  layout.value = b.layout.map((x) => ({ ...x }));
}, { immediate: true });

const titleOf = (id: string): string => {
  const it = catalog.value.find((c) => c.id === id);
  return it ? it.title : id;
};
const unused = computed(() => catalog.value.filter(
  (c) => !layout.value.some((l) => l.card_id === c.id)));

/* ---- 保存：任何一次改动都立刻落库，失败时保留本地改动并提示重试 ---- */
const saving = ref(false);
const saveError = ref("");
async function persist(next: WorkbenchLayoutItem[], tip: string): Promise<void> {
  layout.value = next;
  dirty.value = true;
  saving.value = true;
  saveError.value = "";
  try {
    const res = await saveWorkbench(next);
    if (!res.ok) {
      saveError.value = errorDetail(res);
      toast("布局保存失败：" + saveError.value, "error");
      return;
    }
    dirty.value = false;
    toast(tip, "ok");
    await reload();
  } catch (e) {
    saveError.value = e instanceof Error ? e.message : "网络错误";
    toast("布局保存失败：" + saveError.value, "error");
  } finally {
    saving.value = false;
  }
}

function retrySave(): void {
  void persist(layout.value, "布局已保存");
}

/* ---- 配置模式：默认只看功能卡片，进配置模式才露出排序、宽度、增删与卡片库 ---- */
const configMode = ref(false);
function toggleConfig(): void {
  configMode.value = !configMode.value;
  if (!configMode.value) resetDrag();
}

/* ---- 占满一屏：功能模式下把卡片区撑到视口底部，页面不用滚就能看完一屏 ---- */
const stageRef = ref<HTMLElement | null>(null);
let stageObserver: ResizeObserver | null = null;

function fitStage(): void {
  const el = stageRef.value;
  if (!el) return;
  if (configMode.value) {
    /* 配置模式下要露出卡片库，高度随内容长，不再强行占满 */
    el.style.minHeight = "";
    return;
  }
  const head = document.querySelector(".page-head") as HTMLElement | null;
  const body = el.parentElement;
  const cs = body ? getComputedStyle(body) : null;
  const pad = cs ? parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) : 0;
  const headBottom = head ? head.offsetTop + head.offsetHeight : 0;
  const avail = window.innerHeight - headBottom - pad;
  el.style.minHeight = Math.max(320, Math.round(avail)) + "px";
}

onMounted(() => {
  void loadAttendState();
  fitStage();
  window.addEventListener("resize", fitStage);
  /* 页头高度会随描述换行变化（窄屏尤其明显），跟着它一起重算 */
  const head = document.querySelector(".page-head");
  if (head && typeof ResizeObserver !== "undefined") {
    stageObserver = new ResizeObserver(() => fitStage());
    stageObserver.observe(head);
  }
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", fitStage);
  if (stageObserver) stageObserver.disconnect();
  stageObserver = null;
});

watch(configMode, () => fitStage());

/* ---- 拖拽排序 ---- */
const dragFrom = ref(-1);
const dragOver = ref(-1);
function onDragStart(i: number, e: DragEvent): void {
  if (!configMode.value) return;
  dragFrom.value = i;
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(i));
  }
}
function onDragOver(i: number, e: DragEvent): void {
  e.preventDefault();
  dragOver.value = i;
  if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
}
function onDrop(i: number): void {
  const from = dragFrom.value;
  resetDrag();
  if (from < 0 || from === i) return;
  moveTo(from, i);
}
function resetDrag(): void {
  dragFrom.value = -1;
  dragOver.value = -1;
}
function moveTo(from: number, to: number): void {
  const next = layout.value.map((x) => ({ ...x }));
  const [it] = next.splice(from, 1);
  next.splice(to, 0, it);
  void persist(next, "顺序已更新");
}

/* ---- 键盘可用的上下移动（拖拽的等价操作） ---- */
function nudge(i: number, delta: number): void {
  const to = i + delta;
  if (to < 0 || to >= layout.value.length) return;
  moveTo(i, to);
}

function setWidth(i: number, width: CardWidth): void {
  const next = layout.value.map((x) => ({ ...x }));
  next[i] = { ...next[i], width };
  void persist(next, "宽度已更新");
}

function addCard(id: string): void {
  const meta = catalog.value.find((c) => c.id === id);
  const next = layout.value.concat([{ card_id: id, width: meta ? meta.width : "half" }]);
  void persist(next, "已添加「" + titleOf(id) + "」");
}

function removeCard(i: number): void {
  const next = layout.value.filter((_, idx) => idx !== i);
  void persist(next, "已移除「" + titleOf(layout.value[i].card_id) + "」");
}

const resetting = ref(false);
async function reset(): Promise<void> {
  resetting.value = true;
  saveError.value = "";
  try {
    const res = await resetWorkbench();
    if (!res.ok) {
      toast("恢复默认失败：" + errorDetail(res), "error");
      return;
    }
    dirty.value = false;
    toast("已恢复默认布局", "ok");
    await reload();
  } catch {
    toast("网络错误，布局未改动", "error");
  } finally {
    resetting.value = false;
  }
}

/* ---- 各卡的数据取值：服务端已按卡片类型组装好，这里只做类型收敛 ---- */
function cardOf(id: string): WorkbenchCard | undefined {
  return cards.value.find((c) => c.id === id);
}
function items<T>(c: WorkbenchCard | undefined): T[] {
  return (c && c.data && (c.data.items as T[])) || [];
}
const myAttendance = computed(() => {
  const c = cardOf("my_attendance");
  return { card: c, row: (c && c.data && c.data.item) as WorkbenchAttendanceRow | null,
           threshold: (c && c.data && c.data.threshold) || 0,
           note: (c && c.data && c.data.note) || "" };
});
/* 打卡按钮的「签到 / 签退」以服务端 status 为准：花名册快照还没同步出本人记录时，
 * 卡片正文可能只有一句「找不到记录」，但签到本身仍可用（服务端会建行）。 */
const checkedIn = ref(false);
async function loadAttendState(): Promise<void> {
  try {
    const st = await fetchAttendance();
    checkedIn.value = !!st.checked_in;
  } catch {
    /* 读不到状态就先按「未签到」显示，真点下去服务端会给出准话 */
  }
}
/* 轮询或别处写入后，打卡按钮的状态也跟着服务端走一遍 */
watch(() => store.refreshSeq, loadAttendState);
/* 打卡返回后让整页数据重拉：签到态与本周工时都是服务端算的，前端不自己改数 */
function onCheckinDone(): void {
  requestRefresh();
  void loadAttendState();
}
function tasksOf(id: string): Task[] {
  return items<Task>(cardOf(id));
}
function projectsOf(): WorkbenchProjectRow[] {
  return items<WorkbenchProjectRow>(cardOf("project_progress"));
}
function milestonesOf(): WorkbenchMilestoneRow[] {
  return items<WorkbenchMilestoneRow>(cardOf("milestones"));
}
function dutyOf(): { people: WorkbenchDutyPerson[]; source: string | null; date: string } {
  const c = cardOf("duty");
  return { people: items<WorkbenchDutyPerson>(c),
           source: (c && c.data && c.data.source) || null,
           date: (c && c.data && c.data.date) || "" };
}
function rankOf(): MemberRank[] {
  return items<MemberRank>(cardOf("attendance_rank"));
}
function procurementOf(): Procurement[] {
  return items<Procurement>(cardOf("my_procurement"));
}
function qualityOf(): { total: number; byTable: Array<{ table_name: string; count: number }> } {
  const c = cardOf("data_quality");
  return { total: (c && c.data && c.data.total) || 0,
           byTable: (c && c.data && c.data.by_table) || [] };
}
const EMPTY_BACKLOG = { pending: 0, pushing: 0, failed: 0, open_conflicts: 0 };
function syncOf(): {
  lastSuccess: number | null; stale: boolean; error: string | null;
  backlog: typeof EMPTY_BACKLOG;
} {
  const c = cardOf("sync_status");
  const d = (c && c.data) || {};
  return { lastSuccess: d.last_success_at || null, stale: !!d.stale,
           error: d.last_error || null, backlog: d.backlog || EMPTY_BACKLOG };
}
const TABLE_LABEL: Record<string, string> = {
  projects: "项目", tasks: "事项", milestones: "重要节点", schedule: "公共日程",
  procurement: "物资采购", members: "成员数据", duty: "值日表", roster: "花名册",
};
function taskMeta(t: Task): string {
  const bits: string[] = [];
  if (t.status) bits.push(t.status);
  if (t.priority) bits.push(t.priority);
  if (t.owner_group) bits.push(t.owner_group);
  return bits.join(" │ ");
}
function taskDue(t: Task): string {
  if (!t.due) return "";
  const base = fmtDT(t.due.ts);
  if (t.overdue) return base + " │ 逾期";
  if (t.due_soon) return base + " │ 临期";
  return base;
}
function countdown(m: WorkbenchMilestoneRow): string {
  const c = m.countdown_days;
  if (!m.target) return "";
  const base = fmtDT(m.target.ts);
  if (!c) return base;
  return base + " │ 还有 " + c.days + " 天";
}
</script>

<template>
  <PortalPage :no="navNo('/')" en="PERSONAL │ WORKBENCH" title="工作台"
              desc="自己的事项与考勤 │ 需要调整时进配置模式" watermark="WORKBENCH">
    <template #actions>
      <button v-if="!configMode" type="button" class="btn ghost" id="workbench-config"
              @click="toggleConfig">配置</button>
      <template v-else>
        <ConfirmButton label="恢复默认布局" armed-label="再点一次恢复" :busy="resetting" @confirm="reset" />
        <button type="button" class="btn" id="workbench-done" @click="toggleConfig">完成</button>
      </template>
    </template>

    <div class="wb-stage" :class="{ 'is-config': configMode }" ref="stageRef">
      <p id="workbench-status" class="note" role="status" v-if="configMode || saving || saveError">
        <template v-if="saving">正在保存布局…</template>
        <template v-else-if="saveError">布局未保存：{{ saveError }}
          <button type="button" class="btn ghost xs" @click="retrySave">重试保存</button>
        </template>
        <template v-else-if="savedOnce">布局已保存在服务端<template v-if="updatedAt">（{{ fmtDT(updatedAt) }}）</template></template>
        <template v-else>当前是默认布局，改一次就会记住你的选择</template>
      </p>

      <p v-if="loaded && !cards.length" id="workbench-empty" class="empty">
        还没有可显示的卡片——点右上角「配置」添加几张吧
      </p>

      <div id="workbench-grid" class="wb-grid">
        <article v-for="(it, i) in layout" :key="it.card_id"
                 class="wb-card" :class="'is-' + it.width"
                 :data-card="it.card_id" :draggable="configMode"
                 :aria-label="titleOf(it.card_id)"
                 :data-drag-over="dragOver === i ? 'true' : undefined"
                 @dragstart="onDragStart(i, $event)" @dragover="onDragOver(i, $event)"
                 @drop="onDrop(i)" @dragend="resetDrag">
          <header class="wb-head">
            <h3 class="wb-title">{{ titleOf(it.card_id) }}</h3>
            <div class="wb-tools" v-if="configMode">
              <button type="button" class="btn ghost xs" :disabled="i === 0"
                        aria-label="上移" @click="nudge(i, -1)">↑</button>
                <button type="button" class="btn ghost xs" :disabled="i === layout.length - 1"
                        aria-label="下移" @click="nudge(i, 1)">↓</button>
                <button type="button" class="btn ghost xs" :aria-pressed="it.width === 'half'"
                        :aria-label="it.width === 'half' ? '改为整屏' : '改为半屏'"
                        @click="setWidth(i, it.width === 'half' ? 'full' : 'half')">
                  {{ it.width === 'half' ? '半屏' : '整屏' }}
                </button>
                <button type="button" class="btn ghost xs" :aria-label="'移除' + titleOf(it.card_id)"
                        @click="removeCard(i)">移除</button>
              </div>
            </header>

            <p v-if="(cardOf(it.card_id) || {}).error" class="wb-err">
              {{ (cardOf(it.card_id) || {}).error }}
            </p>

            <!-- 我的考勤 -->
            <template v-else-if="it.card_id === 'my_attendance'">
              <p v-if="myAttendance.note" class="empty">{{ myAttendance.note }}</p>
              <template v-else-if="myAttendance.row">
                <p class="wb-metric">
                  <b>{{ myAttendance.row.week_hours == null ? "—" : myAttendance.row.week_hours.toFixed(2) }}</b>
                  <span class="wb-unit">h / 本周（达标线 {{ myAttendance.threshold }} h）</span>
                </p>
                <p class="wb-line">
                  <span class="badge" :class="checkedIn ? 'ok' : 'neutral'">
                    {{ checkedIn ? '已签到' : '未签到' }}
                  </span>
                  <span v-if="checkedIn && myAttendance.row.checked_in_since" class="row-meta">
                    自 {{ fmtDT(myAttendance.row.checked_in_since) }}
                  </span>
                  <span v-if="myAttendance.row.leave" class="badge warn">本周请假</span>
                  <span v-if="myAttendance.row.pending" class="badge neutral">待结算</span>
                </p>
              </template>
              <!-- 签到 / 签退入口：工作台就是默认首页，不用为了打卡再跑考勤页；
                   本行不依赖上面的花名册行，新成员第一次签到也能点 -->
              <p class="wb-line wb-attend-row">
                <CheckinButton :checked-in="checkedIn" btn-id="workbench-attend-btn" @done="onCheckinDone" />
                <span class="row-meta">网段校验、跨零点作废等规则在服务端</span>
              </p>
            </template>

            <!-- 我的事项 / 临期与逾期 -->
            <template v-else-if="it.card_id === 'my_tasks' || it.card_id === 'urgent_tasks'">
              <p v-if="!tasksOf(it.card_id).length" class="empty">
                {{ it.card_id === 'my_tasks' ? '名下没有未完成的事项' : '没有临期或逾期的事项' }}
              </p>
              <ul v-else class="wb-list">
                <li v-for="t in tasksOf(it.card_id)" :key="t.id" class="wb-row">
                  <RouterLink :to="'/tasks/' + encodeURIComponent(t.id)" class="lnk">{{ t.name }}</RouterLink>
                  <span class="row-meta">{{ taskMeta(t) }}</span>
                  <span v-if="taskDue(t)" class="row-meta">{{ taskDue(t) }}</span>
                </li>
              </ul>
            </template>

            <!-- 项目进度 -->
            <template v-else-if="it.card_id === 'project_progress'">
              <p v-if="!projectsOf().length" class="empty">暂无在研项目</p>
              <ul v-else class="wb-list">
                <li v-for="p in projectsOf()" :key="p.id" class="wb-row">
                  <RouterLink :to="'/projects/' + encodeURIComponent(p.id)" class="lnk">{{ p.name }}</RouterLink>
                  <span class="row-meta">
                    {{ p.progress && p.progress.percent != null
                      ? p.progress.percent + '%（' + p.progress.done + '/' + p.progress.total + '）'
                      : '暂无事项' }}
                  </span>
                </li>
              </ul>
            </template>

            <!-- 近期里程碑 -->
            <template v-else-if="it.card_id === 'milestones'">
              <p v-if="!milestonesOf().length" class="empty">接下来没有安排节点</p>
              <ul v-else class="wb-list">
                <li v-for="m in milestonesOf()" :key="m.id" class="wb-row">
                  <span class="row-name">{{ m.name }}</span>
                  <span class="row-meta">{{ countdown(m) }}</span>
                </li>
              </ul>
            </template>

            <!-- 今日值日 -->
            <template v-else-if="it.card_id === 'duty'">
              <p v-if="!dutyOf().people.length" class="empty">今天没有安排值日</p>
              <p v-else class="wb-line">
                <span class="badge neutral sm" v-for="p in dutyOf().people" :key="(p.id || '') + p.name">{{ p.name }}</span>
                <span class="row-meta">{{ dutyOf().date }}{{ dutyOf().source === 'weekday' ? '（按星期）' : '' }}</span>
              </p>
            </template>

            <!-- 工时排行 -->
            <template v-else-if="it.card_id === 'attendance_rank'">
              <RankList :members="rankOf()" box-id="wb-rank" :limit="5" />
            </template>

            <!-- 我的采购 -->
            <template v-else-if="it.card_id === 'my_procurement'">
              <p v-if="!procurementOf().length" class="empty">没有我经手的采购</p>
              <ul v-else class="wb-list">
                <li v-for="p in procurementOf()" :key="p.id" class="wb-row">
                  <RouterLink :to="'/procurement/' + encodeURIComponent(p.id)" class="lnk">{{ p.name }}</RouterLink>
                  <span class="row-meta">
                    <template v-if="p.code">{{ p.code }} │ </template>
                    {{ p.purchased ? '已采购' : '未采购' }} │ {{ p.invoice_done ? '已报销' : '未报销' }}
                  </span>
                </li>
              </ul>
            </template>

            <!-- 数据质量 -->
            <template v-else-if="it.card_id === 'data_quality'">
              <p class="wb-metric"><b>{{ qualityOf().total }}</b><span class="wb-unit">条异常记录</span></p>
              <p v-if="!qualityOf().byTable.length" class="empty">没有质量异常</p>
              <p v-else class="wb-line">
                <span class="badge warn sm" v-for="q in qualityOf().byTable" :key="q.table_name">
                  {{ TABLE_LABEL[q.table_name] || q.table_name }} {{ q.count }}
                </span>
              </p>
            </template>

            <!-- 同步状态 -->
            <template v-else-if="it.card_id === 'sync_status'">
              <p class="wb-line">
                <span class="badge" :class="syncOf().stale ? 'warn' : 'ok'">
                  {{ syncOf().stale ? '数据可能过期' : '数据是最新的' }}
                </span>
                <span class="row-meta">上次成功同步 {{ fmtDT(syncOf().lastSuccess, '—') }}</span>
              </p>
              <p v-if="syncOf().error" class="wb-err">最近错误：{{ syncOf().error }}</p>
              <p class="row-meta">
                待推送 {{ syncOf().backlog.pending + syncOf().backlog.pushing }}
                │ 失败 {{ syncOf().backlog.failed }}
                │ 冲突 {{ syncOf().backlog.open_conflicts }}
              </p>
            </template>

          <p v-else class="empty">这张卡片暂无展示内容</p>
        </article>
      </div>

      <section v-if="configMode" class="module enter wb-lib-wrap">
        <div class="module-head">
          <span class="module-tag">LIBRARY / 02</span><h2>卡片库</h2>
          <span class="module-sub">点「添加」放到底部，再拖到想要的位置</span>
        </div>
        <div class="module-body">
          <p v-if="!unused.length" class="empty">所有卡片都已经在你的面板上了</p>
          <div v-else id="workbench-library" class="wb-lib">
            <div v-for="c in unused" :key="c.id" class="wb-lib-item" :data-cat="c.id">
              <div class="wb-lib-main">
                <span class="row-name">{{ c.title }}</span>
                <span class="row-meta">{{ c.hint }}</span>
              </div>
              <button type="button" class="btn ghost xs" :disabled="saving" @click="addCard(c.id)">添加</button>
            </div>
          </div>
        </div>
      </section>
    </div>
  </PortalPage>
</template>

<style scoped>
/* 功能模式占满整页：卡片区随可用高度拉伸；配置模式下多出的工具栏与卡片库自然往下长 */
/* 高度由 fitStage() 按视口与页头现算（含兜底值），保证功能模式一屏装满 */
.wb-stage { display: flex; flex-direction: column; gap: 12px; min-height: 320px; }
.wb-grid {
  flex: 1 1 auto; display: grid; gap: 12px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  grid-auto-rows: minmax(140px, 1fr);
  align-content: stretch;
}
.wb-card {
  border: 1px solid var(--c-line, #ddd); border-radius: 8px; padding: 10px 12px;
  background: #fff; min-width: 0; overflow: auto;
}
.wb-stage.is-config .wb-card { cursor: grab; }
.wb-card.is-full { grid-column: 1 / -1; }
.wb-card[data-drag-over="true"] { outline: 2px dashed var(--c-ink, #222); }
.wb-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
.wb-title { font-size: 15px; margin: 0; }
.wb-tools { display: flex; flex-wrap: wrap; gap: 4px; }
.wb-metric { margin: 4px 0; font-size: 22px; }
.wb-unit { font-size: 12px; margin-left: 6px; }
.wb-line { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 4px 0; }
.wb-attend-row { margin-top: auto; }
.wb-attend-row .btn { flex: none; }
.wb-list { list-style: none; margin: 0; padding: 0; }
.wb-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline; padding: 3px 0; border-top: 1px dashed var(--c-line, #eee); }
.wb-row:first-child { border-top: none; }
.wb-err { color: var(--c-danger-2, #a8211a); margin: 4px 0; }
.wb-lib-wrap { margin-top: 0; }
.wb-lib { display: grid; gap: 8px; }
.wb-lib-item { display: flex; align-items: center; justify-content: space-between; gap: 10px;
  border: 1px solid var(--c-line, #ddd); border-radius: 6px; padding: 6px 10px; }
.wb-lib-main { display: flex; flex-direction: column; gap: 2px; }
@media (max-width: 720px) {
  .wb-stage { min-height: 0; }
  .wb-grid { grid-template-columns: minmax(0, 1fr); grid-auto-rows: minmax(120px, auto); }
  .wb-card.is-half { grid-column: 1 / -1; }
}
</style>
