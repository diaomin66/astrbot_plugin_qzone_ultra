# QQ 空间 daemon 原生视频发布逆向记录

日期：2026-06-02；H5 路径更新：2026-06-03；OneBot/Tencent upload 稳定化：2026-06-05；Android has_video/封面业务体对齐：2026-06-05；OneBot 协议端通用化：2026-06-05；Cookie H5 就绪语义更新：2026-06-06；公开视频权限确认：2026-06-06；H5 乱报错与私密权限诊断：2026-06-06；H5 发布后权限修改链路：2026-06-12

## 结论

Current daemon path no longer uses QQ/QQNT handoff or video-frame image fallback. With `native_video_publish` enabled, a single local video can publish through either the Qzone Web Cookie/`p_skey` H5 upload → `emotion_cgi_publish_v6` → `emotion_cgi_update` public-permission path, the Android-style Tencent upload `video_qzone` + `pic_qzone` chain backed by QQ upload A2/vLoginData, or a OneBot protocol-end native publish action. Every success still requires feed/detail verification of `appid=311`, the same `sVid`, the login user as host, and explicit public/all-visible permissions.

已知的普通 PC/Web 图文路径是：

1. `cgi_upload_image` 上传图片，拿到图片 `richval`。
2. `emotion_cgi_publish_v6` 发布图文说说。

这条图文链路没有接收本地视频文件、视频分片、`vid` 或腾讯上传 SDK 结果的参数。把本地视频路径拼进正文只会变成普通文本，不能让 QQ 空间上传视频。当前可用的 Web/H5 视频模型是先走 H5 `sliceUpload/FileUploadVideo` 拿到 `sVid` 并上传封面，再用视频 `richval` 调 `emotion_cgi_publish_v6` 生成视频说说，最后用 `emotion_cgi_update` 把可能仅自己可见的动态改成全部人可见。

## 已确认的客户端路径

Android OpenSDK 的 QzonePublish 使用 `mqqapi://qzone/publish` 唤起 QQ 客户端，并在 query 里带 `req_type=4`、`videoPath`、`videoDuration`、`videoSize` 等字段。这是客户端跳转/人工确认路径，不是 daemon 可调用的 HTTP 上传接口。

参考：<https://github.com/megahertz0/android_thunder/blob/master/dex_src/com/tencent/connect/share/QzonePublish.java>

QQ/空间客户端内部还有一条静默或插件内发布路径：

1. `QZoneHelper.publishPictureMoodSilently(...)` 会把 `param.images`、`param.source`、`param.subtype` 放进 Bundle，并发送 `cmd.publishMixMood`。
2. `RemoteHandleConst` 中存在 `cmd.publishVideoMood`、`cmd.publishMixMood`、`cmd.videoUploadForH5`、`value.videoSign` 等命令/来源常量。
3. `WebPluginHandleLogic` 的 `cmd.publishVideoMood` 分支会读取 `param.videoPath`、`param.videoSize`、`param.videoType`、`param.thumbnailPath`、`param.thumbnailWidth`、`param.thumbnailHeight`、`param.duration`、`param.totalDuration`、`param.needProcess`、`param.isUploadOrigin`、`param.source` 等字段，组装 `ShuoshuoVideoInfo`。
4. `QZoneWriteOperationService` 会把视频模型交给 `QZoneUploadShuoShuoTask` / `QZonePublishQueue`。
5. `QzoneMediaUploadRequest` 创建 `QZoneVideoUploadTask`，设置 `vLoginData`、`vBusiNessData`、`iBusiNessType`、`sRefer`、`sVid` 等上传协议字段，最终由 Tencent upload SDK 完成视频上传，再回到说说发布队列。

参考：

- <https://github.com/tsuzcx/qq_apk/blob/main/com.tencent.mobileqq/classes.jar/cooperation/qzone/QZoneHelper.java>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes8/com/qzone/common/webplugin/WebPluginHandleLogic.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes21/com/qzone/common/business/service/QZoneWriteOperationService%242.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes11/com/qzone/publish/business/task/QZoneUploadShuoShuoTask.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes34/com/qzone/publish/business/protocol/QzoneMediaUploadRequest.smali>

## 已继续确认的 Tencent upload SDK 协议

继续从 `QzoneMediaUploadRequest -> QZoneVideoUploadTask -> Tencent upload SDK` 往下追，已经确认 daemon 真视频直发至少不是普通 HTTPS CGI，而是腾讯上传 SDK 的 socket/PDU/JCE 链路：

