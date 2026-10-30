<script setup lang="ts">
/* 考勤打卡条（排行卡头部）：签到 / 签退；网段校验在服务端 */
import { onMounted, ref, watch } from "vue";
import { attendanceAction, fetchAttendance } from "../api";
import { requestRefresh, store } from "../store";
import { errorDetail } from "@/shared/http";
import { fmtDT } from "@/shared/format";
import { toast } from "@/shared/toast";

const stateText = ref("考勤状态读取中…");
const checkedIn = ref(false);
const ready = ref(false);
const busy = ref(false);
const hours = ref("0.00");
/* 首次读取失败时不能把按钮永久藏起来：给一个「重试」出口，
 * 否则网络抖一下这个页面就再也签不上到（审计 P1-3）。 */
const failed = ref(false);

async function refresh() {
  try {
    const st = await fetchAttendance();
    hours.value = st.work_hours != null ? Number(st.work_hours).toFixed(2) : "0.00";
    checkedIn.value = !!st.checked_in;
    stateText.value = st.checked_in ? "已签到 " + fmtDT(st.since || null) : "未签到";
    ready.value = true;
    failed.value = false;
  } catch {
    stateText.value = "考勤状态不可用";
    failed.value = true;
  }
}

/* 打卡按钮：状态未知时点它先重试读取，已知时才真正打卡 */
function onButton() {
  if (failed.value) { void refresh(); return; }
  void onClick();
}

async function onClick() {
  busy.value = true;
  const act = checkedIn.value ? "check-out" : "check-in";
  try {
    const res = await attendanceAction(act);
    busy.value = false;
    if (res.ok) {
      const msg = act === "check-out"
        ? "已签退 · 本次 +" + res.body.session_hours + "h（累计 " + res.body.work_hours + "h）"
        : "已签到";
      toast(msg + (res.body.queued ? "，正在推送到飞书" : ""), "ok");
      requestRefresh();
      void refresh();
    } else {
      toast("打卡失败：" + errorDetail(res), "error");
      void refresh();
    }
  } catch {
    busy.value = false;
    toast("网络错误，未打卡", "error");
  }
}

onMounted(refresh);
/* 轮询或写入后全局刷新时同步卡片状态，避免显示的是上一轮的结果 */
watch(() => store.refreshSeq, refresh);
</script>

<template>
  <div id="attend-box" class="checkin-bar">
    <span class="dot" :class="checkedIn ? 'ok' : 'warn'" aria-hidden="true"></span>
    <span id="attend-state" class="checkin-text" role="status">{{ stateText }}<template v-if="ready"><span class="sep">│</span>我的工时 <b class="t-num">{{ hours }}</b> h</template></span>
    <button
      id="attend-btn"
      type="button"
      class="btn sm"
      :hidden="!ready && !failed"
      :disabled="busy"
      :aria-busy="busy ? 'true' : undefined"
      @click="onButton"
    >{{ failed ? "重试" : (checkedIn ? "签退" : "签到") }}</button>
  </div>
</template>

<style scoped>
.checkin-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 14px;
  padding: 10px 14px;
  background: var(--c-surface);
  border: 1px solid var(--c-divider);
  margin-bottom: 10px;
  flex: none;
}
.checkin-text { font-size: 13px; color: var(--c-text-2); min-width: 0; }
.checkin-text .sep { color: var(--c-divider); margin: 0 8px; }
.checkin-text b { color: var(--c-ink); }
.checkin-bar .btn { margin-left: auto; }
</style>
