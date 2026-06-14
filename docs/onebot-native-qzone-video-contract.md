# OneBot 原生 QQ 空间视频发布契约

日期：2026-06-06

本文档定义 AstrBot 插件与 OneBot 协议端之间的稳定边界，用于后台 daemon 视频说说直发，并确保最终动态是全部人可见。它不是标准 OneBot v11 action，而是 NapCat、LLOneBot/LLBot、Shamrock 或其它协议端可以实现的扩展 action 契约。

## 目标

- 协议端在自己的 NTQQ/QQ 运行进程内完成 QQ 空间视频发布，内部使用 A2、vLoginData、设备态或其它上传登录材料。
- AstrBot 插件只传入单个可信本地视频路径、正文和公开视频参数，不接收也不记录 A2/vLoginData。
- 协议端必须返回可验证的视频标识 `sVid`/`vid`；插件再让本地 daemon 轮询 QQ 空间 feed/detail，只有验证到 `appid=311`、同一 `sVid`、全部人可见时才报告成功。
- 任一环节不能证明公开视频成功时必须失败，不能回退成视频封面图，也不能重复尝试另一个发布 action 造成多发。

## action 名称

首选 action：

```text
publish_qzone_video_mood
```

插件也会按顺序探测这些兼容名称及其下划线扩展形式：

```text
publish_qzone_video_mood
_publish_qzone_video_mood
```

Only `publish_qzone_video_mood` is canonical. `_publish_qzone_video_mood` is accepted as a OneBot extension-name compatibility form. Other publish/upload aliases were removed from the plugin probing list to avoid duplicate publishing after a partially successful protocol-end invocation.

## 请求参数

插件会发送多种常见参数形态以兼容不同协议端。协议端至少应支持下面这种：

```json
{
  "video_path": "C:\\path\\clip.mp4",
  "file_path": "C:\\path\\clip.mp4",
  "content": "视频说说正文",
  "text": "视频说说正文",
  "desc": "视频说说正文",
  "sync_weibo": false,
  "appid": 311,
  "who": 1,
  "ugc_right": 1,
  "visibility": "public",
  "permission": "public",
  "privacy": "public",
  "visible": "all",
  "visible_to": "all",
  "right": "public",
  "public": true
}
```

这些权限字段不是普通回显字段。协议端必须把它们映射到真实 QQ 空间发布权限：

- `who=1`
- `ugc_right=1`
- mobile/native 发布体中的公开视频结构，例如 `UgcRightInfo(ugc_right=1)`
- 不得发布为仅自己可见、好友可见、部分可见、指定好友可见或不给谁看

协议端也可以支持这些等价媒体形态：

```json
{"path": "C:\\path\\clip.mp4", "...": "..."}
{"file": "C:\\path\\clip.mp4", "...": "..."}
{"video": {"type": "video", "file": "C:\\path\\clip.mp4", "path": "C:\\path\\clip.mp4"}, "...": "..."}
{"media": [{"type": "video", "file": "C:\\path\\clip.mp4", "path": "C:\\path\\clip.mp4"}], "...": "..."}
```

## 成功响应

成功时必须返回 `sVid` 或 `vid`。推荐响应：

```json
{
  "retcode": 0,
  "data": {
    "sVid": "qzone-video-id",
    "fid": "optional-feed-id",
    "visibility": "public",
    "ugc_right": 1
  }
}
```

兼容字段：

- 视频 ID：`sVid`、`svid`、`vid`、`videoId`、`video_id`、`qzoneVideoId`
- 动态 ID：`fid`、`tid`、`feedId`、`feed_id`、`cellId`、`topicId`

协议端可以返回 `fid`/`tid` 来加速 daemon detail 校验，但 `fid` 不能替代 `sVid`。如果协议端只返回成功状态而没有 `sVid`，插件会把它视为不安全成功并报错。

## 失败响应

推荐失败响应：

