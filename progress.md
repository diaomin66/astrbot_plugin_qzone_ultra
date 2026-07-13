## 2026-07-13 - Task: 处理 Issue #88 随机偏移最大值建议
### What was done
- 核对现有配置已提供 0–3600 秒明确上限，保留该上限并明确显示在配置提示中，避免擅自扩大到会跨越下一调度周期的范围。
### Testing
- `python -m pytest tests/test_issue_regressions.py -q -p no:cacheprovider`：7 passed。
### Notes
- `_conf_schema.json`：明确随机偏移最大值与新的延迟语义。
- `docs/issue-fixes-v0.8.2.md`：记录定时偏移的正式使用规则。
- 回滚方式：回退本任务对应提交，或恢复上述文件中 `publish_offset` 的旧提示文字。

## 2026-07-13 - Task: 修复 Issue #95 LLM 工具结果直发用户
### What was done
- 将全部 8 个 Qzone LLM 工具统一改为向 Agent 返回文本，避免结果绕过 LLM 直接发送给用户。
### Testing
- `python -m pytest tests/test_issue_regressions.py -q -p no:cacheprovider`：覆盖列表工具返回值及全部工具协程类型，7 passed。
- `python -m pytest tests/test_security_hardening.py tests/test_issue_regressions.py -q -p no:cacheprovider`：332 passed。
### Notes
- `main.py`：统一 Qzone LLM 工具返回契约。
- `tests/test_issue_regressions.py`：新增 Agent 工具返回契约回归测试。
- `docs/issue-fixes-v0.8.2.md`：记录工具结果由 Agent/LLM 组织回复。
- 回滚方式：回退本任务对应提交；若局部回滚，恢复 8 个工具的旧 async-generator 出口并删除对应回归测试。

## 2026-07-13 - Task: 修复 Issue #97 同一时段连续自动发布
### What was done
- 将随机偏移从基准时间前后浮动改为基准时间后的随机延迟，移除过期目标被钳成 1 秒后执行的连发触发器。
### Testing
- `python -m pytest tests/test_issue_regressions.py -q -p no:cacheprovider`：验证 21:00 基准、1500 秒偏移在 20:40 计算时不会生成过期目标，7 passed。
- `python -m pytest tests/test_security_hardening.py tests/test_issue_regressions.py -q -p no:cacheprovider`：332 passed。
### Notes
- `qzone_bridge/scheduler.py`：随机偏移只在基准时间之后取值。
- `_conf_schema.json`：同步更新配置语义。
- `tests/test_issue_regressions.py`：新增定时偏移回归测试。
- `docs/issue-fixes-v0.8.2.md`：记录调度行为变化。
- 回滚方式：回退本任务对应提交；局部回滚可恢复 `randint(-offset, offset)` 与旧配置提示。

## 2026-07-13 - Task: 修复 Issue #98 daemon Secret 时序侧信道
### What was done
- 使用常量时间比较验证 daemon Secret，保留原有公开健康检查与未授权拒绝逻辑。
### Testing
- `python -m pytest tests/test_issue_regressions.py -q -p no:cacheprovider`：验证鉴权中间件使用 `hmac.compare_digest` 且不再使用字符串 `==`，7 passed。
- `python -m pytest tests/test_security_hardening.py tests/test_issue_regressions.py -q -p no:cacheprovider`：332 passed。
### Notes
- `qzone_bridge/daemon.py`：Secret 改用常量时间比较。
- `tests/test_issue_regressions.py`：新增鉴权实现回归测试。
- `docs/issue-fixes-v0.8.2.md`：记录安全行为。
- 回滚方式：回退本任务对应提交；不建议恢复普通字符串比较。

## 2026-07-13 - Task: 修复 Issue #99 自动评论可能评论自己的说说
### What was done
- 当前登录 QQ 状态探测失败时停止本轮自动评论并记录警告，不再以 `login_uin=0` 继续执行。
### Testing
- `python -m pytest tests/test_issue_regressions.py -q -p no:cacheprovider`：验证状态探测失败后不会进入详情或评论阶段，7 passed。
- `python -m pytest tests/test_security_hardening.py tests/test_issue_regressions.py -q -p no:cacheprovider`：332 passed。
### Notes
- `main.py`：自动评论身份探测改为失败关闭。
- `tests/test_issue_regressions.py`：新增身份探测失败回归测试。
- `docs/issue-fixes-v0.8.2.md`：记录失败关闭行为。
- 回滚方式：回退本任务对应提交；局部回滚可恢复异常后继续执行的旧逻辑。

## 2026-07-13 - Task: 修复 Issue #100 bind/autobind 展示陈旧状态
### What was done
- 绑定成功后的状态刷新失败会明确说明“绑定已完成、刷新失败”，并提示稍后执行 `/qzone status`，不再渲染绑定阶段旧快照。
### Testing
- `python -m pytest tests/test_issue_regressions.py -q -p no:cacheprovider`：分别覆盖 bind 与 autobind 刷新失败，7 passed。
- `python -m pytest tests/test_security_hardening.py tests/test_issue_regressions.py -q -p no:cacheprovider`：332 passed。
### Notes
- `main.py`：bind/autobind 状态刷新异常改为显式反馈。
- `tests/test_issue_regressions.py`：新增两个命令的失败反馈回归测试。
- `docs/issue-fixes-v0.8.2.md`：记录绑定成功与刷新失败的分离语义。
- 回滚方式：回退本任务对应提交；局部回滚可恢复刷新异常静默忽略的旧逻辑。

