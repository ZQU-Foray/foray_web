<script setup lang="ts">
/* 开通页：首次飞书登录后设置本站用户名与密码（只有本组织成员可经飞书开通，没有公开注册） */
import { onMounted, ref } from "vue";
import AuthFrame from "./AuthFrame.vue";
import { errorDetail, sendJSON } from "@/shared/http";
import { toast } from "@/shared/toast";

const who = ref("—");
const user = ref("");
const pass = ref("");
const pass2 = ref("");
const busy = ref(false);
const done = ref(false);

async function submit() {
  busy.value = true;
  try {
    const res = await sendJSON("POST", "/api/auth/account/onboard",
      { username: user.value.trim(), password: pass.value, password2: pass2.value });
    if (res.ok) {
      done.value = true;
      toast("设置完成，正在进入门户…", "ok");
      location.href = "/";
      return;
    }
    toast("设置失败：" + errorDetail(res), "error");
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
  <AuthFrame eyebrow="ACCOUNT │ ONBOARD" watermark="SETUP">
    <template #title>设置<span class="hl">本站账号</span></template>
    <p class="login-small">飞书身份已核验 · 设置用户名与密码后即可使用 · <span id="who">{{ who }}</span></p>
    <p class="note">本站账号用于登录门户与调试台；断网时也能用它登录调试。用户名 3–32 个字符（中文/字母/数字/下划线/连字符），密码至少 8 位。</p>
    <form id="ob-form" autocomplete="off" @submit.prevent="submit">
      <label class="field"><span>用户名</span>
        <input id="ob-user" v-model="user" name="username" class="input" required maxlength="32" autocomplete="username" autofocus>
      </label>
      <label class="field"><span>密码</span>
        <input id="ob-pass" v-model="pass" name="password" type="password" class="input" required autocomplete="new-password">
      </label>
      <label class="field"><span>确认密码</span>
        <input id="ob-pass2" v-model="pass2" name="password2" type="password" class="input" required autocomplete="new-password">
      </label>
      <button class="btn block" type="submit" :disabled="busy || done">{{ done ? "正在跳转…" : busy ? "提交中…" : "完成设置 →" }}</button>
    </form>
    <p class="login-small">只有本组织成员可通过飞书开通，没有公开注册。退出组织后本站账号会被删除。</p>
  </AuthFrame>
</template>
