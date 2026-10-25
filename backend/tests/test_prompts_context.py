"""结构化镜头上下文：解析、兜底与提示词拼装顺序。"""
from __future__ import annotations

import re
from types import SimpleNamespace

from app.services import prompts
from app.services.generation import (
    _breakdown_payload,
    _clamp_int,
    _normalize_resolution,
    _pick_references,
    _rescale_beats,
    _strip_timed_beats,
)


def test_parse_shot_context_extracts_all_fields():
    context = prompts.parse_shot_context(
        {
            "duration_seconds": 5,
            "subjects": [
                {
                    "name": "杨间",
                    "position": "画面左侧，面向右",
                    "action": "撑开黑伞",
                    "expression": "警惕",
                    "gaze": "看向巷口",
                    "holds": "黑色鬼伞",
                    "is_primary": True,
                }
            ],
            "props": [],
            "action_timeline": [
                {"start": 0, "end": 2, "action": "撑伞", "expression": "警觉"},
                {"start": 2, "end": 5, "action": "转身", "gaze": "望向巷口"},
            ],
            "start_state": {"location": "雨巷", "time_of_day": "夜"},
            "end_state": {"positions": "转身背对镜头"},
        }
    )

    assert context["subjects"][0]["name"] == "杨间"
    assert context["subjects"][0]["holds"] == "黑色鬼伞"
    # props 缺失时从 subjects.holds 反推，保证道具归属不丢
    assert context["props"] == [
        {
            "name": "黑色鬼伞",
            "asset_id": None,
            "owner": "杨间",
            "state": "",
            "description": "",
        }
    ]
    assert len(context["action_timeline"]) == 2
    assert context["action_timeline"][1]["end"] == 5
    assert context["start_state"]["location"] == "雨巷"


def test_normalize_timeline_clamps_and_falls_back_to_beats():
    # 起点等于总时长时不允许 end 溢出
    timeline = prompts.normalize_timeline(
        [{"start": 5, "end": 9, "action": "定格"}], 5
    )
    assert timeline[0]["start"] == 4.9
    assert timeline[0]["end"] == 5

    # 模型没给时间轴时，用 action_beats 均分时长兜底
    fallback = prompts.normalize_timeline(None, 6, ["抬眼", "撑伞", "转身"])
    assert [item["action"] for item in fallback] == ["抬眼", "撑伞", "转身"]
    assert fallback[0]["start"] == 0
    assert fallback[-1]["end"] == 6


def test_compose_image_prompt_orders_context_blocks():
    prompt = prompts.compose_generation_prompt(
        base="雨夜巷口，中景",
        kind="image",
        style="写实电影感",
        subjects=[
            {
                "name": "杨间",
                "position": "画面左侧",
                "action": "撑开黑伞",
                "holds": "黑色鬼伞",
                "is_primary": True,
            }
        ],
        props=[{"name": "黑色鬼伞", "owner": "杨间", "state": "撑开"}],
        references=[
            {"kind": "actor", "name": "杨间", "purpose": "本镜头出场角色"},
            {"kind": "prop", "name": "黑色鬼伞"},
        ],
        directives={"shot_size": "中景", "camera_movement": "缓慢推镜"},
        timeline=[
            {"start": 0, "end": 1.5, "action": "抬手撑伞", "expression": "警惕"},
            {"start": 1.5, "end": 3, "action": "抬眼看向巷口", "gaze": "巷口"},
        ],
        duration=3,
        start_state={"location": "雨巷"},
        end_state={"positions": "持伞站立"},
    )

    order = [
        prompt.index("雨夜巷口，中景"),
        prompt.index("【风格收束】"),
        prompt.index("【本镜头主体与道具归属"),
        prompt.index("【参考图对应关系"),
        # 场景锚点先于镜头语言与动作：先交代「在哪、什么光」，再说「怎么拍、做什么」
        prompt.index("【镜头起始状态】"),
        prompt.index("【镜头语言"),
        prompt.index("【本帧定格时刻】"),
        prompt.index("【画面质量与细节要求】"),
        prompt.index("【负面提示"),
    ]
    assert order == sorted(order)
    assert "图1：角色「杨间」" in prompt
    assert "图2：道具「黑色鬼伞」" in prompt
    assert "持有者=杨间" in prompt
    assert "定格在 0-1.5s 的瞬间：抬手撑伞" in prompt
    # 图片只要第一拍定格，不应把整条时间轴塞进去
    assert "1.5-3s" not in prompt


