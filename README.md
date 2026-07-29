# 爱丽丝的图片助手

为 AstrBot 提供一套统一的图片工作流：Bot 可以按用户意图自行精确找图、从候选中挑图、在来源失败时自动换源，也可以对用户发送的图片查找出处。完整保留 Pixiv、文字搜图和多引擎以图搜图能力，但所有公开命令、LLM 工具和数据目录均使用新的 `爱图` / `alice_image` 命名，不会与四个上游插件冲突。

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-5b8def)
![License](https://img.shields.io/badge/license-AGPL--3.0-blue)

## 功能

| 模块 | 能力 |
|---|---|
| 找图 | Pixiv 插画、小说、画师、排行榜、Fanbox、订阅、随机推送；搜图神器主图源加 Bing 补充；SerpApi Google Images 文字搜图 |
| 精确挑图 | Bot 可指定 `pixiv`、`soutu`、`serpapi`，或选 `auto`；支持候选拼图交给视觉模型审核，来源失败按顺序回退 |
| 以图搜图 | SauceNAO、Google Lens、Ascii2d；支持附图、回复图片、先发指令后补图、会话图片上下文和 LLM 自主选择图片 |
| 可控性 | 两个模块、所有搜索源、每个 Pixiv 功能组、每个反搜引擎、指令、LLM 工具、审核、回退、上传和图片上下文都有独立开关 |

## 安装

在 AstrBot 插件市场安装，或把仓库放到 `data/plugins/astrbot_plugin_alice_image_assistant` 后重载插件：

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

第二行是搜图神器来源所需的浏览器本体。若不使用该来源，可以在“找图模块 -> 搜图神器来源”关闭它，不必安装 Chromium。

插件要求 AstrBot `>=4.16,<5`，建议使用支持 Function Calling 的模型；启用视觉审核时，所选模型还必须支持图片输入。

## 快速开始

1. 打开插件配置，按需要开启“找图模块”和“以图搜图模块”。
2. 配置至少一个来源的凭据：Pixiv Refresh Token、SerpApi Key、SauceNAO Key 或 Ascii2d Cookie。
3. 使用 `/爱图` 查看当前启用状态与常用命令。

```text
/爱图找 富士山 日出
/爱图P 初音ミク,冬
/爱图溯 google
```

`/爱图溯` 可和图片同发、回复一条图片使用，或先发指令后在限定时间内补发图片。

## 指令

### 通用找图与反搜

| 指令 | 用途 |
|---|---|
| `/爱图` | 状态与速查帮助 |
| `/爱图找 <关键词>` | 自动选择来源找图，失败时按配置回退 |
| `/爱图神 <关键词>` | 仅优先使用搜图神器来源 |
| `/爱图S <关键词>` | 仅优先使用 SerpApi Google Images |
| `/爱图溯 [saucenao,google,ascii2d]` | 以图搜图；可附图、回复图或随后补图 |

### Pixiv

所有 Pixiv 原命令均改为独立短命令，避免与原插件的 `/pixiv*` 冲突。

| 指令 | 用途 |
|---|---|
| `/爱图P <标签>` | 标签搜索插画 |
| `/爱图P新 [类型] [最大作品ID]` | 最新插画 |
| `/爱图P荐` | 推荐插画 |
| `/爱图P并 <标签>` | AND 多标签搜索 |
| `/爱图PID <作品ID>` | 作品详情，含 Ugoira GIF |
| `/爱图P榜 [模式] [日期]` | 排行榜 |
| `/爱图P似 <作品ID>` | 相关作品 |
| `/爱图P深 <标签>` | 深度搜索 |
| `/爱图P评 <作品ID> [偏移]` | 插画评论 |
| `/爱图P辑 <特辑ID>` | 特辑详情 |
| `/爱图P热 <标签> [范围] [页数]` | 按收藏热度搜索 |
| `/爱图P趋势` | 趋势标签 |
| `/爱图PAI <true/false>` | 会话内 AI 作品显示设置 |
| `/爱图P画师 <关键词>` | 搜索画师 |
| `/爱图P画师详 <用户ID>` | 画师详情 |
| `/爱图P画师作 <用户ID>` | 画师作品 |
| `/爱图P画师找 <画师名或用户ID> \| <关键词>` | 指定画师找图；关键词可省略 |
| `/爱图P文 <标签>` | 搜索小说 |
| `/爱图P文荐` / `/爱图P文新` | 推荐 / 最新小说 |
| `/爱图P文系 <系列ID>` | 小说系列 |
| `/爱图P文评 <小说ID> [偏移]` | 小说评论 |
| `/爱图P文下 <小说ID>` | 下载小说 PDF |
| `/爱图P订 <画师ID>` / `/爱图P退 <画师ID>` / `/爱图P订阅` | 画师订阅管理 |
| `/爱图P随加 <标签>` / `/爱图P随删 <序号>` / `/爱图P随列` | 随机标签搜索管理 |
| `/爱图P随停` / `/爱图P随开` / `/爱图P随态` / `/爱图P随跑` | 随机搜索控制 |
| `/爱图P随榜加 <模式> [日期]` / `/爱图P随榜删 <序号>` / `/爱图P随榜列` | 随机排行榜管理 |
| `/爱图F主 <创作者> [数量]` | Fanbox 创作者和帖子 |
| `/爱图F帖 <帖子ID或链接>` | Fanbox 帖子详情 |
| `/爱图F荐 [数量]` | 推荐 Fanbox 创作者 |
| `/爱图F找 <关键词> [数量]` | 搜索 Fanbox 画师 |
| `/爱图P设置 show` | 查看 Pixiv 运行时设置 |
| `/爱图P设置 <键> <值>` | 修改允许的 Pixiv 运行时设置 |
| `/爱图P帮助` | Pixiv 指令与设置帮助 |

## LLM 工具

在相应开关开启时，插件注册以下唯一工具名：

| 工具 | 用途 |
|---|---|
| `alice_image_find` | 文字找图。模型可指定 `auto`、`pixiv`、`soutu` 或 `serpapi`；如需锁定 Pixiv 画师，可填写 `artist_name` 或 `pixiv_user_id`。 |
| `alice_image_pixiv_novel` | 搜索或下载 Pixiv 小说。 |
| `alice_image_list_session_images` | 列出当前会话可用于反搜的图片和稳定 `image_id`。 |
| `alice_image_reverse_search` | 按 `image_id` 或索引以图搜图。 |

找图工具会自行发送图片，并向模型返回结构化结果。`auto` 会对二次元、插画、日文标签等优先尝试 Pixiv，普通实体优先尝试搜图神器；是否继续回退由配置决定。模型不需要也不应该虚构图片链接。

## 配置说明

配置页只有两个顶层分组。

### 找图模块

- `enabled`、`commands_enabled`、`llm_tools_enabled`：分别控制整个模块、所有指令和模型工具。
- `llm_search_progress_message_enabled`：控制 LLM 自主找图前是否发送“正在为你寻找...”这类前置提示；默认关闭，只保留最终图片或失败结果。
- `tool_send_wait_timeout_seconds`：LLM 工具等待平台发图确认的最长秒数；默认 45，设为 `0` 表示一直等待。
- `auto_source_enabled`、`default_source`、`fallback_enabled`、`fallback_order`：控制自动选源与回退策略。
- `llm_review`：控制视觉审核、审核模型、失败时放行或触发回退、候选数量；`fail_open` 对 Pixiv、搜图神器和 SerpApi 三个来源统一生效。
- `pixiv`：有模块总开关、三十五个功能开关和完整 Pixiv 参数。需要填写 `refresh_token` 才能使用 Pixiv API；Fanbox 受限内容可另填 Cookie。
- `soutu`：可分别关主图源、Bing 补充和视觉挑图。关闭 `enabled` 后不会启动 Playwright 抓取。
- `serpapi`：仅包含文字搜图；填写 `serpapi_keys` 后可轮询多个 Key，并可独立关闭 `vlm_selection_enabled` 视觉淘汰赛。

### 以图搜图模块

- `enabled`、`commands_enabled`、`llm_tools_enabled`：分别控制模块、`/爱图溯` 和模型工具；`inject_tool_guidance_enabled` 可单独关闭模型提示注入。
- `ai_behavior.capture_image_context`：控制是否保存用户发图供 Bot 主动反搜；关闭后不会保留会话图片。
- `strategies`：可独立启用 SauceNAO、Google Lens、Ascii2d。
- `network.allow_image_upload`：本地或平台临时图片无法直接给外部引擎时，是否上传到 Catbox 获取公开 URL；隐私敏感场景请关闭。
- `network.allow_local_file_access`：默认关闭。保持关闭可避免模型利用路径读取并上传服务器本地文件。
- `display.max_results`：每个反搜引擎的最大返回数；本版本已实际应用该限制。

SerpApi Key 可只在任意一个模块填写一次。若另一个模块的 Key 列表为空，运行时会复用已填写的一侧。

## 凭据获取

不需要一次性填完所有凭据：只启用某个来源时，才需要配置对应的 Key、Token 或 Cookie。下面步骤参考了 [astrbot_plugin_imgexploration](https://github.com/iona-s/astrbot_plugin_imgexploration) 的说明，并结合本插件的配置项整理。

### Pixiv Refresh Token

用途：Pixiv 插画、小说、排行榜、推荐、画师、订阅和 Pixiv 精确找图。

填写位置：`find_image.pixiv.settings.refresh_token`

获取方法：

1. 登录自己的 Pixiv 账号。
2. 按 [pixivpy3](https://pypi.org/project/pixivpy3/) 文档或常用的 Pixiv OAuth 获取工具，在本机登录并换取 `refresh_token`。
3. 把得到的 `refresh_token` 填入插件配置，然后重载插件。

注意：请填写 `refresh_token`，不是短期 `access_token`。如果日志提示 `invalid_grant` 或鉴权失败，通常需要重新获取。

### SerpApi API Key

用途：SerpApi Google Images 文字搜图，以及 Google Lens 以图搜图。

填写位置：

- `find_image.serpapi.serpapi_keys`
- `reverse_image.api_keys.serpapi_keys`

获取方法：

1. 打开 [SerpApi](https://serpapi.com/) 并注册或登录账号。
2. 进入账号 Dashboard / API Key 页面。
3. 复制 API Key，填入上面的 `serpapi_keys` 列表；多个 Key 可以分多项填写，插件会轮询使用。

提示：两个模块会互相复用 SerpApi Key。如果只在找图模块或以图搜图模块填了一处，另一处为空时插件会自动复用已填写的 Key。

### SauceNAO API Key

用途：二次元图片、Pixiv、Danbooru 等来源的以图搜图。

填写位置：`reverse_image.api_keys.saucenao_api_key`

获取方法：

1. 打开 [SauceNAO API 页面](https://saucenao.com/user.php?page=search-api)。
2. 注册或登录 SauceNAO 账号。
3. 复制页面中的 API Key，填入插件配置。

提示：免费额度和调用限制可能调整，请以 SauceNAO 页面显示为准。

### Ascii2d Cookie

用途：Ascii2d 以图搜图。

填写位置：

- `reverse_image.api_keys.ascii2d_session_id`
- `reverse_image.api_keys.ascii2d_cf_clearance`

获取方法：

1. 用 Chrome 或 Edge 打开 [Ascii2d](https://ascii2d.net/)。
2. 上传任意图片并完成一次搜索；如果出现 Cloudflare 验证，先正常通过验证。
3. 按 `F12` 打开开发者工具，进入 `Application` -> `Cookies` -> `https://ascii2d.net`。
4. 找到 `_session_id`，复制它的 `Value`，填入 `ascii2d_session_id`。
5. 如果页面里有 `cf_clearance`，也复制它的 `Value`，填入 `ascii2d_cf_clearance`。

提示：Cookie 会过期。遇到 Ascii2d 403、验证失败或长期无结果时，重新按上面步骤获取即可。

### Fanbox Cookie

用途：访问需要登录态或受限的 Fanbox 帖子内容。普通 Pixiv 插画搜索不需要它。

填写位置：`find_image.pixiv.settings.fanbox_cookie`

获取方法：

1. 用浏览器登录 [pixivFANBOX](https://www.fanbox.cc/)。
2. 打开开发者工具，进入 `Application` -> `Cookies`，选择 `fanbox.cc` 或具体创作者的 `*.fanbox.cc` 域。
3. 复制完整 Cookie 字符串，建议至少包含 `FANBOXSESSID`；如果有 Cloudflare 验证，连同 `cf_clearance` 一起保留。
4. 填入 `fanbox_cookie`，并尽量让 `fanbox_user_agent` 与获取 Cookie 时的浏览器一致。

请把这些凭据当作账号密码处理，不要发到群聊、公开 Issue、截图或提交记录里。

## 隐私与内容提示

- 以图搜图会将图片 URL 发送给对应搜索服务。启用图片上传时，本地或临时图片还会上传至 Catbox。
- Pixiv、Fanbox、搜索引擎和图床均有自己的服务条款与内容规则；请确保使用场景符合当地法律和平台条款。
- 默认 Pixiv 配置过滤 R18。不要把 Bot 配置为向不适合的群组或未成年人发送成人内容。
- 视觉审核只做候选匹配，不保证图片的版权、来源或事实描述；Bot 回复应保留不确定性。
- 关闭会话图片记录不会影响 `/爱图溯` 附图、回复图片或“先发指令后补图”的指令流程。

## 常见问题

| 现象 | 处理方式 |
|---|---|
| 搜图神器没有结果 | 执行 `python -m playwright install chromium`，检查网络；也可启用 Bing 补充或开启 SerpApi 回退。 |
| Pixiv 认证失败 | 检查 `refresh_token`、代理和反代设置；不要把 Token 贴到群聊。 |
| 视觉审核回退到首图 | 当前模型不支持图片、审核超时或候选下载失败。可换视觉模型，或关闭 `fail_open` 让插件改用下一个来源。 |
| Ascii2d 403 | 重新获取 Cookie，必要时使用代理。 |
| 无法反搜本地图片 | 在隐私风险可接受时开启图片上传；需要读取服务器路径时还必须单独开启本地文件访问。 |

## 上游致谢

本项目基于并感谢以下上游插件：

- [vmoranv-reborn/astrbot_plugin_pixiv_reborn](https://github.com/vmoranv-reborn/astrbot_plugin_pixiv_reborn)
- [674537331/astrbot_plugin_soutushenqi](https://github.com/674537331/astrbot_plugin_soutushenqi)
- [monbed/astrbot_plugin_serpapi_imgsearch](https://github.com/monbed/astrbot_plugin_serpapi_imgsearch)
- [iona-s/astrbot_plugin_imgexploration](https://github.com/iona-s/astrbot_plugin_imgexploration)

精确引用的上游 commit、修改范围和许可证兼容说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 许可证

本项目按 [AGPL-3.0](LICENSE) 发布。使用、分发或部署修改版时，请遵守该许可证及所有上游项目的许可证义务。
