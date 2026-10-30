<script setup lang="ts">
/* 总览（2026-10-21 布局）：只放摘要，每块有条数上限并给出「查看全部」，格子里不再套滚动。
 * - 顶部计数条：逾期 / 72 小时内到期（点一下筛选下方列表）、今日日程、签到中、推送异常；
 * - 重要节点：3 张矮卡横排（手机上横向滑动）；
 * - 需要关注的事项：全部组别一张列表（逾期 → 临期 → 受阻 → P0），按组别芯片切换，最多 8 条；
 * - 近期日程：按天分组，最多 3 天、8 条（与事项并排，两列高度相近）；
 * - 工时排行：前 10 名 + 我，整行两列排在最下面。 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import PortalPage from "../components/PortalPage.vue";
import SyncMark from "../components/SyncMark.vue";
import { navNo } from "../nav";
import SrcLink from "../components/SrcLink.vue";
import DueTag from "../components/DueTag.vue";
import Attendance from "../components/Attendance.vue";
import RankList from "../components/RankList.vue";
import { RouterLink, router } from "@/shared/router";
import { fetchOverview } from "../api";
import { groups, localNowSec, queueCounts, store } from "../store";
import { usePageData } from "../usePageData";
import { clockText, fmtD, fmtTime, pad2 } from "@/shared/format";
import type { AttentionTask, Milestone, Overview, ScheduleItem, UserRef } from "../types";

const TASK_LIMIT = 8;
const SCHED_LIMIT = 8;
const SCHED_DAYS = 3;
const RANK_LIMIT = 10;

const { body } = usePageData(fetchOverview);
const data = computed<Overview | null>(() => (body.value && body.value.data) || null);
const kpi = computed(() => (data.value && data.value.kpi) || null);

/* 倒计时：本地每秒重算（以 server_time 校时），不向服务器轮询 */
const now = ref(localNowSec());
let tick = 0;
onMounted(() => { tick = window.setInterval(() => { now.value = localNowSec(); }, 1000); });
onBeforeUnmount(() => window.clearInterval(tick));

interface Countdown { big: string; unit: string; rem: string; tone: string }
function countdown(m: Milestone): Countdown {
  const target = m.target.ts;
  if ((m.target.precision || "date") === "date") {
    /* 纯日期：只给天数 / 今天（不虚构小时级精度） */
    const t0 = new Date(target * 1000); t0.setHours(0, 0, 0, 0);
    const n0 = new Date(now.value * 1000); n0.setHours(0, 0, 0, 0);
    const dd = Math.round((t0.getTime() - n0.getTime()) / 86400000);
    if (dd > 0) return { big: String(dd), unit: "天", rem: "", tone: dd <= 10 ? "warn" : "" };
    if (dd === 0) return { big: "今天", unit: "", rem: "", tone: "danger" };
    return { big: "已过 " + -dd + " 天", unit: "", rem: "", tone: "" };
  }
  const sec = Math.max(target - now.value, 0);
  const days = Math.floor(sec / 86400);
  return {
    big: String(days),
    unit: "天",
    rem: "剩余 " + pad2(Math.floor((sec % 86400) / 3600)) + ":" +
      pad2(Math.floor((sec % 3600) / 60)) + ":" + pad2(Math.floor(sec % 60)),
    tone: days <= 10 ? "warn" : "",
  };
}

function names(list?: UserRef[]): string {
  return (list || []).map((u) => u.name || "…" + u.id.slice(-4)).join("、") || "待分配";
}

/* ---- 需要关注的事项：组别芯片 + 计数条带来的「只看逾期 / 临期」 ---- */
const group = ref("全部");
const level = ref<"" | "overdue" | "due_soon">("");
const LEVEL_TEXT = { overdue: "逾期", due_soon: "72 小时内到期" } as const;
const attention = computed<AttentionTask[]>(() => (data.value && data.value.attention) || []);
const byLevel = computed(() => attention.value.filter((t) => !level.value || t.level === level.value));
const chips = computed(() => {
  const list = [{ id: "全部", n: byLevel.value.length, overdue: byLevel.value.filter((t) => t.overdue).length }];
  for (const g of groups()) {
    const items = byLevel.value.filter((t) => t.owner_group === g);
    list.push({ id: g, n: items.length, overdue: items.filter((t) => t.overdue).length });
  }
  return list;
});
const filtered = computed(() => byLevel.value.filter((t) => group.value === "全部" || t.owner_group === group.value));
const shown = computed(() => filtered.value.slice(0, TASK_LIMIT));
const listBox = ref<HTMLElement | null>(null);

