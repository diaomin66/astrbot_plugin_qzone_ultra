# Changelog

## Unreleased

- fix: accept more OneBot protocol-end A2/vLoginData shapes, including hex/base64 alias fields, targeted NTQQ raw binary strings, and LLOneBot `llonebot_debug` PMHQ `httpSend` login-misc forwarding.
- fix: block video posts when `native_video_publish` is disabled so video attachments can no longer be reported as successful cover/rendered-image publishes.
- fix: support generic OneBot protocol dispatchers for video auth probing, including `send_api`/`send_action`/`request_api`/`api_call`, nested protocol client wrappers, single `{"action": "...", "params": {...}}` envelopes, and the `llbot` platform alias.
- fix: generalize OneBot video auth probing beyond aiocqhttp/NapCat by defaulting the source to `onebot`, trying leading-underscore extension actions, and supporting `request`/`call` wrappers with `params`/`data`/`payload`.

## 未发布
- 修复：视频封面 `pic_qzone` 上传现在和 Android 一样携带同一份 `iBusiNessType=1` / `vBusiNessData=publishmood`，避免只在 `video_qzone` 阶段带发布体导致封面 fake feed 不能触发真实视频动态。
- 修复：解析 Tencent upload 返回的 `operation_publishmood_rsp`，记录 `tid/msg/verifyurl`；当 `ret` 非 0 时直接报出服务端发布失败，当返回 `tid` 时优先用该 fid 做详情验证，加快真实视频 feed 确认。
- 修复：daemon 原生视频发布的 `publishmood` 业务体对齐 Android 录制视频路径，默认写入 `mediatype=1`、`mediabittype=1` 和 `extend_info["has_video"]="1"`，避免只拿到 `sVid` 但 feed 不按视频动态落库。
- 修复：OneBot 客户端调用不再只识别 `call_action`，同时兼容 `call_api`、`call_action(action, params)`、`call_action(action=..., params=...)`，并从 AstrBot 上下文按通用 OneBot 平台别名获取客户端，NapCat/LLOneBot/LLBot 只是重点适配实现而不是唯一目标。
- 修复：引用视频发布会按 OneBot `get_msg` 的 `message_id/id`、字符串/整数参数变体依次获取原消息，并兼容 `bot.api.call_action` 与平台 `get_client()` 暴露的客户端，避免只回复视频时 payload 为空而报“说说内容或图片/视频不能为空”。

- 修复：引用视频或 Pages 上传视频落到无扩展名临时文件时仍按视频保留，避免媒体被丢弃后误报“说说内容或图片不能为空”。
- 修复：Pages 发布入口支持视频上传、视频预览和视频-only 发布，上传按钮与提示从图片扩展为图片/视频。
- 新增：daemon 原生视频直发优先使用 Qzone H5 `sliceUpload/FileUploadVideo`，直接复用已绑定 Cookie 的 `p_skey` 上传视频取得 `sVid`，再通过 Web 视频 `richval` 调 `emotion_cgi_publish_v6` 发布真实视频说说。
- 新增：H5 视频分片上传使用 `filename="blob"` 且默认 `Content-Type: application/octet-stream` 的 multipart `data` 片段；若接口返回 `-115`，自动后备重试无 part `Content-Type` 形态；发布成功前继续轮询最近动态验证同一 `sVid`。
- 变更：QQ upload A2/vLoginData 材料和 `/qzone autovideoauth` 变为旧 Tencent upload SDK 后备；已有 Qzone Cookie/`p_skey` 时不再因 OneBot 没有返回 A2 材料而阻止 daemon 原生视频直发。
- 新增：daemon 原生视频直发补齐 Android 双腿上传链路：`video_qzone` 上传视频取得 `sVid` 后，继续用 `pic_qzone` 上传视频封面，并携带 `vid/clientkey/mobile_fakefeeds_clientkey/mix_*` 字段触发真实视频动态。
- 新增：daemon 原生视频发布成功前会轮询最近动态并验证同一 `sVid`；只拿到上传响应但没有生成 feed 时会报错，不再宣称发布成功。
- 修复：单个视频在缺少 QQ upload 登录材料时不再静默提取封面并按图片说说发布；daemon 会阻止视频帧替代发布并提示绑定 `/qzone videoauth` 或 `/qzone autovideoauth`。
- 修复：Linux CI 上 `.3gp` 被 `mimetypes` 识别为 `audio/3gpp` 时仍归一化为 QQ 空间视频媒体。
- 变更：完全移除运行时 QQ/QQNT 客户端视频发布 handoff；单个本地视频只交给 daemon 后台发布链路处理，缺少 QQ upload 登录材料时阻止发布并提示绑定。
- 新增：`/qzone videoauth` 与 `/qzone autovideoauth` 可把 QQ upload 二进制登录材料写入 daemon 状态，发布前也会尝试从 OneBot 自动获取并绑定。
- 修复：`/qzone autovideoauth` 改为面向通用 OneBot 协议端探测视频上传材料，新增 `get_login_misc_data key=a2/vLoginData` 等通用扩展 action，并兼容 LLOneBot `llonebot_debug -> nodeIKernelLoginService/getLoginMiscData`；NapCat/LLOneBot/LLBot/其他 OneBot 实现返回的 Cookie/CSRF 或 `clientkey/keyIndex` 仍会被拒绝当作 A2。
- 测试：新增客户端 handoff 移除、daemon 原始视频接收、OneBot 上传材料自动绑定回归用例。

