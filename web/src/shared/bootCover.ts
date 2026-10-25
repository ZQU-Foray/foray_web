/* 开机引导盖：登录成功后的首次进入播放一次（门户与调试台共用同一标记）。
 * 首帧遮底由 head-early.js 写 html[data-boot="pending"]；这里在应用挂载后
 * 换成真正的引导盖并播放，结束后彻底移除。任何异常都不允许困住页面。 */
import markInverse from "./assets/foray-mark-r1-inverse.svg";

const KEY = "foray.post-login-boot";

function build(): HTMLDivElement {
  const box = document.createElement("div");
  box.className = "loading-cover";
  box.id = "loading-cover";
  box.setAttribute("aria-hidden", "true");
  box.innerHTML =
    '<div class="boot-material"></div>' +
    '<div class="boot-contours"><i></i><i></i><i></i></div>' +
    '<div class="boot-index">FORAY PORTAL<br>信息面板</div>' +
    '<div class="boot-loader">' +
      '<div class="boot-loader-track"><i class="boot-loader-fill"></i></div>' +
      '<div class="boot-loader-value"><b class="progress-percent">0%</b></div>' +
    "</div>" +
    '<div class="boot-identity">' +
      '<div class="boot-project-logo"><img alt="FORAY"><span>FORAY ROBOMASTER</span></div>' +
      '<div class="boot-logo-divider"><i></i><span>&times;</span><i></i></div>' +
      '<div class="boot-unit-logo"><strong>战队工作台</strong><span>TEAM OPERATIONS PORTAL</span></div>' +
    "</div>" +
    '<div class="boot-status-copy"><span class="status-text">载入中</span><b>系统资源载入中</b></div>' +
    '<div class="boot-yellow-wipe"></div>';
  const img = box.querySelector("img");
  if (img) img.src = markInverse;
  return box;
}

/** 应用挂载后调用：需要时播放引导动画（否则只撤掉首帧遮底） */
export function playBootIfPending(): void {
  const root = document.documentElement;
  let pending = false;
  try {
    pending = sessionStorage.getItem(KEY) === "1";
    if (pending) sessionStorage.removeItem(KEY);
  } catch { pending = false; }
  const reduced = (typeof window.matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches);
  if (!pending || reduced) {
    root.removeAttribute("data-boot");
    return;
  }
  const cover = build();
  document.body.prepend(cover);
  root.removeAttribute("data-boot");
  const prevOverflow = document.body.style.overflow;
  let done = false;
  const dismiss = () => {
    if (done) return;
    done = true;
    document.body.style.overflow = prevOverflow;
    cover.remove();
  };
  const watchdog = window.setTimeout(dismiss, 6000);
  try {
    const percentEl = cover.querySelector(".progress-percent");
    const statusEl = cover.querySelector(".status-text");
    const phases: Record<string, string> = {
      init: "载入中", loading: "载入中", complete: "就绪", sweeping: "进入", fadeout: "欢迎",
    };
    const setPhase = (p: string) => {
      cover.className = "loading-cover " + p;
      if (statusEl) statusEl.textContent = phases[p];
    };
    document.body.style.overflow = "hidden";
    let display = 0;
    let target = 0;
    let fontsDone = false;
    const t0 = performance.now();
    setPhase("init");
    window.setTimeout(() => setPhase("loading"), 150);
    window.setTimeout(() => { if (target < 90) target = 90; }, 500);
    /* 字体就绪（或 1.2s 兜底）后才收尾，避免揭幕瞬间字形跳变 */
    Promise.race([
      document.fonts ? document.fonts.ready : Promise.resolve(),
      new Promise((res) => window.setTimeout(res, 1200)),
    ]).then(() => { fontsDone = true; });
    let last = performance.now();
    const animate = (now: number) => {
      if (!cover.isConnected) return;
      if (fontsDone && now - t0 >= 750) target = 100;
      const dt = Math.min(50, (now - last) / 16.7);
      last = now;
      if (display < target) {
        const step = Math.max(0.4 * dt, (target - display) * 0.1 * dt);
        display = Math.min(target, display + step);
        cover.style.setProperty("--progress", display + "%");
        if (percentEl) percentEl.textContent = Math.floor(display) + "%";
      }
      if (display < 100) {
        requestAnimationFrame(animate);
      } else {
        setPhase("complete");
        window.setTimeout(() => {
          setPhase("sweeping");
          window.setTimeout(() => {
            setPhase("fadeout");
            window.setTimeout(() => { window.clearTimeout(watchdog); dismiss(); }, 300);
          }, 400);
        }, 320);
      }
    };
    requestAnimationFrame(animate);
  } catch (e) {
    console.error("引导动画异常:", e);
    window.clearTimeout(watchdog);
    dismiss();
  }
}
