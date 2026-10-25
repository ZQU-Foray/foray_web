<script setup lang="ts">
/* 波形区外壳：勾选（电机 / 数据类型 / 目标叠加）、列数、开始/停止绘图、清空、导出 CSV，
 * 以及数值总览与分图网格两个容器（内容由 lib/chart.ts 命令式绘制）。
 * 勾选框不绑定 Vue 状态：绘制与导出时直接读 DOM 的 checked（与旧版同口径）。 */
import { onBeforeUnmount, onMounted, ref } from "vue";
import { FIELDS, detail, detailUi } from "../lib/chart";

const grid = ref<HTMLElement | null>(null);
const numgrid = ref<HTMLElement | null>(null);
const COLS: [string, string][] = [["auto", "自动"], ["1", "1 列"], ["2", "2 列"]];

onMounted(() => { if (grid.value && numgrid.value) detail.attach(grid.value, numgrid.value); });
onBeforeUnmount(() => detail.detach());
</script>

<template>
  <div id="detail-panel" class="detail-panel" @change="detail.onPanelChange()">
    <div class="detail-controls">
      <div class="dc-group">
        <span class="dc-label">电机</span>
        <label class="chk"><input id="dc-m1" type="checkbox" checked> M-1</label>
        <label class="chk"><input id="dc-m2" type="checkbox" checked> M-2</label>
        <label class="chk"><input id="dc-m3" type="checkbox" checked> M-3</label>
      </div>
      <div class="dc-group">
        <span class="dc-label">数据</span>
        <label v-for="f in FIELDS" :key="f.key" class="chk"><input :id="'dc-' + f.key" type="checkbox" :checked="f.on"><span class="sw" :style="{ '--c': f.color }"></span>{{ f.key === "freq" ? "频率" : f.name }}</label>
        <label class="chk"><input id="dc-target" type="checkbox" checked> 目标叠加</label>
      </div>
      <div class="dc-group dc-actions">
        <span class="seg-cols" role="group" aria-label="图表布局列数" title="图表布局列数（绘图较多或分辨率不足时可手动收紧）">
          <button v-for="[c, t] in COLS" :key="c" type="button" :data-cols="c" :class="{ 'is-on': detailUi.cols === c }" :aria-pressed="detailUi.cols === c ? 'true' : 'false'" @click="detail.setCols(c)">{{ t }}</button>
        </span>
        <button id="dc-start" type="button" class="btn sm" :class="{ ghost: detailUi.started }" @click="detail.toggleStart()">{{ detailUi.started ? "停止绘图" : "开始绘图" }}</button>
        <button id="dc-clear" type="button" class="btn ghost sm" @click="detail.clear()">清空</button>
        <button id="dc-export" type="button" class="btn sec sm" @click="detail.exportCsv()">导出 CSV</button>
      </div>
    </div>
    <div id="numgrid" ref="numgrid" class="numgrid"></div>
    <div id="detail-charts" ref="grid" class="dchart-grid"></div>
    <p class="hint">每个数据类型独立一图（图内三电机同量纲同尺度，可直接对比）；
      线型区分电机（实线/长虚/点虚），半透明细线=目标值。
      绘图默认关闭，点「开始绘图」后录制最近 60 秒 @50Hz；
      停止后画面冻结仍可导出。频率图仅对 M2006（需在机械参数填槽数）。</p>
  </div>
</template>