function pickLevel(l: "overdue" | "due_soon") {
  level.value = level.value === l ? "" : l;
  /* 窄屏上列表在计数条下面很远：滚过去，点了有反馈 */
  if (window.matchMedia("(max-width: 1100px)").matches) {
    listBox.value?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}
/* 「查看全部」带上当前组别，事项页直接是筛好的 */
function goAllTasks() {
  store.taskFilters.group = group.value;
  router.push("/tasks");
}

/* ---- 近期日程：按天分组（进行中的算今天），最多 3 天、8 条 ---- */
const WEEKDAY = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
function dayKey(ts: number): string {
  const d = new Date(ts * 1000);
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}
const schedDays = computed(() => {
  const items = (data.value && data.value.schedule) || [];
  const today = dayKey(now.value);
  const tomorrow = dayKey(now.value + 86400);
  const days: Array<{ key: string; label: string; items: ScheduleItem[] }> = [];
  let count = 0;
  for (const s of items) {
    if (count >= SCHED_LIMIT) break;
    const key = s.ongoing ? today : dayKey(s.start.ts);
    let day = days.find((x) => x.key === key);
    if (!day) {
      if (days.length >= SCHED_DAYS) break;
      const d = new Date((s.ongoing ? now.value : s.start.ts) * 1000);
      const head = key === today ? "今天" : key === tomorrow ? "明天" : WEEKDAY[d.getDay()];
      day = { key, label: head + " · " + pad2(d.getMonth() + 1) + "." + pad2(d.getDate()) + (key === today || key === tomorrow ? " " + WEEKDAY[d.getDay()] : ""), items: [] };
      days.push(day);
    }
    day.items.push(s);
    count++;
  }
  return { days, shown: count, total: items.length };
});
function schedTime(s: ScheduleItem): string {
  if (s.all_day) return "全天";
  if (s.ongoing && dayKey(s.start.ts) !== dayKey(now.value)) return "进行中";
  return fmtTime(s.start.ts);
}
function schedMeta(s: ScheduleItem): string {
  return [s.kind, s.location, s.ongoing ? "进行中" : ""].filter(Boolean).join(" · ");
}

/* ---- 排行：前 10 名 + 我 ---- */
const myId = computed(() => {
  const u = (store.me && store.me.user) || "";
  return u.indexOf("feishu:") === 0 ? u.slice(7) : null;
});

const pushIssues = computed(() => { const q = queueCounts(); return q.failed + q.conflicts; });
/* 铭牌日期跟随上面的秒级 tick：原先在 setup 里求值一次，开着页面跨零点就不翻（审计 P3） */
const plate = computed(() => "FORAY PORTAL │ " + clockText(new Date(now.value * 1000)).slice(0, 10));
</script>

<template>
  <PortalPage :no="navNo('/')" en="HOME │ OVERVIEW" title="总览" desc="计数 │ 重要节点 │ 需要关注 │ 近期日程 │ 工时排行" watermark="OVERVIEW" :plate="plate">
    <p v-if="data && data.quality_issues" id="ov-quality" class="note">
      ※ {{ data.quality_issues }} 条源数据存在质量异常（未知枚举/失效关联等），已排除出统计。
      <RouterLink to="/quality">查看异常明细 →</RouterLink>
    </p>

    <!-- 计数条：数据再多也只占一行 -->
    <div id="ov-kpi" class="ov-kpi enter">
      <button type="button" class="card ov-kpi-tile" :class="{ 'is-danger': kpi && kpi.overdue, 'is-on': level === 'overdue' }"
              :aria-pressed="level === 'overdue' ? 'true' : 'false'" data-kpi="overdue" @click="pickLevel('overdue')">
        <span class="ov-kpi-tag">OVERDUE</span>
        <b class="ov-kpi-num t-num">{{ kpi ? kpi.overdue : "—" }}</b>
        <small>逾期事项{{ level === "overdue" ? " · 已筛选" : "" }}</small>
      </button>
      <button type="button" class="card ov-kpi-tile" :class="{ 'is-warn': kpi && kpi.due_soon, 'is-on': level === 'due_soon' }"
              :aria-pressed="level === 'due_soon' ? 'true' : 'false'" data-kpi="due_soon" @click="pickLevel('due_soon')">
        <span class="ov-kpi-tag">DUE SOON</span>
        <b class="ov-kpi-num t-num">{{ kpi ? kpi.due_soon : "—" }}</b>
        <small>72 小时内到期{{ level === "due_soon" ? " · 已筛选" : "" }}</small>
      </button>
      <RouterLink to="/schedule" class="card ov-kpi-tile" data-kpi="today">
        <span class="ov-kpi-tag">TODAY</span>
        <b class="ov-kpi-num t-num">{{ kpi ? kpi.today_schedule : "—" }}</b>
        <small>今日日程</small>
      </RouterLink>
      <RouterLink to="/attendance" class="card ov-kpi-tile" data-kpi="checked_in">
        <span class="ov-kpi-tag">ON SITE</span>
        <b class="ov-kpi-num t-num">{{ kpi && kpi.checked_in != null ? kpi.checked_in : "—" }}<i v-if="kpi && kpi.checked_in != null"> 人</i></b>
        <small>当前签到中</small>
      </RouterLink>
      <RouterLink v-if="pushIssues" to="/quality" class="card ov-kpi-tile is-danger" data-kpi="push">
        <span class="ov-kpi-tag">PUSH</span>
        <b class="ov-kpi-num t-num">{{ pushIssues }}</b>
        <small>推送失败与冲突</small>
      </RouterLink>
    </div>

    <!-- 01 重要节点：矮卡横排 -->
    <section class="module enter ov-ms">
      <div class="module-head">
        <span class="module-tag">MILESTONES / 01</span>
        <h2>重要节点倒计时</h2>
        <RouterLink to="/schedule" class="btn ghost sm">查看全部 →</RouterLink>
      </div>
      <div class="module-body">
        <div id="count-box" class="ov-cd-strip">
          <div
            v-for="m in data?.milestones || []"
            :key="m.id"
            class="card cd-card"
            :data-target="m.target.ts"
            :data-precision="m.target.precision || 'date'"
          >
            <div class="countdown ov-cd" :class="countdown(m).tone">
              <div class="days t-num" :class="{ 'is-text': !countdown(m).unit }">{{ countdown(m).big }}<sup v-if="countdown(m).unit">{{ countdown(m).unit }}</sup></div>
              <div class="ov-cd-info">
                <span class="cd-label">{{ m.name }}</span>
                <span class="cd-date">{{ fmtD(m.target.ts) }}<template v-if="m.kind"> · <span class="cd-kind">{{ m.kind }}</span></template></span>
                <span class="cd-rem">{{ countdown(m).rem }}</span>
              </div>
              <SrcLink :url="m.url" />
            </div>
          </div>
          <p v-if="data && !data.milestones.length" class="empty">暂无展示中的未来节点</p>
        </div>
        <RouterLink to="/schedule" class="more-link">查看全部节点与日程 →</RouterLink>
      </div>
    </section>

    <div class="ov-main">
      <!-- 02 需要关注的事项 -->
      <section id="ov-attention" ref="listBox" class="module enter">
        <div class="module-head">
          <span class="module-tag">URGENT / 02</span>
          <h2>需要关注的事项</h2>
          <span class="module-sub">逾期 → 72 小时内到期 → 受阻 → P0</span>
        </div>
        <div class="module-body">
          <div id="ov-group-chips" class="chip-bar compact" role="group" aria-label="按主责组筛选">
            <button
              v-for="c in chips"
              :key="c.id"
              type="button"
              class="chip"
              :aria-pressed="group === c.id ? 'true' : 'false'"
              @click="group = c.id"
            >{{ c.id }}<b class="ov-chip-n" :class="{ 'has-overdue': c.overdue }" :title="c.overdue ? '其中逾期 ' + c.overdue + ' 条' : ''">{{ c.n }}</b></button>
          </div>
          <p v-if="level" class="ov-filter-note">
            只看{{ LEVEL_TEXT[level] }}的事项 ·
            <button type="button" class="ov-link-btn" @click="level = ''">看全部</button>
          </p>
          <div id="urgent-box">
            <div v-for="t in shown" :key="t.id" class="task-row ov-task" :data-level="t.level">
              <span class="prio" :data-p="t.priority || ''">{{ t.priority || "—" }}</span>
              <span class="ov-task-name">
                <RouterLink :to="'/tasks/' + encodeURIComponent(t.id)" class="task-name lnk" :title="t.name">{{ t.name }}</RouterLink>
                <SyncMark :mark="t.sync" />
              </span>
              <span class="badge neutral sm ov-task-group">{{ t.owner_group || "—" }}</span>
              <span class="row-meta ov-task-owner" :title="names(t.owners)">{{ names(t.owners) }}</span>
              <span class="ov-task-due">
                <span v-if="t.level === 'blocked'" class="badge warn sm">受阻</span>
                <DueTag v-if="t.due || t.level !== 'blocked'" :task="t" />
              </span>
            </div>
            <p v-if="data && !filtered.length" class="empty">
              {{ level ? "没有" + LEVEL_TEXT[level] + "的事项" : "没有逾期、临期、受阻或 P0 事项" }}{{ group !== "全部" ? "（" + group + "）" : "" }}
            </p>
          </div>
          <div class="ov-foot">
            <span class="row-meta">共 {{ filtered.length }} 条<template v-if="filtered.length > shown.length">，显示最急的 {{ shown.length }} 条</template></span>
            <a href="/tasks" class="more-link" @click.prevent="goAllTasks">查看{{ group === "全部" ? "全部" : group + "组" }}事项 →</a>
          </div>
        </div>
      </section>

      <div class="ov-side">
        <!-- 03 近期日程 -->
        <section class="module enter">
          <div class="module-head">
            <span class="module-tag">SCHEDULE / 03</span>
            <h2>近期日程</h2>
            <RouterLink to="/schedule" class="btn ghost sm">日历 →</RouterLink>
          </div>
          <div class="module-body">
            <div id="schedule-box">
              <template v-for="day in schedDays.days" :key="day.key">
                <h3 class="ov-day">{{ day.label }}</h3>
                <div v-for="s in day.items" :key="s.id" class="sched-row ov-sched">
                  <span class="ov-sched-time t-num">{{ schedTime(s) }}</span>
                  <div class="row-main">
                    <RouterLink :to="'/schedule/' + encodeURIComponent(s.id)" class="row-name lnk">{{ s.name }}</RouterLink>
                    <span v-if="schedMeta(s)" class="row-meta">{{ schedMeta(s) }}</span>
                  </div>
                  <div class="row-side">
                    <SyncMark :mark="s.sync" />
                    <span v-if="s.status === '已取消'" class="badge danger sm">已取消</span>
                  </div>
                </div>
              </template>
              <p v-if="data && !schedDays.days.length" class="empty">未来 7 天暂无日程</p>
            </div>
            <p v-if="schedDays.total > schedDays.shown" class="ov-foot row-meta">
              未来 7 天共 {{ schedDays.total }} 条，这里显示最近的 {{ schedDays.shown }} 条
            </p>
          </div>
        </section>
      </div>
    </div>

    <!-- 04 打卡工时排行（服务端已按有效时长降序，前端只按序渲染）：整行，两列 -->
    <section class="module enter ov-rank">
      <div class="module-head">
        <span class="module-tag">RANK / 04</span>
        <h2>打卡工时排行</h2>
        <span class="module-sub">累计有效时长 · 前 {{ RANK_LIMIT }} 名</span>
        <RouterLink to="/attendance" class="btn ghost sm">考勤 →</RouterLink>
      </div>
      <div class="module-body">
        <Attendance />
        <RankList :members="data ? data.members : null" box-id="rank-box" :limit="RANK_LIMIT" :me-id="myId" />
      </div>
    </section>
  </PortalPage>
</template>

<style scoped>
/* ---- 计数条 ---- */
.ov-kpi {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 170px), 1fr));
  gap: 12px;
  margin-bottom: 20px;
}
.ov-kpi-tile {
  display: grid;
  gap: 6px;
  padding: 14px 16px 12px;
  border-top: 4px solid var(--c-ink);
  min-width: 0;
  text-align: left;
  font: inherit;
  color: inherit;
  text-decoration: none;
  cursor: pointer;
  transition: background var(--t-fast), transform var(--t-fast);
}
.ov-kpi-tile:hover { background: var(--c-surface); }
.ov-kpi-tile:focus-visible { outline: 2px solid var(--c-ink); outline-offset: 2px; }
.ov-kpi-tile.is-on { background: var(--c-yellow); }
.ov-kpi-tile.is-danger { border-top-color: var(--c-danger); }
.ov-kpi-tile.is-warn { border-top-color: var(--c-warn); }
.ov-kpi-tag { font: 700 9px/1 var(--f-wide); letter-spacing: .18em; color: var(--c-text-3); }
.ov-kpi-num { font: 700 28px/1.05 var(--f-num); color: var(--c-ink); }
.ov-kpi-tile.is-danger .ov-kpi-num { color: var(--c-danger); }
.ov-kpi-num i { font-style: normal; font-size: 14px; color: var(--c-text-3); }
.ov-kpi-tile small { font-size: 12px; color: var(--c-text-3); }

