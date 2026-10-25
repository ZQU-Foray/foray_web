/* 静态资源导入的类型声明（Vite 把它们解析成带哈希的 URL） */
declare module "*.svg" {
  const url: string;
  export default url;
}
declare module "*.png" {
  const url: string;
  export default url;
}
