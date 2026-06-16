# QQ空间Ultra（QzoneUltra）

特别鸣谢 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone)。QQ空间Ultra 的中文命令体系、表白墙工作流和部分用户体验设计参考了该项目；本插件在此基础上整合了本地 daemon、Cookie 管理、LLM 工具、发布结果渲染和 AstrBot 兼容层，方便在当前 AstrBot 环境中继续扩展和维护。

QQ空间Ultra 是一个面向 AstrBot 的 QQ 空间插件，提供中文命令、LLM 工具、Cookie 绑定、本地 daemon、图片/视频发布、说说渲染、投稿审核和自动评论能力。插件优先适配 OneBot v11 / aiocqhttp，也支持手动 Cookie 绑定后使用核心 QQ 空间功能。

交流与反馈：**[点击加入 QQ 群 1081773675](https://qm.qq.com/q/Qr45Vz0a8o)**

## 亮点：自动发布「会生活」的日常说说

QzoneUltra 现在可以把 **Life Scheduler 日程插件** 和 **OmniDraw 自拍生成** 串起来，自动把当天行程变成一条更像真人日常的 QQ 空间说说：先读取日程上下文，再由 LLM 生成适合自拍模式的画面提示词，调用 OmniDraw `generate_selfie(return_result=true)` 取回图片，最后自动配文并发布到 QQ 空间，或按配置先写入草稿。特别感谢木有知大佬的日程插件 [muyouzhi6/astrbot_plugin_life_scheduler](https://github.com/muyouzhi6/astrbot_plugin_life_scheduler)，推荐搭配安装使用，让自动说说能真正基于每天的日程和生活上下文生成。

这条链路适合做每日生活号、陪伴型人设、自动运营空间：它不只是定时发一段文字，而是把「今天要做什么」变成「看起来真的去过、拍过、分享过」的图文日常。

<p align="center">
  <img src="docs/assets/life-selfie-qzone-demo.png" alt="Life Scheduler + OmniDraw 自动日常说说效果展示" width="520">
</p>

- 定时任务：复用 `trigger.publish_cron`，开启 `life_publish.enabled=true` 后优先走日常自拍链路。
- 日程联动：建议搭配 [木有知大佬的 Life Scheduler](https://github.com/muyouzhi6/astrbot_plugin_life_scheduler) 使用，调用 `astrbot_plugin_life_scheduler.get_life_context()` 读取今日安排，让内容贴合当天场景。
- OmniDraw 联动：强制使用 `return_result=true` 回收图片结果，不依赖 OmniDraw 自己下发消息，方便统一发布到 QQ 空间。
- 失败策略：支持 `life_publish.image_retry_count`、`life_publish.failure_policy=skip/text_only`，生成失败时可跳过或退回文字发布。
- 手动体验：管理员发送 `发日常说说` 可立即跑完整链路，并收到“完成文本 + 渲染图片”的直观反馈。

## 功能概览

- 查看好友动态、指定用户说说、说说详情、评论和最近访客。
- 点赞、评论、回复评论、发布说说、删除自己发布的说说。
- AI 写说说、AI 评论、AI 回评，生成内容会走当前 AstrBot 会话 provider 和人设。
- 支持文本、图片和单个本地视频发布；视频会交给本地 daemon 走 QQ 空间 Web Cookie/`p_skey` 的 H5 `video_qzone` + `pic_qzone` 链路，先绑定公开相册并创建 `appid=311` 视频说说，再调用权限更新接口做公开修复，最后通过 feed/detail 校验同一 `sVid` 和全部人可见后才返回成功。不会再唤起 QQ/QQNT 客户端、调用 OneBot 端发布 action、要求 QQ upload A2/vLoginData，或退回成视频封面图发布。
- 看说说、读说说、评论说说、点赞说说和自动评论反馈可复用同款 QQ 空间风格说说卡片渲染。
- 表白墙投稿、匿名投稿、撤稿、看稿、过稿、拒稿。
- 定时自动发说说、自动评论好友最新动态，并记录已处理动态，避免重复打扰；其中自动发说说可联动 Life Scheduler + OmniDraw，把今日行程生成自拍图和日常文案后自动发布。
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
| 发日常说说 | - | 管理员 | `发日常说说` | 立即执行 Life Scheduler 日程、LLM 自拍提示词、OmniDraw 自拍图、QQ 空间发布完整链路 |
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

`native_video_publish` 开启后，单个本地视频会通过已验证的 daemon 后台路径发布。只要 QQ 空间 Web Cookie/`p_skey` 可用，daemon 会使用 H5 `sliceUpload/FileUploadVideo` 上传视频和封面，确保封面绑定真实公开相册，调用 `emotion_cgi_publish_v6` 创建 `appid=311` 视频说说，保留发现到的 `tid/fid`，再调用 `emotion_cgi_update` 写入 `ugc_right=1`/`who=1` 做幂等公开修复。只有最终 feed/detail 同时验证到 `appid=311`、同一 `sVid` 和明确全部人可见标记时，插件才报告发布成功；不会使用视频封面图、OneBot 协议端发布 action、Tencent upload A2/vLoginData 或 QQ/QQNT 界面作为回退。

`/qzone autovideoauth` 现在只负责确保 daemon 拥有可用的 QQ 空间 Cookie/`p_skey`，用于 H5 视频上传、公开创建、权限修复和公开校验链路；它不再探测或绑定 QQ upload A2/vLoginData。状态里出现“视频直发：可用（公开视频校验）”表示 Cookie/H5 视频路径可用。视频发布契约是：所有成功的视频说说都必须来自 daemon 的 H5 公开创建 + 权限修复 + 公开校验链路；如果缺少 Cookie/`p_skey`、权限更新失败，或 feed/detail 公开校验失败，插件会拒绝宣称成功，而不是重复发布或退回封面图。

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


## 定时生活说说联动（Life Scheduler + OmniDraw）

开启后，`trigger.publish_cron` 触发时会先走这条链路：

1. 调用 `astrbot_plugin_life_scheduler.get_life_context()` 获取今日日程
2. 把日程交给 LLM 生成适合 OmniDraw 自拍模式的提示词
3. 调用 OmniDraw 的 `generate_selfie(return_result=true)` 获取返回式图片
4. 自动配文并发说说，或按配置写入草稿

推荐配置：

- `life_publish.enabled=true`
- `life_publish.use_life_context=true`
- `life_publish.use_llm_image_prompt=true`
- `life_publish.use_omnidraw_selfie=true`
- `life_publish.auto_caption=true`
- `life_publish.mode=publish` 或 `draft`
- `life_publish.failure_policy=skip` 或 `text_only`
- `life_publish.image_retry_count=1`（生图失败后的额外重试次数，0=不重试，最大 5）

说明：

- 这里不会调用 OmniDraw 的自动下发路径，只使用 `return_result=true` 取回图片结果。
- 如果 Life Scheduler 或 OmniDraw 不可用，会按 `failure_policy` 处理。
- OmniDraw 未返回图片或调用抛错时，会按 `life_publish.image_retry_count` 重试，每次仍强制传 `return_result=true`。
- 关闭 `life_publish.enabled` 后，`trigger.publish_cron` 会退回原来的纯文本定时发说说。
- 管理员可发送 `发日常说说` 立即跑完整链路并直接发布；该指令会沿用各项日程/LLM/OmniDraw 开关，但会强制发布，不受 `life_publish.mode=draft` 影响。
