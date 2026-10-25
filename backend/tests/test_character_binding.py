"""拆解链路的角色建档与绑定：先有演员资产，关键帧才能锁住同一张脸。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.assets import _actor_look, _match_asset
from app.services.generation import (
    _drop_library_faces,
    _extract_character_profiles,
    _missing_subject_names,
)


def test_match_asset_prefers_exact_name_over_partial():
    actors = [
        SimpleNamespace(id=1, name="杨间（驭鬼者成神形态）"),
        SimpleNamespace(id=2, name="杨间"),
    ]
    assert _match_asset(actors, "杨间").id == 2


def test_match_asset_falls_back_to_partial_match():
    actors = [SimpleNamespace(id=7, name="杨间（驭鬼者成神形态）")]
    assert _match_asset(actors, "杨间").id == 7
    assert _match_asset(actors, "周正") is None


def test_actor_look_joins_appearance_and_costume():
    assert _actor_look({"appearance": "短发", "costume": "灰色风衣"}) == "短发；灰色风衣"
    assert _actor_look({"appearance": "", "costume": "校服"}) == "校服"
    assert _actor_look({}) == ""


def test_missing_subject_names_skips_known_and_bound():
    shots = [
        SimpleNamespace(
            subjects=[
                {"name": "林越", "asset_id": None},
                {"name": "陈默", "asset_id": 50},
                {"name": "班主任", "asset_id": None},
                {"name": "班主任", "asset_id": None},
            ]
        )
    ]
    assert _missing_subject_names(shots, ["林越"]) == ["班主任"]


def test_extract_character_profiles_drops_names_not_in_script():
    class FakeProvider:
        async def generate_text(self, prompt, **kwargs):
            return (
                '{"characters": ['
                '{"name": "林越", "appearance": "短发", "costume": "校服"},'
                '{"name": "张三", "appearance": "不存在的人"}'
                "]}"
            )

    profiles = asyncio.run(
        _extract_character_profiles(
            FakeProvider(),
            script="林越坐在教室最后一排",
            style=None,
            aspect_ratio="9:16",
            model=None,
        )
    )
    assert [item["name"] for item in profiles] == ["林越"]


def test_extract_character_profiles_degrades_to_empty_on_unparsable_json():
    class BrokenProvider:
        async def generate_text(self, prompt, **kwargs):
            return "抱歉，我无法输出 JSON"

    profiles = asyncio.run(
        _extract_character_profiles(
            BrokenProvider(),
            script="林越坐在教室",
            style=None,
            aspect_ratio="9:16",
            model=None,
        )
    )
    assert profiles == []


def test_drop_library_faces_keeps_characters_without_existing_sheet():
    shared = [
        SimpleNamespace(name="杨间", media_id=357),
        SimpleNamespace(name="周正", media_id=None),
    ]
    profiles = [
        {"name": "杨间", "appearance": "新描述"},
        {"name": "周正", "appearance": "另一个描述"},
        {"name": "宋岚", "appearance": "短发"},
    ]
    kept = _drop_library_faces(profiles, shared)
    # 库里已有定妆照的杨间不再建档；没有图的两个照建
    assert [item["name"] for item in kept] == ["周正", "宋岚"]
