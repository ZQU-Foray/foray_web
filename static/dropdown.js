/* 自绘下拉组件：包裹原生 select，收起态复刻样式、展开态完全风格化。
   原生 select 保留为数据源与表单后备：选项仍由既有代码维护，
   选择后派发原生 change 事件，现有监听逻辑零改动。 */
'use strict';

window.ShootDropdown = (function () {

  /* 拦截原生 value 访问器：外部代码（fillSelect / 遥测同步）赋值时刷新自绘 UI */
  const valueDesc =
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');

  function attach(select) {
    if (select._ddAttached) return;
    select._ddAttached = true;

    /* 包裹层继承原 select 的尺寸类（.sel-model 等），原生 select 转为隐藏 */
    const wrap = document.createElement('span');
    wrap.className = 'dd ' + (select.className || '');
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    select.className = 'dd-native';
    select.setAttribute('aria-hidden', 'true');
    select.tabIndex = -1;

    /* 触发按钮 */
    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'dd-trigger';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    const label = document.createElement('span');
    label.className = 'dd-label';
    trigger.appendChild(label);

    /* 展开面板 */
    const panel = document.createElement('div');
    panel.className = 'dd-panel';
    panel.setAttribute('role', 'listbox');
    panel.hidden = true;

    wrap.appendChild(trigger);
    /* 面板挂到 body，而不是 wrap 内：祖先一旦有 transform（卡片入场动画、折叠容器
       都会产生），position:fixed 会退化为"相对该祖先"定位，按视口算出的 left/top
       随即错位，再叠加祖先的 overflow:hidden 就被裁掉——实测电机栏里面板右偏
       300px、下偏 317px、高度只剩 2px。挂到 body 后 fixed 才真正相对视口。 */
    document.body.appendChild(panel);

    let panelOpen = false;
    let activeIdx = -1;             /* 键盘高亮索引 */
    let pendingValue;               /* 忙时暂存的外部赋值，交互结束后落盘 */
    let pinnedViewport = null;      /* 展开时的视口尺寸，用于过滤滚动条引起的伪 resize */
    /* <label> 包住 select 时（如“当前设备”），点标签文字原本只会聚焦被隐藏的
       原生 select、不展开自绘面板；记下标签节点以便把点击转成展开。 */
    const labelEl = select.closest('label');

    const opts = () => [...select.options];

    /* 面板挂在 body 上（见上），宿主 select 若被重建移除，旧面板会留在 body 里；
       它们虽然 hidden 不可见，但会在反复重建的容器下累积，故展开时顺手清理。 */
    panel.__ddSelect = select;
    function pruneOrphans() {
      document.querySelectorAll('body > .dd-panel').forEach((p) => {
        if (!p.__ddSelect || !p.__ddSelect.isConnected) p.remove();
      });
    }

    /* 无选项时的占位项：不设 dataset.value，导航与 choose 都会跳过它，
       只为让面板有实体高度并说明原因（此前空面板展开只有 2px 的一条线） */
    function hasNoOptions() { return select.options.length === 0; }

    function syncLabel() {
      const opt = select.selectedOptions[0];
      label.textContent = opt ? opt.textContent : (hasNoOptions() ? '（无可选项）' : '');
      trigger.disabled = select.disabled;
      trigger.classList.toggle('is-empty', hasNoOptions());
      for (const el of panel.children) {
        const sel = el.dataset.value === select.value;
        el.setAttribute('aria-selected', sel ? 'true' : 'false');
      }
    }

    function rebuild() {
      panel.innerHTML = '';
      for (const opt of opts()) {
        const it = document.createElement('div');
        it.className = 'dd-option';
        it.setAttribute('role', 'option');
        it.dataset.value = opt.value;
        it.textContent = opt.textContent;
        panel.appendChild(it);
      }
      if (hasNoOptions()) {
        const hint = document.createElement('div');
        hint.className = 'dd-option dd-option-empty';
        hint.setAttribute('role', 'option');
        hint.setAttribute('aria-disabled', 'true');
        hint.textContent = '（无可选项）';
        panel.appendChild(hint);
      }
      activeIdx = -1;
      syncLabel();
    }

    function setActive(idx, scroll) {
      activeIdx = idx;
      [...panel.children].forEach((el, i) => {
        el.classList.toggle('dd-active', i === idx);
        if (i === idx) el.setAttribute('aria-selected', 'true');
      });
      if (scroll && idx >= 0 && panel.children[idx])
        panel.children[idx].scrollIntoView({ block: 'nearest' });
      if (idx >= 0) trigger.setAttribute('aria-activedescendant',
                                         'dd-opt-' + select.id + '-' + idx);
    }

    function pinFixed() {
      const r = trigger.getBoundingClientRect();
      const viewport = window.visualViewport;
      const left = viewport ? viewport.offsetLeft : 0;
      const top = viewport ? viewport.offsetTop : 0;
      const vw = viewport ? viewport.width : document.documentElement.clientWidth;
      const vh = viewport ? viewport.height : innerHeight;
      const width = Math.min(Math.max(r.width, 220), vw - 24);
      const below = Math.max(0, top + vh - r.bottom - 12);
      const above = Math.max(0, r.top - top - 12);
      const upwards = below < 180 && above > below;
      panel.style.position = 'fixed';
      panel.style.left = Math.max(left + 12, Math.min(r.left, left + vw - width - 12)) + 'px';
      panel.style.width = width + 'px';
      panel.style.minWidth = '0';
      panel.style.maxHeight = Math.min(280, upwards ? above : below) + 'px';
      panel.style.top = (upwards ? r.top - panel.offsetHeight - 4 : r.bottom + 4) + 'px';
      pinnedViewport = {w: vw, h: vh};
      window.addEventListener('scroll', onScroll, { capture: true, passive: true });
      window.addEventListener('resize', onViewportResize);
      if (viewport) viewport.addEventListener('resize', onViewportResize);
    }

    function onScroll(ev) {
      if (!panel.contains(ev.target)) closePanel();
    }

    /* 视口尺寸真正变化才关闭：滚动条出现/消失（经典滚动条占布局）也会派发
       resize，但那是瞬时状态，若直接关闭，面板会在点击后 2~3ms 被误杀，
       表现为“下拉框点不开”。只有宽度/高度变化超过 1px 才视为真实转屏/缩放。 */
    function onViewportResize() {
      const viewport = window.visualViewport;
      const w = viewport ? viewport.width : document.documentElement.clientWidth;
      const h = viewport ? viewport.height : innerHeight;
      if (!pinnedViewport) return;
      if (Math.abs(w - pinnedViewport.w) > 1 || Math.abs(h - pinnedViewport.h) > 1) {
        closePanel();
      }
    }

    function unpinFixed() {
      panel.style.position = '';
      panel.style.left = panel.style.top = '';
      panel.style.width = panel.style.minWidth = panel.style.maxHeight = '';
      pinnedViewport = null;
      window.removeEventListener('scroll', onScroll, { capture: true });
      window.removeEventListener('resize', onViewportResize);
      if (window.visualViewport) window.visualViewport.removeEventListener('resize', onViewportResize);
    }

    function openPanel() {
      pruneOrphans();               /* 清理宿主 select 已被移除的残留面板 */
      panel.dataset.for = select.id || '';   /* 面板在 body 上，标明归属便于定位 */
      panelOpen = true;
      /* 必须先落 fixed 再显示：面板基态是 position:absolute; top:100%，又挂在
         body 下（包含块=视口），一旦先显示就会瞬时把文档撑高、弹出纵向滚动条，
         而滚动条出现/消失会派发 visualViewport resize，面板随即被关闭——
         这就是“下拉框点不开/一闪即关”的根因（带经典滚动条的页面必现）。 */
      panel.style.position = 'fixed';
      panel.hidden = false;
      wrap.classList.add('open');
      pinFixed();
      trigger.setAttribute('aria-expanded', 'true');
      /* 初始高亮当前选中项；无选项时不设高亮，避免把占位项标成可选项 */
      const cur = opts().findIndex(o => o.value === select.value);
      setActive(cur >= 0 ? cur : (hasNoOptions() ? -1 : 0), true);
    }

    function closePanel() {
      panelOpen = false;
      panel.hidden = true;
      wrap.classList.remove('open');
      unpinFixed();
      trigger.setAttribute('aria-expanded', 'false');
      trigger.removeAttribute('aria-activedescendant');
      [...panel.children].forEach(el => el.classList.remove('dd-active'));
      flushPending();               /* 交互期间积压的遥测回写此刻落盘 */
    }

    function flushPending() {
      if (pendingValue === undefined) return;
      const v = pendingValue;
      pendingValue = undefined;
      /* 选项可能在暂存期间被重建（如设备列表刷新），暂存值已不存在时丢弃，
         否则会把 select 写成空值、触发器标签变成空白 */
      if (!opts().some(o => o.value === v)) return;
      valueDesc.set.call(select, v);
      syncLabel();
    }

    function choose(value) {
      pendingValue = undefined;     /* 用户显式选择优先，丢弃交互期积压 */
      if (value === undefined || value === null) return;   /* 占位项不可选 */
      valueDesc.set.call(select, value);
      syncLabel();
      select.dispatchEvent(new Event('change', { bubbles: true }));
      closePanel();
      trigger.focus();
    }

    trigger.addEventListener('click', () => {
      if (select.disabled) return;
      panelOpen ? closePanel() : openPanel();
      trigger.focus();
    });

    panel.addEventListener('click', (e) => {
      const it = e.target.closest('.dd-option');
      if (it && it.dataset.value !== undefined) choose(it.dataset.value);
    });

    /* 键盘：上下移动高亮，回车/空格确认，Esc 关闭 */
    trigger.addEventListener('keydown', (e) => {
      if (select.disabled) return;
      const n = panel.children.length;
      if (!n || hasNoOptions()) return;    /* 空选择：无项可导航，只有占位说明 */
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!panelOpen) { openPanel(); return; }
        let i = activeIdx + (e.key === 'ArrowDown' ? 1 : -1);
        if (i < 0) i = n - 1;
        if (i >= n) i = 0;
        setActive(i, true);
      } else if ((e.key === 'Enter' || e.key === ' ') && panelOpen) {
        e.preventDefault();
        if (activeIdx >= 0) choose(panel.children[activeIdx].dataset.value);
      } else if (e.key === 'Escape' && panelOpen) {
        e.preventDefault();
        closePanel();
      }
    });

    /* 点击外部关闭（面板已挂到 body，不在 wrap 内，需单独排除面板自身；
       标签文字属于“本控件”范围，点击它是展开而非关闭） */
    document.addEventListener('click', (e) => {
      if (panelOpen && !wrap.contains(e.target) && !panel.contains(e.target)
          && !(labelEl && labelEl.contains(e.target))) {
        closePanel();
      }
    });

    /* 点 <label> 文字也展开面板（原生 select 已被隐藏，否则表现为“点不动”） */
    if (labelEl && labelEl.control === select) {
      labelEl.addEventListener('click', (e) => {
        if (wrap.contains(e.target) || panel.contains(e.target)) return;
        if (select.disabled) return;
        e.preventDefault();          /* 阻止 label 默认聚焦隐藏的原生 select */
        if (!panelOpen) openPanel();
        trigger.focus();
      });
    }

    /* 原生 select 选项/禁用变化 → 重建面板 */
    new MutationObserver(rebuild)
      .observe(select, { childList: true, attributes: true,
                         attributeFilter: ['disabled'] });

    /* 拦截 value 赋值（在本元素上遮蔽原型访问器）：
       交互期间（面板开/触发器聚焦）暂存而不落盘，避免遥测回写踩掉用户选择 */
    Object.defineProperty(select, 'value', {
      get() { return valueDesc.get.call(this); },
      set(v) {
        if (panelOpen || document.activeElement === trigger) {
          pendingValue = v;
          return;
        }
        valueDesc.set.call(this, v);
        syncLabel();
      },
      configurable: true,
    });

    trigger.addEventListener('blur', flushPending);

    rebuild();
  }

  function attachAll(root) {
    (root || document).querySelectorAll('select').forEach(attach);
  }

  /* 静态 select 就绪后自动挂载 */
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', () => attachAll());
  else
    attachAll();

  return { attach, attachAll };
})();