## 2026-07-13 - Task: 发布 v0.8.2
### What was done
- 将插件与 daemon bridge 版本统一更新为 0.8.2，并补充本版本更新日志。
### Testing
- `python -m pytest -q -p no:cacheprovider`：串行复跑 442 passed。
- `python -m ruff check .`：通过。
- `python -m compileall -q main.py daemon_main.py qzone_bridge tests`：通过。
- `python -m json.tool _conf_schema.json`：通过。
- 版本一致性脚本确认 `metadata.yaml`、bridge `__version__` 与 `RuntimeState` 均为 0.8.2。
- 首轮并行执行测试与 `compileall` 时出现 1 次模块导入路径隔离误判；失败用例单独复跑通过，随后串行全量复跑 442 项全部通过。
### Notes
- `metadata.yaml`：插件版本更新为 0.8.2。
- `qzone_bridge/__init__.py`：bridge 版本更新为 0.8.2。
- `qzone_bridge/models.py`：运行时状态默认版本更新为 0.8.2。
- `CHANGELOG.md`：新增 v0.8.2 更新记录。
- `progress.md`：新增各 issue 与发布任务记录。
- 回滚方式：回退本任务对应提交，或将上述版本字段恢复为 0.8.1 并删除 v0.8.2 日志条目。

## 2026-07-13 - Task: 修复 QQ 空间 Page 媒体读取、播放与原文件下载
### What was done
- 规范化说说正文、昵称表情及图片、视频、音频、普通附件的读取结果，避免 QQ 表情和点赞人表情污染图片与正文。
- 视频预览固定使用 QQ 原视频源，封面仅作 poster；下载通过 AstrBot Bridge 与后端流式响应输出原始文件。
- 修复下载路由直接返回异步迭代器三元组造成的 HTTP 500，改为 Quart 流式 Response，并保留 Range、MIME、长度及附件文件名。
- 更新媒体使用文档、问题记录、版本号与更新日志，并同步至指定本地 AstrBot 插件目录。
### Testing
- `python -m pytest tests\test_page_api.py tests\test_qzone_page_frontend.py -q -p no:cacheprovider`：65 passed。
- `node --check pages\qzone\app.js`、`python -m ruff check .`、`python -m compileall -q main.py daemon_main.py qzone_bridge tests`、`git diff --check`：全部通过。
- 真实 QQ 视频：HTTP Range 返回 206，H.264 High + AAC LC；Edge Headless 达到 `readyState=4`、无媒体错误。
- 真实 AstrBot Bridge：`page/status`、`page/feed` 返回 200；`page/media` 返回原始图片字节、正确 MIME 与 `Content-Disposition: attachment`，未再出现 500。
- 全量测试首次执行超过 240 秒工具时限，因此以受影响的 65 项定向回归和静态检查作为本轮可信验证；未将超时表述为通过。
### Notes
- `main.py`：注册媒体路由并使用 Quart 流式 Response 返回原文件。
- `qzone_bridge/page_media.py`：新增安全、支持 Range 的远程媒体流。
- `qzone_bridge/page_api.py`：注册脱敏媒体引用并输出统一媒体对象。
- `qzone_bridge/social.py`：规范提取表情与多类型媒体。
- `pages/qzone/app.js`：安全渲染表情、多类型预览及 Bridge 下载。
- `pages/qzone/style.css`：增加媒体和附件展示样式。
- `tests/test_page_api.py`、`tests/test_qzone_page_frontend.py`：增加媒体、表情、下载及前端回归。
- `docs/qzone-pages-media.md`、`docs/qzone-pages-issue-log.md`：记录媒体能力、限制及本次根因。
- `metadata.yaml`、`qzone_bridge/__init__.py`、`qzone_bridge/models.py`、`CHANGELOG.md`：更新为 0.9.0 并记录变更。
- `progress.md`：追加本任务实施和验证证据。
- 回滚方式：回退本任务对应提交；本地 AstrBot 可从 `core\data\plugin_backups\astrbot_plugin_qzone_ultra_media_proxy_20260713_095208` 恢复后重启。

## 2026-07-13 - Task: QQ 空间 Page 媒体修复最终全量复核
### What was done
- 在定向回归与真实环境验证之后，再执行一次不受前次工具时限影响的完整测试集。
### Testing
- `python -m pytest tests -q -p no:cacheprovider`：454 passed，耗时 157.36 秒。
### Notes
- `progress.md`：追加最终全量测试结果；前一条中的“首次执行超过工具时限”保留为真实历史记录。
- 回滚方式：删除本条追加记录；代码回滚仍使用上一任务记录中的提交或本地插件备份。
