"""项目级风格文案：视觉风格不能把「整体风格关键词」顶掉。"""
from __future__ import annotations

from types import SimpleNamespace

from app.services import prompts
from app.services.generation import _project_style_text, sheet_prompt_for


def _project(visual_style, style):
    return SimpleNamespace(visual_style=visual_style, style=style)


def test_style_text_merges_visual_and_global_keywords():
    text = _project_style_text(_project("现实", "电影感，冷色调，厚涂半写实"))
    assert text == "现实，电影感，冷色调，厚涂半写实"


def test_style_text_falls_back_and_dedupes():
    assert _project_style_text(_project(None, "国风水墨")) == "国风水墨"
    assert _project_style_text(_project("现实", "现实")) == "现实"
    assert _project_style_text(_project("  ", None)) is None
    assert _project_style_text(None) is None


def test_anime_style_becomes_hard_medium_requirement():
    """画风只写「动画质感」时模型仍出真人照片：必须显式声明画法媒介并排除照片质感。"""
    full = prompts.compose_generation_prompt(
        base="少女撑伞站在站台",
        kind="image",
        style="日系手绘动画质感，夏日傍晚柔光逆光",
    )

    assert "【画风硬要求】画法必须是：2D 手绘动画赛璐璐上色" in full
    assert "photorealistic" in full
    assert "【风格收束】" not in full


def test_photoreal_style_keeps_soft_style_block_without_anime_constraints():
    full = prompts.compose_generation_prompt(
        base="男人在巷口点烟",
        kind="image",
        style="写实电影感，冷色调，悬疑氛围",
    )

    assert "【风格收束】写实电影感" in full
    assert "画风硬要求" not in full
    assert "photorealistic" not in full


def test_3d_style_is_not_mistaken_for_2d_anime():
    rule = prompts.style_medium_rule("3D 动画，皮克斯质感")

    assert rule is not None
    assert rule[0].startswith("3D 动画渲染")


def test_asset_sheet_prompt_survives_lean_truncation():
    """定妆照的构图指令写在长外观描述末尾：lean 的 300 字截断会把它整段切掉，
    实测背景从灰幕布变成海边夕阳。定妆照必须走 lean=False。"""
    asset = SimpleNamespace(
        kind="actor",
        name="小满",
        prompt="女性，十八岁，鹅蛋脸偏圆；" + "齐肩黑色直发，中分，发质顺滑。；" * 12,
        description=None,
        attributes=None,
    )
    base, negative = sheet_prompt_for(asset)
    assert len(base) > 300

    lean = prompts.compose_generation_prompt(
        base=base, kind="image", style="日系手绘动画质感", negative=negative,
        with_quality=False, max_chars=None, lean=True,
    )
    full = prompts.compose_generation_prompt(
        base=base, kind="image", style="日系手绘动画质感", negative=negative,
        with_quality=False, max_chars=None, lean=False,
    )

    assert "三视图" not in lean
    assert "三视图" in full
    assert "。。" not in full