1. `QZoneVideoUploadTask` 继承 `VideoUploadTask`，构造 `ServerRouteTable(FileType.Video, BusinessType.QZoneVideo, ConnectType.Epoll, ...)`。
2. 默认主机是 `video.upqzfile.com`，备份主机是 `video.upqzfilebk.com`；`ServerRouteTable` 给默认 host route 使用端口 `80`，session size 为 `2`。
3. `VideoUploadTask` 默认 `mAppid = "video_qzone"`，`AbstractUploadTask.getControlRequest()` 看到这个 appid 后把文件校验切到 `TYPE_SHA1`。
4. 控制包是 `FileControlRequest`，默认 cmd id 为 `1`，JCE 结构是 `SLICE_UPLOAD/FileControlReq`，其中 `biz_req` 由 `VideoUploadTask.buildExtra()` 生成的 `FileUpload/UploadVideoInfoReq` 填充。
5. 分片包是 `FileUploadRequest`，cmd id 为 `2`，JCE 结构是 `SLICE_UPLOAD/FileUploadReq`，包含 `uin/appid/offset/session/check_type/data_type/extend_info/checksum/data`。
6. PDU 帧由 `PDUtil.encode(cmd, seq, jce)` 生成：`0x04 + 23字节 PduHeader + JCE bytes + 0x05`。总长度写在 header offset `0x13`，值为 `len(JCE) + 0x19`。
7. `PduHeader$OFFSET` 确认字段位置：`CMD=1`、`CHECKSUM=5`、`SEQ=7`、`KEY=0x0b`、`RESPONSE_FLAG=0x0f`、`RESPONSE_INFO=0x10`、`RESERVED=0x12`、`LEN=0x13`。
8. `TokenProvider.getAuthToken(vLoginData, vLoginKey)` 会构造 `SLICE_UPLOAD/AuthToken`，并通过可插拔 `ITokenEncryptor` 处理 `vLoginData`；当前 PC Cookie 登录态不能直接证明可生成这两个二进制字段。
9. 上传完成响应由 `VideoUploadTask.processFileUploadFinishRsp()` 解 `FileUpload/UploadVideoInfoRsp`，关键字段是 `sVid`、`iBusiNessType`、`vBusiNessData`；这些字段随后进入 Qzone 发布队列。

参考：

- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes34/com/qzone/publish/business/task/upload/QZoneVideoUploadTask.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes34/com/tencent/upload/network/route/ServerRouteTable.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes34/com/tencent/upload/uinterface/data/VideoUploadTask.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes11/com/tencent/upload/uinterface/AbstractUploadTask.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes34/com/tencent/upload/request/impl/FileControlRequest.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes34/com/tencent/upload/request/impl/FileUploadRequest.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes32/com/tencent/upload/utils/PDUtil.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes32/com/tencent/upload/utils/PduHeader.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes32/com/tencent/upload/utils/PduHeader%24OFFSET.smali>
- <https://github.com/cxxsheng/Android_9.2.5_64/blob/main/smali_classes5/com/tencent/upload/uinterface/token/TokenProvider.smali>

## 已落地到代码的上传协议层

新增 `qzone_bridge/tencent_upload.py` 和 `qzone_bridge/jce.py`，把当前已确认、可确定的协议常量、PDU 帧、JCE schema 和上传客户端固化为测试覆盖的基础模块：

- `QZONE_VIDEO_UPLOAD_APPID = "video_qzone"`
- `QZONE_VIDEO_UPLOAD_HOST = "video.upqzfile.com"`
- `QZONE_VIDEO_UPLOAD_BACKUP_HOST = "video.upqzfilebk.com"`
- `QZONE_VIDEO_UPLOAD_PORT = 80`
- `TENCENT_UPLOAD_CMD_CONTROL = 1`
- `TENCENT_UPLOAD_CMD_FILE = 2`
- `encode_upload_pdu()` / `decode_upload_pdu()` / `decode_upload_pdu_size()`
- `encode_upload_video_info_req()` / `decode_upload_video_info_rsp()`
- `encode_file_batch_control_req()` / `decode_file_batch_control_rsp()`
- `encode_file_upload_req()` / `decode_file_upload_rsp()`
- `QzoneTencentVideoUploader`：可按控制包、分片包顺序走 socket/PDU/JCE 上传流程，成功响应里解析 `sVid/iBusiNessType/vBusiNessData`。
- `qzone_video_upload_probe()`：面向 daemon 后续接入的协议探针，明确当前阻塞项已经从编码层收敛为 QQ upload 二进制登录材料；录制视频说说的发布体已确认嵌入 `UploadVideoInfoReq.vBusiNessData`，且 Android 的视频封面 `pic_qzone` 上传腿也已落地。

这一步把“已确认的 wire protocol”从文档变成可回归测试的代码边界。v0.6.8 继续确认了录制视频说说的发布体其实嵌在 `vBusiNessData` 中；v0.6.9 继续确认只上传视频拿到 `sVid` 还不够，Android 客户端还会上传视频封面来触发混排视频动态。后续真正依赖外部补齐的是 `vLoginData/vLoginKey` 这类 QQ upload 二进制登录材料。

## 需要继续逆向的点

要让 daemon 真正原生发视频，必须补齐以下协议，而不是复用图片说说接口：

