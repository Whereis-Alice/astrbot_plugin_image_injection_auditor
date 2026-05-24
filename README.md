# Image Injection Auditor

AstrBot 图片注入审计插件。它会在每次 LLM 请求前统计图片数量，并在日志中输出图片来自哪里，帮助排查类似下面的错误：

```text
Too many images were provided, we currently limit the number of images per conversation to 30
```

## 功能

- 统计当前请求中的 `req.image_urls` 图片。
- 统计 `req.extra_user_content_parts` 中的 `ImageURLPart` 图片。
- 可选统计 `req.contexts` / Agent 上下文里已经存在的历史图片。
- 尝试识别来源插件：
  - 通过包装 `req.image_urls` / `req.extra_user_content_parts` 捕获 `append`、`extend`、`insert`、切片赋值等注入动作。
  - 通过调用栈识别 `astrbot_plugin_*` 插件目录名。
  - 通过临时文件名前缀识别 `astrbot_plugin_video_vision_helper` 和 `astrbot_plugin_gif_frame_vision`。
- 图片总数达到 `warn_image_limit` 时输出 warning 日志。
- 使用 `/image_audit_status` 查看当前会话最近一次审计摘要。

## 日志示例

```text
[ImageAudit] phase=llm_request umo=aiocqhttp:GroupMessage:12345 session=... model=-
images_total=34 current=18 context_or_history=16 warn_limit=30
[ImageAudit] channels: request.contexts=16, request.image_urls=12, request.extra_user_content_parts=6
[ImageAudit] sources: conversation_history=16, astrbot_plugin_video_vision_helper=12, astrbot_plugin_gif_frame_vision=6
[ImageAudit] tracked mutations: astrbot_plugin_video_vision_helper request.image_urls.extend +12 [...]
```

## 说明

`on_llm_request` 发生在 AstrBot 构建 AgentRunner 之前，因此本插件记录的是进入本轮 Agent 请求前的图片数量。`agent_begin_pre_compaction` 日志发生在上下文压缩之前，真实发送给 provider 的数量可能因为后续压缩而更低。

本插件默认只记录日志，不会删除或修改任何图片输入。

注意：如果某个插件直接执行 `req.image_urls = new_list` 替换整个列表，`tracked mutations` 不会显示这次赋值动作；最终 `sources` 仍会通过前后差异和图片路径特征尽量归因。
