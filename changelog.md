# Changelog

## 0.1.1 - 2026-05-24

- 修复 `metadata.yaml`、`main.py`、`_conf_schema.json`、`README.md` 的 UTF-8 编码问题，确保中文内容正常显示。
- 增加历史回带识别：区分本轮新增图片和上一轮请求带入上下文的图片。
- 在日志中新增 `history_carryover` 和 `carryover` 字段，帮助确认是否把上一轮图片重新送入了本轮请求。
- 为历史回带识别补充配置项 `track_history_carryover`。
- 更新 README 的日志示例和排查说明。

## 0.1.0 - 2026-05-24

- 初始版本。
- 统计 `req.image_urls`、`req.extra_user_content_parts`、`req.contexts` 和 Agent 上下文中的图片数量。
- 尝试识别图片来源插件，并记录注入动作。
- 提供 `/image_audit_status` 命令查看最近一次审计摘要。
