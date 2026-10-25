<script setup lang="ts">
/* 重设密码页：须先经飞书再验证，改密窗口默认 10 分钟；重设后其他设备上的旧会话失效 */
import { onMounted, ref } from "vue";
import AuthFrame from "./AuthFrame.vue";
import { errorDetail, sendJSON } from "@/shared/http";
import { toast } from "@/shared/toast";

const who = ref("—");
const pass = ref("");
const pass2 = ref("");
const busy = ref(false);
const done = ref(false);

async function submit() {
  busy.value = true;
  try {
    const res = await sendJSON("POST", "/api/auth/account/reset-password",
      { password: pass.value, password2: pass2.value });
    if (res.ok) {
      done.value = true;
      toast("密码已重设，正在返回登录页…", "ok");
      location.href = "/login?reset=1";
      return;
    }
    toast("重设失败：" + errorDetail(res), "error");
  } catch {
    toast("网络错误，无法连接服务器。", "error");
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  try {
    const r = await fetch("/api/auth/me", { credentials: "same-origin" });
    if (r.ok) who.value = (await r.json()).display || "—";
  } catch { /* 身份未知时显示占位 */ }
});
</script>

<template>
  <AuthFrame eyebrow="ACCOUNT │ RESET" watermark="RESET">
    <template #title>重设<span class="hl">密码</span></template>
    <p class="login-small">飞书已重新验证 · 请在 10 分钟内设置新密码 · <span id="who">{{ who }}</span></p>
    <p class="note">重设后其他设备上的旧会话会失效。断网无法通过飞书验证时，请联系维护者在服务器本地重置。</p>
    <form id="rs-form" autocomplete="off" @submit.prevent="submit">
      <label class="field"><span>新密码</span>
        <input id="rs-pass" v-model="pass" name="password" type="password" class="input" required autocomplete="new-password" autofocus>
      </label>
      <label class="field"><span>确认新密码</span>
        <input id="rs-pass2" v-model="pass2" name="password2" type="password" class="input" required autocomplete="new-password">
      </label>
      <button class="btn block" type="submit" :disabled="busy || done">{{ done ? "正在跳转…" : busy ? "提交中…" : "重设密码 →" }}</button>
    </form>
  </AuthFrame>
</template>
