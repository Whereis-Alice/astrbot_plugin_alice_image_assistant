# 更新日志

## v1.0.2 - 2026-07-29

- 修复 LLM 自动找图时 Pixiv 已找到候选图、但平台发图确认超时后被误判为“找图失败”的问题。现在只要 Pixiv 已找到并开始发送，就不会再切换到搜图神器或 SerpApi，避免发出不相关图片。
- 新增发送状态字段：`warnings`、`send_attempted`、`delivery_uncertain`、`pixiv_found_count`。模型可以区分“搜索失败”和“已找到但发送确认较慢”。
- 新增配置 `find_image.tool_send_wait_timeout_seconds`。默认等待平台发图确认 45 秒；超时后工具先返回，图片发送任务留在后台继续。设为 `0` 可恢复一直等待发送结束。
- 保留用户手动设置的 Pixiv `original` 画质；仅把空配置时的运行时默认画质与配置面板默认值统一为 `medium`。
- 增加回归测试，覆盖“Pixiv 找到但发送超时不回退”和 Pixiv 画质默认值一致性。
