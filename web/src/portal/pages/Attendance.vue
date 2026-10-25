<script setup lang="ts">
/* 02 考勤（数据全部来自飞书）：
 * - 本周时长与达标：飞书公式结算，一周 = 周一 00:00 至周日 24:00，大于 30 小时为达标；
 * - 请假：组长可给成员标本周请假，请假那周没达标、当天没签到都显示「请假」，下周自动恢复；
 * - 今日值日：按飞书值日表判定（当天有按日期的行以它为准，否则按星期轮换）；
 * - 打卡工时排行：累计有效时长（与总览同一份数据）。
 * 签到 / 签退在总览的打卡条。 */
import { computed, ref } from "vue";
import PortalPage from "../components/PortalPage.vue";
import { navNo } from "../nav";
import RankList from "../components/RankList.vue";
import SrcLink from "../components/SrcLink.vue";
import { fetchAttendancePage, setLeave } from "../api";
import { usePageData } from "../usePageData";
import { afterWrite, isLeader } from "../store";
import { errorDetail } from "@/shared/http";
import { fmtD, fmtTime, pad2 } from "@/shared/format";
import type { AttendancePage, AttendanceRow } from "../types";
import { toast } from "@/shared/toast";

const { body } = usePageData(fetchAttendancePage);
const d = computed<AttendancePage | null>(() => (body.value && body.value.data) || null);
const rows = computed<AttendanceRow[]>(() => (d.value && d.value.members) || []);
const threshold = computed(() => (d.value ? d.value.threshold : 30));

function md(ts: number): string {
  const x = new Date(ts * 1000);
  return pad2(x.getMonth() + 1) + "." + pad2(x.getDate());
}
const weekRange = computed(() => (d.value ? md(d.value.week.start) + " – " + md(d.value.week.end - 1) : "—"));
const weekYear = computed(() => (d.value ? fmtD(d.value.week.start).slice(0, 4) : ""));

const dutyNames = computed(() => (d.value ? d.value.duty.people.map((p) => p.name) : []));
const dutySource = computed(() => {
  const x = d.value;
  if (!x) return "";
  if (!x.configured.duty) return "值日表尚未接入";
  if (x.duty.source === "date") return x.today.date.replace(/-/g, ".") + " 的日期安排";
  if (x.duty.source === "weekday") return "按星期轮换（" + x.today.label + "）";
  return x.today.label + " · 值日表里没有今天的安排";
});

function hours(v?: number | null): string {
  return v == null ? "—" : v.toFixed(2);
}
/* 进度条：以达标线为满格；超过达标线后保持满格并换成达标色 */
function pct(v?: number | null): number {
  if (v == null || threshold.value <= 0) return 0;
  return Math.max(0, Math.min(100, (v / threshold.value) * 100));
}
function status(r: AttendanceRow): { text: string; cls: string } {
  if (!d.value || !d.value.configured.week) return { text: "未接入", cls: "neutral" };
  if (r.leave) return { text: "请假", cls: "neutral is-leave" };
  if (r.pending) return { text: "待结算", cls: "neutral" };
  if (r.week_ok === true) return { text: "达标", cls: "ok" };
  if (r.week_ok === false) return { text: "未达标", cls: "warn" };
  return { text: "—", cls: "neutral" };
}

/* ---- 组长：设 / 取消本周请假 ---- */
const canLeave = computed(() => isLeader() && !!(d.value && d.value.configured.leave));
const leaveBusy = ref("");
async function toggleLeave(r: AttendanceRow) {
  if (!r.id) return;
  leaveBusy.value = r.id;
  try {
    const res = await setLeave(r.id, !r.on_leave);
    if (res.ok) afterWrite(res.body);
    else toast("设置请假失败：" + errorDetail(res), "error");
  } catch {
    toast("网络错误，请假未设置", "error");
  } finally {
    leaveBusy.value = "";
  }
}
</script>

