# 第三方来源与许可证

“爱丽丝的图片助手”整合并修改了以下开源 AstrBot 插件。感谢原作者与后续维护者公开源码。

| 上游项目 | 作者/维护者 | 本次参考版本 | 许可证 | 本项目使用范围 |
|---|---|---|---|---|
| [vmoranv-reborn/astrbot_plugin_pixiv_reborn](https://github.com/vmoranv-reborn/astrbot_plugin_pixiv_reborn) | vmoranv-reborn 及贡献者 | `12423b84142bb5c994ea68bfdd2eaee20d3a2528` | AGPL-3.0 | Pixiv 客户端、插画/小说/用户/Fanbox/订阅/随机搜索处理、过滤和发送工具 |
| [674537331/astrbot_plugin_soutushenqi](https://github.com/674537331/astrbot_plugin_soutushenqi) | RyanVaderAN 及贡献者 | `dd99dfa9166bd5714c9ea04db85136c537a338b2` | GPL-3.0 | 搜图神器抓取、Bing 补充、候选下载/去重/拼图与视觉挑图 |
| [monbed/astrbot_plugin_serpapi_imgsearch](https://github.com/monbed/astrbot_plugin_serpapi_imgsearch) | monbed 及贡献者 | `37d892200add8dda105488022db79632e5b2b7ca` | AGPL-3.0 | 仅文字搜图：SerpApi 多 Key 客户端、Google Images 候选、拼图与 VLM 淘汰赛 |
| [iona-s/astrbot_plugin_imgexploration](https://github.com/iona-s/astrbot_plugin_imgexploration) | FlanChanXwO、iona-s 及贡献者 | 初始整合 `49e79e6bcdf2b790260f08823264718642c4de03`；v2.0.0 追加参考 `9ba9e3e8e19ca4cb69ae5418f65b118a861e83fa` | AGPL-3.0 | 完整以图搜图；追加借鉴明确选图、静默工具输出、Lens 搜索类型/裁剪/语言/地区、429 换 Key、脏项补位与引擎失败区分 |

v2.0.0 在本插件现有架构中实现独立的模型证据池、候选视觉核对和同轮工具缓存，继续允许模型为图片问题自主检索。没有采用上游要求用户明确提出反搜后才能调用工具的策略。

v2.0.1 继续参考同一版本中的提供商结果/提示分离、失败与零匹配区分、多图 URL 配对、等待图片独占消费、参数分隔符、仅凭据问题换 Key、日志脱敏与本地文件读取处理，并适配至本插件的排序、缓存、Yandex 和 WebUI 流程。具体取舍见 [上游评估](docs/upstream-review-2026-10-06.md)。

Yandex 策略的页面入口、Cookie 可选和 `.com -> .ru` 回退思路参考了
[OMSociety/astrbot_plugin_reverse_searcher](https://github.com/OMSociety/astrbot_plugin_reverse_searcher)。
本项目对 Yandex 的请求、HTML 状态解析、结果模型和安全处理均为独立实现，未复制该项目代码，
也不引入其 `pyquery` 依赖；该项目不作为本插件的运行时依赖。

本项目整体按 `AGPL-3.0` 发布。GPL-3.0 来源代码依照 GPLv3 第 13 条与 AGPLv3 代码组合，组合后的作品按 AGPL-3.0 提供。完整条款见根目录 [LICENSE](LICENSE)。

主要修改包括：统一插件命名空间与命令/工具标识符；加入两级模块配置和功能级开关；加入自动选源与失败回退；为 Pixiv 增加候选视觉审核；修复配置持久化、未生效的结果上限、阻塞式 Token 刷新、插件加载阶段的同步网络探测、关闭图片上下文后指令等待失效，以及失效的模拟 Agent 上下文。
