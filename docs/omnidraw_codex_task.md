# OmniDraw 侧 Codex 任务：支持 QQ 空间定时生活说说返回式自拍生图

## 背景

QQ 空间 Ultra 插件将新增 Life Scheduler + OmniDraw 定时生活说说联动：

1. QQ 空间插件定时触发。
2. QQ 空间插件读取 Life Scheduler 的 `get_life_context()`。
3. QQ 空间插件让 LLM 写自拍 action/prompt。
4. QQ 空间插件调用 OmniDraw 返回式自拍接口拿图。
5. QQ 空间插件用返回图片自动配文并发 QQ 空间说说。

OmniDraw 侧只需要保证“返回式自拍接口”稳定可被其他插件调用。

## 必须接口

请在 OmniDraw 插件实例上提供以下任一可调用方法（推荐两个都兼容）：

```python
async def generate_selfie(
    self,
    event,
    action: str,
    count: int = 1,
    aspect_ratio: str = "",
    size: str = "",
    extra_params: str = "",
    return_result: bool = False,
    refs: str = "",
) -> str | dict:
    ...
```

或现有 LLM tool：

```python
async def tool_generate_selfie(
    self,
    event,
    action: str,
    count: int = 1,
    aspect_ratio: str = "",
    size: str = "",
    extra_params: str = "",
    return_result: bool = False,
    refs: str = "",
) -> str:
    ...
```

## 硬性要求

1. QQ 空间侧只会使用 `return_result=True`。
2. `return_result=True` 时绝不能自动下发图片到聊天窗口，不能依赖 `event.send()`。
3. `return_result=True` 时必须返回 dict 或 JSON 字符串。
4. 返回结构必须包含：

```json
{
  "success": true,
  "message": "ok",
  "images": [
    {
      "index": 1,
      "file_path": "D:/path/to/image.png",
      "url": "https://...",
      "image_url": "https://...",
      "data_url": "data:image/png;base64,...",
      "content_type": "image/png",
      "provider_id": "...",
      "model": "...",
      "elapsed_seconds": 1.23,
      "prompt": "最终请求提示词"
    }
  ]
}
```

5. `images[*]` 至少提供 `file_path`、`url`、`image_url`、`data_url` 中任意一个。
6. 图片源推荐优先提供本地 `file_path`，其次 `url` / `image_url` / `data_url`。
7. `count`、`aspect_ratio`、`size`、`extra_params`、`refs` 都要能正常透传。
8. `mode="selfie"` 或自拍链路必须真正使用 OmniDraw 的人设/参考图/自拍链路；不能退化成普通 text2img，除非自拍链未配置时按 OmniDraw 现有逻辑降级。
9. 权限和额度检查仍可基于传入 `event` 归属到调用方提供的管理员/管理群虚拟事件。
10. 失败时返回：

```json
{"success": false, "message": "可读错误信息", "images": []}
```

而不是抛出未处理异常或返回“已下发图片”之类聊天提示。

## 推荐实现路线

如果当前已有 `generate_images_for_plugin(...)`：

- 给它增加或确认已有 `mode="selfie"` 参数。
- 当 `mode == "selfie"` 时：
  - 先走自拍提示词优化 / 人设 prompt 拼接。
  - 使用 `chain_selfie`，没有自拍链时再降级 `text2img`。
  - 使用 `refs` 或当前人设参考图。
  - 返回统一 `images` 结构。
- `tool_generate_selfie(..., return_result=True)` 内只调用返回式逻辑，不调用发送逻辑。

## QQ 空间侧调用方式

QQ 空间插件会这样调用：

```python
raw = await omnidraw.tool_generate_selfie(
    event,
    action=image_prompt,
    count=1,
    aspect_ratio="3:4",
    size="",
    extra_params="",
    return_result=True,
    refs="",
)
```

也可能优先调用 `generate_selfie(...)`，参数相同。

## 测试建议

请至少添加/确认以下测试：

1. `return_result=True` 时不调用 event 发送方法。
2. `tool_generate_selfie(..., return_result=True)` 返回 JSON，可 `json.loads()`。
3. 返回 JSON 中 `success=True` 且 `images[0]` 有可用 `file_path` 或 URL。
4. `aspect_ratio`、`size`、`extra_params` 被传入底层 provider/chain。
5. `mode="selfie"` 时使用自拍链 `chain_selfie` 或等价自拍逻辑。
6. 无图/权限/额度失败时返回 `success=False, images=[]`。
