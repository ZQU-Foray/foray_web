<script setup lang="ts">
/* 写入弹窗：按 forms.ts 的字段定义渲染；新建走 POST、编辑走 PATCH。
 * 采购新建时选了附件：记录创建成功后按目标列逐份上传，失败不回滚记录本身。 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import PeoplePicker from "./PeoplePicker.vue";
import {
  FORMS, collectPayload, ensureProcOptions, ensureProjectOptions, missingRequired,
  procOptionList, type FieldSpec, type FormKind,
} from "../forms";
import { createRecord, patchRecord, uploadProcAttachment } from "../api";
import { afterWrite, closeForm, isLeader, requestRefresh, type Option } from "../store";
import { errorDetail } from "@/shared/http";
import { toast } from "@/shared/toast";

const props = defineProps<{
  kind: FormKind;
  preset?: Record<string, unknown>;
  recordId?: string;
}>();

const spec = FORMS[props.kind];
const editing = !!props.recordId;
const title = (editing ? "编辑" : "") + spec.title;
const submitText = editing ? "保存修改" : "创建并写回飞书";

/* 组长专属行非组长不渲染（载荷也就不带该键）；附件选择仅新建用 */
const rows = computed<FieldSpec[]>(() =>
  spec.rows.filter((r) => !(r.leaderOnly && !isLeader()) && !(r.t === "files" && editing)));

const values = reactive<Record<string, unknown>>({});
for (const r of spec.rows) {
  const pv = props.preset ? props.preset[r.k] : undefined;
  const v = pv != null ? pv : (r.def != null ? r.def : "");
  if (r.t === "checks" || r.t === "people") values[r.k] = Array.isArray(v) ? v.slice() : [];
  else if (r.t === "check") values[r.k] = !!v;
  else values[r.k] = v;
}
const files = reactive<Record<string, File[]>>({});
const projectOpts = ref<Option[]>([]);
const busy = ref(false);
const btnText = ref(submitText);
const box = ref<HTMLElement | null>(null);

/* 下拉候选：首项「不设置」；当前值不在候选里也保留，避免保存时被静默丢掉 */
function selectOptions(r: FieldSpec): Array<{ v: string; label: string }> {
  const parent = r.link ? String(values[r.link] || "") : null;
  const list = procOptionList(r.optsFrom || "", parent);
  const cur = String(values[r.k] || "");
  const out = [{ v: "", label: "（不设置）" }].concat(list.map((x) => ({ v: x, label: x })));
  if (cur && list.indexOf(cur) < 0) out.push({ v: cur, label: cur + "（已从飞书选项移除）" });
  return out;
}

/* 联动：一级分类改了，二级分类不在新候选里就复位 */
for (const r of spec.rows) {
  if (r.t !== "select" || !r.link) continue;
  const linkKey = r.link;
  watch(() => values[linkKey], () => {
    const list = procOptionList(r.optsFrom || "", String(values[linkKey] || ""));
    if (list.indexOf(String(values[r.k] || "")) < 0) values[r.k] = "";
  });
}

function onFiles(r: FieldSpec, e: Event) {
  files[r.k] = Array.from((e.target as HTMLInputElement).files || []);
}

async function submit() {
  const payload = collectPayload(values, rows.value);
  const miss = missingRequired(rows.value, payload);
  if (miss) { toast("缺必填项：" + miss, "error"); return; }
  busy.value = true;
  btnText.value = "写入中…";
  try {
    const res = editing
      ? await patchRecord(spec.table, props.recordId as string, payload)
      : await createRecord(spec.table, payload);
    if (!res.ok) {
      toast("写入失败：" + errorDetail(res), "error");
      return;
    }
    const rid = (res.body as { record_id?: string }).record_id;
    const jobs: Array<{ file: File; col: "image" | "invoice" }> = [];
    if (spec.uploadAfter && !editing && rid) {
      for (const r of spec.rows) {
        if (r.t !== "files") continue;
        (files[r.k] || []).forEach((f) => jobs.push({ file: f, col: r.col || "image" }));
      }
    }
    for (let i = 0; i < jobs.length; i++) {
      btnText.value = "上传附件 " + (i + 1) + "/" + jobs.length + "…";
      try {
        const up = await uploadProcAttachment(rid as string, jobs[i].col, jobs[i].file);
        if (!up.ok) {
          closeForm();
          toast("记录已创建，但附件『" + jobs[i].file.name + "』上传失败：" + errorDetail(up) +
            "——可在详情页继续上传", "error");
          requestRefresh();
          return;
        }
      } catch {
        closeForm();
        toast("记录已创建，但附件上传中断（网络错误）——可在详情页继续上传", "error");
        requestRefresh();
        return;
      }
    }
    closeForm();
    afterWrite(res.body);
  } catch {
    toast("网络错误，未写入", "error");
  } finally {
    busy.value = false;
    btnText.value = submitText;
  }
}

