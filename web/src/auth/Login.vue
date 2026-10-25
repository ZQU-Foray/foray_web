<script setup lang="ts">
/* 登录页：本站账号登录 + 飞书登录 / 开通 / 重设入口；状态与结果从右侧通知框弹出。
 * 不伪造成功：错误原样展示；飞书未配置时按钮禁用并说明。登录页自身不跳转（防 401 重定向环）。 */
import { onMounted, ref } from "vue";
import AuthFrame from "./AuthFrame.vue";
import { sendJSON, errorDetail } from "@/shared/http";
import { toast, type ToastTone } from "@/shared/toast";

const params = new URLSearchParams(location.search);
/* 登录后跳转目标：只接受站内绝对路径（拒绝 // 开头的协议相对地址），缺省回门户首页 */
const rawNext = params.get("next");
const next = rawNext && rawNext.startsWith("/") && !rawNext.startsWith("//") ? rawNext : "/";

/* 跳转带来的说明（复核不可用 / 越权 / 授权失败 / 已重设），进页后逐条弹出 */
const notes: Array<[string, ToastTone]> = [];
if (params.get("recheck") === "1") {
  notes.push(["暂时无法确认成员身份（上游复核不可用，并非退出组织）。此时可登录并进入调试台，管理数据需等复核恢复。", "info"]);
}
if (params.get("forbidden") === "1") {
  notes.push(["当前身份只能使用调试台，不能查看管理数据。", "info"]);
}
if (params.get("oauth_error")) notes.push(["飞书授权失败：" + params.get("oauth_error"), "error"]);
if (params.get("reset") === "1") notes.push(["密码已重设，请用新密码登录。", "ok"]);

const username = ref("");
const password = ref("");
const busy = ref(false);
const done = ref(false);
const feishuHref = ref<string | null>("/auth/feishu/start?next=" + encodeURIComponent(next));
const feishuText = ref("飞书登录");

async function submit() {
  busy.value = true;
  try {
    const res = await sendJSON<{ detail?: string; code?: string }>("POST", "/api/auth/local/login",
      { username: username.value, password: password.value });
    if (res.ok) {
      done.value = true;
      toast("登录成功，正在跳转…", "ok");
      /* 登录后首启：落地页播一次开机引导，并以整屏揭幕入场 */
      try {
        sessionStorage.setItem("foray.post-login-boot", "1");
        sessionStorage.setItem("foray.handoff", "1");
      } catch { /* 隐私模式：跳过动画 */ }
      const reduced = (typeof window.matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches);
      if (reduced || !document.querySelector(".app-handoff-cover")) {
        location.href = next;
        return;
      }
      document.documentElement.setAttribute("data-handoff", "out");
      window.setTimeout(() => { location.href = next; }, 240);
      return;
    }
    toast((res.status === 429 ? "" : "登录失败：") + errorDetail(res), "error");
    if (res.status === 403 && res.body && res.body.code === "onboarding_required") location.href = "/onboarding";
  } catch {
    toast("网络错误，无法连接服务器。", "error");
  } finally {
    busy.value = false;
  }
}

/* 飞书按钮可用性：预检 start 端点，未配置（503）→ 禁用并说明 */
onMounted(async () => {
  notes.forEach(([text, tone]) => toast(text, tone));
  try {
    const r = await fetch("/auth/feishu/start?next=" + encodeURIComponent(next), { redirect: "manual" });
    if (r.status === 503) {
      feishuHref.value = null;
      feishuText.value = "飞书登录（未配置）";
    }
  } catch { /* 网络异常保持默认跳转 */ }
});
</script>

<template>
  <AuthFrame eyebrow="TEAM PORTAL │ ACCESS" watermark="LOGIN">
    <template #title>登录</template>
    <form id="local-form" autocomplete="on" @submit.prevent="submit">
      <label class="field"><span>用户名</span>
        <input id="lo-user" v-model="username" name="username" class="input" required maxlength="32" autocomplete="username" autofocus>
      </label>
      <label class="field"><span>密码</span>
        <input id="lo-pass" v-model="password" name="password" type="password" class="input" required autocomplete="current-password">
      </label>
      <button class="btn block" type="submit" :disabled="busy || done">{{ done ? "正在跳转…" : busy ? "登录中…" : "登录" }}</button>
    </form>
    <div class="login-or" aria-hidden="true">或</div>
    <a id="feishu-btn" class="btn ghost block login-feishu" :href="feishuHref || undefined" :aria-disabled="feishuHref ? undefined : 'true'">
      <span class="login-feishu-mark" aria-hidden="true"></span>{{ feishuText }}
    </a>
    <p class="login-small">忘记密码？<a href="/auth/feishu/start?purpose=reset">通过飞书验证后重设</a></p>
  </AuthFrame>
</template>
