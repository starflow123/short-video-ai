"""项目级风格文案：视觉风格不能把「整体风格关键词」顶掉。"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.generation import _project_style_text


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
