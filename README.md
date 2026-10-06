# 爱丽丝的图片助手

为 AstrBot 提供一套统一的图片工作流：Bot 可以按用户意图自行精确找图、从候选中挑图、在来源失败时自动换源，也可以对用户发送的图片查找出处。完整保留 Pixiv、文字搜图和多引擎以图搜图能力，公开命令使用直白的中文名称，LLM 工具和数据目录继续使用稳定的 `alice_image` 命名。

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-5b8def)
![License](https://img.shields.io/badge/license-AGPL--3.0-blue)

## 功能

| 模块 | 能力 |
|---|---|
| 找图 | Pixiv 插画、小说、画师、排行榜、Fanbox、订阅、随机推送；搜图神器主图源加 Bing 补充；SerpApi Google Images 文字搜图 |
| 精确挑图 | Bot 可指定 `pixiv`、`soutu`、`serpapi`，或选 `auto`；支持候选拼图交给视觉模型审核，来源失败按顺序回退 |
| 以图搜图 | SauceNAO、Google Lens、Ascii2d、Yandex；支持附图、回复图片、先发指令后补图、会话图片上下文和 LLM 自主选择图片 |
| 可控性 | 两个模块、所有搜索源、每个 Pixiv 功能组、每个反搜引擎、指令、LLM 工具、审核、回退、上传和图片上下文都有独立开关 |

## 安装

在 AstrBot 插件市场安装，或把仓库放到 `data/plugins/astrbot_plugin_alice_image_assistant` 后重载插件：

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

第二行是搜图神器来源所需的浏览器本体。若不使用该来源，可以在“找图模块 -> 搜图神器来源”关闭它，不必安装 Chromium。

插件要求 AstrBot `>=4.16,<5`，建议使用支持 Function Calling 的模型；启用视觉审核时，所选模型还必须支持图片输入。

### 更新到 v2.0.1

在 AstrBot 的插件管理中更新“爱丽丝的图片助手”，然后重载插件或重启 AstrBot。也可从本仓库的 [v2.0.1 Release](https://github.com/Whereis-Alice/astrbot_plugin_alice_image_assistant/releases/tag/v2.0.1) 获取源码包。

从 v2.0.0 起全部 48 条公开指令改为中文，旧 `aa` / `aaP` / `aaF` 命令不再注册；更新后发送 `/图片帮助`，Pixiv 详细用法见 `/插画帮助`。凭据、订阅和运行数据继续沿用；人格、快捷回复中的旧命令需要同步替换。自主识图升级后默认不发结果卡片，详见下方“LLM 工具”。完整变化见 [CHANGELOG.md](CHANGELOG.md)。

## 快速开始

1. 打开插件配置，按需要开启“找图模块”和“以图搜图模块”。
2. 按需配置来源凭据：Pixiv Refresh Token、SerpApi Key、SauceNAO Key 或 Ascii2d Cookie；Yandex 不需要 API Key，直接开启即可尝试。
3. 使用 `/图片帮助` 查看当前启用状态与常用命令。

```text
/找图 富士山 日出
/插画 星之卡比 1
/识图 google
```

`/识图` 可和图片同发、回复一条图片使用，或先发指令后在限定时间内补发图片。
多个引擎可用空格、逗号、顿号或分号分隔，例如 `/识图 google yandex`、`/识图 google，saucenao`。

## 指令

所有命令已重置为中文名称，旧前缀命令不再注册。`<...>` 是必填参数，`[...]` 是可选参数。

开启作品链接自动解析后，直接发送 Pixiv 作品链接即可取图。`/识图` 支持附图、回复图或随后补图，可指定 `google`、`saucenao`、`ascii2d`、`yandex`，或填写“出处”“相似图”等意图。

### 核心

| 指令 | 用途 |
|---|---|
| `/图片帮助` | 查看插件状态、当前找图来源与识图引擎。 |
| `/找图 <关键词>` | 自动选源找图：按关键词判断走 Pixiv / 搜图 / SerpApi。 |
| `/搜图 <关键词>` | 优先使用搜图神器来源，结合 Bing 补充和视觉挑图；按配置回退。 |
| `/谷歌搜图 <关键词>` | 优先使用 SerpApi Google 图片搜索；按配置回退。 |
| `/识图 [引擎/意图]` | 以图搜图：可指定引擎，也可写‘出处’‘相似图’等意图；附图、回复图片或随后补图均可。 |

### Pixiv 插画

| 指令 | 用途 |
|---|---|
| `/插画 <标签> [数量]` | 按标签搜索插画，支持逗号分隔与排除标签。 |
| `/插画并搜 <标签1,标签2>` | 多标签 AND 搜索，要求同时命中全部标签。 |
| `/深度插画 <标签>` | 跨多页搜索插画，匹配任一指定标签。 |
| `/热门插画 <标签> [时间范围] [页数]` | 热度搜索：在时间窗内按收藏数排序。 |
| `/最新插画 [类型] [最大作品ID]` | 获取最新作品；类型为 illust（插画）或 manga（漫画）。 |
| `/推荐插画` | Pixiv 为你推荐的插画。 |
| `/作品详情 <作品ID>` | 按作品 ID 直接取图。 |
| `/相关插画 <作品ID>` | 查看与该作品相关的推荐。 |
| `/插画榜 [模式] [日期]` | 排行榜：day / week / month / day_r18 等。 |
| `/插画评论 <作品ID> [偏移量]` | 查看作品评论。 |
| `/插画特辑 <特辑ID>` | 查看 Pixiv 特辑（showcase）内容。 |
| `/趋势标签` | 查看当前热门标签趋势。 |

### Pixiv 画师

| 指令 | 用途 |
|---|---|
| `/画师 <画师名>` | 按名字搜索画师。 |
| `/画师详情 <画师ID>` | 查看画师资料与统计。 |
| `/画师作品 <画师ID或名字> [数量]` | 取该画师的作品列表。 |
| `/随机插画 <画师ID或名字> [数量]` | 从该画师作品里随机抽图。 |
| `/画师找图 <画师名或ID> [\| 关键词] [数量]` | 先锁定画师，再在其作品内按关键词做视觉挑图。 |

### Pixiv 小说

| 指令 | 用途 |
|---|---|
| `/小说 <标签>` | 按标签搜索小说。 |
| `/推荐小说` | Pixiv 推荐小说。 |
| `/最新小说 [最大小说ID]` | 获取最新小说。 |
| `/小说系列 <系列ID>` | 查看小说系列目录。 |
| `/小说评论 <小说ID> [偏移量]` | 查看小说评论。 |
| `/下载小说 <小说ID>` | 把小说导出为 PDF 发送。 |

### 订阅与定时

| 指令 | 用途 |
|---|---|
| `/订阅画师 <画师ID>` | 订阅画师更新。 |
| `/退订画师 <画师ID>` | 取消订阅画师。 |
| `/画师订阅` | 查看本会话的订阅列表。 |
| `/随机添加 <标签>` | 添加随机推送标签；推送间隔和数量使用配置值。 |
| `/随机删除 <序号>` | 删除指定的随机推送任务。 |
| `/随机列表` | 列出本会话的随机推送任务。 |
| `/随机暂停` | 暂停本会话的随机推送。 |
| `/随机开启` | 恢复本会话的随机推送。 |
| `/随机状态` | 查看随机推送调度器状态。 |
| `/随机执行` | 立刻手动触发一次随机推送。 |
| `/榜单添加 <模式> [日期]` | 新增榜单定时推送。 |
| `/榜单删除 <序号>` | 删除榜单定时推送。 |
| `/榜单列表` | 列出榜单定时推送。 |

### Fanbox

| 指令 | 用途 |
|---|---|
| `/赞助画师 <创作者> [数量]` | 查看 Fanbox 创作者主页与帖子。 |
| `/赞助帖子 <帖子ID或链接>` | 查看单个 Fanbox 帖子。 |
| `/赞助推荐 [数量]` | Fanbox 推荐创作者。 |
| `/赞助搜索 [关键词] [数量]` | 按画师名反查 Fanbox 创作者。 |

### 设置与帮助

| 指令 | 用途 |
|---|---|
| `/插画设置 [键] [值]` | 查看或修改 Pixiv 运行设置。 |
| `/生成图设置 <true/false>` | 设置 Pixiv AI 作品显示偏好并同步本地过滤设置。 |
| `/插画帮助` | 查看 Pixiv 全部指令说明。 |

## LLM 工具

在相应开关开启时，插件注册以下唯一工具名：

| 工具 | 用途 |
|---|---|
| `alice_image_find` | 文字找图。模型可指定 `auto`、`pixiv`、`soutu` 或 `serpapi`；如需锁定 Pixiv 画师，可填写 `artist_name` 或 `pixiv_user_id`。 |
| `alice_image_pixiv_novel` | 搜索或下载 Pixiv 小说。 |
| `alice_image_list_session_images` | 列出当前会话可用于反搜的图片和稳定 `image_id`。 |
| `alice_image_reverse_search` | 明确指定 `image_id` 或 `image_index` 后查证图片；可指定 `strategies` 或 `intent`，省略 `send_results` 默认静默。 |

找图工具会自行发送图片，并向模型返回结构化结果。`auto` 对二次元、插画、日文标签等按 Pixiv → 搜图神器 → SerpApi 尝试；普通实体和真实照片按搜图神器 → SerpApi → Pixiv 尝试。是否继续回退由配置决定，模型不需要也不应该虚构图片链接。

反搜工具可为图片问题主动检索。先从 `alice_image_list_session_images` 取得目标 `image_id`，再调用 `alice_image_reverse_search`；不指定目标或使用失效 ID 会返回选图错误，不会偷偷改查最新图片。

反搜的发送行为由可选参数 `send_results` 控制：省略时遵循配置（默认静默），`false` 强制静默，`true` 请求发送结果卡片。静默检索只把证据交给模型组织回答，手动 `/识图` 不受影响。旧 `llm_tool_silent_mode` 已被新配置替代。

**推荐配置：文字找图发图片开启，以图识图发结果卡片关闭。** 两者各管一个工具，不存在覆盖关系：

| 配置位置 | 控制的行为 | 推荐值 |
|---|---|---|
| 找图模块 → 文字找图：将找到的图片发到聊天（`find_image.tool_send_images`） | “帮我找张风景图”时，`alice_image_find` 把找到的图片发出来 | 开 |
| 以图搜图模块 → 图片上下文与工具行为 → 以图识图：默认将搜索结果卡片发到聊天（`reverse_image.ai_behavior.llm_tool_send_results_default`） | 分析你发来的图片时，`alice_image_reverse_search` 是否额外展示检索卡片；显式发送参数优先 | 关 |

模型收到的证据包含每个来源的标题、摘要、链接、缩略图 URL，以及内部视觉核对的画面对应关系；不会返回图片二进制。网页标题可能与配图无关，排序分也不是识别正确率。视觉核对会区分拼图区域，无法核验时明确返回未核验状态。同一消息下的重复检索会短时复用结果；它能减少线索丢失和重复调用，但不保证每张图都能找到正确出处。

## 配置说明

配置页包含“找图模块”“以图搜图模块”和“Dashboard WebUI”三个顶层分组。

### 找图模块

- `enabled`、`commands_enabled`、`llm_tools_enabled`：分别控制整个模块、所有指令和模型工具。
- `llm_search_progress_message_enabled`：控制 LLM 自主找图前是否发送“正在为你寻找...”这类前置提示；默认关闭，只保留最终图片或失败结果。
- `tool_send_wait_timeout_seconds`：LLM 工具等待平台发图确认的最长秒数；默认 45，设为 `0` 表示一直等待。
- `auto_source_enabled`、`default_source`、`fallback_enabled`、`fallback_order`：控制自动选源与回退策略。
- `llm_review`：控制视觉审核、审核模型和候选数量。审核模型留空且 `current_session_bot_enabled` 开启时，当前 Bot 会结合当前人格与最近几轮对话筛选候选；这次筛选不带工具，不会递归找图，也不会写入聊天历史。`strict_match_enabled` 默认拒绝发送视觉模型明确否决的候选；`fail_open` 只决定审核模型不可用、超时或解析失败时是否放行首图。
- `pixiv`：有模块总开关、各项功能开关和完整 Pixiv 参数。需要填写 `refresh_token` 才能使用 Pixiv API；Fanbox 受限内容可另填 Cookie。
- `pixiv.settings.return_count`：Pixiv 指令默认返回作品数。`/插画`、`/画师作品` 和 `/随机插画` 末尾的数量可只覆盖本次调用，不会修改配置。
- `pixiv.settings.randomize_search_results`：默认随机抽取多候选 Pixiv 结果；`recent_dedup_enabled` 会按群聊或私聊避开近期已发作品。作品 ID 和链接直取不受影响，候选全部用完时会自动复用旧作品。
- `pixiv.settings.artist_random_blocked_tags`：仅用于 `/随机插画`，精确屏蔽带有指定原始标签或翻译标签的作品；`artist_random_pages` 控制随机作品池页数。
- `soutu`：可分别关主图源、Bing 补充和视觉挑图。关闭 `enabled` 后不会启动 Playwright 抓取。
- `serpapi`：仅包含文字搜图；填写 `serpapi_keys` 后可轮询多个 Key，并可独立关闭 `vlm_selection_enabled` 视觉淘汰赛。

### 以图搜图模块

- `enabled`、`commands_enabled`、`llm_tools_enabled`：分别控制模块、`/识图` 和模型工具；`inject_tool_guidance_enabled` 可单独关闭模型提示注入。
- `ai_behavior.capture_image_context`：控制是否保存用户发图供 Bot 主动反搜；关闭后不会保留会话图片。
- `ai_behavior.llm_tool_send_results_default`：省略工具发送参数时是否发结果卡片，默认 `false`。工具显式传参优先，手动指令不受影响。
- `ai_behavior.llm_evidence_max_results`：模型证据条数，默认 12，独立于聊天展示上限。
- `ai_behavior.visual_evidence_enabled`：默认开启原图与候选核对，会增加一次内部视觉模型调用，最多等待 30 秒；无可用视觉模型时保留文本线索。`visual_evidence_provider_id` 留空使用当前会话模型，`visual_evidence_max_images` 默认 6，控制候选图片数。
- `strategies`：可独立启用 SauceNAO、Google Lens、Ascii2d、Yandex；Yandex 还可设置抓取条数与 `.ru` 回退。
- `strategies.google_lens_search_type`：`all` / `exact_matches` / `visual_matches` / `products`，默认 `all`；`google_lens_language`、`google_lens_country` 控制语言与地区；`google_lens_auto_crop` 默认关闭，减少拼图只查到局部的情况。
- `network.allow_image_upload`：本地或平台临时图片无法直接给外部引擎时，是否上传到 Catbox 获取公开 URL；隐私敏感场景请关闭。
- `network.allow_local_file_access`：默认关闭。保持关闭可避免模型利用路径读取并上传服务器本地文件。
- `display.max_results`：跨引擎融合、去重和排序后的最终展示条数上限；各引擎内部抓取条数由各自策略配置控制。

SerpApi Key 可只在任意一个模块填写一次。若另一个模块的 Key 列表为空，运行时会复用已填写的一侧。

### Dashboard WebUI

`webui.enabled` 控制是否启用工作台与后端接口，修改后重载插件。工作台可以配置选项、查看全部指令和试跑检索；预览图缓存数量与有效期可通过 `preview_cache_max_items`、`preview_ttl_seconds` 调整。

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

### Yandex Cookie（可选）

用途：降低 Yandex 图片搜索触发地区限制或 CAPTCHA 的概率。Yandex 不要求 API Key；不填 Cookie 也会先尝试公开页面。

填写位置：`reverse_image.api_keys.yandex_cookies`

获取方法：

1. 用 Chrome 或 Edge 打开 [Yandex Images](https://yandex.com/images/)，必要时先完成一次验证。
2. 按 `F12` 打开开发者工具，进入 `Application` -> `Cookies`，选择 `https://yandex.com`（若实际使用 `.ru` 则选择 `https://yandex.ru`）。
3. 复制浏览器请求里的完整 Cookie 字符串（格式如 `name=value; name2=value2`），粘贴到配置项。
4. Cookie 等同登录态凭据，过期或失效时直接清空并重试公开访问即可。

插件只把 Cookie 发给 Yandex 请求和 Yandex 自有缩略图域名，不会向搜索结果中的第三方网站转发；日志不会输出 Cookie 值。

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
- 关闭会话图片记录不会影响 `/识图` 附图、回复图片或“先发指令后补图”的指令流程。

## 常见问题

| 现象 | 处理方式 |
|---|---|
| 搜图神器没有结果 | 执行 `python -m playwright install chromium`，检查网络；也可启用 Bing 补充或开启 SerpApi 回退。 |
| Pixiv 认证失败 | 检查 `refresh_token`、代理和反代设置；不要把 Token 贴到群聊。 |
| Pixiv 总是发同一张图 | 保持 `randomize_search_results` 和 `recent_dedup_enabled` 开启；需要更长记忆时提高 `recent_dedup_retention_days`。 |
| 视觉审核回退到首图 | 当前模型不支持图片、审核超时或候选下载失败。可换视觉模型，或关闭 `fail_open` 让插件改用下一个来源；明确不匹配的候选在 `strict_match_enabled` 开启时不会发送。 |
| Ascii2d 403 | 重新获取 Cookie，必要时使用代理。 |
| Yandex 没有结果或出现 CAPTCHA | 先在配置中填写 `reverse_image.api_keys.yandex_cookies`，确认 `yandex_use_ru_fallback` 开启，并检查代理/地区网络；验证页或页面结构变化会报告该引擎暂不可用，不会丢掉其它引擎的结果。 |
| 无法反搜本地图片 | 在隐私风险可接受时开启图片上传；需要读取服务器路径时还必须单独开启本地文件访问。 |

## 上游致谢

本项目基于并感谢以下上游插件：

- [vmoranv-reborn/astrbot_plugin_pixiv_reborn](https://github.com/vmoranv-reborn/astrbot_plugin_pixiv_reborn)
- [674537331/astrbot_plugin_soutushenqi](https://github.com/674537331/astrbot_plugin_soutushenqi)
- [monbed/astrbot_plugin_serpapi_imgsearch](https://github.com/monbed/astrbot_plugin_serpapi_imgsearch)
- [iona-s/astrbot_plugin_imgexploration](https://github.com/iona-s/astrbot_plugin_imgexploration)

Yandex 策略的接口选择与风控回退思路参考了 [OMSociety/astrbot_plugin_reverse_searcher](https://github.com/OMSociety/astrbot_plugin_reverse_searcher)，本项目为独立实现，未复制其代码。

精确引用的上游 commit、修改范围和许可证兼容说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 许可证

本项目按 [AGPL-3.0](LICENSE) 发布。使用、分发或部署修改版时，请遵守该许可证及所有上游项目的许可证义务。
