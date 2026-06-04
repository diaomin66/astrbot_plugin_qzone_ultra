# QQ 空间 daemon 原生视频发布逆向记录

日期：2026-06-02；H5 路径更新：2026-06-03；OneBot/Tencent upload 稳定化：2026-06-05；Android has_video/封面业务体对齐：2026-06-05

## 结论

当前仓库里的 daemon 已完全废除 QQ/QQNT 客户端 handoff，也不再把视频帧伪装成图片说说发布。`native_video_publish` 开启时，单个本地视频的稳定默认链路是 Android/Tencent upload SDK 同源的 `video_qzone` + `pic_qzone` 后台上传：先用 QQ upload A2/vLoginData 走 socket/PDU/JCE 上传视频并携带 `publishmood` 业务体，再用同一个 `clientkey/iUploadTime` 上传视频封面绑定 `sVid`。H5 `sliceUpload/FileUploadVideo` 只保留为显式实验路径（`QZONE_EXPERIMENTAL_H5_VIDEO_PUBLISH`），因为它能稳定上传资源但 Web `publish_v6` 的 richval 回显不等于可见视频动态。任一路径上传后都必须在最近动态或详情中验证到同一 `sVid`，否则直接报错。

已知的普通 PC/Web 图文路径是：

1. `cgi_upload_image` 上传图片，拿到图片 `richval`。
2. `emotion_cgi_publish_v6` 发布图文说说。

这条图文链路没有接收本地视频文件、视频分片、`vid` 或腾讯上传 SDK 结果的参数。把本地视频路径拼进正文只会变成普通文本，不能让 QQ 空间上传视频。真实 Web 视频路径需要先走 H5 `sliceUpload/FileUploadVideo` 拿到 `sVid`，再用视频 `richval` 发布。

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
- 如果视频源可读取，daemon 先本地化视频；单个本地视频优先进入 Tencent upload 后台直发链路，渲染结果仍可使用视频封面图保留播放标识。
- 运行时已废除 `mqqapi://qzone/publish` / QQ/QQNT 客户端确认发布路径；客户端跳转只保留在逆向背景说明中，不再由插件调用。
- daemon 原生视频发布仅当提供 QQ upload 二进制登录材料时启用后台上传发布；未提供或视频组合不适合原生发布时会阻止发布并提示绑定/调整媒体，不再把视频封面帧当作图片说说发出。只有关闭 `native_video_publish` 后才明确走视频封面图发布。
- `/qzone autovideoauth` 面向 OneBot 协议端做通用扩展 action 探测：优先尝试 `get_qzone_video_upload_credentials` / `get_video_upload_credentials` 等协议端自定义 action，其次尝试 `get_login_misc_data(key/name/field=a2/vLoginData/...)`，再尝试 LLOneBot `llonebot_debug` 的登录 misc/A2 入口；`get_cookies`、`get_credentials`、`get_clientkey`、`forceFetchClientKey` 返回的 Web Cookie/CSRF/clientkey/keyIndex 只记录诊断，不会冒充 A2。

## v0.6.8 进展：发布体嵌入上传业务数据

继续追 `QZoneUploadShuoShuoTask.getUploadMoodBytes4RecordVideo()` 后，确认普通“录制视频说说”不是在上传成功后再单独调用一个最终发布 RPC。客户端会先构造 `QZonePublishMoodRequest`，把 `operation_publishmood_req.mediainfo` 置空，再用 OldUniAttribute 编码：

- `hostuin`：当前登录 QQ 号。
- `publishmood`：`NS_MOBILE_OPERATION.operation_publishmood_req`，包含正文、同步微博标记、来源 `Source(subtype=0, termtype=4, apptype=1)`、权限 `UgcRightInfo(ugc_right=1)`、`ShootInfo` 和 `extend_info`。
- `extend_info`：录制视频路径会写入 `iIsOriginalVideo`、`iIsFormatF20`、`videoSize`，以及可能的 `sync` / `sync_qqstory` 等扩展。

这段 UniAttribute 字节会作为 `VideoUploadTask.vBusiNessData`，并将 `VideoUploadTask.iBusiNessType` 置为 `1`。因此 daemon 直发的主链路已经改为：先在 `UploadVideoInfoReq` 中带上 `iBusiNessType=1` 和 `vBusiNessData=UniAttribute(hostuin,publishmood)`，再走 `video_qzone` 控制包与分片上传。

`QZoneVideoShuoshuoUploadFinishRequest` 的命令是 `rptVSUploadFinish`，结构为 `NS_MOBILE_EXTRA.mobile_video_shuoshuo_upload_finish_req(iSize, iTimeLength)`。从调用位置看，它更像上传完成上报/统计，不是发布正文的主入口。

当前代码已把 Android/Tencent upload 后备链路落地为：

- `encode_record_video_publish_business_data()`：生成 `publishmood` OldUniAttribute。
- `QzoneTencentVideoUploader.upload_video(..., publish_content=...)`：自动嵌入发布业务体并使用 `iBusiNessType=1`。
- daemon `publish_post()`：当 H5 Cookie 路径不可用但状态或环境里存在 `QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64` 等 QQ upload 登录材料时，单个本地视频可交给 Tencent upload 后台路径；未配置时直接报错并阻止封面帧替代发布，不再唤起客户端。

Tencent upload SDK 后备链路仍然必须外部提供 QQ upload 二进制登录材料：`QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64`，可选 `QZONE_VIDEO_UPLOAD_LOGIN_KEY_B64`、`QZONE_VIDEO_UPLOAD_TOKEN_TYPE`、`QZONE_VIDEO_UPLOAD_TOKEN_APPID`、`QZONE_VIDEO_UPLOAD_TOKEN_WT_APPID`。PC/Web Cookie、`p_skey`、`pt4_token` 不能直接等价为 Tencent upload SDK 的 `vLoginData/vLoginKey`，但已确认可用于 H5 JSON `sliceUpload` 链路。

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

## v0.6.9 H5 补充：`p_skey` 视频上传 + Web richval 发布

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

发布说说时调用 `emotion_cgi_publish_v6`，携带 `richtype=3`、`subrichtype=7`、上述 `richval`、正文、`ugc_right=1` 等字段。daemon 只在显式开启 `QZONE_EXPERIMENTAL_H5_VIDEO_PUBLISH` 且没有 QQ upload 登录材料时尝试这条 H5 路径，然后仍然轮询最近动态验证同一 `sVid`；只有验证到 feed 才返回成功。默认稳定路径仍是 A2/vLoginData 驱动的 Tencent upload SDK 链路。

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
