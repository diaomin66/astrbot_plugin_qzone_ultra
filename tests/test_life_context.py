from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import qzone_bridge.life_context as life_context
from qzone_bridge.life_context import (
    LifeTimezoneError,
    capture_life_inputs,
    project_life_context,
)


def _clock(value: str, zone: str = "Asia/Shanghai"):
    return capture_life_inputs(zone, "fixture style", utc_now=datetime.fromisoformat(value).replace(tzinfo=timezone.utc)).clock


def test_sample_0119_source_yesterday_keeps_daytime_history_out_of_image_context():
    local_clock = _clock("2026-11-04T17:19:00")
    utc_clock = capture_life_inputs(
        "Asia/Shanghai", utc_now=datetime.fromisoformat("2026-11-05T01:19:00+08:00")
    ).clock
    projection = project_life_context({
        "date": "2026-11-04",
        "outfit": "浅色卫衣",
        "timeline": [
            {"time": "10:30", "content": "拉花咖啡"},
            {"time": "16:30", "content": "书店看书"},
            {"time": "20:00", "content": "回家换衣"},
            {"time": "21:30", "content": "放松休息"},
        ],
    }, local_clock)
    same = project_life_context({
        "date": "2026-11-04",
        "outfit": "浅色卫衣",
        "timeline": [
            {"time": "10:30", "content": "拉花咖啡"},
            {"time": "16:30", "content": "书店看书"},
            {"time": "20:00", "content": "回家换衣"},
            {"time": "21:30", "content": "放松休息"},
        ],
    }, utc_clock)
    assert local_clock.local_now == utc_clock.local_now
    assert projection.current_plan is None
    image = projection.image_context()
    assert "当前活动：未知" in image
    assert "拉花咖啡" not in image and "书店看书" not in image
    caption = projection.caption_context()
    assert "昨日" in caption
    assert "已过时间的日程" in caption
    assert "书店看书" in caption
    assert "回家换衣" in caption
    assert "昨日" in same.caption_context()


def test_dated_cross_midnight_interval_can_cover_current_time_but_point_cannot():
    clock = _clock("2026-11-04T17:19:00")  # 2026-11-05 01:19 Asia/Shanghai
    projection = project_life_context({
        "date": "2026-11-04",
        "timeline": [
            {"start": "22:00", "end": "02:00", "content": "跨夜的明确安排"},
            {"time": "23:45", "content": "点状记录"},
        ],
    }, clock)
    assert projection.current_plan is not None
    assert projection.current_plan.text == "跨夜的明确安排"
    assert projection.current_plan.end.date() == date(2026, 11, 5)
    assert any(node.text == "点状记录" for node in projection.past_nodes)


def test_today_points_are_history_or_future_not_implicitly_continuous():
    clock = _clock("2026-11-04T17:19:00")
    projection = project_life_context({
        "date": "2026-11-05",
        "timeline": [
            {"time": "00:50", "content": "过去的点状安排"},
            {"time": "02:00", "content": "之后的安排"},
        ],
    }, clock)
    assert projection.current_plan is None
    assert [node.text for node in projection.past_nodes] == ["过去的点状安排"]
    assert [node.text for node in projection.future_nodes] == ["之后的安排"]
    assert "之后的安排" in projection.caption_context()
    assert "正在发生" in projection.caption_context()


def test_explicit_covering_plan_selected_even_after_index_eight_and_ranges_parse():
    clock = _clock("2026-11-05T09:00:00")
    timeline = [{"time": f"{hour:02d}:00", "content": f"node-{hour}"} for hour in range(8, 18)]
    timeline[-1] = {"time": "17:30-19:00", "content": "完整输入靠后的有效区间"}
    projection = project_life_context({"date": "2026-11-05", "timeline": timeline}, clock)
    assert projection.current_plan is None  # no fabricated continuity to 09:00
    evening = project_life_context(
        {"date": "2026-11-05", "timeline": timeline}, _clock("2026-11-05T02:00:00")
    )
    assert evening.current_plan is None
    after = project_life_context(
        {"date": "2026-11-05", "schedule": "08:00 breakfast；09:00 commute；17:30-19:00 evening plan"},
        _clock("2026-11-05T01:00:00"),
    )
    assert any(node.text == "evening plan" for node in after.future_nodes)
    active = project_life_context(
        {"date": "2026-11-05", "timeline": timeline}, _clock("2026-11-05T10:00:00")
    )
    assert active.current_plan is not None and active.current_plan.text == "完整输入靠后的有效区间"


@pytest.mark.parametrize(
    "schedule, expected",
    [
        ("20:00-22:00 blue hoodie, do not wear the white shirt;22:30 rest", "blue hoodie, do not wear the white shirt"),
        ("20:00-22:00 蓝色卫衣，今天不穿白衬衫；22:30 休息", "蓝色卫衣，今天不穿白衬衫"),
    ],
)
def test_schedule_continuation_commas_and_negations_remain_in_the_timed_node(schedule, expected):
    projection = project_life_context(
        {"date": "2026-11-05", "schedule": schedule}, _clock("2026-11-05T13:00:00")
    )
    assert projection.current_plan is not None
    assert projection.current_plan.text == expected
    assert expected in projection.image_context()


