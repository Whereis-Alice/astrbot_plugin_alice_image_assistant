# 更新日志

## v1.0.4 - 2026-07-29

- 扩展 Pixiv 作品链接自动解析：链接可以夹在普通文字中，并兼容 `/en/artworks/` 等语言路径、查询参数、片段、末尾标点、移动端域名、无协议链接和旧版 `member_illust.php` 链接。
- 一条消息包含多个作品链接时只处理第一条有效链接，避免自动解析连续刷图；原有 `url_lookup` 与 `pixiv_urlsearch_enabled` 开关保持不变。

## v1.0.3 - 2026-07-29

- `alice_image_find` 新增可选参数 `artist_name` 和 `pixiv_user_id`。填写后会限定在指定 Pixiv 画师作品中找图；未填写时普通找图流程不变。
- 新增 `/爱图P画师找 <画师名或用户ID> | <关键词>` 指令，便于手动测试指定画师找图；关键词可省略。
- 新增 Pixiv 功能开关 `artist_search`，可单独关闭指定画师精确找图。
- 作者限定找图不会回退到搜图神器或 SerpApi，避免“指定 P 站画师”失败后发送无关图源。

## v1.0.2 - 2026-07-29

- 修复 LLM 自动找图时 Pixiv 已找到候选图、但平台发图确认超时后被误判为“找图失败”的问题。现在只要 Pixiv 已找到并开始发送，就不会再切换到搜图神器或 SerpApi，避免发出不相关图片。
- 新增发送状态字段：`warnings`、`send_attempted`、`delivery_uncertain`、`pixiv_found_count`。模型可以区分“搜索失败”和“已找到但发送确认较慢”。
- 新增配置 `find_image.tool_send_wait_timeout_seconds`。默认等待平台发图确认 45 秒；超时后工具先返回，图片发送任务留在后台继续。设为 `0` 可恢复一直等待发送结束。
- 保留用户手动设置的 Pixiv `original` 画质；仅把空配置时的运行时默认画质与配置面板默认值统一为 `medium`。
- 增加回归测试，覆盖“Pixiv 找到但发送超时不回退”和 Pixiv 画质默认值一致性。
