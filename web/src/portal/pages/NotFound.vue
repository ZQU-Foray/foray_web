<script setup lang="ts">
/* 404 兜底页：地址没有匹配的路由时渲染（原来是整片空白，用户以为页面挂了）。
 * 只列回得去的入口，不做自动跳转——手误的地址直接跳走反而让人不知道自己点了什么。 */
import PortalPage from "../components/PortalPage.vue";
import { NAV } from "../routes";
import { RouterLink, useRoute } from "@/shared/router";

const route = useRoute();
</script>

<template>
  <PortalPage no="—" en="NOT FOUND" title="页面不存在" watermark="404">
    <p class="note">
      ※ 地址 <code>{{ route.path }}</code> 没有对应的页面 —— 可能是链接打错，或这个页面已经不在了。
    </p>
    <p class="t-small">
      可以从下面这些地方回去：
      <template v-for="sec in NAV" :key="sec.label">
        <template v-for="it in sec.items" :key="it.href">
          <RouterLink :to="it.href" class="lnk">{{ it.name }}</RouterLink>
        </template>
      </template>
    </p>
    <p><RouterLink to="/" class="btn ghost xs">← 回到总览</RouterLink></p>
  </PortalPage>
</template>
