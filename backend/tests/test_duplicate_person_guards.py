"""分身根因回归：人数约束只数「人」，同场戏换机位也要认成同一场景。

实测（项目 49 · 日系治愈短片）：shot 215 / 216 的 subjects 是「小满 + 三花猫」，
提示词写的是「仅 小满、三花猫 2 人」，模型照数补一个人类，画面里就多了第二个小满。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services import prompts
from app.services.generation import _same_scene

_XIAOMAN = {
    "name": "小满",
    "appearance": "女性，十八岁，鹅蛋脸偏圆，齐肩黑色直发",
    "position": "画面右侧长椅",
}
_CAT = {
    "name": "三花猫",
    "appearance": "黑白橘三色短毛猫，成年，四爪皆白，肉垫粉色，尾巴根部黑色",
    "position": "蹲在小满脚边",
}


def test_animal_subject_is_not_counted_as_a_person():
    block = prompts.build_subject_block([_XIAOMAN, _CAT], lean=True)
    assert "仅 小满 1 人" in block
    assert "2 人" not in block
    assert "是动物" in block
    assert "三花猫" in block.split("【人数】")[1]


def test_animal_subject_full_block_counts_only_humans():
    block = prompts.build_subject_block([_XIAOMAN, _CAT])
    assert "画面中只有 1 名人物：小满" in block
    assert "不得拟人化" in block


def test_human_name_with_animal_surname_stays_a_person():
    block = prompts.build_subject_block(
        [{"name": "牛大爷", "appearance": "六十岁男性，圆脸，戴毡帽", "position": "柜台后"}],
        lean=True,
    )
    assert "仅 牛大爷 1 人" in block
    assert "是动物" not in block


def test_animal_only_shot_forbids_people():
    block = prompts.build_subject_block([_CAT])
    assert "本镜头没有人物" in block
    assert "三花猫" in block


def test_reference_count_check_separates_persons_and_animals():
    references = [{"kind": "continuity", "name": "上一镜头的关键帧", "url": "https://x/1.png"}]
    block = prompts.build_reference_count_check(references, [_XIAOMAN, _CAT])
    assert "就是这 1 位" in block
    assert "三花猫（动物）" in block
    assert "不得补成人类" in block


def _shot(order_index: int, location: str, subjects: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        order_index=order_index,
        start_state={"location": location},
        subjects=subjects,
    )


def test_same_scene_accepts_camera_change_within_one_location():
    bakery_door = _shot(5, "旧面包店门口的木质门槛前", [_XIAOMAN])
    bakery_window = _shot(7, "旧面包店木质橱窗前", [_XIAOMAN])
    assert _same_scene(bakery_window, bakery_door)
    bench = _shot(2, "海边小镇的旧巴士站（沿海公路旁）", [_XIAOMAN])
    bus_stop = _shot(1, "海边小镇的旧巴士站（沿海公路旁）", [_XIAOMAN])
    assert _same_scene(bench, bus_stop)


def test_same_scene_uses_the_most_specific_place_not_the_region():
    """地点串从大到小写：「…旧巴士站旁，通向面包店的窄巷口」本镜在巷口，不在巴士站。

    早先按逗号前第一段判，4 号镜（窄巷口）与 3 号镜（巴士站）被判成同一场戏，
    接力参考把上一镜的海边长椅构图整张抄了过来（实测 media 495 的构图不符项）。
    """
    alley = _shot(4, "海边小镇的旧巴士站旁，通向面包店的窄巷口", [_XIAOMAN])
    bus_stop = _shot(3, "海边小镇的旧巴士站（沿海公路旁）", [_XIAOMAN])
    assert not _same_scene(alley, bus_stop)
    assert not _same_scene(bus_stop, alley)


def test_same_scene_rejects_different_location_and_missing_anchor():
    bench = _shot(8, "海边长椅", [_XIAOMAN])
    bus_stop = _shot(1, "海边小镇的旧巴士站（沿海公路旁）", [_XIAOMAN])
    assert not _same_scene(bench, bus_stop)
    assert not _same_scene(bus_stop, _shot(2, "", [_XIAOMAN]))
    # 两边都没写 location 时沿用旧行为：算同一场戏，不至于把老数据的接力断掉
    assert _same_scene(_shot(2, "", [_XIAOMAN]), _shot(3, "", [_XIAOMAN]))


def test_lean_reference_legend_stays_a_single_compact_line():
    """图例被截成半句话 = 模型拿到 4 张参考图却不知道「图几是谁、几个人」。"""
    references = [
        {"kind": "actor", "name": "小满", "purpose": "本镜头出场角色"},
        {"kind": "actor", "name": "三花猫", "purpose": "本镜头出场角色"},
        {"kind": "prop", "name": "帆布包", "purpose": "本镜头道具"},
        {"kind": "continuity", "name": "第 3 镜头的关键帧", "purpose": "接上一帧"},
    ]
    legend = prompts.build_reference_legend(references, lean=True)

    assert "\n" not in legend
    assert legend.startswith("【参考图】")
    assert "图1=角色「小满」" in legend
    assert "图4=上一帧画面" in legend
    assert "张数≠人数" in legend
    # 压成一行是为了在 820 字预算里活下来：比多行版省一半以上
    assert len(legend) < 110
