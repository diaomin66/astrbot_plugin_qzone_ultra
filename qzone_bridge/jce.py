"""Minimal Tencent JCE/Tars codec helpers used by Qzone upload research."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Any, Iterable


JCE_BYTE = 0
JCE_SHORT = 1
JCE_INT = 2
JCE_LONG = 3
JCE_FLOAT = 4
JCE_DOUBLE = 5
JCE_STRING1 = 6
JCE_STRING4 = 7
JCE_MAP = 8
JCE_LIST = 9
JCE_STRUCT_BEGIN = 10
JCE_STRUCT_END = 11
JCE_ZERO_TAG = 12
JCE_SIMPLE_LIST = 13


class JceEncodeError(ValueError):
    """Raised when a value cannot be encoded as JCE."""


class JceDecodeError(ValueError):
    """Raised when bytes cannot be decoded as JCE."""


@dataclass(frozen=True, slots=True)
class JceField:
    tag: int
    value: Any


@dataclass(frozen=True, slots=True)
class JceStructValue:
    fields: tuple[JceField, ...]


@dataclass(frozen=True, slots=True)
class JceNode:
    tag: int
    type_code: int
    value: Any


def jce_struct(fields: Iterable[JceField]) -> JceStructValue:
    return JceStructValue(tuple(fields))


def encode_struct(fields: Iterable[JceField]) -> bytes:
    writer = _JceWriter()
    for field in fields:
        writer.write(field.tag, field.value)
    return writer.to_bytes()


def decode_struct(payload: bytes) -> list[JceNode]:
    reader = _JceReader(bytes(payload or b""))
    return reader.read_struct_body(stop_at_struct_end=False)


def field_value(nodes: Iterable[JceNode], tag: int, default: Any = None) -> Any:
    for node in nodes:
        if node.tag == tag:
            return node.value
    return default


def as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def as_bytes(value: Any, default: bytes = b"") -> bytes:
    if value is None:
        return default
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    return default


def as_map(value: Any) -> dict[Any, Any]:
    return value if isinstance(value, dict) else {}


def as_nodes(value: Any) -> list[JceNode]:
    return value if isinstance(value, list) and all(isinstance(item, JceNode) for item in value) else []


class _JceWriter:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def to_bytes(self) -> bytes:
        return bytes(self._buffer)

    def write(self, tag: int, value: Any) -> None:
        tag = _checked_tag(tag)
        if isinstance(value, JceStructValue):
            self._write_head(JCE_STRUCT_BEGIN, tag)
            for field in value.fields:
                self.write(field.tag, field.value)
            self._write_head(JCE_STRUCT_END, 0)
        elif isinstance(value, bool):
            self._write_int(tag, 1 if value else 0)
        elif isinstance(value, int):
            self._write_int(tag, value)
        elif isinstance(value, (bytes, bytearray)):
            self._write_bytes(tag, bytes(value))
        elif isinstance(value, str):
            self._write_string(tag, value)
        elif isinstance(value, dict):
            self._write_map(tag, value)
        elif isinstance(value, (list, tuple)):
            self._write_list(tag, value)
        elif value is None:
            return
        else:
            raise JceEncodeError(f"unsupported JCE value type: {type(value).__name__}")

    def _write_head(self, type_code: int, tag: int) -> None:
        if not 0 <= type_code <= 15:
            raise JceEncodeError("JCE type code is outside nibble range")
        tag = _checked_tag(tag)
        if tag < 15:
            self._buffer.append((tag << 4) | type_code)
        else:
            self._buffer.append(0xF0 | type_code)
            self._buffer.append(tag)

    def _write_int(self, tag: int, value: int) -> None:
        value = int(value)
        if value == 0:
            self._write_head(JCE_ZERO_TAG, tag)
        elif -128 <= value <= 127:
            self._write_head(JCE_BYTE, tag)
            self._buffer.extend(int(value).to_bytes(1, "big", signed=True))
        elif -32768 <= value <= 32767:
            self._write_head(JCE_SHORT, tag)
            self._buffer.extend(int(value).to_bytes(2, "big", signed=True))
        elif -2147483648 <= value <= 2147483647:
            self._write_head(JCE_INT, tag)
            self._buffer.extend(int(value).to_bytes(4, "big", signed=True))
        elif -9223372036854775808 <= value <= 9223372036854775807:
            self._write_head(JCE_LONG, tag)
            self._buffer.extend(int(value).to_bytes(8, "big", signed=True))
        else:
            raise JceEncodeError("JCE integer is outside int64 range")

    def _write_string(self, tag: int, value: str) -> None:
        data = str(value).encode("utf-8")
        if len(data) <= 255:
            self._write_head(JCE_STRING1, tag)
            self._buffer.append(len(data))
        else:
            self._write_head(JCE_STRING4, tag)
            self._buffer.extend(len(data).to_bytes(4, "big", signed=False))
        self._buffer.extend(data)

    def _write_bytes(self, tag: int, value: bytes) -> None:
        self._write_head(JCE_SIMPLE_LIST, tag)
        self._write_head(JCE_BYTE, 0)
        self._write_int(0, len(value))
        self._buffer.extend(value)

    def _write_map(self, tag: int, value: dict[Any, Any]) -> None:
        self._write_head(JCE_MAP, tag)
        self._write_int(0, len(value))
        for key, item in value.items():
            self.write(0, key)
            self.write(1, item)

    def _write_list(self, tag: int, value: Iterable[Any]) -> None:
        items = list(value)
        self._write_head(JCE_LIST, tag)
        self._write_int(0, len(items))
        for item in items:
            self.write(0, item)


class _JceReader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._pos = 0

    def read_struct_body(self, *, stop_at_struct_end: bool) -> list[JceNode]:
        nodes: list[JceNode] = []
        while self._pos < len(self._payload):
            type_code, tag = self._read_head()
            if type_code == JCE_STRUCT_END:
                if stop_at_struct_end:
                    return nodes
                nodes.append(JceNode(tag=tag, type_code=type_code, value=None))
                continue
            nodes.append(JceNode(tag=tag, type_code=type_code, value=self._read_value(type_code)))
        if stop_at_struct_end:
            raise JceDecodeError("JCE struct did not contain a struct-end marker")
        return nodes

    def _read_head(self) -> tuple[int, int]:
        if self._pos >= len(self._payload):
            raise JceDecodeError("unexpected end of JCE data while reading head")
        first = self._payload[self._pos]
        self._pos += 1
        type_code = first & 0x0F
        tag = (first & 0xF0) >> 4
        if tag == 15:
            if self._pos >= len(self._payload):
                raise JceDecodeError("extended JCE tag is incomplete")
            tag = self._payload[self._pos]
            self._pos += 1
        return type_code, tag

    def _read_value(self, type_code: int) -> Any:
        if type_code == JCE_ZERO_TAG:
            return 0
        if type_code == JCE_BYTE:
            return self._read_signed_int(1)
        if type_code == JCE_SHORT:
            return self._read_signed_int(2)
        if type_code == JCE_INT:
            return self._read_signed_int(4)
        if type_code == JCE_LONG:
            return self._read_signed_int(8)
        if type_code == JCE_FLOAT:
            return struct.unpack(">f", self._read_exact(4))[0]
        if type_code == JCE_DOUBLE:
            return struct.unpack(">d", self._read_exact(8))[0]
        if type_code == JCE_STRING1:
            length = self._read_unsigned_int(1)
            return self._read_exact(length).decode("utf-8", "replace")
        if type_code == JCE_STRING4:
            length = self._read_unsigned_int(4)
            return self._read_exact(length).decode("utf-8", "replace")
        if type_code == JCE_SIMPLE_LIST:
            return self._read_simple_list()
        if type_code == JCE_LIST:
            return self._read_list()
        if type_code == JCE_MAP:
            return self._read_map()
        if type_code == JCE_STRUCT_BEGIN:
            return self.read_struct_body(stop_at_struct_end=True)
        raise JceDecodeError(f"unsupported JCE type code: {type_code}")

    def _read_simple_list(self) -> bytes:
        element_type, _element_tag = self._read_head()
        if element_type != JCE_BYTE:
            raise JceDecodeError("only byte simple lists are supported")
        length_type, _length_tag = self._read_head()
        length = as_int(self._read_value(length_type), 0)
        if length < 0:
            raise JceDecodeError("JCE simple-list length is negative")
        return self._read_exact(length)

    def _read_list(self) -> list[Any]:
        length_type, _length_tag = self._read_head()
        length = as_int(self._read_value(length_type), 0)
        if length < 0:
            raise JceDecodeError("JCE list length is negative")
        values: list[Any] = []
        for _ in range(length):
            type_code, _tag = self._read_head()
            values.append(self._read_value(type_code))
        return values

    def _read_map(self) -> dict[Any, Any]:
        length_type, _length_tag = self._read_head()
        length = as_int(self._read_value(length_type), 0)
        if length < 0:
            raise JceDecodeError("JCE map length is negative")
        values: dict[Any, Any] = {}
        for _ in range(length):
            key_type, _key_tag = self._read_head()
            key = self._read_value(key_type)
            value_type, _value_tag = self._read_head()
            value = self._read_value(value_type)
            values[_hashable_key(key)] = value
        return values

    def _read_exact(self, length: int) -> bytes:
        if length < 0:
            raise JceDecodeError("negative read length")
        end = self._pos + length
        if end > len(self._payload):
            raise JceDecodeError("unexpected end of JCE data")
        data = self._payload[self._pos:end]
        self._pos = end
        return data

    def _read_signed_int(self, length: int) -> int:
        return int.from_bytes(self._read_exact(length), "big", signed=True)

    def _read_unsigned_int(self, length: int) -> int:
        return int.from_bytes(self._read_exact(length), "big", signed=False)


def _checked_tag(tag: int) -> int:
    try:
        value = int(tag)
    except (TypeError, ValueError) as exc:
        raise JceEncodeError("JCE tag must be an integer") from exc
    if not 0 <= value <= 255:
        raise JceEncodeError("JCE tag is outside uint8 range")
    return value


def _hashable_key(value: Any) -> Any:
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value
