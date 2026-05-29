# QQ空间 Pages 问题记录

这个文件记录 WebUI Pages 功能实施和验证时遇到的问题、根因、修复方式和回归用例，后续维护同类功能时先看这里。

## 2026-05-27 实施阶段

### 本地测试环境没有 quart

- 症状：当前插件测试环境可以导入 `aiohttp` 和 `pytest`，但没有 `quart`；如果在模块顶层强依赖 `quart.request/jsonify`，单测导入会失败。
- 根因：AstrBot WebUI 运行时提供 Web API 所需环境，但插件仓库的本地测试依赖没有直接安装 quart。
- 修复：`main.py` 对 `quart` 做可选导入；真实 WebUI 中返回 `jsonify` 响应，本地测试中回退为普通 dict。
- 回归用例：`python -m py_compile main.py qzone_bridge/page_api.py` 和 Page API 单测必须能在未安装 quart 的环境通过。

### 浏览器不能接触 daemon secret 或 raw QQ空间字段

- 症状：如果前端直连本地 daemon，必须暴露 `X-Qzone-Secret`，也容易把 `fid/raw/curkey/unikey` 带到页面。
- 根因：AstrBot Pages 的安全模型要求 Page 通过 bridge 访问插件后端，不能绕过 Dashboard 鉴权。
- 修复：新增 `qzone_bridge/page_api.py`，前端只拿到脱敏 `id`、展示内容和统计信息；点赞、评论、删除都通过 `id` 回到后端解码执行。
- 回归用例：feed/detail 响应不得包含 `raw`、`fid`、`curkey`、`unikey`、`busi_param`、Cookie 或 daemon secret。

### Base64 编码的 id 仍然会泄漏 fid

- 症状：第一轮实现把 `{hostuin, fid, appid}` 做成 base64-url 字符串返回给前端，虽然字段名不叫 `fid`，但浏览器可以直接解码拿到真实 fid。
- 根因：编码不是脱敏；只要令牌包含可逆载荷，就仍然属于内部字段泄漏。
- 修复：改成服务端内存中的不透明随机 token，`fid` 只保存在 `QzonePageApi` 的后端映射中；页面刷新后如果 token 失效，会提示刷新列表重试。
- 回归用例：Page feed 测试必须确认 `post.id` 不包含真实 `fid`、`curkey` 等内部值，同时仍能通过后端映射执行详情/点赞/评论。

### 点赞读回延迟不能误报失败

- 症状：QQ空间已接受点赞请求，但读取状态可能短时间未同步。
- 根因：历史上把 `verified=false` 当成失败会造成“实际成功却报错”的坏体验。
- 修复：Page API 保留 `ok=true`，并把 `verified=false` 映射为 `operation_status=accepted_pending_verification`；前端提示“QQ空间已接受操作，读回状态可能稍后同步”。
- 回归用例：`page/like` 在 controller 返回 `verified: false` 时仍返回成功。

### WebUI 发布不能复活 qzone post 前缀泄漏

- 症状：历史命令链路曾出现 `/qzone post` 或 `qzone post` 被一起发到说说里的问题。
- 根因：聊天命令需要剥离命令前缀，WebUI 文本框则应被视为已经是正文，两种入口混用会出错。
- 修复：WebUI 发布直接调用 `controller.publish_post(..., content_sanitized=True)`，不再经过命令文本解析。
- 回归用例：Page API 发布 `qzone post literal` 时传给 controller 的内容必须保持原样，且 `content_sanitized=True`。

### 插件文件更新后仍显示旧 daemon 版本

- 症状：实际插件目录更新后，WebUI 或 `/qzone status` 仍可能显示旧的 daemon 版本号。
- 根因：controller 只检查 `bridge_api_version` 是否兼容，没有检查运行中的 daemon `daemon_version` 是否等于当前插件版本；API 版本未变时会复用旧 daemon。
- 修复：`_health_payload_is_compatible()` 同时要求 `daemon_version == BRIDGE_VERSION`，版本不一致时关闭旧 daemon 并启动新进程。
- 回归用例：插件版本升级但 `BRIDGE_API_VERSION` 不变时，重载后 daemon 也必须切换到新版本。