1. `vLoginData/vLoginKey` 的生成来源，尤其是 QQ 登录态、设备态、uin、skey/pt4_token 之外的二进制字段，以及 `ITokenEncryptor` 是否在 QQ/Qzone 运行时被替换。
2. `vBusiNessData` 的结构已确认包含 `UniAttribute(hostuin, publishmood)`；后续需要继续实测不同来源、同步选项、权限设置下的扩展字段差异。
3. 视频封面上传已确认需要 `pic_qzone` / `UploadPicInfoReq` / `PicExtendInfo.mapParams[vid,clientkey]` / `stExternalMapExt[mix_*]`；后续需要继续实测转码/原画、`needProcess`、不同封面宽高与服务端审核状态的差异。
4. 成功上传后的 `rptVSUploadFinish` 上报是否必需，以及它是否需要 WNS/移动端 SSO 会话才能补发。

## 当前实现策略

- aiocqhttp/OneBot 视频引用按协议端通用字段解析，不绑定 NapCat；优先兼容 LLOneBot、NapCat、Shamrock 的 `url`、`download_url`、`file_url`、`file_id`、`get_file`、`get_video`、群/私聊文件 URL 扩展。
- 裸 `file` / `file_id` 只当作文件标识或文件名，不当作本地路径。
- 如果视频源可读取，daemon 先本地化视频；单个本地视频优先进入 daemon 后台直发链路。可用 Web Cookie/`p_skey` 时优先走 H5 上传 + 发布 + `emotion_cgi_update` 权限修改并校验公开；否则使用 QQ upload A2/vLoginData 驱动的 Tencent upload/Android 同源路径。渲染结果仍可使用视频封面图保留播放标识。
- 运行时已废除 `mqqapi://qzone/publish` / QQ/QQNT 客户端确认发布路径；客户端跳转只保留在逆向背景说明中，不再由插件调用。
- daemon 原生视频发布缺少 Web Cookie/`p_skey` 且缺少 QQ upload A2/vLoginData 时会直接阻止发布。已有 Qzone Web Cookie/`p_skey` 会让 H5 发布/权限修改链路进入 ready，但最终仍以公开 feed/detail 校验为准。关闭 `native_video_publish`、视频组合不适合原生发布或缺少所有可用登录材料时，都会阻止视频发布并提示绑定/调整媒体，不再把视频封面帧或渲染图当作图片说说发出。
- `/qzone autovideoauth` 面向 OneBot 协议端做通用扩展 action 探测：优先确保 daemon 有可用 Qzone Cookie/`p_skey` 作为诊断材料，再尝试 `get_qzone_video_upload_credentials` / `get_video_upload_credentials` 等协议端自定义 action，其次尝试 `get_login_misc_data(key/name/field=a2/vLoginData/...)`，再尝试 LLOneBot/LLBot `llonebot_debug` 的登录 misc/A2 入口；`get_cookies`、`get_credentials`、`get_clientkey`、`forceFetchClientKey`、PSKey 返回的 Web Cookie/CSRF/clientkey/keyIndex 只记录诊断，不会冒充 A2。

## v0.6.8 进展：发布体嵌入上传业务数据

继续追 `QZoneUploadShuoShuoTask.getUploadMoodBytes4RecordVideo()` 后，确认普通“录制视频说说”不是在上传成功后再单独调用一个最终发布 RPC。客户端会先构造 `QZonePublishMoodRequest`，把 `operation_publishmood_req.mediainfo` 置空，再用 OldUniAttribute 编码：

- `hostuin`：当前登录 QQ 号。
- `publishmood`：`NS_MOBILE_OPERATION.operation_publishmood_req`，包含正文、同步微博标记、来源 `Source(subtype=0, termtype=4, apptype=1)`、权限 `UgcRightInfo(ugc_right=1)`、`ShootInfo` 和 `extend_info`。
- `extend_info`：录制视频路径会写入 `iIsOriginalVideo`、`iIsFormatF20`、`videoSize`，以及可能的 `sync` / `sync_qqstory` 等扩展。

这段 UniAttribute 字节会作为 `VideoUploadTask.vBusiNessData`，并将 `VideoUploadTask.iBusiNessType` 置为 `1`。因此 daemon 直发的主链路已经改为：先在 `UploadVideoInfoReq` 中带上 `iBusiNessType=1` 和 `vBusiNessData=UniAttribute(hostuin,publishmood)`，再走 `video_qzone` 控制包与分片上传。

`QZoneVideoShuoshuoUploadFinishRequest` 的命令是 `rptVSUploadFinish`，结构为 `NS_MOBILE_EXTRA.mobile_video_shuoshuo_upload_finish_req(iSize, iTimeLength)`。从调用位置看，它更像上传完成上报/统计，不是发布正文的主入口。

当前代码已把 Android/Tencent upload 稳定链路落地为：