<template>
  <PortalPage :no="navNo('/attendance')" en="ATTENDANCE │ WEEKLY" title="考勤" :desc="'本周时长 │ 大于 ' + threshold + ' 小时达标 │ 今日值日 │ 工时排行'" watermark="ATTENDANCE">
    <div id="att-summary" class="att-stats enter">
      <div class="card att-stat">
        <span class="att-stat-tag">WEEK</span>
        <b class="att-stat-num">{{ weekRange }}</b>
        <small>{{ weekYear }} · 周一 00:00 至周日 24:00</small>
      </div>
      <div class="card att-stat">
        <span class="att-stat-tag">MET</span>
        <b class="att-stat-num">
          <template v-if="d && d.configured.week">{{ d.summary.met }}<i> / {{ d.summary.required ?? d.summary.members }}</i></template>
          <template v-else>—</template>
        </b>
        <small>本周已达标（大于 {{ threshold }} h）<template v-if="d && d.summary.leave"> · 请假 {{ d.summary.leave }} 人</template></small>
      </div>
      <div class="card att-stat">
        <span class="att-stat-tag">ON SITE</span>
        <b class="att-stat-num">{{ d ? d.summary.checked_in : "—" }}<i v-if="d"> 人</i></b>
        <small>当前签到中</small>
      </div>
      <div id="att-duty-card" class="card att-stat">
        <span class="att-stat-tag">DUTY</span>
        <b class="att-stat-num att-stat-names">{{ dutyNames.length ? dutyNames.join("、") : "无" }}</b>
        <small>{{ dutySource }}</small>
      </div>
    </div>

    <p v-if="d && !d.configured.week" id="att-week-note" class="note">
      ※ 飞书侧还没有开启周时长结算，暂时只能看累计工时（管理员按运维文档建好结算列后自动显示）。
    </p>
    <p v-else-if="d && d.summary.pending" class="note">
      ※ 标「待结算」的成员：本周的换周还没同步过来（通常 1 分钟内），本周时长稍后显示。
    </p>

    <div class="att-grid">
      <section class="module enter">
        <div class="module-head">
          <span class="module-tag">WEEKLY / 01</span>
          <h2>本周考勤</h2>
          <span class="module-sub">按本周时长排序 · 签到中的本次时长签退后计入</span>
        </div>
        <div class="module-body flush">
          <div class="tbl-wrap in-module">
            <table class="tbl att-tbl">
              <thead>
                <tr>
                  <th>成员</th>
                  <th class="num">本周时长</th>
                  <th>达标</th>
                  <th class="num">上周</th>
                  <th>今日</th>
                  <th v-if="canLeave">请假</th>
                  <th><span class="sr-only">飞书记录</span></th>
                </tr>
              </thead>
              <tbody id="att-week-body">
                <tr v-for="r in rows" :key="r.id || r.member.id" :data-member="r.member.id">
                  <td class="att-c-name"><b>{{ r.member.name || "(未命名)" }}</b></td>
                  <td class="num att-c-hours" data-label="本周">
                    <span class="att-hours"><b>{{ hours(r.week_hours) }}</b><i v-if="r.week_hours != null"> h</i></span>
                    <span v-if="r.week_hours != null" class="prog att-prog" aria-hidden="true"><span class="prog-fill" :class="{ ok: r.week_ok === true }" :style="{ width: pct(r.week_hours) + '%' }"></span></span>
                  </td>
                  <td class="att-c-status"><span class="badge sm att-status" :class="status(r).cls">{{ status(r).text }}</span></td>
                  <td class="num att-last att-c-last" data-label="上周">
                    <template v-if="r.last_week_hours != null">
                      {{ hours(r.last_week_hours) }}<span class="att-last-mark" :class="r.last_week_ok ? 'is-ok' : r.last_week_leave ? 'is-leave' : 'is-miss'">{{ r.last_week_ok ? "达标" : r.last_week_leave ? "请假" : "未达标" }}</span>
                    </template>
                    <template v-else>—</template>
                  </td>
                  <td class="att-today att-c-today">
                    <span v-if="r.checked_in" class="badge sm yellow">签到中{{ r.checked_in_since ? " " + fmtTime(r.checked_in_since) : "" }}</span>
                    <span v-if="r.on_duty" class="badge sm ink">值日</span>
                    <span v-if="!r.checked_in && r.on_leave" class="badge sm neutral is-leave">请假</span>
                    <span v-if="!r.checked_in && !r.on_duty && !r.on_leave" class="row-meta">—</span>
                  </td>
                  <td v-if="canLeave" class="att-c-leave">
                    <button
                      type="button"
                      class="btn ghost xs"
                      :class="{ 'is-on': r.on_leave }"
                      :aria-pressed="r.on_leave ? 'true' : 'false'"
                      :disabled="leaveBusy === r.id || !r.id"
                      @click="toggleLeave(r)"
                    >{{ leaveBusy === r.id ? "写入中…" : r.on_leave ? "取消请假" : "本周请假" }}</button>
                  </td>
                  <td class="nowrap att-c-link"><SrcLink :url="r.url" label="飞书" /></td>
                </tr>
                <tr v-if="d && !rows.length"><td :colspan="canLeave ? 7 : 6" class="is-empty"><p class="empty">暂无成员考勤数据</p></td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <div class="att-side">
        <section id="att-duty" class="module enter">
          <div class="module-head">
            <span class="module-tag">DUTY / 02</span>
            <h2>今日值日</h2>
          </div>
          <div class="module-body">
            <p class="row-meta att-duty-src">{{ d ? d.today.date.replace(/-/g, ".") + " " + d.today.label : "" }} · {{ dutySource }}</p>
            <ul v-if="d && d.duty.people.length" class="att-duty-list">
              <li v-for="p in d.duty.people" :key="(p.id || '') + p.name" class="att-duty-person">
                <b>{{ p.name }}</b>
                <span v-if="!p.in_members" class="badge sm neutral" title="值日表里的这位不在成员数据表中">不在成员表</span>
              </li>
            </ul>
            <p v-else-if="d" class="empty">{{ d.configured.duty ? "今天没有值日安排" : "值日表尚未接入" }}</p>
          </div>
        </section>

        <section class="module enter">
          <div class="module-head">
            <span class="module-tag">RANK / 03</span>
            <h2>打卡工时排行</h2>
            <span class="module-sub">累计</span>
          </div>
          <div class="module-body body-stack">
            <RankList :members="d ? d.rank : null" box-id="attend-rank-box" />
          </div>
        </section>
      </div>
    </div>
  </PortalPage>
