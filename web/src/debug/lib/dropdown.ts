/* 自绘下拉控制器（移植自旧版 static/dropdown.js，交互语义逐项保持一致）。
 * 原生 select 保留为数据源与表单后备（带原 id，测试与表单都读它的 value）；
 * 收起态是 .dd-trigger 按钮，展开态是挂在 body 上的 .dd-panel（Teleport，fixed 定位）。
 *
 * 2026-10-19 线上修复的语义必须保留：
 * - 展开前先落 position:fixed 再显示：面板若先以文档流定位显示，会瞬时撑出纵向滚动条，
 *   滚动条出现派发的 visualViewport resize 会在点击后 2~3ms 把面板关掉（“下拉点不开”）；
 * - resize / visualViewport resize 只在视口尺寸真变化（>1px）时才关闭；
 * - 交互期间（面板展开或触发器聚焦）外部对 select.value 的赋值先暂存、交互结束再落盘，
 *   落盘时暂存值已不在选项里就丢弃（避免触发器标签变空白）；
 * - 点包住 select 的 <label> 文字也能展开面板；面板内滚动不关闭，页面其它滚动才关闭。 */

export interface DdOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface DdElements {
  wrap: HTMLElement;
  select: HTMLSelectElement;
  trigger: HTMLButtonElement;
  label: HTMLElement;
  panel: HTMLElement;
}

export interface DdController {
  /** 按原生 select 当前选项重建面板（选项增删 / 禁用变化后调用） */
  rebuild(): void;
  /** 收起态标签与禁用态跟随原生 select */
  syncLabel(): void;
  /** 交互中（面板展开或触发器聚焦）：此时的外部赋值会被暂存 */
  busy(): boolean;
  /** 卸载：移除全局监听与 value 拦截 */
  destroy(): void;
}

/* 原型上的原生 value 访问器：实例上的拦截器最终都落到它 */
const valueDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value") as PropertyDescriptor;

/** 绕过拦截器直接写原生值（组件内部落盘用） */
export function writeNativeValue(select: HTMLSelectElement, v: string): void {
  (valueDesc.set as (this: HTMLSelectElement, v: string) => void).call(select, v);
}

