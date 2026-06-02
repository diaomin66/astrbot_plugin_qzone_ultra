# QQ 空间 daemon 原生视频发布逆向记录

日期：2026-06-02

## 结论

当前仓库里的 daemon 已完全废除 QQ/QQNT 客户端 handoff，也不再把视频帧伪装成图片说说发布。`native_video_publish` 开启时，单个本地视频只走 Tencent upload 后台直发链路；缺少 QQ upload 登录材料、媒体组合不适合原生发布，或上传后未能在最近动态中验证到同一 `sVid` 时，都会直接报错。

已知的 PC/Web 路径是：

1. `cgi_upload_image` 上传图片，拿到图片 `richval`。
2. `emotion_cgi_publish_v6` 发布图文说说。

这条链路没有接收本地视频文件、视频分片、`vid` 或腾讯上传 SDK 结果的参数。把本地视频路径拼进正文只会变成普通文本，不能让 QQ 空间上传视频。

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

- aiocqhttp/OneBot 视频引用按协议端通用字段解析，不绑定 NapCat；优先兼容 LLOneBot、NapCat、Shamrock 的 `url`、`download_url`、`file_url`、`file_id`、`get_file`、群/私聊文件 URL 扩展。
- 裸 `file` / `file_id` 只当作文件标识或文件名，不当作本地路径。
- 如果视频源可读取，daemon 先本地化视频；单个本地视频优先进入 Tencent upload 后台直发链路，渲染结果仍可使用视频封面图保留播放标识。
- 运行时已废除 `mqqapi://qzone/publish` / QQ/QQNT 客户端确认发布路径；客户端跳转只保留在逆向背景说明中，不再由插件调用。
- daemon 原生视频发布仅当提供 QQ upload 二进制登录材料时启用后台上传发布；未提供或视频组合不适合原生发布时会阻止发布并提示绑定/调整媒体，不再把视频封面帧当作图片说说发出。只有关闭 `native_video_publish` 后才明确走视频封面图发布。

## v0.6.8 进展：发布体嵌入上传业务数据

继续追 `QZoneUploadShuoShuoTask.getUploadMoodBytes4RecordVideo()` 后，确认普通“录制视频说说”不是在上传成功后再单独调用一个最终发布 RPC。客户端会先构造 `QZonePublishMoodRequest`，把 `operation_publishmood_req.mediainfo` 置空，再用 OldUniAttribute 编码：

- `hostuin`：当前登录 QQ 号。
- `publishmood`：`NS_MOBILE_OPERATION.operation_publishmood_req`，包含正文、同步微博标记、来源 `Source(subtype=0, termtype=4, apptype=1)`、权限 `UgcRightInfo(ugc_right=1)`、`ShootInfo` 和 `extend_info`。
- `extend_info`：录制视频路径会写入 `iIsOriginalVideo`、`iIsFormatF20`、`videoSize`，以及可能的 `sync` / `sync_qqstory` 等扩展。

这段 UniAttribute 字节会作为 `VideoUploadTask.vBusiNessData`，并将 `VideoUploadTask.iBusiNessType` 置为 `1`。因此 daemon 直发的主链路已经改为：先在 `UploadVideoInfoReq` 中带上 `iBusiNessType=1` 和 `vBusiNessData=UniAttribute(hostuin,publishmood)`，再走 `video_qzone` 控制包与分片上传。

`QZoneVideoShuoshuoUploadFinishRequest` 的命令是 `rptVSUploadFinish`，结构为 `NS_MOBILE_EXTRA.mobile_video_shuoshuo_upload_finish_req(iSize, iTimeLength)`。从调用位置看，它更像上传完成上报/统计，不是发布正文的主入口。

当前代码已把这条链路落地为：

- `encode_record_video_publish_business_data()`：生成 `publishmood` OldUniAttribute。
- `QzoneTencentVideoUploader.upload_video(..., publish_content=...)`：自动嵌入发布业务体并使用 `iBusiNessType=1`。
- daemon `publish_post()`：当状态或环境里存在 `QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64` 等 QQ upload 登录材料时，单个本地视频优先交给 Tencent upload 后台路径；未配置时直接报错并阻止封面帧替代发布，不再唤起客户端。

仍然必须外部提供 QQ upload 二进制登录材料：`QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64`，可选 `QZONE_VIDEO_UPLOAD_LOGIN_KEY_B64`、`QZONE_VIDEO_UPLOAD_TOKEN_TYPE`、`QZONE_VIDEO_UPLOAD_TOKEN_APPID`、`QZONE_VIDEO_UPLOAD_TOKEN_WT_APPID`。PC/Web Cookie、`p_skey`、`pt4_token` 不能直接等价为 Tencent upload SDK 的 `vLoginData/vLoginKey`。

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

因此，当前剩余的真实运行阻塞点不是 Web Cookie，也不是 `clientKey/p_skey`，而是必须取得 QQ/客户端上传 SDK 能接受的 `vLoginData/A2` 类二进制登录材料。OneBot 适配器如果只返回 `clientKey`、`p_skey`、Cookie 或 Web `qzonetoken`，daemon 会保持未配置状态并拒绝原生直发。
