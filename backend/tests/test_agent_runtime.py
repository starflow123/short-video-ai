"""修复 agent 运行时：工具白名单、预算、错误恢复与完整轨迹。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.agents import repair, runtime


def _tool(name: str, kind: str = "read", max_calls: int | None = None, *, args=None, handler=None):
    async def default_handler(_: dict) -> runtime.ToolResult:
        return runtime.ToolResult(f"{name} 完成")

    return runtime.AgentTool(
        name,
        f"{name} 工具",
        args or {},
        handler or default_handler,
        kind=kind,
        max_calls=max_calls,
    )


def _decide(script: list[dict]):
    calls: list[str] = []

    async def decide(prompt: str) -> dict:
        calls.append(prompt)
        return script.pop(0)

    return decide, calls


def test_agent_runs_tool_loop_and_records_trace():
    seen: list[str] = []

    async def patch(args: dict) -> runtime.ToolResult:
        seen.append(f"patch:{args['patch']}")
        return runtime.ToolResult("已记录 patch")

    async def redraw(_: dict) -> runtime.ToolResult:
        seen.append("redraw")
        return runtime.ToolResult("候选帧已生成", data={"media_id": 9})

    async def review(_: dict) -> runtime.ToolResult:
        seen.append("review")
        return runtime.ToolResult("复审通过")

    decide, _ = _decide(
        [
            {"thought": "先写修正", "action": "patch_prompt", "args": {"patch": "补上柴刀"}},
            {"thought": "重绘", "action": "redraw", "args": {}},
            {"thought": "复审", "action": "review_candidate", "args": {}},
            {"thought": "通过", "action": "finish", "args": {"status": "repaired", "summary": "已修好"}},
        ]
    )
    run = asyncio.run(
        runtime.run_agent(
            goal="修复关键帧",
            tools=[
                _tool("patch_prompt", "write", args={"patch": "修正"}, handler=patch),
                _tool("redraw", "expensive", handler=redraw),
                _tool("review_candidate", handler=review),
            ],
            decide=decide,
            initial_observation="图里少了一把刀",
            max_steps=6,
            max_expensive_calls=1,
        )
    )

    assert run.status == "repaired"
    assert seen == ["patch:补上柴刀", "redraw", "review"]
    assert [item["action"] for item in run.trace] == [
        "patch_prompt",
        "redraw",
        "review_candidate",
        "finish",
    ]
    assert run.trace[1]["ok"] is True
    assert run.steps == 4


def test_unknown_tool_and_missing_args_are_observations_not_crashes():
    decide, _ = _decide(
        [
            {"action": "fly", "args": {}},
            {"action": "patch_prompt", "args": {}},
            {"action": "patch_prompt", "args": {"patch": "补光"}},
            {"action": "finish", "args": {"status": "needs_human"}},
        ]
    )
    run = asyncio.run(
        runtime.run_agent(
            goal="修复",
            tools=[_tool("patch_prompt", "write", args={"patch": "修正"})],
            decide=decide,
            initial_observation="观察",
            max_steps=6,
            max_consecutive_errors=3,
        )
    )

    assert run.status == "needs_human"
    failed = [item for item in run.trace if not item["ok"]]
    assert len(failed) == 2
    assert "未知工具" in failed[0]["observation"]
    assert "缺少参数" in failed[1]["observation"]


def test_expensive_budget_blocks_second_redraw():
    counter = {"n": 0}

    async def redraw(_: dict) -> runtime.ToolResult:
        counter["n"] += 1
        return runtime.ToolResult("已重绘")

    decide, _ = _decide(
        [
            {"action": "redraw", "args": {}},
            {"action": "redraw", "args": {}},
            {"action": "finish", "args": {"status": "needs_human"}},
        ]
    )
    run = asyncio.run(
        runtime.run_agent(
            goal="修复",
            tools=[_tool("redraw", "expensive", handler=redraw)],
            decide=decide,
            initial_observation="观察",
            max_steps=4,
            max_expensive_calls=1,
        )
    )

    assert counter["n"] == 1
    assert run.status == "needs_human"
    budget = [item for item in run.trace if "预算已用尽" in item["observation"]]
    assert len(budget) == 1


def test_tool_exception_is_reported_as_failed_step():
    async def boom(_: dict) -> runtime.ToolResult:
        raise RuntimeError("厂商超时")

    decide, _ = _decide(
        [
            {"action": "redraw", "args": {}},
            {"action": "finish", "args": {"status": "escalated"}},
        ]
    )
    run = asyncio.run(
        runtime.run_agent(
            goal="修复",
            tools=[_tool("redraw", "expensive", handler=boom)],
            decide=decide,
            initial_observation="观察",
            max_steps=3,
        )
    )

    assert run.status == "escalated"
    assert run.trace[0]["ok"] is False
    assert "厂商超时" in run.trace[0]["observation"]


def test_repair_observation_lists_failed_checks_and_hard_constraints():
    shot = SimpleNamespace(
        title="抢刀",
        image_prompt="杨间夺下柴刀",
        description="",
        subjects=[{"name": "杨间", "costume": "黑色校服", "holds": "柴刀"}],
        props=[{"name": "柴刀", "owner": "杨间", "state": "被握紧"}],
        directives={"shot_size": "中景", "camera_angle": "平视"},
    )
    keyframe = SimpleNamespace(id=3, label="起幅", media_id=77, prompt="杨间伸手")
    review = {
        "verdict": "fail",
        "checks": {"prop_holder": "fail", "cast_count": "pass"},
        "reasons": ["柴刀不在杨间手里"],
        "prompt_patch": "柴刀握在右手",
    }

    text = repair.build_initial_observation(
        shot=shot, keyframe=keyframe, review=review, max_rounds=2
    )

    assert "杨间" in text
    assert "柴刀" in text
    assert "道具归属" in text
    assert "柴刀不在杨间手里" in text
    assert "最多重绘 2 次" in text


def test_repair_patch_survives_budget_trimming():
    """正文/预算都会裁剪，修正说明必须独立成块并保留在最终提示词里。"""
    from app.services import prompts

    base = "杨间在雨夜的旧街举起柴刀的连续动作描写。" * 24
    patch = "柴刀必须在右手，刀尖朝下"
    composed = prompts.compose_generation_prompt(
        base=base,
        kind="image",
        repair_patch=patch,
        max_chars=360,
    )
    assert isinstance(composed, str)
    assert f"【本次修正】{patch}" in composed
    assert "除这条修正外" in composed
    assert len(composed) <= 360
    assert composed.index("【本次修正】") > composed.index("杨间在雨夜")
    # 预算不够时只压正文，修正块原样保留
    assert composed.count("杨间在雨夜") < 24


def test_repair_patch_is_not_lost_under_image_budget():
    """真实图片预算（820 字）下，长正文 + 长修正都远超预算时修正仍在。"""
    from app.services import prompts

    base = "陈默在暴雨的教室后门旁盯着玻璃上的雾气掌印，呼吸发紧。" * 20
    patch = "林越必须穿深蓝色拉链校服外套，" * 8
    composed = prompts.compose_generation_prompt(
        base=base,
        kind="image",
        repair_patch=patch,
        max_chars=prompts.MAX_IMAGE_PROMPT_CHARS,
    )
    assert isinstance(composed, str)
    assert "【本次修正】" in composed
    assert patch[:40] in composed
    assert len(composed) <= prompts.MAX_IMAGE_PROMPT_CHARS


def test_agent_run_read_tolerates_null_trace():
    """审查器写的轨迹是 NULL：读出时补空列表，不能让「AI 处理记录」整张列表 500。"""
    from app.schemas.agent import AgentRunRead

    row = SimpleNamespace(
        id=1,
        project_id=49,
        kind="visual_review",
        target_type="media",
        target_id=11,
        status="pass",
        model="deepseek-flash",
        steps=1,
        stop_reason=None,
        summary={"verdict": "pass"},
        trace=None,
        error=None,
        advisory=True,
        created_at=datetime.now(timezone.utc),
    )

    assert AgentRunRead.model_validate(row).trace == []