export function attachDropdown(els: DdElements): DdController {
  const { wrap, select, trigger, label, panel } = els;

  let panelOpen = false;
  let activeIdx = -1;                 /* 键盘高亮索引 */
  let pendingValue: string | undefined; /* 忙时暂存的外部赋值，交互结束后落盘 */
  let pinnedViewport: { w: number; h: number } | null = null;   /* 展开时的视口尺寸，过滤滚动条引起的伪 resize */
  /* <label> 包住 select 时（如“当前设备”），点标签文字原本只会聚焦被隐藏的原生 select、
     不展开自绘面板；记下标签节点以便把点击转成展开。 */
  const labelEl = select.closest("label");

  const opts = () => [...select.options];
  const hasNoOptions = () => select.options.length === 0;
  const optionEls = () => [...panel.children] as HTMLElement[];

  function syncLabel() {
    const opt = select.selectedOptions[0];
    label.textContent = opt ? opt.textContent : (hasNoOptions() ? "（无可选项）" : "");
    trigger.disabled = select.disabled;
    trigger.classList.toggle("is-empty", hasNoOptions());
    const cur = valueDesc.get ? (valueDesc.get as (this: HTMLSelectElement) => string).call(select) : select.value;
    for (const el of optionEls()) {
      el.setAttribute("aria-selected", el.dataset.value === cur ? "true" : "false");
    }
  }

  /* 无选项时放一个占位项：不设 data-value，导航与选择都会跳过它，
     只为让面板有实体高度并说明原因（此前空面板展开只有 2px 的一条线） */
  function rebuild() {
    panel.replaceChildren();
    opts().forEach((opt, i) => {
      const it = document.createElement("div");
      it.className = "dd-option";
      it.id = "dd-opt-" + (select.id || "x") + "-" + i;
      it.setAttribute("role", "option");
      if (opt.disabled) it.setAttribute("aria-disabled", "true");
      it.dataset.value = opt.value;
      it.textContent = opt.textContent;
      panel.appendChild(it);
    });
    if (hasNoOptions()) {
      const hint = document.createElement("div");
      hint.className = "dd-option dd-option-empty";
      hint.setAttribute("role", "option");
      hint.setAttribute("aria-disabled", "true");
      hint.textContent = "（无可选项）";
      panel.appendChild(hint);
    }
    activeIdx = -1;
    syncLabel();
  }

  function setActive(idx: number, scroll: boolean) {
    activeIdx = idx;
    optionEls().forEach((el, i) => {
      el.classList.toggle("dd-active", i === idx);
      if (i === idx) el.setAttribute("aria-selected", "true");
    });
    const target = optionEls()[idx];
    if (scroll && idx >= 0 && target) target.scrollIntoView({ block: "nearest" });
    if (idx >= 0 && target) trigger.setAttribute("aria-activedescendant", target.id);
  }

  function viewportSize() {
    const viewport = window.visualViewport;
    return {
      w: viewport ? viewport.width : document.documentElement.clientWidth,
      h: viewport ? viewport.height : innerHeight,
    };
  }

  function pinFixed() {
    const r = trigger.getBoundingClientRect();
    const viewport = window.visualViewport;
    const left = viewport ? viewport.offsetLeft : 0;
    const top = viewport ? viewport.offsetTop : 0;
    const { w: vw, h: vh } = viewportSize();
    const width = Math.min(Math.max(r.width, 220), vw - 24);
    const below = Math.max(0, top + vh - r.bottom - 12);
    const above = Math.max(0, r.top - top - 12);
    const upwards = below < 180 && above > below;
    panel.style.position = "fixed";
    panel.style.left = Math.max(left + 12, Math.min(r.left, left + vw - width - 12)) + "px";
    panel.style.width = width + "px";
    panel.style.minWidth = "0";
    panel.style.maxHeight = Math.min(280, upwards ? above : below) + "px";
    panel.style.top = (upwards ? r.top - panel.offsetHeight - 4 : r.bottom + 4) + "px";
    pinnedViewport = { w: vw, h: vh };
    window.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("resize", onViewportResize);
    if (viewport) viewport.addEventListener("resize", onViewportResize);
  }

  function onScroll(ev: Event) {
    if (!panel.contains(ev.target as Node)) closePanel();
  }

  /* 视口尺寸真正变化才关闭：滚动条出现/消失（经典滚动条占布局）也会派发 resize，
     但那是瞬时状态，若直接关闭，面板会在点击后 2~3ms 被误杀，表现为“下拉框点不开”。
     只有宽度/高度变化超过 1px 才视为真实转屏/缩放。 */
  function onViewportResize() {
    if (!pinnedViewport) return;
    const { w, h } = viewportSize();
    if (Math.abs(w - pinnedViewport.w) > 1 || Math.abs(h - pinnedViewport.h) > 1) closePanel();
  }

  function unpinFixed() {
    panel.style.position = "";
    panel.style.left = panel.style.top = "";
    panel.style.width = panel.style.minWidth = panel.style.maxHeight = "";
    pinnedViewport = null;
    window.removeEventListener("scroll", onScroll, { capture: true });
    window.removeEventListener("resize", onViewportResize);
    if (window.visualViewport) window.visualViewport.removeEventListener("resize", onViewportResize);
  }

  function openPanel() {
    panelOpen = true;
    /* 必须先落 fixed 再显示：否则展开瞬间撑出滚动条 → visualViewport resize → 面板被关闭
       （“下拉框点不开 / 一闪即关”的根因，带经典滚动条的页面必现） */
    panel.style.position = "fixed";
    panel.hidden = false;
    wrap.classList.add("open");
    pinFixed();
    trigger.setAttribute("aria-expanded", "true");
    /* 初始高亮当前选中项；无选项时不设高亮，避免把占位项标成可选项 */
    const cur = opts().findIndex((o) => o.value === select.value);
    setActive(cur >= 0 ? cur : (hasNoOptions() ? -1 : 0), true);
  }

  function closePanel() {
    panelOpen = false;
    panel.hidden = true;
    wrap.classList.remove("open");
    unpinFixed();
    trigger.setAttribute("aria-expanded", "false");
    trigger.removeAttribute("aria-activedescendant");
    optionEls().forEach((el) => el.classList.remove("dd-active"));
    flushPending();                   /* 交互期间积压的外部赋值此刻落盘 */
  }

  function flushPending() {
    if (pendingValue === undefined) return;
    const v = pendingValue;
    pendingValue = undefined;
    /* 选项可能在暂存期间被重建（如设备列表刷新），暂存值已不存在时丢弃，
       否则会把 select 写成空值、触发器标签变成空白 */
    if (!opts().some((o) => o.value === v)) return;
    writeNativeValue(select, v);
    syncLabel();
  }

  function choose(value: string | undefined) {
    pendingValue = undefined;         /* 用户显式选择优先，丢弃交互期积压 */
    if (value === undefined || value === null) return;   /* 占位项不可选 */
    writeNativeValue(select, value);
    syncLabel();
    select.dispatchEvent(new Event("change", { bubbles: true }));
    closePanel();
    trigger.focus();
  }

  function onTriggerClick() {
    if (select.disabled) return;
    if (panelOpen) closePanel(); else openPanel();
    trigger.focus();
  }

  function onPanelClick(e: MouseEvent) {
    const it = (e.target as HTMLElement).closest(".dd-option") as HTMLElement | null;
    if (it && it.dataset.value !== undefined && it.getAttribute("aria-disabled") !== "true") choose(it.dataset.value);
  }

  /* 键盘：上下移动高亮，回车/空格确认，Esc 关闭 */
  function onTriggerKeydown(e: KeyboardEvent) {
    if (select.disabled) return;
    const n = panel.children.length;
    if (!n || hasNoOptions()) return;   /* 空选择：无项可导航，只有占位说明 */
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!panelOpen) { openPanel(); return; }
      let i = activeIdx + (e.key === "ArrowDown" ? 1 : -1);
      if (i < 0) i = n - 1;
      if (i >= n) i = 0;
      setActive(i, true);
    } else if ((e.key === "Enter" || e.key === " ") && panelOpen) {
      e.preventDefault();
      const it = optionEls()[activeIdx];
      if (activeIdx >= 0 && it && it.getAttribute("aria-disabled") !== "true") choose(it.dataset.value);
    } else if (e.key === "Escape" && panelOpen) {
      e.preventDefault();
      closePanel();
    }
  }

  /* 点击外部关闭（面板挂在 body 上、不在 wrap 内，需单独排除面板自身；
     标签文字属于“本控件”范围，点击它是展开而非关闭） */
  function onDocClick(e: MouseEvent) {
    const t = e.target as Node;
    if (panelOpen && !wrap.contains(t) && !panel.contains(t) && !(labelEl && labelEl.contains(t))) {
      closePanel();
    }
  }

  /* 点 <label> 文字也展开面板（原生 select 已被隐藏，否则表现为“点不动”） */
  function onLabelClick(e: MouseEvent) {
    const t = e.target as Node;
    if (wrap.contains(t) || panel.contains(t)) return;
    if (select.disabled) return;
    e.preventDefault();               /* 阻止 label 默认聚焦隐藏的原生 select */
    if (!panelOpen) openPanel();
    trigger.focus();
  }

  trigger.addEventListener("click", onTriggerClick);
  trigger.addEventListener("keydown", onTriggerKeydown);
  trigger.addEventListener("blur", flushPending);
  panel.addEventListener("click", onPanelClick);
  document.addEventListener("click", onDocClick);
  const labelBound = !!(labelEl && labelEl.control === select);
  if (labelBound) labelEl!.addEventListener("click", onLabelClick);

  /* 原生 select 的选项 / 文字 / 禁用变化 → 重建面板
     （选项由 Vue 维护时文字是原地更新的，所以连 characterData 一起观察） */
  const observer = new MutationObserver(rebuild);
  observer.observe(select, {
    childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: ["disabled", "value", "label"],
  });

  /* 拦截 value 赋值（在本元素上遮蔽原型访问器）：
     交互期间（面板开 / 触发器聚焦）暂存而不落盘，避免遥测回写踩掉用户选择 */
  Object.defineProperty(select, "value", {
    get() { return (valueDesc.get as (this: HTMLSelectElement) => string).call(this); },
    set(v: unknown) {
      const s = v == null ? "" : String(v);
      if (panelOpen || document.activeElement === trigger) {
        pendingValue = s;
        return;
      }
      writeNativeValue(this as HTMLSelectElement, s);
      syncLabel();
    },
    configurable: true,
  });

  rebuild();

  return {
    rebuild,
    syncLabel,
    busy: () => panelOpen || document.activeElement === trigger,
    destroy() {
      if (panelOpen) closePanel();
      observer.disconnect();
      trigger.removeEventListener("click", onTriggerClick);
      trigger.removeEventListener("keydown", onTriggerKeydown);
      trigger.removeEventListener("blur", flushPending);
      panel.removeEventListener("click", onPanelClick);
      document.removeEventListener("click", onDocClick);
      if (labelBound) labelEl!.removeEventListener("click", onLabelClick);
      delete (select as unknown as Record<string, unknown>).value;
    },
  };
}

/** 选项内容签名：用于判断候选是否真的变化（未变化不重建、不改选中值） */
export function optionsSignature(options: DdOption[]): string {
  return options.map((o) => o.value + "\u0001" + o.label + "\u0001" + (o.disabled ? 1 : 0)).join("\u0002");
}

/** 两组选项是否等价（状态轮询据此决定是否替换选项数组，保持引用稳定） */
export function sameOptions(a: DdOption[], b: DdOption[]): boolean {
  return a.length === b.length && optionsSignature(a) === optionsSignature(b);
}
