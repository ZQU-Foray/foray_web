<script setup lang="ts">
/* 签到 / 签退按钮：按传入的「是否已签到」决定动作，真正的状态判定、网段校验与
 * 「签到不足 1 分钟」「跨零点作废」这些规则都留在服务端，前端只负责点与提示。 */
import { ref } from "vue";
import { attendanceAction } from "../api";
import { errorDetail } from "@/shared/http";
import { toast } from "@/shared/toast";

const props = withDefaults(defineProps<{
  checkedIn: boolean;
  /* 同一个页面里可能有多处入口，id 由调用方给，避免重复 id */
  btnId?: string;
}>(), { btnId: "attend-btn" });

const emit = defineEmits<{ (e: "done"): void }>();

const busy = ref(false);

async function onClick(): Promise<void> {
  busy.value = true;
  const act = props.checkedIn ? "check-out" : "check-in";
  try {
    const res = await attendanceAction(act);
    if (res.ok) {
      const body = res.body || {};
      const msg = act === "check-out"
        ? "已签退 · 本次 +" + (body.session_hours ?? "0") + "h（累计 " + (body.work_hours ?? "—") + "h）"
        : "已签到";
      toast(msg + (body.queued ? "，正在推送到飞书" : ""), "ok");
    } else {
      toast("打卡失败：" + errorDetail(res), "error");
    }
    /* 无论成败都通知调用方重新拉数据：失败时代码里带着服务端原文，成功时状态已经变了 */
    emit("done");
  } catch {
    toast("网络错误，未打卡", "error");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <button :id="btnId" type="button" class="btn sm" :disabled="busy"
          :aria-busy="busy ? 'true' : undefined" @click="onClick">
    {{ checkedIn ? "签退" : "签到" }}
  </button>
</template>
