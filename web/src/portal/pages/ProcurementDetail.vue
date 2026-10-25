<script setup lang="ts">
/* 采购详情：全部字段 + 字段更新卡（已采购 / 发票已开全员可切，组长批复仅组长；附件上传全员可传） */
import { computed, ref } from "vue";
import PortalPage from "../components/PortalPage.vue";
import SyncMark from "../components/SyncMark.vue";
import { navNo } from "../nav";
import SrcLink from "../components/SrcLink.vue";
import ProcFlag from "../components/ProcFlag.vue";
import AttachLink from "../components/AttachLink.vue";
import DeleteButton from "../components/DeleteButton.vue";
import { RouterLink, useRoute } from "@/shared/router";
import { fetchProcurementItem, uploadProcAttachment } from "../api";
import { afterWrite, isLeader, openForm, requestRefresh, store } from "../store";
import { usePageData } from "../usePageData";
import { procPreset } from "../forms";
import { fmtBeijingDate, fmtMoney } from "@/shared/format";
import { errorDetail } from "@/shared/http";
import type { Procurement, UserRef } from "../types";
import { toast } from "@/shared/toast";

const route = useRoute();
const id = route.value.params.id;
const { body, notFound } = usePageData(() => fetchProcurementItem(id));
const p = computed<Procurement | null>(() => (body.value && body.value.data) || null);
const tableLink = computed(() => (store.meta && store.meta.source_links && store.meta.source_links.procurement) || null);

function people(l?: UserRef[]): string {
  return (l || []).map((u) => u.name || "…" + String(u.id || "").slice(-4)).join("、") || "—";
}

/* ---- 附件上传：多选时逐份顺序上传；服务端并入该列既有附件后整列写回 ---- */
const uploads = {
  image: { input: ref<HTMLInputElement | null>(null), text: ref("上传"), busy: ref(false) },
  invoice: { input: ref<HTMLInputElement | null>(null), text: ref("上传"), busy: ref(false) },
};
const imageInput = uploads.image.input;
const invoiceInput = uploads.invoice.input;

async function doUpload(col: "image" | "invoice") {
  const u = uploads[col];
  const files = Array.from((u.input.value && u.input.value.files) || []);
  if (!files.length) { toast("请先选择要上传的文件", "error"); return; }
  if (!p.value) return;
  u.busy.value = true;
  try {
    let done = 0;
    for (let i = 0; i < files.length; i++) {
      u.text.value = "上传中 " + (i + 1) + "/" + files.length + "…";
      const res = await uploadProcAttachment(p.value.id, col, files[i]);
      if (!res.ok) {
        toast("上传失败：" + errorDetail(res) + (done ? "（前 " + done + " 份已上传）" : ""), "error");
        if (done) requestRefresh();
        return;
      }
      done++;
    }
    if (u.input.value) u.input.value.value = "";
    afterWrite(null, "已上传 " + done + " 份并附加到该记录");
  } catch {
    toast("网络错误，未上传", "error");
  } finally {
    u.busy.value = false;
    u.text.value = "上传";
  }
}

function editAll() {
  if (p.value) openForm("procurement", procPreset(p.value), p.value.id);
}
</script>