- `encode_record_video_publish_business_data()`：生成 `publishmood` OldUniAttribute。
- `QzoneTencentVideoUploader.upload_video(..., publish_content=...)`：自动嵌入发布业务体并使用 `iBusiNessType=1`。
- daemon `publish_post()`：当状态或环境里存在 `QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64` 等 QQ upload 登录材料时，单个本地视频可交给 Tencent upload 后台路径；未配置时直接报错并阻止封面帧替代发布，不再唤起客户端。

Tencent upload SDK 稳定链路必须外部提供 QQ upload 二进制登录材料：`QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64`，可选 `QZONE_VIDEO_UPLOAD_LOGIN_KEY_B64`、`QZONE_VIDEO_UPLOAD_TOKEN_TYPE`、`QZONE_VIDEO_UPLOAD_TOKEN_APPID`、`QZONE_VIDEO_UPLOAD_TOKEN_WT_APPID`。PC/Web Cookie、`p_skey`、`pt4_token`、PSKey、clientKey/keyIndex 不能直接等价为 Tencent upload SDK 的 `vLoginData/vLoginKey`，但可用于 H5 JSON `sliceUpload` 诊断链路。

## v0.6.9 进展：Android 视频封面上传腿与 feed 校验

实测和 smali 对照后，单独走 `video_qzone` 上传并返回 `UploadVideoInfoRsp.sVid` 不足以生成 QQ 空间视频动态。Android 路径在视频上传后还会继续创建 `ImageUploadTask` 上传视频封面；这一步使用图片上传 appid/域名，但业务参数里把封面和前一步的 `sVid` 绑定：

| 阶段 | appid | host | 校验 | 关键业务字段 |
| --- | --- | --- | --- | --- |
| 视频上传 | `video_qzone` | `video.upqzfile.com:80` | SHA1 | `UploadVideoInfoReq.iBusiNessType=1`、`vBusiNessData=publishmood`、`stExtendInfo.clientkey` |
| 封面上传 | `pic_qzone` | `pic.upqzfile.com:80` | MD5 | `UploadPicInfoReq.stExtendInfo.mapParams["vid"]`、`mapParams["clientkey"]`、`mapExt["mobile_fakefeeds_clientkey"]`、`stExternalMapExt["is_client_upload_cover"]`、`stExternalMapExt["is_pic_video_mix_feeds"]`、`stExternalMapExt["mix_videoSize"]`、`stExternalMapExt["mix_time"]` |

当前代码对应落地为：

- `UploadPicInfoReq`、`PicExtendInfo`、`UploadPicInfoRsp` 的 JCE 编解码。
- `QzoneTencentVideoUploader.upload_video_cover(...)`：使用 `pic_qzone`、MD5、封面文件分片上传，并携带 `vid/clientkey/mobile_fakefeeds_clientkey/mix_*`。
- daemon 原生视频发布顺序变为：本地化视频 -> 生成封面 -> `video_qzone` 上传视频 -> `pic_qzone` 上传封面 -> 轮询最近动态验证同一 `sVid`。
- 如果未验证到 feed，daemon 抛出 `QzoneRequestError`，不会把“已返回 sVid”包装成发布成功。

## v0.6.9/v0.6.34 H5 `p_skey` 视频上传 + Web richval 发布 + 权限修改

本地实测确认，Qzone H5 JSON `sliceUpload` 可以直接用 Web Cookie 里的 `p_skey` 作为 token 上传 `video_qzone` 视频，不需要 OneBot 返回 QQ upload A2/vLoginData：

- 控制接口：`https://h5.qzone.qq.com/webapp/json/sliceUpload/FileBatchControl/<sha1>?g_tk=<gtk>`
- 分片接口：`https://h5.qzone.qq.com/webapp/json/sliceUpload/FileUploadVideo?seq=...&offset=...&end=...&total=...&type=form&g_tk=<gtk>`
- control 关键字段：`token={type:4,data:p_skey,appid:5}`、`appid=video_qzone`、`cmd=FileUploadVideo`、`check_type=1`、`biz_req.extend_info.video_type=3`、`qz_video_format=mp4`。
- multipart 分片关键点：`data` part 必须等价于 `("blob", chunk)`，即 `Content-Disposition` 有 `filename="blob"`；当前本地实测默认需要该 part 带 `Content-Type: application/octet-stream`，同时代码保留接口返回 `-115` 时自动重试无 part `Content-Type` 的兼容后备。
- 最后一片返回 `data.biz.sVid`。

上传完成后，Web 视频模型 `Video.getValue()` 给出的真实视频 `richval` 形态是：

```text
playurl=<encoded qqplayer swf>&detailurl=<encoded /qzvideo/sVid>&who=5&rich_flag=4&vid=<sVid>
```

