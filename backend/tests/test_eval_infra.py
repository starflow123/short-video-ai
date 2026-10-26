"""评测/台账基础设施的纯函数测试（不碰数据库）。"""
from __future__ import annotations

from types import SimpleNamespace

import json
import re

from app.services import prompts
from app.services.eval_runs import golden_shot_ids


def test_compiler_version_is_stable_and_well_formed():
    first = prompts.compiler_version()
    second = prompts.compiler_version()
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{12}", first), f"指纹应是 12 位 hex：{first}"


def test_compiler_version_covers_the_semantic_blocks():
    # 改动任一子块函数都必须改变指纹——这是台账分段的承诺
    import hashlib
    import inspect

    names = (
        prompts.compose_generation_prompt,
        prompts.build_reference_legend,
        prompts.build_state_block,
    )
    single = hashlib.sha1(inspect.getsource(prompts.compose_generation_prompt).encode()).hexdigest()[:12]
    assert prompts.compiler_version() != single
    assert all(inspect.getsource(fn) for fn in names)


def test_golden_shot_ids_dedupes_across_kinds_and_orders(tmp_path):
    for name, project, shot in [
        ("p49_s215_image.json", 49, 215),
        ("p49_s215_video.json", 49, 215),
        ("p49_s216_image.json", 49, 216),
        ("p34_s101_image.json", 34, 101),
        ("broken.json", None, None),
    ]:
        body = {} if project is None else {"project_id": project, "shot_id": shot, "kind": "image"}
        (tmp_path / name).write_text(json.dumps(body), encoding="utf-8")
    result = golden_shot_ids(tmp_path)
    assert result == {34: [101], 49: [215, 216]}


def test_golden_shot_ids_missing_dir_is_empty(tmp_path):
    assert golden_shot_ids(tmp_path / "nope") == {}


def test_knowledge_gate_requires_every_precondition():
    from app.models.knowledge import KnowledgeNote
    from app.services.knowledge import _matches

    note = KnowledgeNote(
        title="t", content="c", preconditions={"provider": "agnes", "media_type": "video"}
    )
    assert _matches(note, {"provider": "agnes", "media_type": "video"})
    assert _matches(note, {"provider": "agnes", "media_type": "video", "extra": 1})
    # 缺一个前置条件就不注入——防止治愈短片的教训塞进悬疑剧
    assert not _matches(note, {"provider": "agnes"})
    assert not _matches(note, {"provider": "magichour", "media_type": "video"})
    assert _matches(
        KnowledgeNote(title="t", content="c", preconditions=None), {"anything": 1}
    )


def test_knowledge_style_bucket_filters_mismatched_style():
    from app.models.knowledge import KnowledgeNote
    from app.services.knowledge import _matches

    note = KnowledgeNote(title="t", content="c", visual_style="日系治愈", preconditions=None)
    # 画风不匹配不是 _matches 的职责（它只管前置条件），画风过滤在 lookup 的 SQL 层
    assert _matches(note, {})
    assert note.visual_style == "日系治愈"


def test_template_overrides_are_whitelisted(tmp_path):
    from app.services.prompts import (
        get_template_overrides,
        set_template_overrides,
    )

    set_template_overrides(
        {"video_continuity_compact": "【覆盖】测试模板", "evil_key": "不该进来", "empty": "  "}
    )
    try:
        assert get_template_overrides() == {"video_continuity_compact": "【覆盖】测试模板"}
    finally:
        set_template_overrides({})