def test_compose_video_prompt_injects_full_timeline():
    prompt = prompts.compose_generation_prompt(
        base="跟拍雨巷",
        kind="video",
        timeline=[
            {"start": 0, "end": 1.5, "action": "抬手撑伞", "camera": "缓慢推镜"},
            {"start": 1.5, "end": 3, "action": "转身", "camera": "跟镜"},
        ],
        duration=3,
    )
    assert "【动作时间轴（镜头总时长 3 秒" in prompt
    assert "0-1.5s：抬手撑伞；运镜：缓慢推镜" in prompt
    assert "1.5-3s：转身；运镜：跟镜" in prompt


def test_subject_block_marks_primary_first_and_forbids_swap():
    block = prompts.build_subject_block(
        [
            {"name": "林默", "position": "右侧", "is_primary": False},
            {"name": "杨间", "position": "左侧", "holds": "黑色鬼伞", "is_primary": True},
        ],
        [{"name": "黑色鬼伞", "owner": "杨间"}],
    )
    assert block.index("杨间") < block.index("林默")
    assert "不得把道具换给其他角色" in block


_ASSETS = [
    {"id": 1, "kind": "actor", "name": "杨间", "reference_url": "https://x/yang.png"},
    {"id": 2, "kind": "actor", "name": "林默", "reference_url": "https://x/lin.png"},
    {"id": 3, "kind": "prop", "name": "黑色鬼伞", "reference_url": "https://x/umbrella.png"},
    {"id": 4, "kind": "scene", "name": "雨巷", "reference_url": "https://x/alley.png"},
]


def _shot_with(subjects, props):
    return SimpleNamespace(subjects=subjects, props=props)


def test_pick_references_only_sends_on_screen_actors():
    shot = _shot_with(
        [{"name": "杨间", "holds": "黑色鬼伞"}],
        [{"name": "黑色鬼伞", "owner": "杨间"}],
    )
    refs = _pick_references(_ASSETS, shot=shot)

    names = [item["name"] for item in refs]
    assert names == ["杨间", "黑色鬼伞", "雨巷"]
    # 本镜头没出场的林默不能作为参考图送进去，避免脸串角色
    assert "林默" not in names


def test_pick_references_honors_explicit_selection():
    shot = _shot_with([{"name": "杨间"}], [])
    refs = _pick_references(_ASSETS, shot=shot, explicit_ids=[2])
    assert refs[0]["name"] == "林默"
    assert refs[0]["purpose"].startswith("用户显式指定")


def test_pick_references_falls_back_to_all_actors_for_legacy_shots():
    refs = _pick_references(_ASSETS, shot=_shot_with([], []))
    names = [item["name"] for item in refs]
    assert names[:2] == ["杨间", "林默"]


def test_shot_field_prompt_carries_script_subjects_and_timeline():
    prompt = prompts.build_shot_field_prompt(
        field="video_prompt",
        title="雨巷撑伞",
        description="杨间在雨巷撑开黑伞",
        dialogue=None,
        style="写实电影感",
        aspect_ratio="9:16",
        script="上一场：杨间追进雨巷。",
        characters=[{"name": "杨间", "appearance": "短碎发", "costume": "黑色连帽卫衣"}],
        subjects=[{"name": "杨间", "action": "撑伞", "holds": "黑色鬼伞"}],
        props=[{"name": "黑色鬼伞", "owner": "杨间", "state": "撑开"}],
        timeline=[
            {"start": 0, "end": 1.5, "action": "抬手撑伞"},
            {"start": 1.5, "end": 3, "action": "转身"},
        ],
        duration=3,
    )
    assert "本集剧本上下文" in prompt
    assert "短碎发" in prompt
    assert "持有者=杨间" in prompt
    assert "0-1.5s：抬手撑伞" in prompt
    assert "1.5-3s：转身" in prompt


