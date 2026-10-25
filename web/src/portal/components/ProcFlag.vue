<script setup lang="ts">
/* 采购布尔列开关：已采购 / 发票已开全员可切；组长批复只有组长看到按钮，组员看到只读徽标 */
import { computed, ref } from "vue";
import { patchRecord } from "../api";
import { afterWrite, isLeader } from "../store";
import { errorDetail } from "@/shared/http";
import type { Procurement } from "../types";
import { toast } from "@/shared/toast";

const props = defineProps<{
  item: Procurement;
  field: "leader_approval" | "purchased" | "invoice_done";
  on: string;
  off: string;
}>();
const busy = ref(false);
const value = computed(() => !!props.item[props.field]);
const readonly = computed(() => props.field === "leader_approval" && !isLeader());

async function toggle() {
  busy.value = true;
  try {
    const res = await patchRecord("procurement", props.item.id, { [props.field]: !value.value });
    if (res.ok) afterWrite(res.body);
    else toast("写入失败：" + errorDetail(res), "error");
  } catch {
    toast("网络错误，未写入", "error");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <span v-if="readonly" class="badge" :class="value ? 'ok' : 'neutral'">{{ value ? on : off }}</span>
  <button
    v-else
    type="button"
    class="btn ghost xs"
    :class="{ 'is-on': value }"
    :aria-pressed="value ? 'true' : 'false'"
    :disabled="busy"
    @click="toggle"
  >{{ busy ? "写入中…" : value ? on : off }}</button>
</template>
