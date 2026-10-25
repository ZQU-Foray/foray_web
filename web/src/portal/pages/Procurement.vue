<script setup lang="ts">
/* 物资采购：组别芯片 + 赛季 / 一二级分类 / 开票 / 关键词筛选；统计为当前筛选口径。
 * 「总价」是飞书公式列（单价×数量），门户只读；全员可新建与修改，「组长批复」仅组长可改。 */
import { computed, ref, watch } from "vue";
import PortalPage from "../components/PortalPage.vue";
import SyncMark from "../components/SyncMark.vue";
import { navNo } from "../nav";
import ChipFilter, { type ChipOption } from "../components/ChipFilter.vue";
import SrcLink from "../components/SrcLink.vue";
import ProcFlag from "../components/ProcFlag.vue";
import AttachLink from "../components/AttachLink.vue";
import { RouterLink } from "@/shared/router";
import { fetchProcurement } from "../api";
import { groups, openForm, store } from "../store";
import { usePageData } from "../usePageData";
import { procNewPreset, procOptionList } from "../forms";
import { fmtBeijingDate, fmtMoney, fmtNum } from "@/shared/format";
import type { Procurement, UserRef } from "../types";

const f = store.procFilters;
const { body } = usePageData(() => fetchProcurement(f), [() => ({ ...f })]);
const rows = computed<Procurement[]>(() => (body.value && body.value.data) || []);
const stats = computed(() => (body.value && body.value.stats) || {});
watch(body, (b) => { if (b && b.options) store.procOptions = b.options; });

const groupOpts = computed<ChipOption[]>(() =>
  [{ id: "全部", label: "全部" }].concat(groups().map((g) => ({ id: g, label: g }))));

/* 筛选下拉：首项「全部」；当前值已不在候选里也保留，否则筛选会静默失效 */
function filterOptions(key: "season" | "category1" | "category2"): Array<{ v: string; label: string }> {
  const optKey = key === "season" ? "seasons" : key;
  const list = procOptionList(optKey, key === "category2" ? f.category1 : null);
  const cur = f[key];
  const out = [{ v: "全部", label: "全部" }].concat(list.map((x) => ({ v: x, label: x })));
  if (cur && cur !== "全部" && list.indexOf(cur) < 0) out.push({ v: cur, label: cur + "（已从飞书选项移除）" });
  return out;
}
/* 一级分类变了：二级分类候选跟着收窄并复位 */
watch(() => f.category1, () => { f.category2 = "全部"; });

const q = ref(f.q);
function submitSearch() { f.q = q.value.trim(); }
function reset() {
  f.group = "全部"; f.season = "全部"; f.category1 = "全部"; f.category2 = "全部"; f.invoice = "全部";
  f.q = ""; q.value = "";
}

function people(l?: UserRef[]): string {
  return (l || []).map((u) => u.name || "…" + String(u.id || "").slice(-4)).join("、") || "—";
}
</script>

