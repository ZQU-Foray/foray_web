<script setup lang="ts">
/* 日程：列表视图（未来 7 天，含正在进行）/ 日历视图（未来 28 天）；视图选择跨页保留 */
import { computed } from "vue";
import PortalPage from "../components/PortalPage.vue";
import SyncMark from "../components/SyncMark.vue";
import { navNo } from "../nav";
import SrcLink from "../components/SrcLink.vue";
import { RouterLink } from "@/shared/router";
import { fetchSchedule } from "../api";
import { isLeader, localNowSec, openForm, store } from "../store";
import { usePageData } from "../usePageData";
import { fmtD, fmtDT, fmtTime, pad2 } from "@/shared/format";
import type { ScheduleItem } from "../types";

const { body } = usePageData(() => fetchSchedule(store.scheduleView === "calendar" ? 28 : 7),
  [() => store.scheduleView]);
const items = computed<ScheduleItem[]>(() => (body.value && body.value.data) || []);
const useCal = computed(() => store.scheduleView === "calendar");

function spanText(s: ScheduleItem): string {
  let t: string;
  if (s.all_day) {
    const d1 = fmtD(s.start.ts);
    const d2 = s.end ? fmtD(s.end.ts) : null;
    t = "全天 " + d1 + (d2 && d2 !== d1 ? " – " + d2 : "");
  } else {
    t = fmtDT(s.start.ts) + (s.end ? " → " + fmtDT(s.end.ts) : "");
  }
  return t + (s.ongoing ? " · 进行中" : "");
}
function metaText(s: ScheduleItem): string {
  const bits = [spanText(s)];
  if (s.groups && s.groups.length) bits.push(s.groups.join("/"));
  if (s.location) bits.push(s.location);
  return bits.join(" · ");
}

function dayKey(d: Date): string {
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}
/* 日历：从今天起连续 28 天（4 行 × 7 列），表头周一到周日 */
const cells = computed(() => {
  const now = new Date(localNowSec() * 1000);
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const byDay: Record<string, ScheduleItem[]> = {};
  items.value.forEach((s) => {
    const k = dayKey(new Date(s.start.ts * 1000));
    (byDay[k] = byDay[k] || []).push(s);
  });
  const out: Array<{ key: string; label: string; today: boolean; evs: ScheduleItem[] }> = [];
  for (let i = 0; i < 28; i++) {
    const day = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    const k = dayKey(day);
    out.push({ key: k, label: day.getMonth() + 1 + "/" + day.getDate(), today: i === 0, evs: byDay[k] || [] });
  }
  return out;
});
const weekHeads = ["一", "二", "三", "四", "五", "六", "日"];
function evText(s: ScheduleItem): string {
  return (s.all_day ? "全天 " : fmtTime(s.start.ts) + " ") + s.name + (s.status === "已取消" ? "（已取消）" : "");
}
</script>

<template>
  <PortalPage :no="navNo('/schedule')" en="SCHEDULE" title="日程" desc="公共日程 │ 列表 7 天 / 日历 28 天" watermark="SCHEDULE">
    <template v-if="isLeader()" #actions>
      <button type="button" class="btn" @click="openForm('schedule')">＋ 新建日程</button>
    </template>

    <div class="toolbar">
      <div id="view-toggle" class="chip-bar" role="group" aria-label="视图切换">
        <button type="button" class="chip" :aria-pressed="!useCal ? 'true' : 'false'" @click="store.scheduleView = 'list'">列表视图</button>
        <button type="button" class="chip" :aria-pressed="useCal ? 'true' : 'false'" @click="store.scheduleView = 'calendar'">日历视图</button>
      </div>
    </div>

    <section class="module enter">
      <div class="module-head">
        <span class="module-tag">SCHEDULE / 01</span>
        <h2>近期安排</h2>
        <span class="module-sub">{{ useCal ? "日历视图覆盖未来 28 天；点日程进入详情" : "列表视图覆盖未来 7 天（含正在进行）；点日程进入详情" }}</span>
      </div>
      <div class="module-body">
        <div id="schedule-list" :hidden="useCal">
          <div v-for="s in items" :key="s.id" class="sched-row">
            <span class="sched-type">{{ s.kind || "—" }}</span>
            <div class="row-main">
              <RouterLink :to="'/schedule/' + encodeURIComponent(s.id)" class="row-name lnk">{{ s.name }}</RouterLink>
              <span class="row-meta">{{ metaText(s) }}</span>
            </div>
            <div class="row-side">
              <SyncMark :mark="s.sync" />
              <span v-if="s.status === '已取消'" class="badge danger sm">已取消</span>
              <SrcLink :url="s.url" />
            </div>
          </div>
          <p v-if="body && !items.length" class="empty">未来 7 天暂无日程</p>
        </div>
        <div id="schedule-calendar" class="cal-wrap" :hidden="!useCal">
          <div v-if="useCal" class="cal">
            <div v-for="w in weekHeads" :key="w" class="cal-head">周{{ w }}</div>
            <div v-for="c in cells" :key="c.key" class="cal-cell" :class="{ 'is-today': c.today }">
              <div class="cal-date">{{ c.label }}</div>
              <RouterLink
                v-for="s in c.evs"
                :key="s.id"
                :to="'/schedule/' + encodeURIComponent(s.id)"
                class="cal-ev"
                :class="{ 'is-cancel': s.status === '已取消' }"
                :title="s.name + (s.location ? ' · ' + s.location : '')"
              >{{ evText(s) }}</RouterLink>
            </div>
          </div>
        </div>
      </div>
    </section>
  </PortalPage>
</template>
