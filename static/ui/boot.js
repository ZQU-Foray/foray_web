/* 登录后的共享开机动画，仅首个目标页面播放；异常时自动撤下遮罩。 */
(function () {
  "use strict";
function initBootCover() {
  const container = document.createElement('template');
  container.innerHTML = "<div class=\"loading-cover\" id=\"loading-cover\" aria-hidden=\"true\">\n  <div class=\"boot-material\"></div>\n  <div class=\"boot-contours\"><i></i><i></i><i></i></div>\n  <div class=\"boot-index\">FORAY / STARTUP PROCEDURE<br>TEAM OPERATIONS</div>\n  <div class=\"boot-loader\">\n    <div class=\"boot-loader-track\"><i class=\"boot-loader-fill\"></i></div>\n    <div class=\"boot-loader-value\"><b class=\"progress-percent\">0%</b></div>\n  </div>\n  <div class=\"boot-identity\">\n    <div class=\"boot-project-logo\">\n      <img src=\"/ui/foray-robomaster.svg\" alt=\"FORAY\">\n      <span>FORAY ROBOMASTER</span>\n    </div>\n    <div class=\"boot-logo-divider\"><i></i><span>×</span><i></i></div>\n    <div class=\"boot-unit-logo\">\n      <strong>战队工作台</strong>\n      <span>TEAM OPERATIONS PORTAL</span>\n    </div>\n  </div>\n  <div class=\"boot-status-copy\"><span class=\"status-text\">INITIALIZING</span><b>系统资源载入中</b></div>\n  <div class=\"boot-yellow-wipe\"></div>\n</div>";
  const cover = container.content.firstElementChild;
  document.body.prepend(cover);
  // 开机引导仅限登录成功后的首次进入，门户与调试台共用同一标记。
  // 本站登录成功或飞书回调 ?boot=1
  // 时写入 foray.post-login-boot 标记）：门户↔调试台的日常切换改由
  // handoff.js 的快速过场衔接，不再重放整套引导序列。
  // 读取失败（隐私模式等）宁可不放动画，绝不困住页面。
  try {
    if (sessionStorage.getItem('foray.post-login-boot') === '1') {
      sessionStorage.removeItem('foray.post-login-boot');
    } else {
      cover.remove();
      return;
    }
  } catch (e) { cover.remove(); return; }
  // 引导动画任何异常都不允许困住页面：出错即撤盖板
  try {
    // 用户偏好减少动效：直接撤下盖板
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
      cover.remove();
      return;
    }
    const previousOverflow = document.body.style.overflow;
    const dismiss = () => { cover.remove(); document.body.style.overflow = previousOverflow; };
    const watchdog = setTimeout(dismiss, 6000);
    document.body.style.overflow = 'hidden';
    const percentEl = cover.querySelector('.progress-percent');
    const statusEl = cover.querySelector('.status-text');
    const phases = {
      init: 'INITIALIZING', loading: 'LOADING', complete: 'READY',
      sweeping: 'LAUNCHING', fadeout: 'WELCOME',
    };
    const setPhase = (p) => {
      cover.className = 'loading-cover ' + p;
      if (statusEl) statusEl.textContent = phases[p];
    };
    let display = 0;
    let target = 0;
    let fontsDone = false;
    const t0 = performance.now();
    setPhase('init');
    setTimeout(() => setPhase('loading'), 150);
    setTimeout(() => { if (target < 90) target = 90; }, 500);
    // 字体就绪（或 1.2s 兜底超时）后标记可完成
    Promise.race([
      document.fonts ? document.fonts.ready : Promise.resolve(),
      new Promise((res) => setTimeout(res, 1200)),
    ]).then(() => { fontsDone = true; });

    let last = performance.now();
    function animate(now) {
      if (!cover.isConnected) return;
      // 节奏控制：字体就绪且至少经过 750ms 才放行 100%，保证进度可感知
      if (fontsDone && now - t0 >= 750) target = 100;
      // 按帧间隔归一化步长（高刷屏与 60Hz 屏节奏一致）
      const dt = Math.min(50, (now - last) / 16.7);
      last = now;
      if (display < target) {
        // 缓动逼近：步长随差距收敛，且有下限防止停滞
        const step = Math.max(0.4 * dt, (target - display) * 0.10 * dt);
        display = Math.min(target, display + step);
        cover.style.setProperty('--progress', display + '%');
        if (percentEl) percentEl.textContent = Math.floor(display) + '%';
      }
      if (display < 100) {
        requestAnimationFrame(animate);
      } else {
        setPhase('complete');            // READY 停留 320ms 让人看清
        setTimeout(() => {
          setPhase('sweeping');          // 黄色横扫揭幕
          setTimeout(() => {
            setPhase('fadeout');
            setTimeout(() => {
              clearTimeout(watchdog);
              dismiss();                    // 彻底移除，恢复页面滚动
            }, 300);
          }, 400);
        }, 320);
      }
    }
    requestAnimationFrame(animate);
  } catch (e) {
    console.error('引导动画异常:', e);
    cover.remove();
    document.body.style.overflow = '';
  }
}
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initBootCover, {once: true});
  } else { initBootCover(); }
})();