The H5 path is now an active daemon publish route when Cookie/`p_skey` is available. After `FileUploadVideo` returns `sVid`, the daemon uploads the cover as a resource only (`iNeedFeeds=0`, no `is_pic_video_mix_feeds`) and then calls `emotion_cgi_publish_v6` with `richtype=3`, `subrichtype=7`, `ugc_right=1`, and the Web video `richval`. If Qzone returns a recoverable ?invalid link? error while still leaving a video mood behind, the daemon extracts or discovers `tid/fid`, calls `emotion_cgi_update` with `ugc_right=1`/`who=1`/`tid=<fid>` as a public repair, then performs final public feed/detail verification before reporting success.

## v0.6.29 H5 publish_v6 错误归因与私密快速失败

- `emotion_cgi_publish_v6` 对本地上传 `sVid` 返回“链接无效”时不再立即作为发布失败上抛；daemon 会把它记录为 `publish_error(recoverable=true)`，继续用同一 `sVid` 走详情和最近动态校验。
- 如果精确 `tid/fid` 的详情接口返回“没有访问操作权限”“主人设置保密”等访问受限错误，验证器会归因为 `private_visibility`，最终错误明确说明“不是全部人可见”，不再遮蔽成“链接无效”。
- 为了更快失败，精确 `tid/fid` 已确认访问受限时，daemon 会先扫一轮 `self/active/profile` 最近动态兜底；仍找不到同一 `sVid` 的公开视频后即停止长轮询，并记录 `early_stop_reason=publish_tid_detail_access_denied`。

## v0.6.28/v0.6.34 状态语义：Cookie H5 可发布但必须权限修改和验证

- daemon `/health` 的 `video_upload` 摘要包含 `qq_upload_configured`、`web_cookie_configured`、`ready`、`verification_required`、`h5_publish_supported` 和 `h5_publish_permission_update_required`。当 Cookie H5 上传接口可用但 A2 未绑定时，`ready=True`、`web_cookie_configured=True`、`qq_upload_configured=False`、`method=h5_video_publish_update_visibility`。
- `/qzone status` and `/qzone autovideoauth` report video publishing as ready when either QQ upload A2/vLoginData is configured or Cookie/H5 publish is available. Real success still requires daemon verification of `appid=311`, the same `sVid`, and explicit public/all-visible permissions after any H5 permission update.
- feed 校验继续拒绝 `appid=4` 相册/封面 fake feed、只回显 richval 的 Web 响应、以及任何未包含目标 `sVid` 的动态或详情。

## v0.6.18 进展：封面上传也携带 publishmood 与响应 tid 验证

继续追 `QzoneMediaUploadRequest` 后确认，Android 创建视频封面 `ImageUploadTask` 时如果 `uploadParams.iBusiNessType == 1`，会把同一份 `uploadParams.vBusiNessData` 继续写入封面上传任务。也就是说，真实视频动态不是“视频上传带发布体、封面只带 vid/clientkey”这么简单；封面 `pic_qzone` 控制包也需要携带同一个 `publishmood` 业务体，才能和视频 `sVid`、`clientkey`、`mobile_fakefeeds_clientkey` 共同触发 fake feed/真实 feed 关联。

本轮实现：

- daemon 在上传前生成一次 `publishmood` OldUniAttribute，并同时传给 `video_qzone` 与后续 `pic_qzone` 封面上传。
- `UploadVideoInfoRsp.vBusiNessData` 会按 Android 的 `operation_publishmood_rsp` 解码，保留 `ret`、`verifyurl`、`tid`、`msg`。
- 若服务端返回 `publishmood_rsp.ret != 0`，直接报出发布失败，而不是继续等待一个永远不会出现的 feed。
- 若服务端返回 `tid`，feed 验证会优先请求该 fid 的详情并检查同一 `sVid`，再退回最近动态列表轮询；这样成功路径更快，失败诊断也更准。

## v0.6.17 进展：Android 录制视频发布体与通用 OneBot 客户端对齐

继续对照 Android 9.2.5 `QZoneUploadShuoShuoTask.getUploadMoodBytes(...)` 的视频分支后，确认录制视频说说不仅会把视频大小、原画/格式标记写入 `extend_info`，还会显式写入 `extend_info["has_video"]="1"`，并把 `operation_publishmood_req.mediatype` 与 `mediabittype` 都置为 `1`。这三个字段用于让发布队列把 `vBusiNessData` 里的 `publishmood` 识别成视频动态，而不是普通文本/图片动态。

本轮代码把这些字段设为 daemon 原生视频发布的默认值：

- `encode_record_video_publish_business_data()` 默认 `media_type=1`、`media_bit_type=1`。
- `publishmood.extend_info` 默认包含 `has_video=1`，同时保留已有 `iIsOriginalVideo`、`iIsFormatF20`、`videoSize`。
- `QzoneTencentVideoUploader.upload_video(..., publish_content=...)` 继承同样默认值，daemon 无需写 NapCat 专用逻辑即可走 Android 同源业务体。