## v0.6.8 - 2026-06-01

- 新增：daemon 原生视频直发接入 `UploadVideoInfoReq.vBusiNessData`，按 QQ 空间录制视频说说路径编码 `UniAttribute(hostuin, publishmood)`，并使用 `iBusiNessType=1` 随 Tencent upload 控制包提交。
- 新增：支持通过 `QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64`、`QZONE_VIDEO_UPLOAD_LOGIN_KEY_B64` 和 `QZONE_VIDEO_UPLOAD_TOKEN_*` 提供 QQ upload 二进制登录材料；未配置时的旧封面图回退行为已被后续版本废除。
- 修复：有 daemon 上传凭据时，插件入口会把原始视频交给本地 daemon，发布结果渲染仍使用视频封面。
- 文档：更新 daemon 原生视频逆向记录，明确普通视频说说的发布体嵌在上传业务数据中，`rptVSUploadFinish` 更像上传完成上报，不再把“最终发布 RPC”列为当前主阻塞点。
- 测试：补充 publishmood OldUniAttribute 编码、环境凭据解析、daemon 直发分支和插件入口选择的回归用例。

## v0.6.7 - 2026-06-01

- 新增：实现 Tencent upload SDK 所需的最小 JCE/Tars 编解码层，覆盖 `AuthToken`、`FileControlReq`、`FileBatchControlReq/Rsp`、`FileUploadReq/Rsp`、`UploadVideoInfoReq/Rsp` 等已确认 schema。
- 新增：`QzoneTencentVideoUploader` 支持按 `video_qzone` 协议发送控制包和分片包，并能解析最终 `sVid/iBusiNessType/vBusiNessData` 上传响应；没有 `vLoginData` 时会明确报缺少 QQ upload 二进制登录材料。
- 文档：更新 daemon 原生视频发布逆向记录，标明 JCE 与 socket 上传层已落地，剩余阻塞点收敛为 `vLoginData/vLoginKey` 来源和消费 `sVid/vBusiNessData` 的最终 QQ 空间发布 RPC。
- 测试：补充 JCE 字段 tag、嵌套 map/struct、上传响应解码、分片上传 fake socket 流程和 SHA1 校验回归用例。

## v0.6.6 - 2026-06-01

- 新增：沉淀 QQ 空间 daemon 原生视频直发的 Tencent upload SDK 协议骨架，记录 `video_qzone`、`video.upqzfile.com:80`、控制包 cmd=1、分片包 cmd=2、PDU header offset 和 `0x04/0x05` 帧格式。
- 新增：`qzone_bridge.tencent_upload` 提供可测试的 PDU 编解码与 daemon 原生视频上传探针，后续补 JCE/Tars 和 QQ upload 登录材料时可直接接入。
- 文档：更新 daemon 原生视频发布逆向记录，明确现阶段真正缺口是 JCE/Tars schema、`vLoginData/vLoginKey`/`AuthToken` 来源，以及成功上传后消费 `sVid/vBusiNessData` 的最终发布 RPC。
- 测试：补充 Tencent upload PDU round-trip、畸形帧拒绝和 daemon 原生视频协议探针回归用例。

## v0.6.5 - 2026-06-01

