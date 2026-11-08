"""Error types for the QQ空间 bridge."""

from __future__ import annotations


class QzoneBridgeError(Exception):
    """Base error for the bridge."""

    def __init__(self, message: str, *, code: str = "QZONE_ERROR", detail=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail


class LifeGenerationError(QzoneBridgeError):
    """Fixed, safe failure returned by the bounded daily-post generation path."""

    _MESSAGES = {
        "LIFE_TOTAL_TIMEOUT": "日常说说生成超过总时限，已停止且未发布",
        "LIFE_IMAGE_TIMEOUT": "日常说说配图生成超时，已停止且未发布",
        "LIFE_TEXT_TIMEOUT": "日常说说文案生成超时，已停止且未发布",
        "LIFE_CALL_BUDGET_EXHAUSTED": "日常说说生成调用次数已达上限，已停止且未发布",
        "LIFE_PERSONA_FAILED": "日常说说人格一致性检查未通过，已停止且未发布",
        "LIFE_GENERATION_FAILED": "日常说说生成失败，已停止且未发布",
    }
    _PHASES = {"context", "image_prompt", "image", "caption", "generation"}

    def __init__(self, code: str, phase: str):
        if code not in self._MESSAGES:
            code = "LIFE_GENERATION_FAILED"
        if phase not in self._PHASES:
            phase = "generation"
        self.phase = phase
        super().__init__(self._MESSAGES[code], code=code)


class QzoneCookieAcquireError(QzoneBridgeError):
    def __init__(self, message: str = "无法获取 QQ 空间 Cookie", *, detail=None):
        super().__init__(message, code="QZONE_COOKIE_ACQUIRE", detail=detail)


class QzoneAuthError(QzoneBridgeError):
    def __init__(self, message: str, *, detail=None):
        super().__init__(message, code="QZONE_AUTH", detail=detail)


class QzoneNeedsRebind(QzoneAuthError):
    def __init__(self, message: str = "QQ空间登录态已失效，需要重新绑定 Cookie", *, detail=None):
        super().__init__(message, detail=detail)


class QzoneRequestError(QzoneBridgeError):
    def __init__(self, message: str, *, status_code: int | None = None, detail=None):
        super().__init__(message, code="QZONE_REQUEST", detail=detail)
        self.status_code = status_code


class QzoneParseError(QzoneBridgeError):
    def __init__(self, message: str, *, detail=None):
        super().__init__(message, code="QZONE_PARSE", detail=detail)


class DaemonUnavailableError(QzoneBridgeError):
    def __init__(self, message: str = "Qzone daemon is unavailable", *, detail=None):
        super().__init__(message, code="DAEMON_UNAVAILABLE", detail=detail)