### Page API 只记录 Permission denied，缺少 traceback

- 症状：WebUI Page 能打开，但页面请求失败；AstrBot 只打印 `qzone page api failed: [Errno 13] Permission denied`，无法定位具体文件或调用点。
- 根因：Page API 的统一异常处理只记录异常字符串，没有 `exc_info`、路由和 callback 名称；Windows 下文件/进程权限问题经常只显示 `Permission denied`。
- 修复：`_page_json()` 记录路由、callback 和完整 traceback；`PermissionError` 返回脱敏的 `PAGE_PERMISSION_DENIED`，不把本地路径或内部细节暴露给前端。
- 回归用例：后续 Page API 异常日志必须能直接看到堆栈；前端只能看到安全错误码和提示。

### daemon.log 被占用或拒绝访问时不应拖垮 Page

- 症状：运行中旧 daemon/重复 AstrBot 进程可能持有 `daemon.log`，新版本在恢复 daemon 时如果打开日志失败，会把系统 `PermissionError` 透出到 Page。
- 根因：`_spawn_daemon()` 把 `daemon.log` 当成启动前置条件，日志不可写就无法启动 daemon；这类故障在 Windows 文件锁场景更容易出现。
- 修复：新增 daemon 日志 fallback：优先写插件数据目录的 `daemon.log`，失败时写系统临时目录 `astrbot_qzone_daemon_logs`，再失败才退到 `os.devnull`。
- 回归用例：`daemon.log` 是不可写路径时，`_spawn_daemon()` 仍能调用 `subprocess.Popen`，且 stdout/stderr 指向 fallback 日志。

### 本地文件已是 0.4.3，但运行时仍启动 0.4.2 daemon

- 症状：插件目录中的 `metadata.yaml`、`qzone_bridge/__init__.py` 已经是 `0.4.3`，但正在运行的 AstrBot 仍在 21:14 启动 `--version 0.4.2` 的 daemon。
- 根因：AstrBot 主进程在部分文件同步前已经导入旧版 `qzone_bridge`，后续只复制文件或 WebUI 软重载没有清掉进程内旧模块；旧主进程继续用内存中的 `BRIDGE_VERSION=0.4.2` 启动 daemon。
- 修复：本地更新后必须停止旧 daemon/重复 AstrBot 进程，清理插件 `__pycache__`，再让 AstrBot 主进程重新导入插件；代码侧保留 daemon 版本兼容检查，防止重启后继续复用旧 daemon。
- 回归用例：更新后检查正在运行的 `daemon_main.py --version`、`state.json.runtime.version` 和 WebUI 插件版本必须同时是目标版本。

### Page 一直停在“正在连接 / 未读取 / 等待状态”

- 症状：Pages 页面已经能打开，后端 `page/status`、`page/feed` 也能返回成功，但 iframe 内 UI 一直停留在初始文案，像是没有读到状态。
- 根因：AstrBot WebUI 的 Pages bridge 会把插件 API 响应的 `response.data.data` 再剥一层传给 iframe；插件前端仍按未剥开的 `{ok, data}` 判断，导致成功业务对象被误判为失败，状态渲染没有发生。
- 修复：前端统一归一化 bridge 回包，同时兼容 `{ok, data}`、AstrBot 已剥开的业务对象和 `status:error` 形状；`bridge.ready()`、API 请求、上传请求增加超时提示，避免再次无提示卡死。
- 回归用例：`tests/test_qzone_page_frontend.py` 用 Node 模拟 AstrBot 已剥开的 bridge 响应，要求状态、账号、动态列表和上传图片都能正常渲染。
