# 第三方来源与许可证

“爱丽丝的图片助手”整合并修改了以下开源 AstrBot 插件。感谢原作者与后续维护者公开源码。

| 上游项目 | 作者/维护者 | 本次参考版本 | 许可证 | 本项目使用范围 |
|---|---|---|---|---|
| [vmoranv-reborn/astrbot_plugin_pixiv_reborn](https://github.com/vmoranv-reborn/astrbot_plugin_pixiv_reborn) | vmoranv-reborn 及贡献者 | `12423b84142bb5c994ea68bfdd2eaee20d3a2528` | AGPL-3.0 | Pixiv 客户端、插画/小说/用户/Fanbox/订阅/随机搜索处理、过滤和发送工具 |
| [674537331/astrbot_plugin_soutushenqi](https://github.com/674537331/astrbot_plugin_soutushenqi) | RyanVaderAN 及贡献者 | `dd99dfa9166bd5714c9ea04db85136c537a338b2` | GPL-3.0 | 搜图神器抓取、Bing 补充、候选下载/去重/拼图与视觉挑图 |
| [monbed/astrbot_plugin_serpapi_imgsearch](https://github.com/monbed/astrbot_plugin_serpapi_imgsearch) | monbed 及贡献者 | `37d892200add8dda105488022db79632e5b2b7ca` | AGPL-3.0 | 仅文字搜图：SerpApi 多 Key 客户端、Google Images 候选、拼图与 VLM 淘汰赛 |
| [iona-s/astrbot_plugin_imgexploration](https://github.com/iona-s/astrbot_plugin_imgexploration) | FlanChanXwO、iona-s 及贡献者 | `49e79e6bcdf2b790260f08823264718642c4de03` | AGPL-3.0 | 完整以图搜图：SauceNAO、Google Lens、Ascii2d、会话图片上下文、等待/回复图片交互和降级输出 |

本项目整体按 `AGPL-3.0` 发布。GPL-3.0 来源代码依照 GPLv3 第 13 条与 AGPLv3 代码组合，组合后的作品按 AGPL-3.0 提供。完整条款见根目录 [LICENSE](LICENSE)。

主要修改包括：统一插件命名空间与命令/工具标识符；加入两级模块配置和功能级开关；加入自动选源与失败回退；为 Pixiv 增加候选视觉审核；修复配置持久化、未生效的结果上限、阻塞式 Token 刷新、插件加载阶段的同步网络探测、关闭图片上下文后指令等待失效，以及失效的模拟 Agent 上下文。