OneBot 侧继续按协议端抽象处理：通用自定义 action 和 `get_login_misc_data(key/name/field=a2/vLoginData)` 仍优先于 LLOneBot debug 入口；客户端调用兼容 `call_action` 与 `call_api`，以及关键字参数、位置参数字典、`params=` 三种常见封装。AstrBot 上下文捕获也会先找 `aiocqhttp`，再找 `onebot` / `onebot11` / `napcat` / `llonebot` 等平台别名；NapCat、LLOneBot 是重点验证对象，但不是唯一兼容目标。

## v0.6.16 进展：OneBot 协议端兼容与 Android 时间绑定

这次修复两个导致“素材为空 / daemon 请求失败 / 返回 sVid 但不可见”的关键差异：

1. OneBot action 调用不再只假设 aiocqhttp/NapCat 的 `call_action(action, **params)` 形态，也兼容协议端封装常见的 `call_action(action, params)` / `call_action(action=..., params=...)`。
2. A2 探测保持协议优先：通用自定义 action 与 `get_login_misc_data` 都可返回 bytes、base64、hex、`{"type":"Buffer","data":[...]}` 等二进制材料；如果响应里同时带 `clientKey/keyIndex` 这类 bookkeeping 字段，只有明确请求的是 `a2/vLoginData` 时才接受 `value/data` 里的原始材料，避免把普通 clientkey 误当 QQ upload A2。
3. Android 视频上传与封面上传现在共享同一个 `upload_time`：`UploadVideoInfoReq.iUploadTime`、`publishmood.publish_time`、`UploadPicInfoReq.iUploadTime`、`stExtendInfo.clientkey`、`mapExt.mobile_fakefeeds_clientkey` 全部使用同一个 daemon 生成的 `uin_uploadTime`。这对齐了 Android `ImageUploadTask` 继承 `VideoUploadTask.iUploadTime` 的行为，减少视频资源和封面 fake feed 不能关联的风险。


## v0.6.19 OneBot protocol-end auth probing update

- The auth probe remains protocol-first: generic OneBot extension actions such as `get_qzone_video_upload_credentials`, `get_video_upload_credentials`, `get_login_misc_data(key=a2)`, `get_qzone_video_upload_a2`, and `get_vlogin_data` are tried before implementation-specific fallbacks.
- NapCat compatibility is handled through the same protocol contract first. Current NapCat source exposes `NodeIKernelLoginService.getLoginMiscData(key)` internally but does not expose it as a default OneBot action; if an AstrBot adapter surfaces that internal NTQQ service object, the daemon can now call it as an embedded fallback without logging secret material.
- LLOneBot compatibility now covers both `llonebot_debug` -> `pmhq.invoke("nodeIKernelLoginService/getLoginMiscData", [key])` and `llonebot_debug` -> `pmhq.call("loginService.getLoginMiscData", [key])` shapes.
- Node/OneBot binary shapes are normalized more broadly, including `Buffer`/`Uint8Array` JSON (`{"type":"Buffer","data":[...]}`), numeric-key byte objects, hex, base64, and base64url. Cookie/CSRF/clientKey/keyIndex responses are still diagnostic-only and are not accepted as QQ upload A2/vLoginData.
- 2026-06-07 local LLBot live probe: `forceFetchClientKey` is available but only proves Web jump-login material; `loginService.getLoginMiscData` for `a2`, `A2`, `vLoginData`, `uploadLoginData`, and `qzoneUploadLoginData` returned empty values; PMHQ album/personal-album calls did not return a usable personal Qzone video mood publish payload. No sensitive ticket values were logged.

## v0.6.20 OneBot protocol-end compatibility update

- Default video-auth binding source is now `onebot` instead of `aiocqhttp`; AstrBot's adapter may still be named aiocqhttp, but the compatibility contract is the OneBot protocol end, not NapCat or any single implementation.
- Generic extension probing now also tries leading-underscore custom action names such as `_get_qzone_video_upload_credentials` and `_get_login_misc_data(key=a2)`, matching common OneBot extension naming conventions.
- OneBot action invocation now covers `call_action`, `call_api`, `request`, and `call`, plus keyword params, positional params, and `params`/`data`/`payload` wrappers. This keeps NapCat/LLOneBot as primary targets while allowing other OneBot protocol ends to expose the same A2/vLoginData contract.

## v0.6.23 OneBot protocol-end binary shape update

