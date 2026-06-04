# QQ空间Ultra（QzoneUltra）

特别鸣谢 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone)。QQ空间Ultra 的中文命令体系、表白墙工作流和部分用户体验设计参考了该项目；本插件在此基础上整合了本地 daemon、Cookie 管理、LLM 工具、发布结果渲染和 AstrBot 兼容层，方便在当前 AstrBot 环境中继续扩展和维护。

QQ空间Ultra 是一个面向 AstrBot 的 QQ 空间插件，提供中文命令、LLM 工具、Cookie 绑定、本地 daemon、图片/视频发布、说说渲染、投稿审核和自动评论能力。插件优先适配 OneBot v11 / aiocqhttp，也支持手动 Cookie 绑定后使用核心 QQ 空间功能。

交流与反馈：**[点击加入 QQ 群 1081773675](https://qm.qq.com/q/Qr45Vz0a8o)**

## 功能概览

- 查看好友动态、指定用户说说、说说详情、评论和最近访客。
- 点赞、评论、回复评论、发布说说、删除自己发布的说说。
- AI 写说说、AI 评论、AI 回评，生成内容会走当前 AstrBot 会话 provider 和人设。
- 支持文本、图片和视频引用形式的说说发布；单个本地视频会优先交给本地 daemon 通过 QQ upload / `video_qzone` 移动上传协议后台直发，需要协议端扩展 action 或 `/qzone videoauth` 提供 vLoginData/A2 二进制材料，不再依赖 QQ/QQNT 客户端 handoff，也不再把视频帧当图片发出。
- 看说说、读说说、评论说说、点赞说说和自动评论反馈可复用同款 QQ 空间风格说说卡片渲染。
- 表白墙投稿、匿名投稿、撤稿、看稿、过稿、拒稿。
- 定时自动发说说、自动评论好友最新动态，并记录已处理动态，避免重复打扰；定时任务可向管理员发送 QQ 空间风格渲染图。
- 收到消息概率触发自动评论时，会在当前触发会话返回包含原说说和自己评论的 QQ 空间风格渲染图。
- Google News RSS 新闻说说：可按数量获取候选新闻并排序缓存，也可选择序号让 LLM 写成原创短评后立即发布或定时发布。
- 可选 pillowmd 风格渲染；渲染失败时自动回退文本。
- LLM tool 结果会转成自然语言回复，避免向用户暴露 raw JSON、fid、cursor 等内部字段。

## 安装

要求 AstrBot `>=4.16,<5`。

1. 将本仓库放入 AstrBot 的插件目录，例如 `data/plugins/astrbot_plugin_qzone_ultra`。
2. 在插件目录安装依赖：

```bash
pip install -r requirements.txt
```

3. 重启 AstrBot，或在 AstrBot 管理面板重新加载插件。
4. 在 QQ 私聊或群聊里发送 `/qzone status` 检查 daemon 和 Cookie 状态。

插件会在首次使用时按需启动本地 daemon。daemon 默认使用 `18999` 端口并只监听 `127.0.0.1`，用于隔离 QQ 空间请求、Cookie 管理和渲染逻辑。除非端口被占用，不建议修改默认端口；如果系统防火墙或安全软件拦截本地连接，请放行本插件的 `18999` 端口。

浏览器打开 `http://127.0.0.1:18999/` 或 `/health` 时只返回最小公开健康信息，用于确认端口可达。未认证公开响应只包含 `daemon_state`、`daemon_port`、`daemon_version`，其中 `daemon_state` 只表示进程生命周期，不包含登录、绑定、Cookie、QQ 号、时间戳、token、缓存或 revision 等状态；完整 daemon 状态和登录信息仍必须通过插件命令或携带 `X-Qzone-Secret` 的本地请求获取。

## Cookie 绑定

推荐在 OneBot v11 环境使用自动绑定。AstrBot 内置适配器名称仍叫 `aiocqhttp`，但插件逻辑按通用 OneBot 协议端处理，不限定某一个实现；LLOneBot、LLBot、NapCat、Shamrock 等能接入 OneBot v11 反向 WebSocket 的实现都走同一套解析和兜底逻辑。

```text
/qzone autobind
```

插件安装、下载后重载或 AstrBot 启动时会自动尝试执行一次 autobind；如果当时 OneBot 客户端稍后才就绪，插件会在首次捕获 OneBot/aiocqhttp 事件后补触发。自动绑定最多尝试 3 次，仍失败时再使用上面的命令或手动 Cookie 绑定。

如果平台无法提供 Cookie，可以手动绑定：

```text
/qzone bind p_skey=...; p_uin=o123456789; uin=o123456789; skey=...
```

也可以在 AstrBot 插件配置里填写 `cookies_str`，插件初始化时会尝试自动写入登录态。

## 中文命令

序号从 `1` 开始，`1` 或 `最新` 表示最新一条，`-1` 表示当前页最后一条。支持范围语法，例如 `1~3`。旧用法里的 `0` 会兼容为最新一条。

| 命令 | 别名 | 权限 | 用法 | 说明 |
| --- | --- | --- | --- | --- |
| 查看访客 | - | 管理员 | `查看访客` | 查看最近访客 |
| 看说说 | 查看说说 | 管理员 | `看说说 [@用户/QQ] [序号/范围]` | 查看好友动态或指定用户说说；范围结果会合成一张长图返回 |
| 读说说 | - | 管理员 | `读说说 [@用户/QQ] [序号/范围]` | 只读取说说并返回同款渲染卡片，不评论、不点赞 |
| 评说说 | 评论说说 | 管理员 | `评说说 [@用户/QQ] [序号/范围] [评论内容]` | 评论说说并返回原文与评论分区展示的渲染卡片；内容为空时由 AI 生成，空参数会跳过自己和已评论过的说说 |
| 赞说说 | - | 管理员 | `赞说说 [@用户/QQ] [序号/范围]` | 点赞说说 |
| 发说说 | - | 管理员 | `发说说 <文本> [图片/视频]` | 立即发布说说；单视频优先走 daemon 原生视频后台直发，缺少上传材料时阻止发布并提示绑定 |
| 写说说 | 写稿 | 管理员 | `写说说 <主题> [图片/视频]` | 生成待审核或待发布文案 |
| 新闻说说 获取 | 获取新闻、新闻列表 | 管理员 | `新闻说说 获取 [数量] [中国/国际/混合]` | 从 Google News RSS 拉取指定数量候选新闻，按发布时间排序并缓存 |
| 新闻说说 预览 | 新闻说说预览 | 管理员 | `新闻说说 预览 [序号/中国/国际/混合]` | 预览 LLM 基于候选新闻生成的原创说说，不发布 |
| 新闻说说 发布 | 发布新闻说说、发新闻说说 | 管理员 | `新闻说说 发布 <序号>` | 选择已缓存候选新闻，让 LLM 生成原创说说并立即发布 |
| 删说说 | - | 管理员 | `删说说 <序号>` | 删除自己发布的说说 |
| 回评 | 回复评论 | 管理员 | `回评 <稿件ID> [评论序号]` | 回复已缓存稿件或已发布说说下的评论 |
| 投稿 | - | 所有人 | `投稿 <文本> [图片/视频]` | 投稿到表白墙 |
| 匿名投稿 | - | 所有人 | `匿名投稿 <文本> [图片/视频]` | 匿名投稿到表白墙 |
| 撤稿 | - | 所有人 | `撤稿 <稿件ID>` | 撤回自己的待审核投稿 |
| 看稿 | 查看稿件 | 管理员 | `看稿 [稿件ID]` | 查看待审核稿件 |
| 过稿 | 通过稿件、通过投稿 | 管理员 | `过稿 <稿件ID>` | 审核并发布稿件 |
| 拒稿 | 拒绝稿件、拒绝投稿 | 管理员 | `拒稿 <稿件ID> [原因]` | 拒绝稿件 |

保留的兼容命令：

```text
/qzone help
/qzone status
/qzone bind <cookie>
/qzone autobind
/qzone videoauth <login_data_b64> [login_key_b64] [token_type] [token_appid] [token_wt_appid]
/qzone autovideoauth
/qzone unbind
/qzone feed [hostuin] [limit] [cursor]
/qzone detail <hostuin> <fid> [appid]
/qzone post <content>
/qzone comment <hostuin> <fid> <content>
/qzone like <hostuin> <fid> [appid] [unlike]
```

会读取或改变已绑定 QQ 空间状态的兼容命令只允许管理员使用；普通用户仍可使用投稿、匿名投稿和撤回自己的待审核投稿。

## LLM 工具

推荐工具：

- `llm_view_feed`
- `llm_publish_feed`

兼容工具：

- `qzone_get_status`
- `qzone_list_feed`
- `qzone_view_post`
- `qzone_detail_feed`
- `qzone_publish_post`
- `qzone_comment_post`
- `qzone_delete_post`
- `qzone_like_post`

`qzone_view_post`、`qzone_comment_post`、`qzone_delete_post`、`qzone_like_post` 推荐使用 `target_uin` 加 `selector`。`selector` 可以写 `latest`、`最新`、`第2条`、`2`、`1~3` 或真实 `fid`。旧参数 `hostuin`、`fid`、`appid`、`latest`、`index` 仍保留兼容。

LLM tools 中会读取或改变已绑定 QQ 空间状态的工具默认只允许管理员触发，避免群聊成员借用插件 Cookie 查看或操作账号空间。

点赞会区分“请求已被 QQ 空间接受”和“读回校验暂未同步”。如果 QQ 空间读回有延迟，插件会保持成功结果并提示校验不确定，不会把已接受的点赞误报为失败。

## 配置

常用配置项：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `admin_uins` | 空 | 管理员 QQ 号，多个用英文逗号分隔 |
| `cookies_str` | 空 | 可选 Cookie 字符串，用于初始化自动绑定 |
| `daemon_port` | `18999` | 本地 daemon 端口；不建议修改，需在防火墙或安全软件中放行 |
| `auto_start_daemon` | `true` | 首次使用时自动启动 daemon |
| `auto_bind_cookie` | `true` | 登录态缺失时尝试从 OneBot 自动获取 Cookie |
| `manage_group` | 空 | 投稿审核通知群；为空时尝试私发管理员 |
| `pillowmd_style_dir` | 空 | 可选 pillowmd 样式目录 |
| `native_video_publish` | `true` | 单个本地视频说说优先交给 daemon 原生视频后台直发；关闭后会拒绝视频发布，避免误把封面/渲染图当作成功 |
| `render_publish_result` | `true` | 发布成功后返回渲染图；看/读/评/赞说说也会复用同款卡片渲染 |
| `render_feed_card_limit` | `5` | 看/读/评/赞说说时单次最多渲染的卡片数量；多条会合成一张左对齐长图 |
| `llm.post_provider_id` | 空 | 写说说使用的 LLM provider；空表示当前会话默认 provider |
| `llm.news_provider_id` | 空 | 新闻说说使用的 LLM provider；空时优先回退写说说 provider |
| `llm.comment_provider_id` | 空 | 评论使用的 LLM provider |
| `llm.reply_provider_id` | 空 | 回评使用的 LLM provider |
| `trigger.publish_cron` | 空 | 自动发说说 cron 表达式；空表示关闭 |
| `trigger.news_cron` | 空 | Google News RSS 新闻自动发说说 cron 表达式；空表示关闭 |
| `trigger.comment_cron` | 空 | 自动评论 cron 表达式；空表示关闭 |
| `trigger.comment_latest_count` | `1` | 每次定时自动评论从好友最新动态中处理的未评论说说条数 |

| `trigger.read_prob` | `0.0` | 收到消息时概率触发读说说和自动评论 |
| `trigger.send_admin` | `false` | 定时发布、定时评论后向管理群或管理员私聊发送结果和渲染图；收到消息概率触发的自动评论会发回当前会话 |
| `news.scopes` | `["china"]` | 新闻范围，可填 `china`、`world`、`mixed`，也支持 `中国`、`国际`、`混合` |
| `news.keywords` | 空 | 额外 Google News RSS 搜索关键词列表 |
| `news.custom_rss_urls` | 空 | 自定义 Google News RSS 地址列表，仅允许 `https://news.google.com/rss...` |
| `news.max_candidates` | `12` | 默认获取或交给 LLM 的新闻候选数量；命令里可临时覆盖 |
| `news.once_per_day` | `true` | 定时任务每天最多成功发布一次新闻说说；手动选择发布不受该限制 |
| `news.trust_env` | `true` | Google News RSS 请求使用系统代理；只影响新闻 RSS，不影响 QQ 空间 Cookie 请求 |

`native_video_publish` 开启后，单个本地视频会优先走 daemon 原生视频后台路径：稳定路径是 QQ upload / `video_qzone` 移动上传协议，插件会通过 `/qzone videoauth`、`/qzone autovideoauth` 或进程环境变量 `QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64` 获取 vLoginData/A2 类二进制登录材料，再上传视频、上传封面并轮询最近动态验证同一 `sVid`。实测 Qzone H5 `sliceUpload/FileUploadVideo` 能稳定上传视频资源，但 Web `emotion_cgi_publish_v6` + `richval` 可能只回显提交内容而不生成可见视频动态；因此默认不再把 H5 Cookie/`p_skey` 当作稳定视频直发凭据。未验证到 feed 时会阻止发布并报错，不再回退为视频封面图，也不再唤起 QQ/QQNT 客户端确认窗口。关闭 `native_video_publish` 后会直接拒绝包含视频的发布请求，必须重新开启并绑定 QQ upload vLoginData/A2 后才能发布真视频。

`/qzone autovideoauth` 面向 OneBot 协议端能力探测，不绑定单一实现：会优先尝试通用扩展 action（例如 `get_qzone_video_upload_credentials`、`get_video_upload_credentials`、`get_login_misc_data key=a2/vLoginData` 等，也兼容 `_get_*` 这类下划线扩展命名），也会兼容 LLOneBot 的 `llonebot_debug` 透传 `nodeIKernelLoginService/getLoginMiscData`、`loginService.getLoginMiscData` 和 PMHQ `httpSend`。调用层支持 `call_action`、`call_api`、`request`、`call` 以及 `params/data/payload` 包装，NapCat / LLOneBot / LLBot / Shamrock 等 OneBot 实现只要暴露返回 vLoginData/A2 二进制材料的 action 都可被绑定；返回字段支持 `a2`/`a2_hex`/`a2_b64`、`vLoginData`/`vLoginDataHex`/`vLoginDataB64`、Node `Buffer`/数字数组，以及目标 login-misc 调用返回的原始二进制字符串。仅返回 Cookie/CSRF 或 `clientkey/keyIndex` 会被诊断为 Web 登录材料并拒绝当作 A2，避免把不可见视频或 H5 richval 回显误报为发布成功。
  - OneBot protocol compatibility: probing is protocol-first, not NapCat-only. It supports `call_action`/`call_api`/`request`/`call`/`send_api`/`send_action`/`request_api`/`api_call` (and camelCase aliases), nested `api`/`client`/`bot` wrappers, and single OneBot envelopes like `{"action":"...","params":{...}}`; NapCat/LLOneBot/LLBot-specific probes are only optional fallbacks.

完整配置见 `_conf_schema.json`。Cron 表达式格式为 `分 时 日 月 周`，例如 `30 8 * * *` 表示每天 8:30。

## 数据目录

运行数据默认写入 AstrBot 分配给插件的数据目录，通常包括：

- Cookie 和登录状态。
- daemon 状态和保活信息。
- 投稿草稿、稿件 ID、已发布 fid。
- 自动评论去重记录。
- 新闻候选缓存、自动发布日期和已使用新闻候选记录。
- 渲染临时文件和发布结果图。

## 排障

- `/qzone status` 显示未绑定：先执行 `/qzone autobind`，失败后使用 `/qzone bind <cookie>`。
- daemon 无法启动：确认默认 `18999` 端口没有被占用，防火墙或安全软件已放行本地连接，并检查 AstrBot 日志。
- 浏览器访问 `127.0.0.1:18999`：看到 `ok: true` 代表本地 daemon 端口可达；空或错误的 `X-Qzone-Secret` 仍会返回 401。如果需要 Cookie、QQ 号或完整状态，请使用 `/qzone status`。
- 自动绑定失败：确认 AstrBot 使用的是 OneBot v11 协议端（AstrBot 适配器名可能显示为 aiocqhttp），且适配器允许获取 Cookie。
- 图片/视频发布失败：确认图片或引用的视频可被 AstrBot 正常读取；视频会优先使用 OneBot 返回的 `url/download_url/file_url/path/file_id`，再尝试 `get_file`、群/私聊文件直链和 base64 兜底；封面提取依赖系统 `ffmpeg` 或 `imageio-ffmpeg`。
- LLM 生成内容为空：检查 AstrBot 当前会话 provider，或分别配置 `llm.post_provider_id`、`llm.comment_provider_id`、`llm.reply_provider_id`。
- 点赞成功但提示校验不确定：通常是 QQ 空间读回延迟，可稍后再查看目标说说。