```json
{
  "retcode": 10001,
  "message": "qzone video upload failed",
  "data": {
    "stage": "upload_video"
  }
}
```

错误语义要求：

- 视频文件不可读、格式不支持、上传失败、发布失败，都应返回非 0 code 或抛出 OneBot action 错误。
- 真实权限不是全部人可见时，必须返回失败，或者至少在响应里带出非公开标记；插件会拒绝包含 `private`、`friends only`、`仅自己`、`好友可见`、`部分可见`、`ugc_right != 1` 等标记的响应。
- 不允许把“已上传视频但发布失败”伪装为成功；上传得到 `sVid` 只是中间态，最终必须能被 QQ 空间动态验证。

## daemon 校验

协议端 action 返回 `sVid` 后，插件会调用本地 daemon：

```http
POST /native-video/verify
```

请求：

```json
{
  "vid": "qzone-video-id",
  "fid": "optional-feed-id",
  "method": "onebot_protocol_video_publish"
}
```

daemon 只做验证，不接收 A2/vLoginData，也不再次发布。成功条件：

- 最近动态或 detail 中出现同一 `sVid`
- feed/detail 的 `appid=311`
- 存在明确公开权限标记，例如 `ugc_right=1`、`visibility=public`、`visible=all`、`right=public` 或 `public=true`；仅仅没有私密标记不算成功

校验失败时，插件会把发布视为失败或不可信，不会退回封面图发布。

## 凭据边界

推荐实现是协议端内部使用登录态发布视频，不向 AstrBot 返回任何敏感凭据。响应中不得包含：

- Cookie、`p_skey`、`skey`、PSKey
- `clientKey`、`keyIndex`
- A2、vLoginData、vLoginKey
- token、secret 或原始登录数据库内容

如果协议端选择暴露上传凭据而不是实现原生发布，应实现单独 action，例如：

```text
get_qzone_video_upload_credentials
get_video_upload_credentials
get_qzone_video_upload_a2
get_login_misc_data
```

返回值必须是非空 QQ upload A2/A2Ticket/vLoginData 二进制材料，可用 base64、hex、Node `Buffer` JSON 或数字数组表达。Cookie/CSRF、PSKey、`clientKey/keyIndex`、`ForceFetchFileTransSig` 只能作为诊断材料，不能冒充 QQ upload 登录材料。

## NapCat / LLBot 实现建议

- 默认 NapCat OneBot action 能稳定提供 Cookie、CSRF、clientKey 等 Web 登录材料，但这些不是 QQ upload A2/vLoginData。仅改 action 名称不能保证视频发布。
- 如果 NapCat 在 NTQQ 进程内实现本契约，应直接复用内部 QQ/Qzone 上传能力，并把真实发布权限设置成全部人可见；不要把内部 A2/vLoginData 放入 action 响应。
- LLOneBot/LLBot 的 `llonebot_debug` 可以作为诊断入口，但本地实测 `getLoginMiscData('a2'/'A2'/'vLoginData')` 可能返回空值。协议端实现应提供一个稳定扩展 action，而不是依赖 debug 透传。
- 如果协议端只实现 A2/vLoginData 获取，插件仍会走 daemon Tencent upload/Android 同源路径；如果协议端实现原生发布 action，插件会优先使用协议端发布，再由 daemon 验证。

## 最小实现清单

1. 注册 `publish_qzone_video_mood` OneBot 扩展 action。
2. 校验 `video_path` 指向本机可读的单个视频文件。
3. 读取 `content`/`text`，按 QQ 空间视频说说发布。
4. 使用真实 native/mobile 发布体设置 `who=1`、`ugc_right=1`、全部人可见。
5. 上传视频和封面，完成视频说说发布。
6. 返回 `retcode=0`、`data.sVid`，可选返回 `data.fid`。
7. 不在日志或响应中输出 A2/vLoginData/Cookie/clientKey。
8. 发布失败、权限非公开或没有 `sVid` 时返回失败。

