<script setup lang="ts">
/* 自绘下拉组件：包裹原生 select（保留原 id 与 value），交互由 lib/dropdown.ts 驱动。
 * DOM 契约与旧版 dropdown.js 一致：.dd > select.dd-native + button.dd-trigger（触发器是
 * select 的后续兄弟，用例按「#id ~ .dd-trigger」定位）；面板 .dd-panel[data-for=id] 挂 body。
 * 选中值两种用法：
 * - 受控：传 value（配合 v-model:value）。选项变化或 value 变化后按旧版 fillSelect 语义落盘：
 *   值在选项里就选中它，否则选第一项并回报 update:value；交互中的赋值先暂存；
 * - 非受控：不传 value，由业务代码直接写 select.value（经拦截器，交互中暂存），
 *   选项重建时保留当前值、找不到则选第一项。 */
import { onBeforeUnmount, onBeforeUpdate, onMounted, ref, watch } from "vue";
import { attachDropdown, optionsSignature, writeNativeValue, type DdController, type DdOption } from "../lib/dropdown";

const props = defineProps<{
  id: string;
  options: DdOption[];
  value?: string;
  wrapClass?: string;
  disabled?: boolean;
  ariaLabel?: string;
}>();
const emit = defineEmits<{
  (e: "change", value: string): void;
  (e: "update:value", value: string): void;
}>();

const wrap = ref<HTMLElement | null>(null);
const select = ref<HTMLSelectElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
const label = ref<HTMLElement | null>(null);
const panel = ref<HTMLElement | null>(null);

let ctl: DdController | null = null;
let keep = "";                                  // 选项重建前的当前值（非受控模式保留它）
let lastSig = optionsSignature(props.options);

function native(): string {
  return select.value ? select.value.value : "";
}

/* fillSelect 语义：期望值在选项里就选中，否则选第一项；交互中先暂存（经 value 拦截器） */
function settle(want: string) {
  const sel = select.value;
  if (!sel || !ctl) return;
  if (ctl.busy()) {
    sel.value = want;                           // 拦截器暂存，交互结束后落盘
    return;
  }
  writeNativeValue(sel, want);
  if (sel.selectedIndex < 0 && sel.options.length) sel.selectedIndex = 0;
  ctl.syncLabel();
  if (props.value !== undefined && native() !== props.value) emit("update:value", native());
}

function onNativeChange() {
  ctl?.syncLabel();
  const v = native();
  if (props.value !== undefined && v !== props.value) emit("update:value", v);
  emit("change", v);
}

onMounted(() => {
  ctl = attachDropdown({
    wrap: wrap.value!, select: select.value!, trigger: trigger.value!,
    label: label.value!, panel: panel.value!,
  });
  settle(props.value !== undefined ? props.value : native());
});

onBeforeUpdate(() => { keep = native(); });

/* 选项真的变化后（DOM 已更新）：重建面板并按语义落盘选中值 */
watch(() => props.options, (opts) => {
  const sig = optionsSignature(opts);
  if (sig === lastSig) return;
  lastSig = sig;
  ctl?.rebuild();
  settle(props.value !== undefined ? props.value : keep);
}, { flush: "post" });

watch(() => props.value, (v) => {
  if (v !== undefined) settle(v);
}, { flush: "post" });

onBeforeUnmount(() => { ctl?.destroy(); ctl = null; });
</script>

<template>
  <span ref="wrap" class="dd" :class="wrapClass">
    <select
      :id="id"
      ref="select"
      class="dd-native"
      aria-hidden="true"
      tabindex="-1"
      :disabled="disabled"
      @change="onNativeChange"
    >
      <option v-for="o in options" :key="o.value" :value="o.value" :disabled="o.disabled">{{ o.label }}</option>
    </select>
    <button
      ref="trigger"
      type="button"
      class="dd-trigger"
      aria-haspopup="listbox"
      aria-expanded="false"
      :aria-controls="'dd-panel-' + id"
      :aria-label="ariaLabel"
    ><span ref="label" class="dd-label"></span></button>
    <Teleport to="body">
      <div :id="'dd-panel-' + id" ref="panel" class="dd-panel" role="listbox" :data-for="id" hidden></div>
    </Teleport>
  </span>
</template>
