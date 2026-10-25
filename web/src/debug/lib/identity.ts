/* 侧栏身份与退出：首帧用服务端注入的 window.__FORAY_BOOT__.who（不闪「读取中」），
 * 随后以 /api/auth/me 校准；退出后回登录页。文案与门户一致：姓名（成员 / 应急·仅调试）。 */
import { reactive } from "vue";
import { dfetch } from "./api";

interface Who { display: string; role: string }

const boot = window.__FORAY_BOOT__ || {};

export const identity = reactive({
  who: (boot.who || null) as Who | null,
  loaded: false,
});

export function whoText(): string {
  const w = identity.who;
  if (!w) return identity.loaded ? "未登录" : "";
  const suffix = "（" + (w.role === "member" ? "成员" : "应急·仅调试") + "）";
  /* 应急会话的 display 在服务端登录时已写成「用户名（应急·仅调试）」，不再重复追加 */
  return w.display.endsWith(suffix) ? w.display : w.display + suffix;
}

export function avatarText(): string {
  const w = identity.who;
  return w && w.display ? w.display.slice(0, 1) : "?";
}

export async function loadMe(): Promise<void> {
  try {
    const r = await dfetch("/api/auth/me");
    if (r.ok) {
      const j = await r.json() as Partial<Who>;
      if (j.display) identity.who = { display: j.display, role: String(j.role || "") };
    }
  } catch { /* 网络异常：保留首帧身份 */ }
  identity.loaded = true;
}

export async function logout(): Promise<void> {
  try { await dfetch("/api/auth/logout", { method: "POST" }); } catch { /* 无论成败都回登录页 */ }
  location.href = "/login";
}