## 插件侧拒绝条件

插件会在这些情况下拒绝成功：

- action 不存在：继续探测其它名称，最终回到 A2/vLoginData daemon 路径。
- action 成功但无 `sVid`：立即失败，不再尝试其它发布 action。
- action 响应出现非公开权限标记：立即失败。
- daemon 未验证到 `appid=311`、同一 `sVid`、全部人可见：失败。
- 视频组合不是单个可信本地视频：不走协议端原生发布。

## A2Ticket credential extension

If a protocol end cannot implement native `publish_qzone_video_mood`, it may expose true QQ upload binary login material for the daemon Tencent-upload path. Native publish actions must not return sensitive material; only credential-extension actions may return binary upload material. The preferred action names are:

```text
get_qzone_video_upload_a2_ticket
get_qzone_upload_a2_ticket
get_video_upload_a2_ticket
get_qq_upload_a2_ticket
get_ntqq_a2_ticket
get_nt_a2_ticket
get_a2_ticket
```

The plugin also probes the same names with a leading underscore. The response must contain real QQ upload A2/vLoginData/A2Ticket binary material, for example `a2TicketHex`, `a2TicketB64`, `A2TicketBytes`, `vLoginData`, or a Node `Buffer` result. LLBot/LLOneBot PMHQ implementers may wire this to `nodeIKernelTicketService/getA2Ticket` or `wrapperSession.getTicketService().getA2Ticket`.

The code also accepts compatibility aliases such as `get_qzone_video_a2_ticket`, `get_video_a2_ticket`, and `get_upload_a2_ticket`; implementers should prefer the names above so the action purpose stays unambiguous.

These values are not acceptable substitutes for QQ upload A2/vLoginData and will only be recorded as credential diagnostics: Cookie, `p_skey`, PSKey, CSRF/bkn, `clientKey/keyIndex`, `forceFetchClientKey`, `ForceFetchFileTransSig`, file-transfer signatures, or Web jump-login material. `ForceFetchFileTransSig` is rejected even inside a targeted A2/A2Ticket response. A valid Qzone Web Cookie/`p_skey` can make the separate daemon H5 publish/update-visibility path ready, but protocol-end A2 credential actions must not report it as A2/Tencent-upload readiness.

## 2026-06-08 strict native publish contract

The plugin now calls only one native publish action per post: `publish_qzone_video_mood`, plus `_publish_qzone_video_mood` only when the canonical action is explicitly unavailable. The request contains all path aliases in a single payload (`video_path`, `file_path`, `path`, `file`, `video`, and `media`) so protocol ends do not need multiple publish attempts.

After an action has been invoked, timeout or execution failure is treated as an unsafe state and the plugin will not fall back to daemon upload, because doing so could duplicate a video mood. A successful response must include all of the following:

- success status: `retcode=0`, `code=0`, `ret=0`, `ok=true`, `success=true`, or `status=ok/success`
- non-empty `sVid`/`vid`
- positive public visibility proof: `ugc_right=1`, `visibility=public`, `visible=all`, `right=public`, or `public=true`

`public=false`, friend/private/custom visibility text, `ugc_right != 1`, a missing `sVid`, or a missing public marker is rejected. The daemon verification is also positive-proof based: it must find `appid=311`, the same `sVid`, the login user's feed/detail, and an explicit public marker. Merely lacking private markers is no longer enough.

Daemon-side Web/H5 publishing has been re-enabled as a two-step verified path: `FileBatchControl`/`FileUploadVideo` and cover upload create the video material, `emotion_cgi_publish_v6` creates or reveals the video mood publicly, and `emotion_cgi_update` is then called with `tid=<fid>`, `ugc_right=1`, `who=1`, `to_tweet=0`, and `to_sign=0` as an idempotent public repair. The daemon still refuses success until feed/detail verification proves `appid=311`, the same `sVid`, the login user as host, and an explicit public marker.