def test_skills_guidance_is_chinese_and_seconds_based():
    from app.services.skills import SHOT_SKILLS

    assert "为英文" not in SHOT_SKILLS["keyframe_prompt"].guidance
    assert "为英文" not in SHOT_SKILLS["video_prompt"].guidance
    assert "0.0-1.5s" in SHOT_SKILLS["video_prompt"].guidance


def test_video_prompt_uses_compact_blocks_and_keeps_timeline():
    prompt = prompts.compose_generation_prompt(
        base="雨夜教室后门，冷色调漫射光。",
        kind="video",
        timeline=[
            {"start": 0, "end": 3, "action": "林越拽住陈默。", "camera": "手持轻微晃动"},
            {"start": 3, "end": 6, "action": "陈默转身。", "gaze": "看向林越"},
        ],
        subjects=[
            {
                "name": "陈默",
                "action": "这个动作只应出现在时间轴里",
                "holds": "保温杯",
                "is_primary": True,
            }
        ],
        props=[{"name": "保温杯", "owner": "陈默"}],
        directives={
            "shot_size": "中景",
            "camera_movement": "缓慢推镜",
            "composition": "中心构图。门占据画面主体。",
            "script_excerpt": "这一段不应进入提示词",
        },
        start_state={"location": "教室后门", "positions": "陈默在右"},
        end_state={"positions": "两人僵持"},
        duration=6,
        max_chars=prompts.MAX_VIDEO_PROMPT_CHARS,
    )
    assert len(prompt) <= prompts.MAX_VIDEO_PROMPT_CHARS
    # 动作只在时间轴里出现一次；base 与人物卡都不再重复
    assert "这个动作只应出现在时间轴里" not in prompt
    assert "0-3s：林越拽住陈默；运镜：手持轻微晃动" in prompt
    assert "3-6s：陈默转身；视线：看向林越" in prompt
    # 镜头语言压缩后仍在，整块脚本摘录不进来
    assert "【镜头语言】" in prompt
    assert "运镜：缓慢推镜" in prompt
    assert "这一段不应进入提示词" not in prompt
    # 精简档只留底线：不出现多余手臂，场景与人物不变
    assert "只画持物的那只手" not in prompt
    assert "不出现多余手臂" in prompt
    assert "同场景同人物" in prompt
    assert "动作按时间轴推进" in prompt


def test_video_subject_block_compact_skips_duplicated_action():
    block = prompts.build_subject_block(
        [
            {
                "name": "陈默",
                "position": "画面右侧",
                "action": "被拽得转身",
                "expression": "空洞",
                "holds": "保温杯",
            }
        ],
        [{"name": "保温杯", "owner": "陈默"}],
        compact=True,
    )
    assert "被拽得转身" not in block
    assert "画面右侧" in block
    assert "空洞" in block
    assert "保温杯" in block
    assert "只画持物的那只手" not in block


def test_video_quality_lean_keeps_bottom_lines_short():
    for text in (
        prompts.build_quality_block(kind="video", aspect_ratio="9:16"),
        prompts.build_quality_block(kind="video", aspect_ratio="9:16", compact=True),
        prompts.build_quality_block(kind="video", aspect_ratio="9:16", lean=True),
    ):
        assert "同场景同人物" in text
        assert "动作按时间轴推进" in text
        assert "位置、朝向与服装全程延续首帧" not in text
        assert "只允许在本场景内做小幅推、拉、摇、移" not in text
        # 同一段底线每镜都要付字数，必须压在 120 字以内
        assert len(text) <= 120


def test_video_empty_shot_quality_has_no_hand_requirements():
    text = prompts.build_quality_block(
        kind="video", aspect_ratio="9:16", compact=True, empty_shot=True
    )
    assert "双手" not in text
    assert "人体结构" not in text
    assert "不新增人物或人体痕迹" in text


def test_video_negative_is_compact_by_default():
    prompt = prompts.compose_generation_prompt(base="雨夜教室后门。", kind="video")
    assert prompts.DEFAULT_NEGATIVE not in prompt
    assert "deformed" in prompt
    assert "bad anatomy" in prompt
    assert "extra limbs" in prompt


