# v0.8.2 issue 修复说明

## 定时发布

- `publish_offset` 的范围保持为 0–3600 秒。
- 偏移语义改为“在 cron 基准时间之后随机延迟”。
- 不再生成基准时间之前的随机目标，因此不会把过期目标钳成 1 秒后执行，也不会在同一 cron 时段反复重挂。

## Agent 工具

`qzone_get_status`、`qzone_list_feed`、`qzone_detail_feed`、`qzone_view_post`、
`qzone_publish_post`、`qzone_comment_post`、`qzone_delete_post` 和
`qzone_like_post` 均把文本结果返回给 Agent。最终用户回复由 Agent/LLM 组织，
工具本身不再通过 `event.plain_result` 直接发送结果。

## 安全与失败处理

- daemon Secret 使用常量时间比较。
- 自动评论无法确认当前登录 QQ 时中止本轮，不执行评论。
- bind/autobind 已成功但后续状态刷新失败时，命令会保留“绑定成功”的事实，
  同时提示稍后通过 `/qzone status` 重试查询。
