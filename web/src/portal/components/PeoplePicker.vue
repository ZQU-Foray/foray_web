<script setup lang="ts">
/* 人员多选：控件本体是一个下拉形状的框，框内显示已选人员标签（× 可单个移除）；
 * 点开后在浮层里勾选。浮层挂 body 并用 fixed 定位——弹窗本身可滚动且带动画，
 * 留在文档流里会被裁掉或错位。页面滚动会让浮层与触发器脱位，故滚动即关闭，
 * 但浮层自身的滚动不算（人员多时需要在里面滚动查看）。 */
import { computed, nextTick, onBeforeUnmount, ref } from "vue";
import { personLabel, rosterPeople, type Person } from "../forms";

defineProps<{ inputId?: string }>();
const model = defineModel<string[]>({ required: true });

const box = ref<HTMLElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
const panel = ref<HTMLElement | null>(null);
const open = ref(false);
const pos = ref<Record<string, string>>({});

/* 编辑预置的人员可能已不在花名册：补进选项，否则保存时会被静默丢掉 */
const people = computed<Person[]>(() => {
  const list = rosterPeople();
  (model.value || []).forEach((id) => {
    if (!list.some((u) => u.id === id)) list.push({ id, name: "…" + String(id).slice(-6), groups: [], leader: false });
  });
  return list;
});

function personOf(id: string): Person {
  return people.value.find((u) => u.id === id) || { id, name: "…" + String(id).slice(-6), groups: [], leader: false };
}

function toggle(id: string, on: boolean) {
  const cur = model.value || [];
  model.value = on ? (cur.includes(id) ? cur : cur.concat([id])) : cur.filter((v) => v !== id);
  /* 被移除的标签连同它的按钮一起消失：焦点回到「添加人员」，不要掉到 body 上 */
  if (!on) nextTick(() => trigger.value?.focus());
}

function place() {
  const el = box.value;
  if (!el) return;
  const r = el.getBoundingClientRect();
  const below = innerHeight - r.bottom - 12;
  const p: Record<string, string> = {
    minWidth: r.width + "px",
    maxWidth: Math.min(420, innerWidth - 24) + "px",
    left: Math.max(8, Math.min(r.left, innerWidth - 24 - r.width)) + "px",
  };
  if (below < 140 && r.top > below) {
    p.bottom = innerHeight - r.top + 4 + "px";
    p.maxHeight = Math.min(240, Math.max(120, r.top - 12)) + "px";
  } else {
    p.top = r.bottom + 4 + "px";
    p.maxHeight = Math.min(240, Math.max(120, below)) + "px";
  }
  pos.value = p;
}

function onDoc(e: MouseEvent) {
  const t = e.target as Node;
  if (panel.value && !panel.value.contains(t) && box.value && !box.value.contains(t)) close();
}
function onScroll(e: Event) {
  const t = e.target as Node;
  if (panel.value && (t === panel.value || panel.value.contains(t))) return;
  close();
}
function close() {
  open.value = false;
  document.removeEventListener("click", onDoc, true);
  window.removeEventListener("scroll", onScroll, true);
  window.removeEventListener("resize", close);
}
function show() {
  place();
  open.value = true;
  /* 延迟注册：避免本次点击立刻触发「点外部关闭」 */
  window.setTimeout(() => {
    document.addEventListener("click", onDoc, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
  }, 0);
  nextTick(() => panel.value?.querySelector<HTMLInputElement>("input")?.focus());
}
function onTrigger() {
  if (open.value) close(); else show();
}
function onKey(e: KeyboardEvent) {
  if (e.key === "Escape" && open.value) {
    e.stopPropagation();
    close();
    trigger.value?.focus();
  }
}

onBeforeUnmount(close);
</script>

<template>
  <!-- 外壳是 div：里面既有「打开候选」的按钮，也有每个标签各自的「移除」按钮。
       不能再把所有东西塞进一个 button（HTML 不允许按钮嵌套按钮），
       否则移除控件键盘到不了、读屏也只念一个按钮。 -->
  <div :id="inputId" ref="box" class="people-trigger">
    <button
      ref="trigger"
      type="button"
      class="people-open"
      :aria-expanded="open ? 'true' : 'false'"
      aria-haspopup="listbox"
      @click.stop="onTrigger"
      @keydown="onKey"
    ><span class="people-ph">{{ model.length ? "＋ 添加人员" : "＋ 选择人员" }}</span></button>
    <span v-for="id in model" :key="id" class="people-tag">
      {{ personLabel(personOf(id)) }}
      <button
        type="button"
        class="people-tag-x"
        :aria-label="'移除 ' + personLabel(personOf(id))"
        title="移除"
        @click.stop="toggle(id, false)"
      >×</button>
    </span>
  </div>
  <Teleport to="body">
    <div v-if="open" ref="panel" class="people-panel" :style="pos" role="listbox" aria-multiselectable="true" @keydown="onKey">
      <p v-if="!people.length" class="note" style="margin: 4px">花名册暂无人员（请先在飞书花名册里维护）</p>
      <label v-for="u in people" :key="u.id">
        <input type="checkbox" :value="u.id" :checked="model.includes(u.id)" @change="toggle(u.id, ($event.target as HTMLInputElement).checked)">
        {{ personLabel(u) }}
      </label>
    </div>
  </Teleport>
</template>
