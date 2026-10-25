<script setup lang="ts">
/* 单个电机卡（槽位 s）：折叠头（编号 / 故障标签 / 使能徽标）+ 型号 / 模式 / 目标滑杆 /
 * 操作按钮 / 预设 / 控制参数（PID）/ 机械参数 / 遥测数值。
 * DOM 契约沿用旧版（.motor-card[data-slot]、button[data-act]、#model{s} #mode{s} #preset{s}、
 * #tslider{s} #tval{s} #kp_speed{s}… #faults{s} #headen{s} #en{s} #tr{s}…）。
 * 输入框不绑定 value（非受控，逻辑层按 id 读写）；遥测数值节点内容由 lib/shoot.ts 命令式写入。 */
import DdSelect from "./DdSelect.vue";
import {
  PARAMS, shootAct, shootSelect, shootUi, sliderChange, sliderInput, sliderReset, tvalChange,
} from "../lib/shoot";

const props = defineProps<{ s: number }>();
const ui = shootUi.slots[props.s];

function valueOf(e: Event): string {
  return (e.target as HTMLInputElement).value;
}
</script>

<template>
  <div class="motor-card mcollapse" :class="{ open: ui.open }" :data-slot="s">
    <button class="mcol-head" type="button" data-act="mcol" :aria-expanded="ui.open ? 'true' : 'false'" @click="ui.open = !ui.open">
      <span class="mtag">M-{{ s + 1 }}</span>
      <span class="mcol-title">Motor {{ s + 1 }}</span>
      <span :id="'faults' + s" class="fault-tags" :data-offline="ui.offline ? '1' : '0'"><span v-for="(f, i) in ui.faults" :key="i" :class="f.cls">{{ f.text }}</span></span>
      <span :id="'headen' + s" class="badge mcol-en" :class="ui.enRunning ? 'ok' : 'neutral'" title="使能状态">{{ ui.enRunning ? "使能中" : "失能" }}</span>
      <span class="chev" aria-hidden="true">▾</span>
    </button>
    <div class="mcol-body"><div class="mcol-inner">
      <div class="mrow">
        <span class="mgroup"><label>型号</label>
          <DdSelect :id="'model' + s" wrap-class="sel-model" :aria-label="'Motor ' + (s + 1) + ' 型号'" :options="shootUi.modelOpts" @change="(v: string) => shootSelect(s, 'model', v)" /></span>
        <span class="mgroup"><label>模式</label>
          <DdSelect :id="'mode' + s" wrap-class="sel-mode" :aria-label="'Motor ' + (s + 1) + ' 模式'" :options="shootUi.modeOpts" @change="(v: string) => shootSelect(s, 'mode', v)" /></span>
      </div>
      <div v-show="ui.tgtShow" :id="'tgtrow' + s" class="mrow tgt-row">
        <label :id="'tgtlabel' + s">{{ ui.tgtLabel }}</label>
        <span class="mgroup tgt-ctl">
          <span class="tgt-wrap">
            <input
              :id="'tslider' + s" type="range" class="tgt-slider" step="1" title="拖动实时下发 · 双击回零"
              @input="sliderInput(s, valueOf($event))" @change="sliderChange(s, valueOf($event))" @dblclick="sliderReset(s)"
            >
            <span class="tgt-lims">
              <span :id="'tgtmin' + s" class="tgt-lim">{{ ui.tgtMin }}</span>
              <span :id="'tgtmax' + s" class="tgt-lim">{{ ui.tgtMax }}</span>
            </span>
          </span>
          <input :id="'tval' + s" type="number" class="tgt-val" step="any" :aria-label="'Motor ' + (s + 1) + ' 目标值'" @change="tvalChange(s)">
          <span :id="'tgtunit' + s" class="tgt-unit">{{ ui.tgtUnit }}</span>
          <button type="button" data-act="reset" class="btn ghost xs tgt-reset" title="目标复位为 0 并下发" @click="shootAct(s, 'reset')">复位 0</button>
        </span>
      </div>
      <div class="mrow mrow-acts">
        <button type="button" data-act="apply" class="btn sec sm" @click="shootAct(s, 'apply')">应用全部</button>
        <button :id="'en' + s" type="button" data-act="enable" class="btn sm" :class="ui.enRunning ? 'enable' : 'ghost'" @click="shootAct(s, 'enable')">{{ ui.enRunning ? "失能电机" : "使能电机" }}</button>
        <button type="button" data-act="faultclear" class="btn ghost sm" @click="shootAct(s, 'faultclear')">清故障</button>
      </div>
      <div class="mrow">
        <label>预设</label>
        <span class="mgroup">
          <DdSelect :id="'preset' + s" wrap-class="sel-preset" :aria-label="'Motor ' + (s + 1) + ' 预设'" :options="ui.presetOpts" @change="(v: string) => shootSelect(s, 'preset', v)" />
          <button type="button" data-act="psave" class="btn ghost xs" title="把当前表单参数保存为预设（存于 Web 服务器）" @click="shootAct(s, 'psave')">保存</button>
          <button type="button" data-act="pdel" class="btn ghost xs" title="删除所选预设" @click="shootAct(s, 'pdel')">删除</button>
          <button type="button" data-act="pdef" class="btn ghost xs" title="把所选预设设为默认（连接/换型号时自动应用）" @click="shootAct(s, 'pdef')">默认</button>
        </span>
      </div>
      <div class="subcol" :class="{ open: ui.ctrlOpen }">
        <button class="subcol-head" type="button" data-act="subcol" :aria-expanded="ui.ctrlOpen ? 'true' : 'false'" @click="ui.ctrlOpen = !ui.ctrlOpen">
          控制参数<span class="hint inline">（PID {{ PARAMS.length }} 项）</span>
          <span class="chev" aria-hidden="true">▸</span>
        </button>
        <div class="subcol-body"><div class="subcol-inner">
          <div v-for="[k, n] in PARAMS" :key="k" class="mrow">
            <label :for="k + s">{{ n }}</label>
            <input :id="k + s" type="number" step="any">
            <button type="button" data-act="param" class="btn ghost xs" :data-key="k" :data-src="k + s" :aria-label="'下发 ' + n" @click="shootAct(s, 'param', k, k + s)">→</button>
          </div>
        </div></div>
      </div>
      <div class="subcol" :class="{ open: ui.mechOpen }">
        <button class="subcol-head" type="button" data-act="subcol" :aria-expanded="ui.mechOpen ? 'true' : 'false'" @click="ui.mechOpen = !ui.mechOpen">
          机械参数<span class="chev" aria-hidden="true">▸</span>
        </button>
        <div class="subcol-body"><div class="subcol-inner">
          <div class="mrow">
            <span class="mgroup"><label :for="'gear' + s">减速比</label>
              <input :id="'gear' + s" type="number" step="0.1" min="1">
              <button type="button" data-act="param" class="btn ghost xs" data-key="gear_ratio" :data-src="'gear' + s" aria-label="下发减速比" @click="shootAct(s, 'param', 'gear_ratio', 'gear' + s)">→</button></span>
          </div>
          <div v-show="ui.is2006" :id="'poleRow' + s" class="mrow">
            <span class="mgroup"><label :for="'pole' + s">槽数</label>
              <input :id="'pole' + s" type="number" step="1" min="1" value="8" title="电机绕组槽数，用于计算电气频率"></span>
          </div>
          <div v-show="ui.is2006" :id="'freqRow' + s" class="mrow">
            <label>射击频率</label>
            <b :id="'freq' + s" class="freq"></b>
            <span class="hint inline">= 转速 × 槽数 / 60</span>
          </div>
        </div></div>
      </div>
      <div class="telem">
        <div><span>转速</span><b :id="'tr' + s"></b></div>
        <div><span>角度</span><b :id="'td' + s"></b></div>
        <div><span>电流</span><b :id="'tc' + s"></b></div>
        <div><span>力矩</span><b :id="'tq' + s"></b></div>
        <div><span>温度</span><b :id="'tt' + s"></b></div>
        <div><span>在线</span><b :id="'to' + s"></b></div>
        <div><span>使能</span><b :id="'ten' + s"></b></div>
      </div>
    </div></div>
  </div>
</template>
