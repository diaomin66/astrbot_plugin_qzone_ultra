# QQ空间 Pages 后端体验优化记录 2026-05-28

## 按钮点击后会卡一下

- 症状：点赞、评论、回复、发布在点击后会先等待后端状态恢复、daemon 探活或 QQ 空间读回校验，UI 体感像卡住。
- 根因：Page API 写操作先走 `_ensure_ready()`，随后 controller 请求 daemon 又走 `ensure_running()`；点赞还会做多次读回校验。
- 修复：Page 写操作改为本地 Cookie 快速校验，daemon 启动/探活交给 controller 请求层；Pages 点赞传 `fast=True`，先接受成功并更新 UI 与 daemon 缓存。
- 回归：`tests/test_page_api.py` 覆盖 Page 点赞必须传 `fast=True`，且 daemon state 为 `degraded` 时不会被前置 readiness gate 卡住。

## 插件加载和 Pages 首屏预热不足

- 症状：AstrBot 重载或插件更新后，Pages 首次打开可能一边加载页面一边启动 daemon，首个 feed/detail 请求偏慢。
- 根因：原有预热主要启动 daemon，没有在 Pages 入口和插件加载后预先拉取首屏 feed 填充 daemon 缓存。
- 修复：插件 initialize/loaded 和 Page status/feed/detail 等入口去重调度后台预热：刷新 Cookie、启动 daemon、预拉 active feed，并用 45 秒节流避免频繁请求 QQ 空间。
- 回归：预热任务失败只记 debug 日志，不阻塞 Page API 正常响应。

## feed 卡片与详情点赞状态不一致

- 症状：在预览卡片点赞后进入详情，或在详情点赞后回到列表，可能出现一边显示已赞、一边显示未赞。
- 根因：前端 feed/detail 曾经各自持有对象并分开更新；daemon fast like 更新 `feed_cache` 和 `recent_feed_entries` 时，如果二者指向同一个 `FeedEntry`，like_count 可能重复加减。
- 修复：前端统一通过 `updatePost/mergePost` 合并状态，并增加点赞并发保护；daemon fast like 只在 `liked` 真实变化时增减计数，并按对象 id 去重。
- 回归：同一 `FeedEntry` 同时位于 `feed_cache` 与 `recent_feed_entries` 时，fast like 第一次只 +1，重复点赞不再继续 +1。

## 多余换行与测试环境兼容

- 症状：部分说说或评论展示会保留多余空白/换行；Node 前端烟测环境缺少 `document.querySelector` 会导致 app 初始化失败。
- 根因：文本清洗只处理了部分行尾空格；前端初始化直接调用 DOM 查询 API，没有兼容最小测试 DOM。
- 修复：展示文本额外清理行首空白；DOM 查询增加 `queryOne/queryAll` 安全封装。
- 回归：`tests/test_qzone_page_frontend.py` 覆盖 AstrBot Pages bridge 解包和前端初始化。

## Pages 预热不能污染聊天侧“最近说说”

- 症状：如果后台预热调用普通 feed 列表接口，daemon 的 `recent_feed_entries` 会被 Pages 首屏数据覆盖，聊天命令或 LLM 工具里的“最新/第 N 条”可能指向页面预热过的内容。
- 根因：daemon 的 `list_feeds()` 默认会把当前列表写入全局 recent 引用缓存，这个缓存是聊天选择语义的一部分，不应该被 Pages 后台预热改写。
- 修复：feed 链路新增 `record_recent` 参数。聊天侧默认保持 `True`，Pages 和后台预热使用 `False`，只填充 `feed_cache`，不覆盖 recent 引用。
- 回归：`tests/test_page_api.py` 覆盖 Page feed 必须传 `record_recent=False`，以及 daemon 在 `record_recent=False` 时不覆盖原 recent 列表。

## 乐观评论/回复不能整表回滚

- 症状：用户在评论请求未完成时切换详情，或连续发送多条评论/回复，一个请求失败可能把另一条已经成功或仍在发送的评论覆盖掉。
- 根因：成功和失败分支读取 `state.selected.comments` 或旧 comments 整表回滚，依赖当前选中的详情面板，缺少按 temp id 的精确替换/删除。
- 修复：前端新增按 post id 查找当前目标说说，评论/回复成功只替换自己的临时 id，失败只删除自己的临时 id，并只在确实存在临时项时回退评论计数。
- 回归：后续修改 Pages 评论状态时，不能使用旧 comments 整表覆盖当前 post 状态。

## AstrBot 软重载后 page_api 子模块仍是旧构造函数