def test_image_prompt_uses_compact_negative_and_keeps_quality():
    prompt = prompts.compose_generation_prompt(base="雨夜巷口，中景", kind="image")
    assert "【画面质量与细节要求】" in prompt
    # 图片与视频统一走短负面词表：完整词表 400+ 字，实测会挤掉镜头语言
    assert prompts.DEFAULT_NEGATIVE not in prompt
    assert "bad anatomy" in prompt
    assert len(prompt) <= prompts.MAX_IMAGE_PROMPT_CHARS


def test_strip_timed_beats_keeps_scene_anchor():
    text = (
        "全程固定在教室后门。0.0-3.0s：林越拽住陈默。"
        "3.0-6.0s：陈默转身。保持同一场景与光线。"
    )
    stripped = _strip_timed_beats(text)
    assert "0.0-3.0s" not in stripped
    assert "3.0-6.0s" not in stripped
    assert "全程固定在教室后门。" in stripped
    assert "保持同一场景与光线。" in stripped


def test_video_prompt_trims_base_at_sentence_boundary():
    prompt = prompts.compose_generation_prompt(
        base="场景描述。" * 400,
        kind="video",
        timeline=[{"start": 0, "end": 3, "action": "转身"}],
        duration=3,
        max_chars=900,
    )
    assert len(prompt) <= 900
    first = prompt.split("\n\n")[0]
    assert first.endswith("…")
    assert "场景描述。" in first
    assert not re.search(r"\d+\.\d*…$", first)



def test_normalize_resolution_only_keeps_supported_bands():
    assert _normalize_resolution("1080P") == "1080p"
    assert _normalize_resolution("1920x1080") == "1080p"
    assert _normalize_resolution("720p") == "720p"
    # 模型自造的值不能直接透传给厂商，否则整批视频请求都会 400
    assert _normalize_resolution("4K") is None
    assert _normalize_resolution(None) is None
    assert _normalize_resolution("") is None


def test_clamp_int_and_rescale_beats_keep_plan_totals_consistent():
    assert _clamp_int("12", minimum=1, maximum=60, fallback=8) == 12
    assert _clamp_int(999, minimum=1, maximum=60, fallback=8) == 60
    assert _clamp_int(None, minimum=1, maximum=60, fallback=8) == 8
    beats = [{"name": "钩子", "shots": 3}, {"name": "转折", "shots": 3}]
    scaled = _rescale_beats(beats, 6, 9)
    assert sum(beat["shots"] for beat in scaled) == 9


def test_breakdown_system_requires_per_shot_duration_and_resolution():
    assert "【逐镜时长】" in prompts.SCRIPT_BREAKDOWN_SYSTEM
    assert "【逐镜分辨率】" in prompts.SCRIPT_BREAKDOWN_SYSTEM
    assert '"resolution"' in prompts._SHOT_FIELD_LINES["resolution"]
    assert "resolution" in prompts._SHOT_ALWAYS_FIELDS


def test_breakdown_prompt_locks_user_confirmed_plan():
    prompt = prompts.build_breakdown_prompt(
        title="雨巷撑伞",
        script="杨间撑开黑伞，鬼犬从巷口扑入。",
        style="写实电影感",
        aspect_ratio="9:16",
        shot_count=8,
        plan={
            "reasoning": "三个拍点递进，先把伞立起来再放鬼犬",
            "beats": [
                {"name": "开场钩子", "shots": 3, "seconds": 24, "summary": "杨间进巷"},
                {"name": "异象逼近", "shots": 5, "seconds": 40, "summary": "鬼犬扑入"},
            ],
        },
    )
    assert "【已确认的拆解方案（必须严格遵守）】" in prompt
    assert "1. 开场钩子：3 镜" in prompt
    assert "2. 异象逼近：5 镜" in prompt
    assert "三个拍点递进" in prompt


def test_shot_plan_prompt_only_asks_for_planning():
    prompt = prompts.build_shot_plan_prompt(
        title="雨巷撑伞",
        script="杨间撑开黑伞。",
        style="写实电影感",
        aspect_ratio="9:16",
        shot_count=8,
        target_seconds=64,
    )
    assert "不要直接写分镜内容" in prompt
    assert "拆成 8 个镜头" in prompt
    assert "64 秒" in prompt
    assert "image_prompt" not in prompt