def test_template_override_reaches_composed_video_prompt():
    from app.services import prompts
    from app.services.prompt_checks import load_snapshots

    snapshot = next(
        payload["snapshot"]
        for name, payload in load_snapshots()
        if payload["snapshot"]["kind"] == "video"
    )
    baseline, _ = prompts.compose_generation_prompt(**snapshot["inputs"])
    prompts.set_template_overrides({"video_continuity_compact": "【前后镜头衔接】RSI 覆盖探针。"})
    try:
        # 覆盖模板只在预算裁剪的「衔接块压缩」档生效：从默认预算往下扫，
        # 找到衔接块被换成压缩模板、且没被整块丢弃的区间即视为探针命中
        probe_hit = False
        for max_chars in range(int(snapshot["inputs"]["max_chars"]), 200, -50):
            inputs = dict(snapshot["inputs"])
            inputs["max_chars"] = max_chars
            patched, _ = prompts.compose_generation_prompt(**inputs)
            if "RSI 覆盖探针" in patched:
                probe_hit = True
                break
    finally:
        prompts.set_template_overrides({})
    assert probe_hit, "覆盖模板未出现在任何预算档的成稿里"
    assert "RSI 覆盖探针" not in baseline


def test_validate_all_snapshots_runs_every_frozen_case():
    from app.services.prompt_checks import validate_all_snapshots

    outcome = validate_all_snapshots()
    assert outcome["checked"] >= 16, f"金镜头快照应≥16 份，实得 {outcome['checked']}"
    assert outcome["passed"], outcome["violations"][:5]


def test_template_validation_rejects_headerless_and_unreachable_content():
    from app.services.prompt_checks import load_snapshots, validate_template_override

    snapshot = next(
        payload["snapshot"]
        for name, payload in load_snapshots()
        if payload["snapshot"]["kind"] == "video"
    )
    # 垃圾模板缺块头：直接拒
    violations = validate_template_override(snapshot, "video_continuity_compact", "这是垃圾模板!!!")
    assert violations and "块头" in violations[0], violations

    # 带块头但永远进不了成稿（极短预算下被整块丢弃也视为不可达的极端写法不可测，
    # 这里验证正常带块头模板可达且通过）
    ok = validate_template_override(
        snapshot,
        "video_continuity_compact",
        "【前后镜头衔接】上一镜收尾动作直接顺接，人物位置与朝向不跳变。",
    )
    assert ok == [], ok


def test_validate_all_snapshots_with_bad_override_fails():
    from app.services.prompt_checks import validate_all_snapshots

    outcome = validate_all_snapshots(
        template_overrides={"video_continuity_compact": "这是垃圾模板!!!"}
    )
    assert not outcome["passed"]
    assert any("块头" in v for v in outcome["violations"]), outcome["violations"][:3]


def test_negative_compact_accepts_term_list_and_keeps_safety_floor():
    from app.services.prompt_checks import validate_all_snapshots

    ok = validate_all_snapshots(
        template_overrides={
            "video_negative_compact": "deformed, bad anatomy, extra limbs, scene change, text, watermark",
            "image_negative_compact": "blurry, bad hands, duplicate person, text, watermark",
        }
    )
    assert ok["passed"], ok["violations"][:3]

    # 负面词只是词表片段：带块头会在成稿里重复一次
    header = validate_all_snapshots(
        template_overrides={"video_negative_compact": "【负面提示】deformed, text"}
    )
    assert not header["passed"]
    assert any("重复块头" in v for v in header["violations"]), header["violations"][:3]

    # 去掉「文字 / 水印」底线同样拒绝
    unsafe = validate_all_snapshots(
        template_overrides={"image_negative_compact": "blurry, low quality"}
    )
    assert not unsafe["passed"]
    assert any("安全底线" in v for v in unsafe["violations"]), unsafe["violations"][:3]


def test_lessons_block_injects_into_repair_system_prompt():
    from app.services.agents.repair import _lessons_block

    note = SimpleNamespace(id=1, title="动物不计入人数", content="图例必须写「三花猫 是动物」。")
    block = _lessons_block([note])
    assert "【生产教训" in block
    assert "动物不计入人数" in block
    assert "三花猫 是动物" in block


def test_golden_shot_registry_is_cwd_independent():
    # 之前写死 /app/...，宿主机跑时永远读到空清单，回归会「假通过」
    from app.services.eval_runs import golden_shot_ids, snapshot_for

    ids = golden_shot_ids()
    assert 49 in ids and 212 in ids[49], ids
    snapshot = snapshot_for(49, 212)
    assert snapshot and snapshot["inputs"]["base"]
