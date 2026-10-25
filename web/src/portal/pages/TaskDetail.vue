<script setup lang="ts">
/* 事项详情：基本信息 + 字段更新卡（所有人可改两态执行状态，已完成（已归档）的事项只有组长能改回；
 * 组长另可标记已完成——完成时间由服务端自动记为此刻、事项随即归档——以及编辑全部字段与删除） */
import { computed, ref } from "vue";
import PortalPage from "../components/PortalPage.vue";
import SyncMark from "../components/SyncMark.vue";
import { navNo } from "../nav";
import SrcLink from "../components/SrcLink.vue";
import DeleteButton from "../components/DeleteButton.vue";
import ConfirmButton from "../components/ConfirmButton.vue";
import { RouterLink, useRoute } from "@/shared/router";
import { completeTask, fetchTask, patchRecord } from "../api";
import { afterWrite, isLeader, openForm } from "../store";
import { usePageData } from "../usePageData";
import { taskPreset } from "../forms";
import { fmtD, fmtDT } from "@/shared/format";
import { errorDetail } from "@/shared/http";
import type { Task, UserRef } from "../types";
import { isAccepted, statusText } from "../taskStatus";
import { toast } from "@/shared/toast";

const route = useRoute();
const id = route.value.params.id;
const { body, notFound } = usePageData(() => fetchTask(id));
const t = computed<Task | null>(() => (body.value && body.value.data) || null);

function names(list?: UserRef[]): string {
  return (list || []).map((u) => u.name || "…" + u.id.slice(-4)).join("、") || "待分配";
}

const kv = computed(() => {
  const x = t.value;
  if (!x) return [];
  return [
    ["状态", statusText(x.status)],
    ["主责组", x.owner_group || "—"],
    ["协作组", (x.collab_groups || []).join("、") || "—"],
    ["负责人", names(x.owners)],
    ["开始", fmtDT(x.start && x.start.ts)],
    /* 精度为「仅日期」时只显示日期，不虚构时刻 */
    ["截止", x.due ? (x.due_precision === "date" ? fmtD(x.due.ts) + "（仅日期）" : fmtDT(x.due.ts) + "（精确时刻）") : "未设置"],
    /* 重新打开的事项飞书里还留着旧的验收时间，只在已完成时显示 */
    ["完成时间", isAccepted(x) ? fmtDT(x.done_time && x.done_time.ts, "—") : "—"],
    ["提交验收", fmtDT(x.submit_time && x.submit_time.ts)],
  ];
});

/* ---- 字段更新 ---- */
const done = computed(() => !!t.value && isAccepted(t.value));
const busyKey = ref("");

async function write(key: string, send: (id: string) => ReturnType<typeof completeTask>) {
  if (!t.value) return;
  busyKey.value = key;
  try {
    const res = await send(t.value.id);
    if (res.ok) afterWrite(res.body);
    else toast("写入失败：" + errorDetail(res), "error");
  } catch {
    toast("网络错误，未写入", "error");
  } finally {
    busyKey.value = "";
  }
}
function patch(fields: Record<string, unknown>, key: string) {
  void write(key, (rid) => patchRecord("tasks", rid, fields));
}
function complete() {
  void write("complete", completeTask);
}
function editAll() {
  if (t.value) openForm("task", taskPreset(t.value), t.value.id);
}
</script>

<template>
  <div id="task-detail">
    <PortalPage :no="navNo('/tasks')" en="TASKS │ DETAIL" :title="t ? t.name : (notFound ? '记录不存在' : '事项详情')" watermark="TASK">
      <template #actions>
        <RouterLink to="/tasks" class="btn ghost sm">← 返回事项</RouterLink>
      </template>
      <template v-if="t">
        <SyncMark :mark="t.sync" block />
        <p v-if="t.invalid" class="note">※ 该事项源数据存在质量异常（未知枚举/失效关联），已排除出统计。</p>
        <p v-if="t.overdue" class="note is-error">※ 已逾期。</p>
        <p v-else-if="t.due_soon" class="note">※ 即将到期（72 小时内）。</p>

        <div class="stack">
          <section class="module enter">
            <div class="module-head">
              <span class="prio" :data-p="t.priority || ''">{{ t.priority || "—" }}</span>
              <h2>{{ t.name }}</h2>
              <SrcLink :url="t.url" label="在飞书中打开该记录 ↗" />
            </div>
            <dl class="kv">
              <template v-for="[k, v] in kv" :key="k"><dt>{{ k }}</dt><dd>{{ v }}</dd></template>
              <dt>所属项目</dt>
              <dd>
                <RouterLink v-if="t.project_id" :to="'/projects/' + encodeURIComponent(t.project_id)" class="lnk">{{ t.project_name || t.project_id }}</RouterLink>
                <template v-else>—</template>
              </dd>
            </dl>
          </section>

          <section v-if="t.description || t.blocker_note" class="module enter">
            <div class="module-head"><span class="module-tag">NOTE / 02</span><h2>说明</h2></div>
            <div class="module-body">
              <p v-if="t.description" class="prose">说明：{{ t.description }}</p>
              <p v-if="t.blocker_note" class="prose" style="margin-top: 8px">阻塞：{{ t.blocker_note }}</p>
            </div>
          </section>

          <section class="module enter">
            <div class="module-head"><span class="module-tag">UPDATE / 03</span><h2>字段更新</h2></div>
            <div class="module-body">
              <div class="edit-row" id="task-complete">
                <span class="edit-label">完成</span>
                <template v-if="done">
                  <span class="badge neutral">已完成</span>
                  <span class="hint">{{ fmtDT(t.done_time && t.done_time.ts, "") }}{{ t.done_time ? " 完成，" : "" }}已归档</span>
                </template>
                <ConfirmButton
                  v-else-if="isLeader()"
                  cls="sm"
                  label="标记已完成"
                  armed-label="再点一次确认完成（随即归档）"
                  :busy="busyKey === 'complete'"
                  @confirm="complete"
                />
                <span v-else class="hint">由组长标记完成</span>
              </div>
              <div class="edit-row" id="task-status">
                <span class="edit-label">执行状态</span>
                <span v-if="done && !isLeader()" class="hint">已归档，只有组长能重新打开</span>
                <template v-else>
                  <button
                    v-for="st in ['未开始', '进行中']"
                    :key="st"
                    type="button"
                    class="btn ghost sm"
                    :class="{ 'is-on': t.status === st }"
                    :aria-pressed="t.status === st ? 'true' : 'false'"
                    :disabled="busyKey === st"
                    @click="patch({ status: st }, st)"
                  >{{ busyKey === st ? "写入中…" : st }}</button>
                </template>
              </div>
              <div v-if="isLeader()" class="edit-actions">
                <button type="button" class="btn sm" @click="editAll">编辑全部字段（组长）</button>
                <span class="spacer"></span>
                <DeleteButton :id="t.id" table="tasks" label="事项" />
              </div>
            </div>
          </section>
        </div>
      </template>
    </PortalPage>
  </div>
</template>