function onKey(e: KeyboardEvent) {
  if (e.key === "Escape") closeForm();
}

let lastFocus: HTMLElement | null = null;
onMounted(async () => {
  lastFocus = document.activeElement as HTMLElement | null;
  document.addEventListener("keydown", onKey);
  nextTick(() => box.value?.querySelector<HTMLElement>("input, select, textarea, button.people-trigger")?.focus());
  if (spec.rows.some((r) => r.t === "select")) await ensureProcOptions();
  if (spec.rows.some((r) => r.t === "project")) projectOpts.value = await ensureProjectOptions();
});
onBeforeUnmount(() => {
  document.removeEventListener("keydown", onKey);
  lastFocus?.focus?.();
});

/* 编辑预置的项目可能已不在候选里：补一条，保存时不丢 */
const projectChoices = computed<Option[]>(() => {
  const cur = String(values.project || "");
  const list = projectOpts.value.slice();
  if (cur && !list.some((o) => o.id === cur)) list.push({ id: cur, label: "（当前关联项目）" });
  return list;
});
</script>

<template>
  <div class="modal" @click.self="closeForm">
    <div ref="box" class="modal-box" role="dialog" aria-modal="true" :aria-label="title">
      <div class="modal-head">
        <h2>{{ title }}</h2>
        <button type="button" class="modal-close" aria-label="关闭" @click="closeForm">×</button>
      </div>
      <div class="modal-body">
        <div v-for="r in rows" :key="r.k" class="form-row">
          <span class="form-label">{{ r.label }}<span v-if="r.req" class="req">（必填）</span></span>
          <div class="form-ctl">
            <input v-if="r.t === 'text'" v-model="values[r.k]" class="input" type="text" :data-k="r.k">
            <input v-else-if="r.t === 'dt'" v-model="values[r.k]" class="input" type="datetime-local" :data-k="r.k">
            <input v-else-if="r.t === 'date'" v-model="values[r.k]" class="input" type="date" :data-k="r.k">
            <input v-else-if="r.t === 'number'" v-model="values[r.k]" class="input" type="number" step="0.01" min="0" :data-k="r.k">
            <select v-else-if="r.t === 'select'" v-model="values[r.k]" class="select" :data-k="r.k">
              <option v-for="o in selectOptions(r)" :key="o.v" :value="o.v">{{ o.label }}</option>
            </select>
            <select v-else-if="r.t === 'project'" v-model="values[r.k]" class="select" :data-k="r.k">
              <option value="">（无）</option>
              <option v-for="o in projectChoices" :key="o.id" :value="o.id">{{ o.label }}</option>
            </select>
            <label v-else-if="r.t === 'check'" class="opt"><input v-model="values[r.k]" type="checkbox" :data-k="r.k"> 勾选</label>
            <div v-else-if="r.t === 'radio'" class="opts" role="radiogroup" :aria-label="r.label">
              <label v-for="o in r.opts" :key="o" class="opt"><input v-model="values[r.k]" type="radio" :name="r.k" :value="o"> {{ o }}</label>
            </div>
            <div v-else-if="r.t === 'checks'" class="opts" role="group" :aria-label="r.label">
              <label v-for="o in r.opts" :key="o" class="opt"><input v-model="values[r.k]" type="checkbox" :name="r.k" :value="o"> {{ o }}</label>
            </div>
            <PeoplePicker v-else-if="r.t === 'people'" v-model="(values[r.k] as string[])" />
            <input v-else-if="r.t === 'files'" class="file-input" type="file" multiple :accept="r.accept || 'image/*'" @change="onFiles(r, $event)">
          </div>
        </div>
      </div>
      <div class="modal-foot">
        <button type="button" class="btn" :disabled="busy" @click="submit">{{ btnText }}</button>
        <button type="button" class="btn ghost" @click="closeForm">取消</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.form-ctl { min-width: 0; }
</style>
