<script setup lang="ts">
/* 登录类页面的公共框架：装饰画布 + 左上品牌 + 居中卡片（标题插槽 + 内容插槽）+ 右侧通知框 */
import mark from "@/shared/assets/foray-mark-r1.svg";
import ToastStack from "@/shared/components/ToastStack.vue";

defineProps<{ eyebrow: string; watermark: string }>();
</script>

<template>
  <div class="login-canvas" aria-hidden="true"></div>
  <div class="login-page">
    <div class="signal-bar" aria-hidden="true"></div>
    <a class="login-brand" href="/login" aria-label="FORAY RoboMaster">
      <img class="login-brand-mark" :src="mark" alt="FORAY">
      <span class="login-brand-text"><b>FORAY</b><small>TEAM PORTAL</small></span>
    </a>
    <main class="login-stage">
      <section class="login-box" aria-labelledby="login-title">
        <header class="login-box-head">
          <span class="wm" aria-hidden="true">{{ watermark }}</span>
          <span class="eyebrow">{{ eyebrow }}</span>
          <h1 id="login-title"><slot name="title" /></h1>
        </header>
        <div class="login-body"><slot /></div>
      </section>
    </main>
  </div>
  <ToastStack />
</template>