def test_long_bounded_node_keeps_its_trailing_negation_instead_of_clipping():
    text = "描述" * 700 + "，不穿白衬衫"
    projection = project_life_context(
        {"date": "2026-11-05", "timeline": [{"time": "20:00-22:00", "content": text}]},
        _clock("2026-11-05T13:00:00"),
    )
    assert projection.current_plan is not None
    assert projection.current_plan.text.endswith("不穿白衬衫")


@pytest.mark.parametrize(
    "data",
    [
        {"date": "11-04", "schedule": "10:30 拉花咖啡"},
        {"date": "2026-11-05", "schedule_date": "2026-11-04", "schedule": "10:30 拉花咖啡"},
        {"date": "not-a-date", "timeline": [{"time": "10:30", "content": "拉花咖啡"}]},
        {"schedule": "10:30 拉花咖啡"},
    ],
)
def test_unknown_invalid_or_conflicting_dates_never_promote_schedule_to_current(data):
    projection = project_life_context(data, _clock("2026-11-05T01:19:00"))
    assert projection.date_relation == "unknown"
    assert projection.current_plan is None
    assert "当前活动：未知" in projection.image_context()
    assert "拉花咖啡" not in projection.image_context()


def test_datetime_source_date_is_converted_into_configured_calendar():
    clock = _clock("2026-11-04T17:19:00")
    projection = project_life_context({"meta": {"date": "2026-11-04T23:59:00-05:00"}}, clock)
    assert projection.source_date == date(2026, 11, 5)
    assert projection.date_relation == "today"


def test_future_and_stale_dates_never_become_current_or_active_outfits():
    clock = _clock("2026-11-05T01:19:00")
    future = project_life_context({"date": "2026-11-06", "outfit": "未来服装", "timeline": [{"time": "00:30", "content": "未来计划"}]}, clock)
    assert future.current_plan is None
    assert "未来服装" in future.caption_context() and "来源日期未确认或不是今日" in future.caption_context()
    stale = project_life_context({"date": "2026-11-03", "outfit": "旧衣服", "schedule": "18:00 旧地点"}, clock)
    assert stale.current_plan is None
    assert "旧衣服" not in stale.caption_context() and "旧地点" not in stale.caption_context()
    assert "早于昨日" in stale.caption_context()


def test_invalid_times_are_unclassified_and_unknown_reference_is_explicit():
    projection = project_life_context({
        "date": "2026-11-05",
        "timeline": [{"time": "25:99", "content": "invalid time content"}],
        "schedule": "some unsupported note",
    }, _clock("2026-11-05T01:19:00"))
    assert projection.current_plan is None
    assert "invalid time content" in projection.caption_context()


def test_overflow_does_not_select_a_misleading_prefix():
    clock = _clock("2026-11-05T01:19:00")
    projection = project_life_context({
        "date": "2026-11-05",
        "timeline": [{"time": "00:01", "content": "valid early prefix"}] * 257,
    }, clock)
    assert projection.overflow
    assert projection.current_plan is None
    assert "超出时间筛选边界" in projection.image_context()
    assert "valid early prefix" not in projection.image_context()
    long_schedule = project_life_context({"date": "2026-11-05", "schedule": "x" * 12001}, clock)
    assert long_schedule.overflow and long_schedule.current_plan is None


def test_input_object_preserves_source_date_and_utc_offset_zone_needs_no_iana_data(monkeypatch):
    class Source:
        date = "2026-11-05"
        outfit = "浅色卫衣"
        timeline = [{"time": "01:00", "content": "当前区间前点状计划"}]
    projection = project_life_context(Source(), _clock("2026-11-04T17:19:00"))
    assert projection.date_relation == "today"
    assert "浅色卫衣" in projection.caption_context()
    def unavailable(_name):
        raise life_context.ZoneInfoNotFoundError("simulated missing zone database")
    monkeypatch.setattr(life_context, "ZoneInfo", unavailable)
    fixed = capture_life_inputs("UTC+08:00", utc_now=datetime(2026, 11, 4, 17, 19, tzinfo=timezone.utc)).clock
    assert fixed.local_now.isoformat() == "2026-11-05T01:19:00+08:00"
    with pytest.raises(LifeTimezoneError):
        capture_life_inputs("Mars/Olympus", utc_now=datetime(2026, 11, 4, 17, 19, tzinfo=timezone.utc))


@pytest.mark.parametrize("value", ["UTC+24:00", "UTC+08:60", "", "not/a-zone"])
def test_invalid_timezone_fails_with_fixed_configuration_error(value):
    with pytest.raises(LifeTimezoneError) as caught:
        capture_life_inputs(value, utc_now=datetime(2026, 11, 5, 1, 19, tzinfo=timezone.utc))
    assert str(caught.value) == "Life 时区无效；请设置有效 IANA 时区（如 Asia/Shanghai）或 UTC+08:00。"


def test_operation_inputs_freeze_style_and_single_injected_instant():
    instant = datetime(2026, 11, 4, 17, 19, tzinfo=timezone.utc)
    inputs = capture_life_inputs("Asia/Shanghai", "frozen-style", utc_now=instant)
    assert inputs.clock.utc_now == instant
    assert inputs.clock.local_now.hour == 1 and inputs.clock.local_now.day == 5
    assert inputs.image_style_prompt == "frozen-style"
