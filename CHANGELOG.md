# 更新日志

本文件记录 QQ 空间 Ultra 的重要版本变化。版本号遵循语义化版本思路：用户可见的新能力使用次版本号，兼容性修复和小范围修正使用补丁号。

## [0.3.0] - 2026-05-23

### 新增

- 新增安装、下载后重载、AstrBot 启动后的自动 QQ 空间绑定流程。插件会在初始化阶段、AstrBot 加载完成阶段，以及 aiocqhttp / OneBot 客户端稍后可用时主动尝试 autobind。
- 新增受管后台 autobind bootstrap task，启动期自动绑定不再阻塞 AstrBot 插件加载，也能在插件终止时被统一取消。
- 新增自动绑定三次重试机制。OneBot 暂时没有返回可用 Cookie、Cookie 写入短暂失败或平台刚启动未就绪时，插件会自动重试后再暴露失败。
- 新增 late OneBot client 场景覆盖：如果插件加载时还没有拿到 aiocqhttp 客户端，后续首次捕获到 OneBot 事件后仍会补触发后台 autobind。
- 新增针对自动绑定生命周期的回归测试，覆盖重试成功、重试耗尽、启动期非阻塞调度、失败后再次触发、忽略事件仍可后台绑定、终止清理任务、已有 Cookie 直接复用等路径。

### 优化

- 将版本号从 `0.2.1` 调整为 `0.3.0`。这次更新改变了插件安装和启动后的默认体验，属于用户可感知的新能力，而不是单纯补丁修复。
- 优化 `auto_bind_cookie` 配置语义：配置提示现在明确说明启动/加载触发时机、三次重试和失败后的手动兜底命令。
- 优化 README 的 Cookie 绑定说明，明确 `/qzone autobind` 之外，插件也会在安装、重载、启动和 OneBot 客户端晚到时自动尝试绑定。
- 保留手动 `/qzone autobind` 与 `/qzone bind <cookie>` 作为显式恢复路径，避免平台无法提供 Cookie 时用户无路可走。
- 保留已有 Cookie 的 daemon 预热行为：如果历史数据或 `cookies_str` 已经提供登录态，即使 OneBot 客户端尚未捕获，也仍可按原逻辑预热本地 daemon。

### 修复

- 修复生命周期风险：自动绑定不再直接 await 网络获取与重试流程，避免插件初始化或 AstrBot loaded hook 被 Cookie 获取过程拖住。
- 修复后台 autobind 状态问题：首次后台绑定失败后不会永久标记为已尝试，后续 aiocqhttp 事件仍可再次触发绑定。
- 修复概率读空间命中但事件被忽略时的绑定遗漏：忽略群或忽略用户的消息不会触发同步读空间，但仍会用于补调度后台 autobind。
- 修复重复绑定风险：概率读空间路径已经会同步检查 Cookie 时，不再额外叠加后台 autobind，避免两轮重试抢同一个 Cookie 锁。
- 修复插件终止清理遗漏：新增的 autobind bootstrap task 会和定时任务、daemon 预热任务一起在 `terminate()` 中取消。

### 安全与可靠性

- 自动绑定仍走现有 AstrBot / OneBot 客户端能力，不新增静态 Cookie、默认 Cookie 或硬编码密钥。
- 手动绑定命令仍保持管理员权限检查，自动绑定也只复用插件已经捕获到的本地平台客户端。
- 已有 Cookie 状态可用时会直接复用，不重复向 OneBot 请求 Cookie，也不重复写入绑定状态。
- Cookie 相关日志仍走现有脱敏路径，不在 README、配置默认值或更新日志中写入任何真实敏感字段。

### 测试与验证

- `python -m py_compile main.py qzone_bridge\onebot_cookie.py qzone_bridge\controller.py qzone_bridge\daemon.py qzone_bridge\settings.py`
- `python -m pytest -q`

本地验证通过；测试过程中只有 Windows `.pytest_cache` 无写权限 warning，不影响测试结果。

## 历史能力基线（0.2.x 及以前）

- 已支持 QQ 空间中文命令体系，包括看说说、读说说、评论、点赞、发布、AI 写说说、删除、回评、投稿审核等日常操作。
- 已支持独立本地 daemon 承载 QQ 空间请求、Cookie 管理、图片处理和渲染逻辑，降低 AstrBot 主进程负担。
- 已支持 OneBot v11 / aiocqhttp 自动 Cookie 获取和手动 Cookie 绑定双路径。
- 已支持 LLM tools，让模型可以在管理员授权边界内查看、发布、评论、点赞和删除 QQ 空间内容。
- 已支持发布结果、说说卡片、评论卡片等渲染能力，并持续修复热加载、昵称、时间和评论区域兼容问题。
- 已支持自动发说说、自动评论、评论后顺手点赞、管理员反馈和表白墙/投稿审核工作流。