def test_lean_prompt_keeps_every_layer_with_full_body():
    body = "雨巷尽头。" + "青黑色雾气贴着地面漫过来，路灯把水洼照成一块碎掉的镜子。" * 12
    _, blocks = prompts.compose_generation_prompt(
        base=body,
        kind="video",
        style="写实电影感，冷色调。",
        aspect_ratio="9:16",
        subjects=[{"name": "杨间", "position": "画面左侧半跪", "holds": "黑色鬼伞", "is_primary": True}],
        props=[{"name": "黑色鬼伞", "owner": "杨间", "state": "撑开滴水"}],
        timeline=[
            {"start": 0, "end": 4, "action": "撑开黑伞挡住扑来的鬼犬", "camera": "缓慢推镜"},
            {"start": 4, "end": 8, "action": "鬼犬绕开伞面扑向肩头", "camera": "跟镜"},
        ],
        duration=8,
        start_state={"location": "老城北门雨巷", "light": "路灯冷白侧逆光"},
        directives={"shot_size": "中景", "camera_movement": "缓慢推镜"},
        references=[{"name": "杨间", "kind": "actor", "purpose": "长相与服装"}],
        max_chars=prompts.MAX_VIDEO_PROMPT_CHARS,
        with_breakdown=True,
    )
    # 正文收到 200-300 字这一段，硬约束不能被预算整块丢掉（曾经丢掉 quality 层导致人形畸变回潮）
    sizes = {item["tag"]: item["chars"] for item in blocks}
    assert 200 <= sizes["base"] <= 300
    assert [tag for tag, chars in sizes.items() if chars == 0] == []
    assert sizes["quality"] > 0 and sizes["negative"] > 0


def test_segment_breakdown_prompt_only_writes_one_beat():
    prompt = prompts.build_breakdown_prompt(
        title="雨夜教室",
        script="林越拽住陈默的袖子，陈默抬头眼神空洞。",
        style="写实电影感",
        aspect_ratio="9:16",
        shot_count=4,
        plan={
            "reasoning": "四段递进",
            "beats": [
                {"name": "空教室与趴桌者", "shots": 2, "seconds": 14, "summary": "教室只剩两人"},
                {"name": "靠近与试探", "shots": 4, "seconds": 28, "summary": "林越拽袖"},
            ],
        },
        segment={
            "name": "靠近与试探",
            "shots": 4,
            "seconds": 28,
            "summary": "林越慢慢走向陈默，拽住他的袖子",
            "seg_index": 2,
            "seg_total": 2,
        },
        order_start=3,
        previous_shot="陈默趴在最后一排，脸埋在臂弯里",
    )
    assert "只拆第 2/2 段「靠近与试探」" in prompt
    assert "只输出本段 4 个镜头" in prompt
    assert "order_index 从 3 开始连续编号" in prompt
    assert "【上一镜的结束状态（本段第 1 镜必须承接，保证画面接得上）】" in prompt
    # 分段轮次要更省：整集方案已由用户确认，不再让模型重复输出 plan
    assert "【拆解规划（先规划后拆镜）】" not in prompt
    assert '"plan"' not in prompt