</template>

<style scoped>
.att-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr));
  gap: 12px;
  margin-bottom: 20px;
}
.att-stat {
  display: grid;
  gap: 6px;
  padding: 14px 16px 12px;
  border-top: 4px solid var(--c-ink);
  min-width: 0;
}
.att-stat-tag { font: 700 9px/1 var(--f-wide); letter-spacing: .18em; color: var(--c-text-3); }
.att-stat-num { font: 700 26px/1.1 var(--f-num); color: var(--c-ink); overflow-wrap: anywhere; }
.att-stat-num i { font-style: normal; font-size: 14px; color: var(--c-text-3); }
.att-stat-names { font: 700 18px/1.35 var(--f-cn); }
.att-stat small { font-size: 12px; color: var(--c-text-3); }

.att-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr);
  gap: 20px;
  align-items: start;
}
.att-grid > .module, .att-side > .module { margin: 0; }
.att-side { display: grid; gap: 20px; min-width: 0; }
.att-side .body-stack { max-height: 420px; }

.att-tbl td { vertical-align: middle; }
.att-hours { display: inline-flex; align-items: baseline; gap: 2px; }
.att-hours i { font-style: normal; color: var(--c-text-3); }
.att-prog { display: block; width: 96px; margin: 6px 0 0 auto; }
.att-last { color: var(--c-text-2); }
.att-last-mark { display: block; font: 600 10px/1.2 var(--f-cn); letter-spacing: .06em; }
.att-last-mark.is-ok { color: var(--c-ok); }
.att-last-mark.is-miss { color: var(--c-text-3); }
.att-last-mark.is-leave { color: var(--c-text-2); }
/* 请假：虚线边框，和「待结算」等中性状态区分开 */
.badge.is-leave { border-style: dashed; color: var(--c-text-2); }
.att-c-leave { white-space: nowrap; }
.att-today { white-space: nowrap; }
.att-today .badge + .badge { margin-left: 4px; }

.att-duty-src { margin-bottom: 10px; }
.att-duty-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
.att-duty-person {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--c-divider);
  border-left: 3px solid var(--c-ink);
  background: var(--c-white);
}

.sr-only {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap;
}

@media (max-width: 1100px) {
  .att-grid { grid-template-columns: minmax(0, 1fr); }
  .att-side .body-stack { max-height: none; }
}
/* 窄屏：表格改成每人一张卡片（姓名 + 达标 + 飞书 │ 本周时长与进度 │ 上周 + 今日），不再横向滑动 */
@media (max-width: 640px) {
  .att-tbl thead { display: none; }
  .att-tbl, .att-tbl tbody { display: block; }
  .att-tbl tbody tr[data-member] {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    grid-template-areas: "name status link" "hours hours hours" "last today today" "leave leave leave";
    align-items: center;
    gap: 8px 10px;
    padding: 12px 14px;
    border-bottom: 1px solid var(--c-divider);
  }
  .att-tbl tbody tr[data-member] > td { display: block; padding: 0; border: 0; text-align: left; }
  .att-tbl tbody tr:not([data-member]), .att-tbl tbody tr:not([data-member]) > td { display: block; }
  .att-c-name { grid-area: name; min-width: 0; overflow-wrap: anywhere; }
  .att-c-status { grid-area: status; }
  .att-c-link { grid-area: link; }
  .att-c-hours { grid-area: hours; }
  .att-c-last { grid-area: last; }
  .att-c-today { grid-area: today; justify-self: end; }
  .att-c-leave { grid-area: leave; justify-self: end; }
  .att-c-hours::before, .att-c-last::before {
    content: attr(data-label) " ";
    font: 600 11px/1 var(--f-cn);
    letter-spacing: .06em;
    color: var(--c-text-3);
  }
  .att-c-last .att-last-mark { display: inline; margin-left: 6px; }
  .att-prog { width: 100%; margin: 6px 0 0; }
}
</style>
