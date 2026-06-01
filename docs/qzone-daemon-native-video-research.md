# QQ 空间 daemon 原生视频发布逆向记录

日期：2026-06-01

## 结论

当前仓库里的 daemon 已能稳定做到“引用视频 -> 获取真实视频源 -> 提取封面 -> 按图片说说发布”。但“daemon 后台直接发布 QQ 空间原生视频”还不能只靠现有 Web 说说接口完成。

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
- `qzone_video_upload_probe()`：面向 daemon 后续接入的协议探针，明确当前阻塞项已经从编码层收敛为 QQ upload 二进制登录材料和最终发布 RPC。

这一步还不是直发完成，但它把“已确认的 wire protocol”从文档变成可回归测试的代码边界。后续只要补上 `vLoginData/vLoginKey` 来源和最终发布 RPC，就可以把上传成功返回的 `sVid/vBusiNessData` 接到 daemon 原生发布里，而不是继续只停留在文档推测。

## 需要继续逆向的点

要让 daemon 真正原生发视频，必须补齐以下协议，而不是复用图片说说接口：

1. `vLoginData/vLoginKey` 的生成来源，尤其是 QQ 登录态、设备态、uin、skey/pt4_token 之外的二进制字段，以及 `ITokenEncryptor` 是否在 QQ/Qzone 运行时被替换。
2. `vBusiNessData` 的结构，以及它和 `UploadVideoInfoRsp.sVid`、最终说说发布请求之间的关系。
3. 视频封面、时长、宽高、转码/原画、`needProcess` 等字段在上传 SDK 与最终发布模型中的映射。
4. 成功上传后最终发布说说的 RPC/CGI 请求体，确认它是否能在 PC Cookie 登录态下复现。

## 当前实现策略

- aiocqhttp/OneBot 视频引用按协议端通用字段解析，不绑定 NapCat；优先兼容 LLOneBot、NapCat、Shamrock 的 `url`、`download_url`、`file_url`、`file_id`、`get_file`、群/私聊文件 URL 扩展。
- 裸 `file` / `file_id` 只当作文件标识或文件名，不当作本地路径。
- 如果视频源可读取，daemon 先本地化视频再提取封面，然后按图片说说发布，渲染图保留视频播放标识。
- 单个本地视频仍可使用 `mqqapi://qzone/publish` 唤起 QQ/QQNT 原生发布窗口；这是客户端确认路径，不是 daemon 后台直发。
- daemon 原生视频发布应作为后续实验功能单独加开关，必须在抓包和 SDK 协议复现后再接入；失败时继续回退到封面图发布。