- Current NapCat source still exposes `NodeIKernelLoginService.getLoginMiscData(key)` internally and standard `get_clientkey` / cookie APIs externally; those external Web login materials are still not accepted as QQ upload A2/vLoginData.
- Current LLOneBot source registers `llonebot_debug` and its PMHQ bridge can call `loginService.getLoginMiscData`; probing now covers `pmhq.invoke(...)`, `pmhq.call(...)`, and `pmhq.httpSend({type:"call", data:{func:"loginService.getLoginMiscData", args:[key]}})` shapes.
- OneBot extension action responses may now return A2/vLoginData as `a2`/`a2_hex`/`a2_b64`, `vLoginData`/`vLoginDataHex`/`vLoginDataB64`, Node `Buffer`, numeric byte arrays, or a targeted login-misc raw binary JavaScript string. Printable Web clientKey-like strings remain rejected.
- The publish success invariant remains unchanged: daemon only reports success after upload plus feed/detail verification of the same `sVid`; otherwise the result is an explicit failure, never a cover-image fallback.

## v0.6.30 NapCat / LLBot A2 reverse-check

- 2026-06-06 复核 NapCat main `9aa11fd7c4df4d71ba5f115fe9a173f11f1de6e5`：`napcat-core` 内部仍有 `NodeIKernelLoginService.getLoginMiscData(key)` 和 `WrapperSessionInitConfig.a2`，但默认 OneBot action 表仍只公开 `get_cookies`、`get_credentials`、`get_csrf_token`、`get_clientkey` 等 Web 登录材料；WebUI Debug 也是从 OneBot action map 调用，不会绕过 action map 直接暴露内部 login service。
- 2026-06-06 复核 LLOneBot/LuckyLilliaBot main `25f6423cbb91ddb47f1ae5db261698c22e6781fb`：源码仍注册 `llonebot_debug`，Debug action 通过 `ctx.get(apiClass)[method]` 透传；PMHQ 映射里 `nodeIKernelLoginService -> loginService`，`nodeIKernelTicketService -> getTicketService`，`forceFetchClientKey` 的类型返回是 `clientKey/keyIndex`，不是 QQ upload A2/vLoginData。
- 2026-06-06 本地 LLBot PMHQ `127.0.0.1:13000` 复测：`loginService.getLoginMiscData('a2'/'A2'/'vLoginData'/'vloginData')` 都返回 `result=-1,value=""`，`wrapperSession.getLoginService().getLoginMiscData('a2')` 返回方法不存在。因此当前本地运行态没有成功给出 A2；这些响应会被记录为 `empty_login_data_actions`，不会被当成可发布凭据。
- 2026-06-06 对本机路径只做结构检查，不读取登录值：旧 PCQQ `Misc.db` / `MiscHead.db` 不是 sqlite；Androws `login.db` 只有 `configs.key=userinfo`。这类本地数据库即使包含登录态，也不是 OneBot 稳定协议接口，且无法证明可生成 Tencent upload `vLoginData/vLoginKey`，因此不作为自动读取方案。
- A2 探测矩阵已收紧：A2/登录材料 action 优先走无参和少量 video_qzone 业务参数，Cookie/CSRF action 只走域名参数，目标 `get_login_misc_data` 只保留高价值 `key/name/field` 组合。当前本地 LLBot/PMHQ live probe 从 1521 个组合降到 807 个组合，约 1.5 秒完成，仍能记录空 A2 和 clientKey 诊断。
- 插件侧的稳定获取方案因此是协议端扩展优先：实现 `get_qzone_video_upload_credentials`、`get_login_misc_data(key=a2)`、`get_qzone_video_upload_a2` 或等价下划线 action，返回非空 A2/vLoginData 二进制材料。NapCat 若在 AstrBot adapter 内透出内部 `NodeIKernelLoginService`，插件会走 embedded fallback；否则默认 NapCat/LLBot 只能报告“未获得 A2”，不会自动发布视频。

## v0.6.31 OneBot protocol-end native publish contract

- The more feasible stable boundary is now protocol-end native publishing before A2 extraction. NapCat/LLBot-style ends can keep QQ upload login material inside their own NTQQ process and expose the canonical OneBot extension action `publish_qzone_video_mood`; `_publish_qzone_video_mood` is accepted only as the extension-name compatibility form. Other publish/upload aliases were removed to avoid duplicate posts after a partially successful protocol-end invocation.
- 2026-06-08 tightening: OneBot native publish responses must contain success status, non-empty `sVid`, and positive public proof (`ugc_right=1`, `visibility=public`, `visible=all`, `right=public`, or `public=true`). Daemon feed/detail verification now also requires positive public proof; absence of private markers is not enough. The Web/H5 `emotion_cgi_publish_v6` video `richval` method is no longer accepted alone; it must be followed by `emotion_cgi_update` and final public verification.
- Full protocol-end implementer contract: [`onebot-native-qzone-video-contract.md`](onebot-native-qzone-video-contract.md).
- The plugin calls that action only for a single trusted local video and sends explicit public visibility parameters: `who=1`, `ugc_right=1`, `visibility=public`, `permission=public`, `privacy=public`, and `visible=all`.
- The extension action must return `sVid`/`vid` and may return `fid`/`tid`. A success response without `sVid` is treated as unsafe because the daemon cannot prove the final feed is the same uploaded video; the plugin raises instead of trying another publish action and risking duplicate posts.
- The daemon now exposes `/native-video/verify` for plugin-to-daemon verification. It does not publish or receive credentials. It only polls/detail-checks the returned `sVid`/`fid` and accepts success after `appid=311`, same `sVid`, and public visibility are confirmed.
- H5 Cookie `sliceUpload` is promoted to the daemon H5 publish/update-visibility path when Qzone Cookie/`p_skey` is bound. Prior invalid-link/private-visibility side effects are handled by treating publish errors as recoverable only when the same `sVid` feed can be discovered, then calling `emotion_cgi_update` and requiring public verification.
- 2026-06-06 follow-up NapCat service audit: the checked service declarations expose `NodeIKernelLoginService.getLoginMiscData`, `NodeIKernelProfileService.getProfileQzonePicInfo`, `NodeIKernelPersonalAlbumService.forwardAlbumToQzone`, and Qzone unread helpers, but no direct video mood publish/upload service. That means default NapCat cannot be made reliable by just renaming an action; a protocol end must either expose real QQ upload A2/vLoginData or implement a native Qzone video publish action inside the NTQQ process.