- 修复：aiocqhttp/OneBot 视频引用继续按协议字段兼容，不绑定 NapCat；`get_file` 现在会兼容 LLOneBot 的 `base64` 返回，以及 `file_id/file/fid/id`、OneBot v12 风格 `type=path/url` 等常见文件参数组合。
- 修复：群/私聊文件直链兜底会同时尝试 `group_id/group`、`busid` 和 `file_id/file` 参数形态，优先覆盖 LLOneBot、NapCat、Shamrock 等协议端差异。
- 新增：OneBot 返回 base64 视频时会先落盘到插件缓存，再按正常视频流程提取封面和渲染卡片，避免只返回 base64 时误报“视频文件不存在”。
- 测试：补充 LLOneBot `get_file` base64 视频、base64 视频本地化、引用视频 fallback 参数组合回归用例。

## v0.6.4 - 2026-06-01

- 修复：aiocqhttp/OneBot 视频引用不再只按 NapCat 字段解析，新增兼容 `download_url`、`file_url`、`media_url`、`cdn_url`、`file_path`、`absolute_path`、`local_path` 等协议端字段，覆盖 LLOneBot、NapCat、Shamrock 等常见返回组合。
- 修复：引用视频只有 `file_id` 或裸 `file=xxx.mp4` 时仍不会误当成本地路径；会优先使用真实 URL/可读本地文件，再走 `get_file`、`get_group_file_url`、`get_private_file_url` 兜底。
- 文档：新增 QQ 空间 daemon 原生视频发布逆向记录，明确当前真视频直发需要复现 QQ 客户端的 `QZoneVideoUploadTask` / Tencent upload SDK 上传协议；旧的 daemon 视频封面图回退已被后续版本废除。
- 测试：补充协议端 `download_url`、对象 `file_url`、`get_file` 返回下载地址、群文件 URL 返回地址等回归用例。

## v0.6.3 - 2026-06-01

- 修复：引用视频时不再把不存在的 NTQQ/OneBot 本地缓存路径（例如 `D:Documents\Tencent Files\...\Video\...\xxx.mp4`）直接当作可提取封面的文件；只有当前机器确实可读的本地视频路径才会进入发布流程。
- 修复：aiocqhttp/OneBot 视频段同时带有坏 `path` 与可用 `url`、`file_id`、组件 `convert_to_file_path()` 时，现在会继续走可用来源，兼容 llbot、NapCat、Shamrock 等不同协议端字段组合。
- 测试：补充不存在视频路径、坏路径优先级、`get_file` fallback、AstrBot `Reply.chain` 视频组件转换等回归用例，防止再次出现“视频文件不存在，无法提取封面”。

## v0.6.2 - 2026-06-01

- 修复：引用视频现在按 aiocqhttp/OneBot 通用消息结构解析，不再只依赖某一个协议端；支持从结构化消息、CQ/raw_message、AstrBot `Reply.chain` 视频组件和 `get_msg` 返回体中提取真实视频源。
- 修复：当 llbot、NapCat、Shamrock 等协议端只返回 `file=xxx.mp4`、`file_id` 或 `empty` 占位字段时，不再把裸文件名误当成本地路径；会优先使用真实 `url/path`，再尝试 OneBot `get_file`、群/私聊文件 URL 扩展补全。
- 修复：`type=file` 的 mp4/video MIME 附件会按视频处理，不再拼成 `[文件:xxx]` 文本写进说说；daemon 直发视频也会先本地化视频源再提取封面。

## v0.6.1 - 2026-06-01

- 修复：引用 NTQQ/OneBot 视频时会优先补查引用消息并读取真实视频段；若平台只返回视频 URL，会先下载到插件缓存再提取封面，避免把 `file=xxx.mp4` 文件名误当成本地路径导致“视频文件不存在，无法提取封面”。
- 修复：本地视频路径恢复改为通用归一化（盘符斜杠、`file://` 路径、换行/制表符转义），不再扫描固定 Tencent 目录。

- 新增：发说说支持引用本地视频消息，兼容 mp4、mov、mkv、webm、avi、flv、3gp 等常见格式；早期单个本地视频会优先唤起 QQ/QQNT 原生 `mqqapi://qzone/publish` 视频发布窗口（当前运行路径已废除该客户端 handoff）。
- 新增：早期版本在原生视频入口不可用时曾使用 ffmpeg 提取视频封面并按图片发布；该视频封面替代路径已被后续版本废除。
- 优化：发布结果渲染会使用视频封面并叠加播放标识，管理员可直接确认本次引用的视频内容。
- 修复：视频消息不再被拼成 `[视频:xxx] 本地路径` 写进说说正文，避免泄露本地缓存路径并导致发布内容异常。