<template>
  <div id="proc-detail">
    <PortalPage :no="navNo('/procurement')" en="PROCUREMENT │ DETAIL" :title="p ? p.name : (notFound ? '记录不存在' : '采购详情')" watermark="PROCUREMENT">
      <template #actions>
        <RouterLink to="/procurement" class="btn ghost sm">← 返回物资采购</RouterLink>
      </template>
      <template v-if="p">
        <SyncMark :mark="p.sync" block />
        <p v-if="p.invalid" class="note">※ 该记录源数据存在质量异常（如缺物资名称），已排除出统计。</p>
        <div class="stack">
          <section class="module enter">
            <div class="module-head">
              <span class="badge ink">{{ (p.groups || []).join("/") || "采购" }}</span>
              <h2>{{ p.name }}</h2>
              <SrcLink v-if="p.url" :url="p.url" label="在飞书中打开该记录 ↗" />
              <SrcLink v-else :url="tableLink" label="在飞书中打开该表 ↗" />
            </div>
            <dl class="kv">
              <dt>赛季</dt><dd>{{ p.season || "—" }}</dd>
              <dt>采购编号</dt><dd class="t-num">{{ p.code || "—" }}</dd>
              <dt>一级分类</dt><dd>{{ p.category1 || "—" }}</dd>
              <dt>二级分类</dt><dd>{{ p.category2 || "—" }}</dd>
              <dt>申请组别</dt><dd>{{ (p.groups || []).join("、") || "—" }}</dd>
              <dt>数量</dt><dd class="t-num">{{ p.quantity == null ? "—" : p.quantity }}</dd>
              <dt>单价（元）</dt><dd class="t-num">{{ fmtMoney(p.unit_price) }}</dd>
              <dt>总价（元）</dt><dd class="t-num">{{ fmtMoney(p.total_price) }}（飞书公式＝单价×数量）</dd>
              <dt>采购日期</dt><dd class="t-num">{{ fmtBeijingDate(p.purchase_date && p.purchase_date.ts) }}</dd>
              <dt>采购人</dt><dd>{{ people(p.purchaser) }}</dd>
              <dt>购物链接</dt>
              <dd><a v-if="p.link" class="lnk" :href="p.link" target="_blank" rel="noopener" :title="p.link">打开链接 ↗</a><template v-else>—</template></dd>
              <dt>用途说明</dt><dd class="prose">{{ p.purpose || "—" }}</dd>
              <dt>图片补充</dt><dd><AttachLink :list="p.attachments" empty="无（可在下方上传）" /></dd>
              <dt>发票已开</dt><dd>{{ p.invoice_done ? "已开" : "未开" }}</dd>
              <dt>发票文件</dt><dd><AttachLink :list="p.invoice_files" empty="无（可在下方上传）" /></dd>
              <dt>组长批复</dt><dd>{{ p.leader_approval ? "已批复" : "未批复" }}</dd>
              <dt>已采购</dt><dd>{{ p.purchased ? "已采购" : "未采购" }}</dd>
            </dl>
          </section>

          <section class="module enter">
            <div class="module-head"><span class="module-tag">UPDATE / 02</span><h2>字段更新</h2></div>
            <div class="module-body">
              <div class="edit-row">
                <span class="edit-label">已采购</span><ProcFlag :item="p" field="purchased" on="已采购" off="未采购" />
                <span class="edit-label" style="margin-left: 12px">发票已开</span><ProcFlag :item="p" field="invoice_done" on="已开" off="未开" />
              </div>
              <div class="edit-row">
                <span class="edit-label">上传图片</span>
                <input ref="imageInput" class="file-input" type="file" accept="image/*" multiple aria-label="选择要上传的图片">
                <button type="button" class="btn sm" :disabled="uploads.image.busy.value" @click="doUpload('image')">{{ uploads.image.text.value }}</button>
                <span class="hint">全员可传 · 图片 · ≤10MB/张</span>
              </div>
              <div class="edit-row">
                <span class="edit-label">上传发票</span>
                <input ref="invoiceInput" class="file-input" type="file" accept="image/*,.pdf" multiple aria-label="选择要上传的发票">
                <button type="button" class="btn sm" :disabled="uploads.invoice.busy.value" @click="doUpload('invoice')">{{ uploads.invoice.text.value }}</button>
                <span class="hint">全员可传 · PDF/图片 · ≤20MB/份</span>
              </div>
              <div v-if="isLeader()" class="edit-row">
                <span class="edit-label">组长批复</span><ProcFlag :item="p" field="leader_approval" on="已批复" off="未批复" />
                <span class="hint">仅组长可改；其余字段全员可编辑</span>
              </div>
              <p v-else class="note inline">「组长批复」由组长勾选；其它字段全员可修改。</p>
              <div class="edit-actions">
                <button type="button" class="btn sm" @click="editAll">编辑全部字段</button>
                <span class="spacer"></span>
                <DeleteButton v-if="isLeader()" :id="p.id" table="procurement" label="采购记录" />
              </div>
            </div>
          </section>
        </div>
      </template>
    </PortalPage>
  </div>
</template>
