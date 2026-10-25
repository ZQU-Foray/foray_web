<script setup lang="ts">
/* 04 设置：串口指令预设（JSON，按当前设备）+ 固件管理（全局固件库：上传 / 删除）。
 * 固件名是外部字符串，一律按文字渲染。 */
import { computed, ref } from "vue";
import { deviceCtx } from "../lib/device";
import { station } from "../lib/status";
import { presets, savePresets } from "../lib/presets";
import { deleteFirmware, firmware, fwSize, uploadFirmware } from "../lib/firmware";

defineProps<{ active: boolean }>();

const file = ref<HTMLInputElement | null>(null);
const dev = computed(() => deviceCtx.current);

async function onUpload() {
  const el = file.value;
  if (await uploadFirmware(el && el.files ? el.files[0] : null) && el) el.value = "";
}
function mtimeText(t: number): string {
  return new Date(t * 1000).toLocaleString();
}
</script>

<template>
  <section id="tab-settings" class="panel" :class="{ active }" aria-labelledby="tabbtn-settings">
    <div class="settings-grid">
      <div class="card">
        <h2 class="lbl"><span class="tag">PRESETS / 01</span>预设指令 / 串口参数（JSON）</h2>
        <p class="hint">commands[].newline: crlf | lf | cr | none；encoding: text | hex。修改后点保存。</p>
        <textarea id="presets-json" v-model="presets.text" class="textarea code" rows="14" spellcheck="false" aria-label="预设指令 JSON"></textarea>
        <div class="row">
          <button id="presets-save" type="button" class="btn sm" :disabled="!dev" @click="savePresets">保存配置</button>
        </div>
      </div>

      <div class="card">
        <h2 class="lbl"><span class="tag">FIRMWARE / 02</span>固件管理</h2>
        <div class="row wrap">
          <input id="fw-file" ref="file" type="file" class="file-input" accept=".elf,.hex,.bin" aria-label="选择固件文件">
          <button id="fw-upload" type="button" class="btn sm" @click="onUpload">上传固件</button>
        </div>
        <div id="fw-list" class="list">
          <template v-if="firmware.items.length">
            <div v-for="f in firmware.items" :key="f.name" class="item">
              <span class="name">{{ f.name }}</span>
              <span class="size">{{ fwSize(f) }} · {{ mtimeText(f.mtime) }}</span>
              <button type="button" class="del btn ghost xs" :data-name="f.name" @click="deleteFirmware(f.name)">删除</button>
            </div>
          </template>
          <template v-else-if="firmware.loaded">（暂无固件，可在此上传）</template>
        </div>
        <p class="hint">固件目录: <span id="fw-dir">{{ station.fwDir }}</span>（也可直接用 scp 等工具放入服务器目录）</p>
      </div>
    </div>
  </section>
</template>
