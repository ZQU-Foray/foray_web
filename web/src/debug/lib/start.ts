/* 启动编排（对应旧版 static/app.js 末尾「初始化」段 + shoot.js 初始化）：
 * 先注册设备切换处理（顺序与旧版一致：通用页签在前、发射调试在后），再启动探测、
 * 设备列表、日志 WebSocket、状态轮询（3 秒）、固件库与发射调试轮询（1 秒）。
 * 必须在应用挂载之后调用（日志控制台等节点由组件挂载时登记）。 */
import { deviceCtx, startDeviceProbe } from "./device";
import { applyDeviceTarget, refreshStatus } from "./status";
import { clearFlashLog, closeFlashWs, connectFlashWs } from "./flash";
import { clearSerialLog, closeSerialWs, connectSerialWs, serialRxReset } from "./serial";
import { loadPresets, resetPresets } from "./presets";
import { reloadFirmware } from "./firmware";
import { startShoot } from "./shoot";
import { loadMe } from "./identity";

export function startDebugConsole(): void {
  deviceCtx.onChange(() => {
    serialRxReset();
    closeFlashWs();
    closeSerialWs();
    clearFlashLog();
    clearSerialLog();
    resetPresets();
    applyDeviceTarget();
    connectFlashWs();
    connectSerialWs();
    void refreshStatus();
    void loadPresets();
  });
  /* 状态请求最先发出：侧栏「服务在线」指示尽早落定 */
  void refreshStatus();
  startDeviceProbe();
  void deviceCtx.refresh();
  connectFlashWs();
  connectSerialWs();
  void loadPresets();
  void reloadFirmware();
  window.setInterval(() => { void refreshStatus(); }, 3000);
  startShoot();
  void loadMe();
}
