<script setup lang="ts">
/* 组长删除（两步确认防误删）：第一次点击进入待确认态，3 秒内再点才真正删除；
 * 成功后立即回列表（列表里该记录已经不在了），并弹出通知 */
import { onBeforeUnmount, ref } from "vue";
import { deleteRecord, type WriteTable } from "../api";
import { requestRefresh } from "../store";
import { errorDetail } from "@/shared/http";
import { router } from "@/shared/router";
import { toast } from "@/shared/toast";

const props = defineProps<{ id: string; table: WriteTable; label: string }>();
const armed = ref(false);
const busy = ref(false);
let timer = 0;

async function onClick() {
  if (!armed.value) {
    armed.value = true;
    timer = window.setTimeout(() => { armed.value = false; }, 3000);
    return;
  }
  window.clearTimeout(timer);
  busy.value = true;
  try {
    const res = await deleteRecord(props.table, props.id);
    if (res.ok) {
      router.push("/" + props.table);
      toast(res.body.queued ? "已删除，正在推送到飞书" : "已删除", "ok");
      requestRefresh();
      return;
    }
    toast("删除失败：" + errorDetail(res), "error");
  } catch {
    toast("网络错误，未删除", "error");
  }
  busy.value = false;
  armed.value = false;
}
onBeforeUnmount(() => window.clearTimeout(timer));
</script>

<template>
  <button type="button" class="btn danger ghost sm" :class="{ 'is-armed': armed }" :disabled="busy" @click="onClick">
    {{ busy ? "删除中…" : armed ? "再点一次确认删除（不可恢复）" : "删除" + label }}
  </button>
</template>
