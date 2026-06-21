# 图片注入审计器

AstrBot 图片注入审计和坏图护栏插件。它会在每次 LLM 请求前统计图片数量、输出图片来源，并默认移除无法解析的坏图片，帮助排查和缓解类似下面的错误：

```text
Too many images were provided, we currently limit the number of images per conversation to 30
Unable to process input image
Failed to load image: cannot identify image file <_io.BytesIO object>
```

## 功能

- 统计当前请求中的 `req.image_urls` 图片。
- 统计 `req.extra_user_content_parts` 中的 `ImageURLPart` 图片。
- 可选统计 `req.contexts` / Agent 上下文里已经存在的历史图片。
- 尝试识别上一轮请求里的图片是否被带入本轮上下文。
- 默认在发送模型前移除明显坏掉的图片，避免 Gemini、Kimi 等 fallback 模型一起 400。
- 默认会清理本地文件、`file://`、`data:image/...base64`、`base64://`、MCP inline image 里的坏图。
- 可选联网验证远程图片 URL，发现返回 HTML/JSON/错误页时移除。
- 尝试识别来源插件：
- 图片总数达到 `warn_image_limit` 时输出 warning 日志。
- 使用 `/image_audit_status` 查看当前会话最近一次审计摘要。

## 日志示例

```text
[ImageAudit] phase=llm_request umo=aiocqhttp:GroupMessage:12345 session=... model=-
images_total=34 current=18 context_or_history=16 history_carryover=12 warn_limit=30
[ImageAudit] channels: request.contexts=16, request.image_urls=12, request.extra_user_content_parts=6
[ImageAudit] sources: conversation_history=16, astrbot_plugin_video_vision_helper=12, astrbot_plugin_gif_frame_vision=6
[ImageAudit] tracked mutations: astrbot_plugin_video_vision_helper request.image_urls.extend +12 [...]
[ImageAudit] carryover: previous_request_images=12 matched_history_images=12
[ImageAudit] removed invalid images: count=1 #1 conversation_history contexts[3].content[1] bytes look like text/html/json/xml, not an image file:///tmp/bad-image.png
```

## 说明

`on_llm_request` 发生在 AstrBot 构建 AgentRunner 之前，因此本插件记录的是进入本轮 Agent 请求前的图片数量。`agent_begin_pre_compaction` 日志发生在上下文压缩之前，真实发送给 provider 的数量可能因为后续压缩而更低。

从 `0.2.0` 开始，本插件默认会移除能确定是坏图的图片输入。它不会修改正常图片，也不会默认联网下载远程 URL；如果你怀疑远程 URL 返回了错误页，可以开启 `validate_remote_images`。

注意：如果某个插件直接执行 `req.image_urls = new_list` 替换整个列表，`tracked mutations` 不会显示这次赋值动作；最终 `sources` 仍会通过前后差异和图片路径特征尽量归因。

如果同一会话里第二次请求的 `context_or_history` 变大，通常不是重复注入，而是 AstrBot 正常把上一轮多模态消息放进了对话历史；这时重点看 `history_carryover` 和 `carryover` 日志，能更快确认到底是不是把上一轮帧又送了一遍。

## 配置建议

- `remove_invalid_images`：建议保持开启。它负责在请求发送给模型前移除坏图。
- `remove_invalid_context_images`：建议保持开启。坏图经常藏在历史上下文里，不清掉会导致所有模型 fallback 都失败。
- `validate_remote_images`：默认关闭。只有怀疑 HTTP/HTTPS 图片 URL 返回错误页时再开。
- `warn_image_limit`：图片数量告警阈值，只影响日志，不会自动删正常图片。
