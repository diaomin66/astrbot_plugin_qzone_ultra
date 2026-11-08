import json

import pytest

from qzone_bridge.settings import PluginSettings


@pytest.mark.parametrize(
    ("key", "default", "low", "high"),
    [
        ("generation_timeout_seconds", 300, 30, 1800),
        ("image_timeout_seconds", 180, 10, 1200),
        ("text_timeout_seconds", 90, 5, 600),
    ],
)
def test_life_timeout_config_defaults_clamps_and_rejects_invalid(key, default, low, high):
    assert getattr(PluginSettings.from_mapping({}), f"life_publish_{key}") == default
    for value, expected in ((str(low - 1), low), (high + 1, high), (low + 1, low + 1)):
        data = {"life_publish": {key: value}}
        assert getattr(PluginSettings.from_mapping(data), f"life_publish_{key}") == expected
    for value in (None, True, "bad", 2.5, float("nan"), float("inf")):
        data = {"life_publish": {key: value}}
        assert getattr(PluginSettings.from_mapping(data), f"life_publish_{key}") == default


def test_life_timeout_schema_has_matching_numeric_controls_and_ranges():
    with open("_conf_schema.json", encoding="utf-8") as stream:
        schema = json.load(stream)
    expected = {
        "generation_timeout_seconds": (300, 30, 1800),
        "image_timeout_seconds": (180, 10, 1200),
        "text_timeout_seconds": (90, 5, 600),
    }
    for key, (default, minimum, maximum) in expected.items():
        item = schema["life_publish"]["items"][key]
        assert item["type"] == "int" and item["default"] == default
        assert item["slider"] == {"min": minimum, "max": maximum, "step": 1}
        assert "秒" in item["hint"]
