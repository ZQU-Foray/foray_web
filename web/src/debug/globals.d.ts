/* 调试台挂到 window 上的对象（旧版同名全局，用例与现场排障按它读取当前设备） */
interface Window {
  deviceCtx?: import("./lib/device").DeviceContext;
}