<template>
  <PortalPage :no="navNo('/procurement')" en="PROCUREMENT" title="物资采购" desc="采购申请 │ 批复与采购进度 │ 发票" watermark="PROCUREMENT">
    <template v-if="store.me" #actions>
      <button type="button" class="btn" @click="openForm('procurement', procNewPreset(f.season))">＋ 新建采购申请</button>
    </template>

    <div class="filters">
      <ChipFilter id="group-filter" v-model="f.group" label="组别" aria-label="按申请组别筛选" :options="groupOpts" />
    </div>
    <div id="proc-filters" class="filter-row" role="group" aria-label="按赛季与分类筛选">
      <label class="field"><span>赛季</span>
        <select id="proc-f-season" v-model="f.season" class="select">
          <option v-for="o in filterOptions('season')" :key="o.v" :value="o.v">{{ o.label }}</option>
        </select>
      </label>
      <label class="field"><span>一级分类</span>
        <select id="proc-f-category1" v-model="f.category1" class="select">
          <option v-for="o in filterOptions('category1')" :key="o.v" :value="o.v">{{ o.label }}</option>
        </select>
      </label>
      <label class="field"><span>二级分类</span>
        <select id="proc-f-category2" v-model="f.category2" class="select">
          <option v-for="o in filterOptions('category2')" :key="o.v" :value="o.v">{{ o.label }}</option>
        </select>
      </label>
      <label class="field"><span>开票</span>
        <select id="proc-f-invoice" v-model="f.invoice" class="select">
          <option value="全部">全部</option>
          <option value="undone">未开票</option>
          <option value="done">已开票</option>
        </select>
      </label>
      <label class="field grow"><span>搜索</span>
        <input id="proc-f-q" v-model="q" class="input" type="search" placeholder="物资名称 / 采购编号"
          style="height: 36px" @keydown.enter="submitSearch" @search="submitSearch">
      </label>
      <button type="button" class="btn ghost sm" @click="reset">重置</button>
    </div>

    <div id="proc-stats" class="stat-line">
      <span>共 <b>{{ stats.count || 0 }}</b> 条</span>
      <span>合计 <b>{{ fmtMoney(stats.amount) }}</b></span>
      <span class="row-meta">当前筛选口径；「总价」是飞书公式列＝单价×数量，门户只读</span>
    </div>

    <section id="proc-list-card" class="module enter">
      <div class="tbl-wrap in-module">
        <table class="tbl cards-sm">
          <thead>
            <tr>
              <th>物资</th><th>采购编号</th><th>分类</th><th>申请组别</th><th>数量</th>
              <th>单价（元）</th><th>总价（元）</th><th>采购日期</th><th>采购人</th>
              <th>发票</th><th>组长批复</th><th>已采购</th><th>源</th>
            </tr>
          </thead>
          <tbody id="proc-body">
            <tr v-for="p in rows" :key="p.id">
              <td data-label="物资"><span><RouterLink :to="'/procurement/' + encodeURIComponent(p.id)" class="lnk"><b>{{ p.name }}{{ p.invalid ? "（数据异常）" : "" }}</b></RouterLink><SyncMark :mark="p.sync" /></span></td>
              <td data-label="采购编号"><span class="clip t-num">{{ p.code || "—" }}</span></td>
              <td data-label="分类"><span class="clip" :title="(p.category1 || '未分类') + (p.category2 ? ' / ' + p.category2 : '')">{{ p.category1 || "未分类" }}{{ p.category2 ? " / " + p.category2 : "" }}</span></td>
              <td data-label="申请组别">{{ (p.groups || []).join("、") || "—" }}</td>
              <td data-label="数量" class="num">{{ fmtNum(p.quantity) }}</td>
              <td data-label="单价" class="num">{{ fmtMoney(p.unit_price) }}</td>
              <td data-label="总价" class="num">{{ fmtMoney(p.total_price) }}</td>
              <td data-label="采购日期" class="nowrap t-num">{{ fmtBeijingDate(p.purchase_date && p.purchase_date.ts) }}</td>
              <td data-label="采购人">{{ people(p.purchaser) }}</td>
              <td data-label="发票" class="nowrap">
                <ProcFlag :item="p" field="invoice_done" on="已开" off="未开" />
                <template v-if="(p.invoice_files || []).length"> <AttachLink :list="p.invoice_files" /></template>
              </td>
              <td data-label="组长批复"><ProcFlag :item="p" field="leader_approval" on="已批复" off="未批复" /></td>
              <td data-label="已采购"><ProcFlag :item="p" field="purchased" on="已采购" off="未采购" /></td>
              <td data-label="源"><SrcLink :url="p.url" /></td>
            </tr>
            <tr v-if="body && !rows.length">
              <td colspan="13" class="is-empty"><p class="empty">暂无采购记录（源表为空或被筛选）</p></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </PortalPage>
</template>