def test_breakdown_payload_splits_confirmed_plan_into_beats():
    import asyncio

    plan = {
        "reasoning": "四段递进",
        "beats": [
            {"name": "空教室与趴桌者", "shots": 2, "seconds": 14, "summary": "教室只剩两人"},
            {"name": "入场与停步", "shots": 3, "seconds": 21, "summary": "林越进门停步"},
            {"name": "靠近与试探", "shots": 4, "seconds": 28, "summary": "拽袖抬头"},
            {"name": "无声拒绝与环境惊变", "shots": 3, "seconds": 21, "summary": "推杯熄灯"},
        ],
    }

    class FakeProvider:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def generate_text(self, prompt, **kwargs):
            self.prompts.append(prompt)
            beat = plan["beats"][len(self.prompts) - 1]
            shots = [
                {
                    "order_index": i + 1,
                    "title": f"{beat['name']}第{i + 1}镜",
                    "description": "描述",
                    "image_prompt": "正文",
                    "video_prompt": "运动",
                    "end_state": {"positions": "站在陈默桌边"},
                }
                for i in range(int(beat["shots"]))
            ]
            return '{"synopsis": "梗概", "shots": []}'.replace(
                "[]",
                str(shots).replace("'", '"'),
            )

    provider = FakeProvider()
    payload = asyncio.run(
        _breakdown_payload(
            provider,
            model=None,
            title="雨夜教室",
            script="夜自习最后一节，教室只剩林越和陈默。",
            style="写实电影感",
            aspect_ratio="9:16",
            shot_count=12,
            target_seconds=84,
            characters=None,
            skills=None,
            confirmed_plan=plan,
        )
    )
    # 一次调用要写 12 镜会被输出上限截断（实测只剩 8 镜），现在按 4 段各写一次
    assert len(provider.prompts) == 4
    assert len(payload["shots"]) == 12
    assert payload["synopsis"] == "梗概"
    # 段内 order_index 全部丢掉，交给落库时按整集顺序重排
    assert all("order_index" not in item for item in payload["shots"])
    assert "只拆第 4/4 段" in provider.prompts[-1]
    # 后一段要承接前一段末镜，否则切点接不上
    assert "【上一镜的结束状态" in provider.prompts[1]


def test_breakdown_payload_keeps_single_call_for_short_episodes():
    import asyncio

    class FakeProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_text(self, prompt, **kwargs):
            self.calls += 1
            return '{"plan": {"beats": []}, "synopsis": "梗概", "shots": [{"title": "镜一", "description": "d", "image_prompt": "p"}]}'

    provider = FakeProvider()
    payload = asyncio.run(
        _breakdown_payload(
            provider,
            model=None,
            title="雨夜教室",
            script="两个镜头就够。",
            style=None,
            aspect_ratio="9:16",
            shot_count=2,
            target_seconds=None,
            characters=None,
            skills=None,
            confirmed_plan={
                "beats": [
                    {"name": "开场", "shots": 1, "seconds": 8, "summary": "进门"},
                    {"name": "收尾", "shots": 1, "seconds": 8, "summary": "回头"},
                ]
            },
        )
    )
    assert provider.calls == 1
    assert len(payload["shots"]) == 1


def test_full_video_layers_survive_the_budget():
    """带角色图例、人数核对与相邻镜衔接的完整视频层不能被预算挤掉（曾经丢 quality）。"""
    body = "空荡的夜自习教室只剩两个人，最后一排的陈默趴在桌上。" * 6
    _, blocks = prompts.compose_generation_prompt(
        base=body[:300],
        kind="video",
        style="写实电影感，冷色调，夜戏。",
        aspect_ratio="9:16",
        subjects=[
            {"name": "林越", "position": "画面左侧讲台边", "holds": "一叠练习册", "is_primary": True},
            {"name": "陈默", "position": "右侧最后一排课桌后", "is_primary": False},
        ],
        props=[{"name": "黑色保温杯", "owner": "陈默", "state": "立着没动"}],
        timeline=[
            {"start": 0, "end": 3, "action": "推门进来", "camera": "固定"},
            {"start": 3, "end": 6, "action": "停步看向陈默", "camera": "缓推"},
            {"start": 6, "end": 9, "action": "把练习册放上讲台", "camera": "跟镜"},
        ],
        duration=9,
        start_state={"location": "夜自习教室", "light": "日光灯冷白"},
        end_state={"positions": "站在陈默桌边", "expression": "不安"},
        directives={"shot_size": "中景", "camera_movement": "缓慢推镜"},
        references=[
            {"name": "林越", "kind": "actor", "purpose": "长相与服装"},
            {"name": "陈默", "kind": "actor", "purpose": "长相与服装"},
        ],
        continuity="上一镜结束在陈默趴在桌上的特写；下一镜从林越伸手拽袖子接。",
        extra="窗外有雷声。",
        max_chars=prompts.MAX_VIDEO_PROMPT_CHARS,
        with_breakdown=True,
    )
    sizes = {item["tag"]: item["chars"] for item in blocks}
    assert [tag for tag, chars in sizes.items() if chars == 0] == []
    assert 0 < sizes["base"] <= 300