/* ---- 重要节点：矮卡横排 ---- */
.ov-ms { margin: 0 0 20px; }
.ov-cd-strip { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.ov-cd-strip > .card { min-width: 0; }
.ov-cd {
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  padding: 12px 14px;
}
.ov-cd .days { font-size: 52px; }
.ov-cd .days.is-text { font-size: 26px; }
.ov-cd-info { display: grid; gap: 3px; min-width: 0; }
.ov-cd-info .cd-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ov-cd-info .cd-rem:empty { display: none; }

/* ---- 主区：左列事项，右列日程与排行 ---- */
.ov-main {
  display: grid;
  grid-template-columns: minmax(0, 1.7fr) minmax(0, 1fr);
  gap: 20px;
  align-items: start;
}
.ov-main > .module, .ov-side > .module { margin: 0; }
.ov-side { display: grid; gap: 20px; min-width: 0; }
.ov-rank { margin: 20px 0 0; }
/* 排行两列（前 5 名在左、6–10 名在右）；行不跨列断开 */
.ov-rank :deep(#rank-box) { column-count: 2; column-gap: 32px; }
.ov-rank :deep(#rank-box > *) { break-inside: avoid; }

/* 需要关注：一行一条，名称过长省略（悬停看全名） */
.ov-chip-n { margin-left: 2px; font: 700 11px/1 var(--f-num); color: var(--c-text-3); }
.ov-chip-n.has-overdue { color: var(--c-danger); }
#ov-group-chips { margin-bottom: 10px; }
.ov-filter-note { margin: 0 0 8px; font-size: 12px; color: var(--c-text-2); }
.ov-link-btn {
  padding: 0; border: 0; background: none; font: inherit; color: var(--c-ink);
  text-decoration: underline; text-underline-offset: 3px; cursor: pointer;
}
/* 每行是独立的网格：组别 / 负责人 / 截止三列用固定宽度（按最长内容「受阻 + 日期时刻」或
   「日期时刻 │ 逾期」量过），各行位置一致、上下对齐；事项名称占剩余宽度，过长省略 */
.ov-task {
  grid-template-columns: 26px minmax(0, 1fr) 40px 7.5em 152px;
  gap: 10px;
  padding-top: 9px;
  padding-bottom: 9px;
  min-height: 44px;
}
.ov-task-name { display: flex; align-items: center; min-width: 0; }
.ov-task-name .task-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.ov-task-group { justify-self: start; }
.ov-task-owner { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ov-task-due { display: flex; gap: 6px; justify-content: flex-start; white-space: nowrap; min-width: 0; }
.ov-foot {
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
  gap: 6px 12px; margin-top: 12px;
}
.ov-foot .more-link { margin: 0; }

/* 近期日程：按天分组 */
.ov-day {
  margin: 12px 0 4px;
  font: 700 11px/1 var(--f-cn);
  letter-spacing: .12em;
  color: var(--c-text-3);
}
.ov-day:first-child { margin-top: 0; }
.ov-sched { grid-template-columns: 3.4em minmax(0, 1fr) auto; gap: 12px; padding-top: 8px; padding-bottom: 8px; }
.ov-sched-time { font: 600 13px/1 var(--f-num); color: var(--c-ink); }

@media (max-width: 1100px) {
  .ov-main { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 760px) {
  .ov-rank :deep(#rank-box) { column-count: 1; }
}
/* 窄屏：计数两列；节点横向滑动；事项两行（名称一行，组别 · 负责人 · 截止一行） */
@media (max-width: 640px) {
  .ov-kpi { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .ov-kpi-tile { padding: 10px 12px; }
  .ov-kpi-num { font-size: 24px; }
  .ov-cd-strip {
    grid-template-columns: none;
    grid-auto-flow: column;
    grid-auto-columns: 82%;
    overflow-x: auto;
    scroll-snap-type: x mandatory;
    padding-bottom: 4px;
  }
  .ov-cd-strip > .card { scroll-snap-align: start; }
  .ov-cd .days { font-size: 44px; }
  .ov-task {
    grid-template-columns: 26px auto minmax(0, 1fr) auto;
    grid-template-areas: "p name name name" "p group meta due";
    row-gap: 4px;
  }
  .ov-task > .prio { grid-area: p; align-self: start; }
  .ov-task-name { grid-area: name; }
  .ov-task-group { grid-area: group; }
  .ov-task-owner { grid-area: meta; }
  .ov-task-due { grid-area: due; justify-content: flex-end; }
  /* 窄屏触控尺寸把链接改成了 inline-flex，省略号不生效：名称这里换回块级，行高撑到 44px */
  .ov-task-name .task-name { display: block; line-height: 44px; }
}
</style>