## v0.6.0 - 2026-06-01

- 新增：支持 Google News RSS 新闻自动说说，可配置中国新闻、国际新闻、混合范围、关键词和自定义 Google News RSS 地址，由 LLM 改写成原创短评后定时发布。
- 新增：提供 `新闻说说预览 [中国/国际/混合]` 管理员命令，可先查看候选新闻和生成文案，不会直接发布。
- 新增：提供 `新闻说说` 指令组，支持按自定义数量获取并缓存排序后的候选新闻，再按序号选择新闻交给 LLM 生成原创说说并发布。
- 新增：Pages 图片上传支持文件桥接失败后的 JSON/base64 回退，并可在插件数据目录落地临时上传令牌，避免大图直接塞进发布请求导致失败。
- 优化：Pages 会跟随 AstrBot WebUI 主题上下文切换明暗色，移动端详情改为抽屉交互，并补充 toast、骨架屏、字数提示和更稳定的多媒体预览。
- 优化：动态流分页使用带来源的安全游标，兼容现代接口、个人空间 legacy 列表和好友最新动态 legacy 列表，并过滤重复动态、官方/广告动态和头像误判图片。
- 优化：详情接口超时时会先显示缓存动态；状态恢复和预加载失败时页面仍会返回可渲染的最小状态。
- 修复：Pages 删除说说会携带动态真实发布时间，并在前端改为二次点击确认，降低误删和 QQ 空间删除失败概率。
- 修复：页面发布空内容空图片会直接拦截，大图上传请求体和 daemon 接收上限同步放宽，图片内容会校验真实格式和最小尺寸。
- 修复：点赞、评论和详情会复用动态流里的 `curkey`、`unikey`、`busi_param` 等动作元数据，减少真实 QQ 空间数据结构下的操作失败。
- 优化：daemon 独立日志会压低 httpx/httpcore/aiohttp.access 噪音，便于排查真正的插件问题。
- 优化：新闻自动发布会记录已使用新闻、每天最多发布一次，并对过度接近新闻标题的生成内容进行重试或跳过，减少直接搬运新闻标题的风险。
- 优化：配置页面里的英文说明已改为中文，便于在 AstrBot 管理界面直接理解各项配置。
- 修复：AstrBot 热加载后如果复用了旧版 `qzone_bridge.llm` 或 `qzone_bridge.settings`，新闻说说预览可能报缺少 `generate_news_post_text`，新闻定时 cron 也可能无法注册；现在会检测新闻相关桥接契约并强制重新导入。
- 修复：插件生命周期内已有普通发说说或自动评论定时任务时，后续补充的新闻定时任务现在也会注册，不再被已有任务提前返回挡住。
- 测试：补充 Pages 上传、分页、详情降级、删除、主题同步、解析兼容和 daemon 游标行为的回归测试。

## v0.5.1 - 2026-05-30

- 修复：收到消息概率触发自动评说说后，反馈图片现在会发送到当前触发会话，不再误发到管理群或管理员私聊。
- 修复：消息触发的自动评说说会复用评说说卡片渲染，并在图片中展示原说说和本次评论内容，不再只显示原说说。
- 优化：同步更新自动评论反馈相关配置说明，区分定时任务管理员反馈和消息触发会话反馈。

## v0.5.0 - 2026-05-29

- 新增：`pages/qzone` AstrBot Pages 页面端能力，支持动态流浏览、详情查看、发布说说、图片上传、点赞、评论、回复，以及安全删除自己的说说。
- 新增：`qzone_bridge/page_api.py` 页面后端接口与控制器接线，使用不透明说说标识、脱敏返回结构，并兼容 AstrBot Pages bridge。
- 新增：`qzone_bridge/auto_comment.py` 自动评论流水线，补齐分阶段判断、推理、执行以及去重持久化相关的命令、配置和运行时接线。
- 优化：本地 daemon 兼容性、页面与后端健康检查、插件侧安全加固，以及覆盖状态、列表、详情、回复、上传等流程的页面回归测试。
- 优化：WebUI 页面体验，重构三栏布局、详情与回复交互、圆形头像、多图按数量自适应排版，以及混合比例图片的展示效果。

