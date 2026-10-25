# 字体授权说明

本目录随仓库分发的字体都按 SIL Open Font License 1.1 授权，许可证全文见同目录 `ForaySans-OFL.txt`
（OFL 1.1 正文对下列字体通用）。它们不受仓库根目录 MIT 许可证约束。

| 文件 | 字体 | 版权声明 | 来源 |
| --- | --- | --- | --- |
| `ForaySans-*.woff2`、`foray-sans.css`、`foray-sans-manifest.json` | Foray Sans：Noto Sans SC 可变字体按字符范围分包并改名（OFL 修改版本不得沿用保留字体名） | Copyright 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source' | https://github.com/google/fonts/tree/main/ofl/notosanssc |
| `SpaceGrotesk.edd157.woff2` | Space Grotesk | Copyright 2020 The Space Grotesk Project Authors (https://github.com/floriankarsten/space-grotesk) | https://github.com/floriankarsten/space-grotesk |
| `JetBrainsMono-400.woff2`、`JetBrainsMono-700.woff2` | JetBrains Mono | Copyright 2020 The JetBrains Mono Project Authors (https://github.com/JetBrains/JetBrainsMono) | https://github.com/JetBrains/JetBrainsMono |

## 不入库的字体

`Gilroy-*`、`Novecentosanswide-*`、`HarmonyOS_Sans_SC_*` 已在 `.gitignore` 中排除：

- Gilroy 是付费商业字体，Novecento 的免费授权只允许桌面安装、不允许做网页字体，两者都不能随公开仓库分发；
- HarmonyOS Sans SC 的这几个文件是从第三方网站取得的子集，站点并未引用。

生产服务器与本地工作区仍保留这些文件，`deploy.py` 按工作区打包，线上外观不变。新克隆的仓库里缺这些文件时，
`tokens.css` 的字体栈会自动回退：数字用 Space Grotesk，宽体大写标签用 Foray Sans。