- 症状：插件加载失败，日志显示 `TypeError: QzonePageApi.__init__() got an unexpected keyword argument 'preload_scheduler'`，但磁盘上的 `qzone_bridge/page_api.py` 已经包含该参数。
- 根因：AstrBot 软重载重新导入了 `main.py`，但 Python 进程里的 `qzone_bridge.page_api` 旧模块仍留在 `sys.modules`；已有本地包自愈加载器只检查 renderer/social 等旧契约，没有检查 Pages 新增签名。
- 修复：把 `QzonePageApi.__init__(preload_scheduler=...)`、`QzoneDaemonController.list_feeds(record_recent=...)`、`QzoneDaemonService.list_feeds(record_recent=...)` 加入本地 `qzone_bridge` 契约检查。签名不匹配时自动驱逐旧 `qzone_bridge.*` 子模块并从当前插件目录重新导入。
- 回归：`tests/test_security_hardening.py::test_main_import_recovers_from_stale_page_api_constructor` 模拟旧构造函数缓存，要求重新导入 `main.py` 后拿到新 `QzonePageApi`。

## 评论回复按钮无反应、自己的评论昵称/布局异常、详情打开慢

- 症状：Pages 详情里点击评论的“回复”没有明显反应；自己刚发的评论或回复显示成 `QQ 号` 而不是昵称；有回复按钮和没有回复按钮的评论行位置不一致；点击“查看详情”需要等后端详情接口返回后才出现内容；左上角还保留了“QQ空间Ultra / daemon 已就绪”的品牌块。
- 根因：前端回复使用 `window.prompt()`，在 AstrBot WebUI iframe/WebView 里容易被拦截或没有可见反馈；评论布局用 `space-between`，右侧按钮会改变正文列位置；Page comment/reply 返回体没有当前登录作者，详情评论也没有用登录作者修正自己的昵称；详情打开流程先等待网络详情再渲染；头部品牌块和状态文案仍写在 HTML/JS 契约里。
- 修复：回复改为评论下方内联表单，发送后沿用现有乐观更新和精确回滚；评论行改成固定头像/正文/操作三列网格；Page comment/reply/publish/detail 返回或修正登录作者，前端维护 `knownAuthors` 并避免把当前用户回退成 `QQ <uin>`；详情先用 feed/selected 缓存即时渲染，再后台同步详情，并用请求序号避免旧响应污染新详情；删除品牌块，账号卡只显示昵称和小号 QQ 号。
- 回归：`tests/test_qzone_page_frontend.py` 覆盖无 `statusText` 初始化、品牌块删除、缓存详情即时渲染、回复不再调用 `window.prompt()`、内联回复表单和当前昵称复用；`tests/test_page_api.py` 覆盖 comment/reply/publish 返回当前作者以及详情里自己的评论昵称修正。

## 子审查发现：详情旧响应覆盖本地操作、昵称预热耦合渲染开关

- 症状：如果用户打开详情后马上点赞、评论或回复，慢返回的 `page/detail` 可能把本地刚更新的 `liked/stats/comments` 覆盖回旧状态；当 `render_publish_result=false` 时，Pages 当前账号昵称/头像预热可能被跳过。
- 根因：详情接口返回后直接浅合并整条 post，缺少“请求期间是否发生本地修改”的版本保护；账号资料复用了发布结果图片渲染的预热任务，而该任务受发布渲染开关控制。
- 修复：前端给每条说说维护 `localVersions`，本地点赞/评论/回复成功、失败、乐观阶段都会递增；详情返回时若版本变化，保留本地 `liked/stats/comments`，并把远端新增评论按 id 合并进来。回复提交阶段增加 `pendingReplies` 和 `replyDrafts`，慢请求时不会重复提交，失败后恢复回复框。后端新增独立登录资料预热路径，`render_publish_result=false` 时仍会通过 OneBot 快速补齐 `login_nickname/login_avatar`。
- 回归：`tests/test_qzone_page_frontend.py` 覆盖 stale detail 不能覆盖本地回复；`tests/test_security_hardening.py::test_page_status_profile_fetch_is_independent_from_publish_rendering` 覆盖关闭发布渲染时仍能补齐 Pages 登录昵称头像。

## 头像不显示、图片上传大小限制、命令无描述
- 症状：Pages 左上角账号头像和说说头像会显示为空或露出 `Avatar` 文本；超过旧 8MB/32MB 的图片会被插件本地拦截并提示“图片大小超过限制”；AstrBot 指令列表显示“无描述”。
- 根因：前端头像默认使用 `http://q1.qlogo.cn...`，在 WebUI 安全上下文里容易被混合内容/头像源失败拦截，且失败时没有回退；Pages API 有 8MB 固定限制，底层客户端对 base64/URL/本地图片还有 32MB 固定限制；命令函数没有 docstring，AstrBot 的 `get_handler_or_create()` 只能拿到空描述。
- 修复：头像统一改为 HTTPS QQ 头像源，增加 qlogo 备用源和首字母回退，不再渲染 `Avatar` alt 文本；移除插件侧固定图片大小限制，仅保留空内容和图片类型校验；为所有 AstrBot 命令补充简短中文 docstring，供命令列表读取。
- 回归：新增 `tests/test_command_descriptions.py`，并在 `tests/test_qzone_page_frontend.py`、`tests/test_page_api.py`、`tests/test_security_hardening.py` 覆盖 HTTPS 头像/失败回退、Pages 上传不限本地大小、底层 base64 不再按固定阈值拦截。