## v0.4.3 - 2026-05-27

- Added: AstrBot WebUI Pages experience under `pages/qzone`, with feed browsing, publishing, image upload, detail view, likes, comments, replies, and self-post deletion.
- Added: Page backend APIs that reuse the existing daemon/controller path while redacting raw Qzone internals from the browser.
- Added: WebUI-specific regression coverage for raw-field redaction, pending like verification, sanitized publish flow, and delete safeguards.

## v0.4.2 - 2026-05-23

- 修复：开启定时任务管理员反馈后，如果没有单独配置管理群或插件管理员，会自动使用 AstrBot 全局管理员作为私聊通知目标，避免任务已成功但管理员收不到渲染图。
- 优化：管理员通知会记录渲染结果、发送目标和跳过原因；管理群发送失败时会继续尝试管理员私聊，便于定位配置或 OneBot 发送问题。
- 兼容：管理员通知支持 OneBot 客户端直接发送和 `bot.api.call_action` 两种发送方式，提升不同 aiocqhttp 运行环境下的可用性。

## v0.4.1 - 2026-05-23

- 新增：定时自动发布说说成功后，可向管理员或管理群发送 QQ 空间风格发布结果图，方便确认自动任务实际发布内容。
- 新增：定时自动评论支持配置每次处理的最新说说条数，可一次评论多条好友最新动态。
- 优化：定时自动评论会跳过自己的动态、已处理过的动态以及已经由当前账号评论过的动态，避免重复评论和打扰。
- 修复：定时自动评论改为读取好友最新动态流，不再因为误把动态流当成自己的空间内容而出现“没有效果”的情况。
- 优化：定时自动评论成功后会向管理员发送目标说说卡片，并在卡片结果中展示本次评论内容。
- 优化：定时发布、定时评论的启动、跳过、成功和下一次运行信息会写入 AstrBot 日志，方便在后台确认任务状态。

## v0.4 - 2026-05-23

- 新增：安装、更新或 AstrBot 启动后，插件会自动尝试绑定 QQ 空间；遇到平台连接未就绪或临时网络波动时，会自动重试最多 3 次，减少首次使用前的手动配置。
- 优化：已存在可用登录状态时会直接复用，不会重复绑定；QQ 空间服务也会在更新后自动切换到当前版本，避免旧服务进程继续使用过期解析逻辑。
- 修复：看说说时，图文说说现在会同时展示文字与图片；同一张 QQ 空间图片的预览图、高清图和原图只会合并显示一次，不再重复铺满九宫格。
- 修复：纯文字说说不会再误显示上一条图文说说的图片，连续浏览多条说说时内容归属更准确。
- 修复：说说卡片会优先使用 QQ 空间返回的真实发布时间；当列表或详情数据中时间位于 `comm.time`、`cell_comm.time` 等字段时，也能正确识别，不再错误显示当前时间或“未知时间”。
- 优化：单图说说卡片采用更紧凑的自适应布局，短文字 + 单张图片会保持图文同卡展示，整体尺寸更接近 QQ 空间动态流的阅读体验。
- 增强：提升 QQ 空间多种真实数据结构的兼容性，支持从旧版说说列表、说说详情、图文数据、HTML 图片字段和协议相对地址中稳定读取正文、图片和发布时间。
- 增强：状态信息会带上服务版本和内部协议版本，插件更新后能更可靠地判断当前服务是否需要刷新。
- 安全：自动绑定仍只使用当前 AstrBot / OneBot 环境提供的能力；插件不会内置默认 Cookie，也不会在更新日志或配置说明中写入登录凭据。

## 历史版本

- 早期版本已支持看说说、读说说、评论、点赞、发说说、AI 写说说、删除、回评等 QQ 空间操作。
- 支持投稿、匿名投稿、撤稿、稿件审核、自动发说说、自动评论、评论后点赞和管理员反馈。
- 支持 OneBot v11 / aiocqhttp 自动 Cookie 获取，也支持手动 Cookie 绑定。
- 支持独立服务处理 QQ 空间请求、Cookie 管理、图片处理和结果渲染。
- 支持说说卡片、评论卡片和发布结果渲染，并提供面向管理员的 AI 工具调用能力。