## v0.6.32 OneBot A2Ticket / QQNT native audit update

- The daemon-side Tencent upload path remains `video_qzone` video upload plus `pic_qzone` cover upload, with `publishmood` business data embedded and a final feed/detail verification for `appid=311`, same `sVid`, and public/all-visible visibility. Cookie-bound sessions may instead use the H5 publish/update-visibility route first.
- The protocol-end credential probe now accepts true QQ upload `A2Ticket` material through preferred names such as `get_a2_ticket`, `get_ntqq_a2_ticket`, `get_nt_a2_ticket`, `get_qzone_video_upload_a2_ticket`, `get_qzone_upload_a2_ticket`, `get_video_upload_a2_ticket`, `get_qq_upload_a2_ticket`, and matching underscore action aliases. Structured fields such as `a2TicketHex`, `a2TicketB64`, `A2TicketBytes`, and raw Node `Buffer` results are accepted only on explicitly targeted A2/A2Ticket/vLoginData calls.
- LLBot/LLOneBot PMHQ fallbacks now cover `nodeIKernelTicketService/getA2Ticket`, `wrapperSession.getTicketService().getA2Ticket`, and `wrapperSession.getTicketService().GetA2Ticket` in addition to `loginService.getLoginMiscData(key=a2)`. `forceFetchClientKey`, `ForceFetchFileTransSig`, Cookie, PSKey, CSRF, and Web `clientKey/keyIndex` remain diagnostic-only; `ForceFetchFileTransSig` is rejected even if a targeted ticket response also includes a raw `value`.
- QQNT native `NodeIKernelFeedService.publishFeed` was not promoted to the final scheme. The discovered symbols and route names align with GPro/channel feed publishing (`trpc.qchannel.commwriter.ComWriter/PublishFeed`), not personal Qzone video mood publishing, so blindly calling it risks a wrong side effect.
- The "biaobai wall" style public-web path has no independent stable video endpoint in the material checked so far. It reuses Qzone upload/publish primitives; H5 `FileUploadVideo` may produce `sVid`, and `emotion_cgi_publish_v6` can create the video mood, but production success depends on the follow-up `emotion_cgi_update` permission change plus verified public feed/detail.

## v0.6.33 corrected LLBot/PMHQ live probe

- 2026-06-08 corrected a PowerShell probe bug that had accidentally shadowed the argument array variable, then re-ran PMHQ calls with explicit one-argument arrays. The local LLBot PMHQ endpoint returned `result=-1` with empty `value` for `loginService.getLoginMiscData` keys `a2`, `A2`, `a2_ticket`, `A2Ticket`, `vLoginData`, `v_login_data`, `uploadLoginData`, `qzoneUploadLoginData`, `loginData`, `D2`, `D2Key`, `skey`, `pskey`, `p_skey`, and `pt4_token`.
- `wrapperSession.getTicketService().getA2Ticket` and `GetA2Ticket` were classified as missing functions. `wrapperSession.getTicketService().forceFetchClientKey("")` returned a non-empty `clientKey` field with `result=0`, but this remains Web jump-login material and is not accepted as QQ upload login material.
- NapCat source audit by a sub-agent reached the same boundary: default NapCat OneBot actions expose Web Cookie/CSRF/clientKey, group album/H5 upload, raw QQ packet send, and message rich-media upload. They do not expose `publish_qzone_video_mood`, personal Qzone `video_qzone`/`pic_qzone` upload, or a default A2/A2Ticket/vLoginData action. `send_packet` can send QQ SSO/PB packets, but the Android Qzone video mood path uses the Tencent upload SDK socket protocol plus `publishmood` business data, not a known default NapCat OIDB action.
