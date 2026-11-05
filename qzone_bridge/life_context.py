"""Conservative temporal projection for external Life Scheduler context."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_TIMELINE_ITEMS = 256
MAX_SCHEDULE_CHARS = 12000
MAX_UNCLASSIFIED_ITEMS = 8
MAX_UNCLASSIFIED_CHARS = 1200


class LifeTimezoneError(ValueError):
    """Fixed, actionable configuration error; never includes resolver details."""

    def __init__(self) -> None:
        super().__init__("Life 时区无效；请设置有效 IANA 时区（如 Asia/Shanghai）或 UTC+08:00。")


@dataclass(frozen=True, slots=True)
class LifeClock:
    utc_now: datetime
    local_now: datetime
    timezone_label: str


@dataclass(frozen=True, slots=True)
class LifeOperationInputs:
    clock: LifeClock
    image_style_prompt: str


@dataclass(frozen=True, slots=True)
class LifePlanNode:
    text: str
    start: datetime
    end: datetime | None


@dataclass(frozen=True, slots=True)
class LifeContextProjection:
    local_now: datetime
    timezone_label: str
    source_date: date | None
    date_relation: str
    current_plan: LifePlanNode | None
    past_nodes: tuple[LifePlanNode, ...]
    future_nodes: tuple[LifePlanNode, ...]
    unclassified: tuple[str, ...]
    outfit_text: str
    style_text: str
    overflow: bool = False

    def image_scene_context(self) -> str:
        if self.date_relation == "stale":
            return "当前场景未知；过期日程不可用于当前活动或地点，只采用中性人物近景构图。"
        if self.current_plan is not None:
            return "当前时段明确计划（计划，不代表实时确认）：" + self._node_text(self.current_plan)
        return "当前活动和地点未知；采用中性人物近景构图，不推断睡眠、清醒或场所。"

    @staticmethod
    def caption_time_rules() -> str:
        return (
            "时间表达规则：只把明确覆盖当前时刻的计划写作当前计划并标明计划性；已过节点仅可明确回顾，"
            "未来节点仅可写成计划；日期、时间或当前活动未知时，不得推断活动、地点、睡眠、清醒状态或当前穿着。"
        )

    @property
    def source_label(self) -> str:
        if self.date_relation == "today":
            return f"来源日期：{self.source_date.isoformat()}（本地当天）"
        if self.date_relation == "yesterday":
            return f"来源日期：{self.source_date.isoformat()}（昨日）"
        if self.date_relation == "future":
            return f"来源日期：{self.source_date.isoformat()}（未来日期）"
        if self.date_relation == "stale":
            return f"来源日期：{self.source_date.isoformat()}（早于昨日，内容已视为过期）"
        return "来源日期：未知或格式不明确"

    @staticmethod
    def _node_text(node: LifePlanNode) -> str:
        if node.end is None:
            label = node.start.strftime("%H:%M")
        else:
            label = f"{node.start:%H:%M}-{node.end:%H:%M}"
        return f"{label} {node.text}".strip()

    def image_context(self) -> str:
        lines = [
            (
                f"本次生成时间：{self.local_now:%Y-%m-%d %H:%M} {self.timezone_label}"
                if self.local_now is not None else "本次生成时间：未知"
            ),
            self.source_label,
        ]
        if self.date_relation == "stale":
            lines.append("当前活动：未知；过期日程和穿搭不得用于当前场景。不要推测地点、睡眠或是否清醒。")
        elif self.current_plan is not None:
            lines.append(
                "当前时段的明确计划（计划信息，不代表实时确认）："
                + self._node_text(self.current_plan)
            )
        else:
            lines.append("当前活动：未知；没有明确覆盖此时刻的计划。不要推测地点、睡眠或是否清醒。")
        if self.overflow:
            lines.append("输入超出时间筛选边界，未从被截断的前缀挑选活动。")
        return "\n".join(lines)

    def caption_context(self) -> str:
        lines = [
            (
                f"本次生成时间：{self.local_now:%Y-%m-%d %H:%M} {self.timezone_label}"
                if self.local_now is not None else "本次生成时间：未知"
            ),
            self.source_label,
        ]
        if self.date_relation == "stale":
            lines.append("当前活动和穿搭均未知；过期日程不能作为当前事实。")
        elif self.current_plan is not None:
            lines.append(
                "当前时段的明确计划（仅为计划，不代表实时确认）："
                + self._node_text(self.current_plan)
            )
        else:
            lines.append("当前活动未知；不得从前后日程推断正在发生的活动、地点或作息。")
        if self.past_nodes:
            lines.append("已过时间的日程（只可明确标为回顾，不可写成正在发生）：")
            lines.extend(f"- {self._node_text(node)}" for node in self.past_nodes)
        if self.future_nodes:
            lines.append("后续计划（只可写成计划，不可表述为已完成）：")
            lines.extend(f"- {self._node_text(node)}" for node in self.future_nodes)
        if self.outfit_text and self.date_relation != "stale":
            when = "来源日期为今日" if self.date_relation == "today" else "来源日期未确认或不是今日"
            lines.append(f"来源日穿搭背景（{when}；不证明当前穿着）：{self.outfit_text}")
        if self.style_text and self.date_relation != "stale":
            lines.append(f"来源记录的穿搭风格背景（不作为绘画风格或当前活动）：{self.style_text}")
        if self.unclassified:
            lines.append("未确认的背景参考（日期或时段不足以判定当前事实）：")
            lines.extend(f"- {item}" for item in self.unclassified)
        if self.overflow:
            lines.append("输入超出时间筛选边界，未从被截断的前缀挑选活动；当前活动按未知处理。")
        return "\n".join(lines)


def _resolve_timezone(label: str) -> tzinfo:
    value = str(label or "").strip()
    if value == "UTC":
        return timezone.utc
    offset_match = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", value)
    if offset_match:
        hours, minutes = int(offset_match.group(2)), int(offset_match.group(3))
        if hours >= 24 or minutes >= 60:
            raise LifeTimezoneError()
        delta = timedelta(hours=hours, minutes=minutes)
        if offset_match.group(1) == "-":
            delta = -delta
        return timezone(delta, name=value)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise LifeTimezoneError() from None


def capture_life_inputs(
    timezone_label: str,
    image_style_prompt: str = "",
    *,
    utc_now: datetime | None = None,
) -> LifeOperationInputs:
    """Capture one UTC instant and resolve all per-operation settings once."""
    instant = utc_now if utc_now is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise LifeTimezoneError()
    instant = instant.astimezone(timezone.utc)
    zone = _resolve_timezone(timezone_label)
    clock = LifeClock(
        utc_now=instant,
        local_now=instant.astimezone(zone),
        timezone_label=str(timezone_label or "").strip(),
    )
    return LifeOperationInputs(clock=clock, image_style_prompt=str(image_style_prompt or "").strip())


def _as_mapping(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        return {"schedule": data}
    names = ("date", "date_str", "schedule_date", "outfit", "outfit_style", "schedule", "timeline", "meta")
    result: dict[str, Any] = {}
    for name in names:
        try:
            value = getattr(data, name, None)
        except Exception:
            value = None
        if value not in (None, "", [], {}):
            result[name] = value
    return result


def _parse_source_date(value: Any, clock: LifeClock) -> date | None:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            return parsed.date()
        return parsed.astimezone(clock.local_now.tzinfo).date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T[^\s]+", text):
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone(clock.local_now.tzinfo).date()


def _source_date(data: dict[str, Any], clock: LifeClock) -> tuple[date | None, str]:
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    values = [data[key] for key in ("date", "date_str", "schedule_date") if key in data and data[key] not in (None, "")]
    if "date" in meta and meta["date"] not in (None, ""):
        values.append(meta["date"])
    if not values:
        return None, "unknown"
    parsed = [_parse_source_date(value, clock) for value in values]
    if any(value is None for value in parsed) or len(set(parsed)) != 1:
        return None, "unknown"
    source = parsed[0]
    assert source is not None
    delta = (source - clock.local_now.date()).days
    if delta == 0:
        relation = "today"
    elif delta == -1:
        relation = "yesterday"
    elif delta < -1:
        relation = "stale"
    else:
        relation = "future"
    return source, relation


def _clock_time(value: Any) -> time | None:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value.strip()):
        return None
    hour, minute = (int(part) for part in value.strip().split(":"))
    return time(hour, minute)


def _node_from_fields(time_value: Any, content: Any, source: date, clock: LifeClock, end_value: Any = None) -> tuple[LifePlanNode | None, str]:
    text = str(content or "").strip()
    raw_time = str(time_value or "").strip()
    start_time: time | None = None
    end_time: time | None = None
    if end_value not in (None, ""):
        start_time = _clock_time(raw_time)
        end_time = _clock_time(str(end_value))
        if start_time is None or end_time is None:
            return None, " ".join(part for part in (raw_time, str(end_value), text) if part)
    else:
        interval = re.fullmatch(r"\s*((?:[01]\d|2[0-3]):[0-5]\d)\s*[-~—至]\s*((?:[01]\d|2[0-3]):[0-5]\d)\s*", raw_time)
        if interval:
            start_time = _clock_time(interval.group(1))
            end_time = _clock_time(interval.group(2))
        else:
            start_time = _clock_time(raw_time)
        if start_time is None:
            return None, " ".join(part for part in (raw_time, text) if part)
    local_zone = clock.local_now.tzinfo
    start = datetime.combine(source, start_time, tzinfo=local_zone)
    end = None
    if end_time is not None:
        end_date = source + timedelta(days=1) if end_time <= start_time else source
        end = datetime.combine(end_date, end_time, tzinfo=local_zone)
    if not text:
        return None, raw_time
    return LifePlanNode(text=text, start=start, end=end), ""


_TIME_TEXT = r"(?:[01]\d|2[0-3]):[0-5]\d(?:\s*[-~—至]\s*(?:[01]\d|2[0-3]):[0-5]\d)?"
_TEXT_NODE_PREFIX = re.compile(
    rf"(?:^|(?<=[\r\n;；，,、]))\s*({_TIME_TEXT})\s*[:：、\s]+",
    flags=re.MULTILINE,
)


def _parse_timeline(data: dict[str, Any], source: date, clock: LifeClock) -> tuple[list[LifePlanNode], list[str], bool]:
    timeline = data.get("timeline")
    schedule = data.get("schedule")
    timeline_items = timeline if isinstance(timeline, list) else []
    if len(timeline_items) > MAX_TIMELINE_ITEMS:
        return [], [], True
    timeline_chars = 0
    for item in timeline_items:
        if isinstance(item, dict):
            fragments = (item.get("time"), item.get("at"), item.get("start"), item.get("end"), item.get("content"), item.get("activity"), item.get("text"))
            timeline_chars += sum(len(str(value or "")) for value in fragments)
        else:
            timeline_chars += len(str(item or ""))
        if timeline_chars > MAX_SCHEDULE_CHARS:
            return [], [], True
    if isinstance(schedule, str) and len(schedule) > MAX_SCHEDULE_CHARS:
        return [], [], True
    nodes: list[LifePlanNode] = []
    unknown: list[str] = []
    for item in timeline_items:
        if isinstance(item, dict):
            content = item.get("content") or item.get("activity") or item.get("text") or ""
            start = item.get("time") or item.get("at") or item.get("start") or ""
            end = item.get("end")
            node, reference = _node_from_fields(start, content, source, clock, end)
        else:
            raw = str(item or "").strip()
            match = _TEXT_NODE_PREFIX.match(raw)
            if match and match.end() < len(raw):
                node, reference = _node_from_fields(match.group(1), raw[match.end():], source, clock)
            else:
                node, reference = None, raw
        if node is not None:
            nodes.append(node)
        elif reference:
            unknown.append(reference[:300])
    if nodes:
        return nodes, unknown, False
    if isinstance(schedule, str) and schedule.strip():
        matches = list(_TEXT_NODE_PREFIX.finditer(schedule))
        if not matches:
            return [], unknown + [schedule.strip()[:300]], False
        prefix = schedule[:matches[0].start()].strip(" \t\r\n;；，,、")
        if prefix:
            unknown.append(prefix[:300])
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(schedule)
            content = schedule[match.end():end].strip(" \t\r\n;；，,、")
            node, reference = _node_from_fields(match.group(1), content, source, clock)
            if node is not None:
                nodes.append(node)
            elif reference:
                unknown.append(reference[:300])
    return nodes, unknown, False


def _unknown_timeline(data: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    timeline = data.get("timeline")
    if isinstance(timeline, list):
        values.extend(str(item.get("content") or item.get("activity") or item.get("text") or item) if isinstance(item, dict) else str(item) for item in timeline[:MAX_UNCLASSIFIED_ITEMS])
    schedule = data.get("schedule")
    if isinstance(schedule, str) and schedule.strip():
        values.append(schedule.strip())
    return _bound_unknown(values)


def _bound_unknown(values: list[str]) -> tuple[str, ...]:
    cleaned = [str(item or "").strip().replace("\n", " ") for item in values if str(item or "").strip()]
    result: list[str] = []
    total = 0
    for item in cleaned[:MAX_UNCLASSIFIED_ITEMS]:
        part = item[:300]
        if total + len(part) > MAX_UNCLASSIFIED_CHARS:
            break
        result.append(part)
        total += len(part)
    if len(cleaned) > len(result):
        result.append(f"另有{len(cleaned) - len(result)}条未展开的参考记录")
    return tuple(result)


def project_life_context(data: Any, clock: LifeClock) -> LifeContextProjection:
    """Project only bounded, dated schedule facts relative to an injected clock."""
    source_data = _as_mapping(data)
    source_date, relation = _source_date(source_data, clock)
    outfit = str(source_data.get("outfit") or "").strip()[:500]
    style = str(source_data.get("outfit_style") or "").strip()
    meta = source_data.get("meta") if isinstance(source_data.get("meta"), dict) else {}
    if not style:
        style = str(meta.get("style") or "").strip()
    style = style[:300]
    current: LifePlanNode | None = None
    past: list[LifePlanNode] = []
    future: list[LifePlanNode] = []
    unknown: list[str] = []
    overflow = False

    if relation == "stale":
        unknown = ["过期来源中的日程和穿搭已隐藏"]
    elif source_date is None or relation == "unknown":
        unknown = list(_unknown_timeline(source_data))
        if source_data.get("schedule") or source_data.get("timeline"):
            unknown.insert(0, "源日期未知，以上内容不能判定为当前活动")
    else:
        nodes, unclassified, overflow = _parse_timeline(source_data, source_date, clock)
        unknown.extend(unclassified)
        if overflow:
            unknown = ["输入超出筛选边界，未从部分日程选择活动"]
        else:
            covering: list[LifePlanNode] = []
            for node in nodes:
                if node.end is not None and node.start <= clock.local_now < node.end:
                    if relation in {"today", "yesterday"}:
                        covering.append(node)
                    else:
                        future.append(node)
                elif node.end is None and node.start <= clock.local_now:
                    past.append(node)
                elif node.start > clock.local_now:
                    future.append(node)
                else:
                    past.append(node)
            if covering:
                current = max(covering, key=lambda node: node.start)
            past.sort(key=lambda node: node.start)
            future.sort(key=lambda node: node.start)
            past = past[-3:]
            future = future[:2]

    if relation == "stale":
        outfit = ""
        style = ""
    return LifeContextProjection(
        local_now=clock.local_now,
        timezone_label=clock.timezone_label,
        source_date=source_date,
        date_relation=relation,
        current_plan=current,
        past_nodes=tuple(past),
        future_nodes=tuple(future),
        unclassified=_bound_unknown(unknown),
        outfit_text=outfit,
        style_text=style,
        overflow=overflow,
    )


def unknown_life_context(clock: LifeClock | None = None) -> LifeContextProjection:
    """Return a direct-helper safe state without inventing a source date or plan."""
    return LifeContextProjection(
        local_now=clock.local_now if clock is not None else None,
        timezone_label=clock.timezone_label if clock is not None else "未知时区",
        source_date=None,
        date_relation="unknown",
        current_plan=None,
        past_nodes=(),
        future_nodes=(),
        unclassified=(),
        outfit_text="",
        style_text="",
    )
