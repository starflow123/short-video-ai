"""通用 agent 运行时：模型选工具 → 执行 → 观察 → 再决策。

这里刻意不依赖任何厂商的 function calling：
决策模型只输出一个 JSON（thought / action / args），由运行时校验工具名、
必填参数、单工具次数与昂贵调用预算，再执行对应的 Python 工具。

这样做的原因：
- 厂商对 tools 协议支持不一致，JSON 决策在 DeepSeek / 本地模型上更稳；
- 工具白名单与预算由代码强制，模型无法越权调用或无限重绘；
- 每一步都进 trace，可审计、可测试，失败不会拖垮主任务。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# 工具种类：read 只读；write 改内存状态；expensive 会真正产生厂商费用
TOOL_KINDS = {"read", "write", "expensive"}


@dataclass
class ToolResult:
    """一次工具执行的结果。observation 会原样回灌给决策模型。"""

    observation: str
    data: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    done: bool = False
    status: str | None = None


@dataclass
class AgentTool:
    name: str
    description: str
    args: dict[str, str]
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    kind: str = "read"
    max_calls: int | None = None


@dataclass
class AgentRun:
    status: str
    trace: list[dict[str, Any]]
    final: dict[str, Any]
    steps: int
    stop_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "trace": self.trace,
            "final": self.final,
            "steps": self.steps,
            "stop_reason": self.stop_reason,
        }


def _brief(value: Any, limit: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else f"{text[:limit]}…"


def build_decision_prompt(
    *,
    goal: str,
    tools: list[AgentTool],
    transcript: list[str],
    step: int,
    max_steps: int,
) -> str:
    lines = [
        f"目标：{goal}",
        "可用工具（只能从下列名字里选，不要发明工具）：",
    ]
    for tool in tools:
        args = "、".join(f"{name}（{desc}）" for name, desc in tool.args.items()) or "无参数"
        lines.append(f"- {tool.name}：{tool.description}；参数：{args}；类型：{tool.kind}")
    lines.extend(
        [
            "- finish：结束并说明结果；参数：status（repaired / needs_human / escalated）、summary",
            "",
            "规则：",
            "1. 每轮只输出一个 JSON 对象，不要输出 Markdown 或额外解释。",
            '2. 格式：{"thought": "简短判断", "action": "工具名", "args": {}}。',
            "3. 先看观察结果再决定；信息不足时先调用只读工具。",
            "4. 修改必须落到具体参数上，不要重复提交同一份修改。",
            "5. 预算用尽或连续失败时，调用 finish 并说明原因，不要硬撑。",
            f"6. 当前第 {step}/{max_steps} 步。",
            "",
            "历史观察：",
        ]
    )
    lines.extend(transcript[-12:])
    return "\n".join(lines)


async def run_agent(
    *,
    goal: str,
    tools: list[AgentTool],
    decide: Callable[[str], Awaitable[dict[str, Any]]],
    initial_observation: str,
    max_steps: int = 6,
    max_expensive_calls: int = 2,
    max_consecutive_errors: int = 2,
) -> AgentRun:
    """执行 agent 循环。

    decide 收到完整决策提示词，返回已解析的 JSON dict；解析失败由调用方处理。
    """
    registry = {tool.name: tool for tool in tools}
    for tool in tools:
        if tool.kind not in TOOL_KINDS:
            raise ValueError(f"工具 {tool.name} 的类型不合法：{tool.kind}")

    transcript = [f"初始观察：{_brief(initial_observation, 2000)}"]
    trace: list[dict[str, Any]] = []
    calls: dict[str, int] = {}
    expensive_used = 0
    consecutive_errors = 0

    for step in range(1, max_steps + 1):
        prompt = build_decision_prompt(
            goal=goal,
            tools=[*tools, AgentTool("finish", "结束并返回结果", {"status": "结束状态", "summary": "总结"}, _never, "read")],
            transcript=transcript,
            step=step,
            max_steps=max_steps,
        )
        try:
            decision = await decide(prompt)
        except Exception as exc:  # noqa: BLE001 - 决策失败要保留 trace，不能吞掉
            logger.warning("agent 决策失败：%s", exc)
            trace.append(
                {"step": step, "thought": "", "action": "decide", "args": {}, "ok": False,
                 "observation": f"决策模型调用失败：{exc}"}
            )
            return AgentRun(
                status="agent_error",
                trace=trace,
                final={"error": str(exc)},
                steps=step,
                stop_reason="decision_error",
            )

        if not isinstance(decision, dict):
            observation = "决策输出不是 JSON 对象，请按要求重新输出。"
            transcript.append(f"第 {step} 步：{observation}")
            trace.append(
                {"step": step, "thought": "", "action": "invalid", "args": {}, "ok": False,
                 "observation": observation}
            )
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                return AgentRun("agent_error", trace, {"error": observation}, step, "invalid_output")
            continue

        thought = _brief(decision.get("thought"), 400)
        action = str(decision.get("action") or "").strip()
        args = decision.get("args") if isinstance(decision.get("args"), dict) else {}

        if not action:
            observation = "缺少 action 字段，请从工具列表中选择。"
            transcript.append(f"第 {step} 步：{observation}")
            trace.append(
                {"step": step, "thought": thought, "action": "", "args": {}, "ok": False,
                 "observation": observation}
            )
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                return AgentRun("agent_error", trace, {"error": observation}, step, "missing_action")
            continue

        if action == "finish":
            status = str(args.get("status") or "finished").strip() or "finished"
            summary = _brief(args.get("summary"), 800)
            trace.append(
                {"step": step, "thought": thought, "action": "finish", "args": {"status": status},
                 "ok": True, "observation": summary}
            )
            return AgentRun(status, trace, {"status": status, "summary": summary}, step, "finish")

        tool = registry.get(action)
        if tool is None:
            observation = f"未知工具「{action}」，只能使用：{', '.join(registry)}。"
            transcript.append(f"第 {step} 步：{observation}")
            trace.append(
                {"step": step, "thought": thought, "action": action, "args": args, "ok": False,
                 "observation": observation}
            )
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                return AgentRun("agent_error", trace, {"error": observation}, step, "unknown_tool")
            continue

        missing = [name for name in tool.args if name not in args]
        if missing:
            observation = f"工具 {action} 缺少参数：{', '.join(missing)}。"
            transcript.append(f"第 {step} 步：{observation}")
            trace.append(
                {"step": step, "thought": thought, "action": action, "args": args, "ok": False,
                 "observation": observation}
            )
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                return AgentRun("agent_error", trace, {"error": observation}, step, "missing_args")
            continue

        used = calls.get(action, 0)
        if tool.max_calls is not None and used >= tool.max_calls:
            observation = f"工具 {action} 已达到本次上限（{tool.max_calls} 次），请换方案或 finish。"
            transcript.append(f"第 {step} 步：{observation}")
            trace.append(
                {"step": step, "thought": thought, "action": action, "args": args, "ok": False,
                 "observation": observation}
            )
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                return AgentRun("agent_error", trace, {"error": observation}, step, "tool_limit")
            continue

        if tool.kind == "expensive" and expensive_used >= max_expensive_calls:
            observation = f"昂贵调用预算已用尽（{max_expensive_calls} 次），请 finish 或 escalate。"
            transcript.append(f"第 {step} 步：{observation}")
            trace.append(
                {"step": step, "thought": thought, "action": action, "args": args, "ok": False,
                 "observation": observation}
            )
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                return AgentRun("agent_error", trace, {"error": observation}, step, "budget")
            continue

        calls[action] = used + 1
        try:
            result = await tool.handler(args)
        except Exception as exc:  # noqa: BLE001 - 工具失败要变成观察，让模型决定下一步
            logger.warning("agent 工具 %s 执行失败：%s", action, exc)
            observation = f"工具 {action} 执行失败：{exc}"
            transcript.append(f"第 {step} 步：{observation}")
            trace.append(
                {"step": step, "thought": thought, "action": action, "args": args, "ok": False,
                 "observation": observation}
            )
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                return AgentRun("agent_error", trace, {"error": observation}, step, "tool_error")
            continue

        consecutive_errors = 0
        if tool.kind == "expensive" and result.ok:
            expensive_used += 1
        observation = _brief(result.observation, 1200)
        trace.append(
            {"step": step, "thought": thought, "action": action, "args": args, "ok": result.ok,
             "observation": observation}
        )
        if result.done:
            return AgentRun(
                result.status or "finished",
                trace,
                result.data,
                step,
                "tool_finish",
            )
        transcript.append(f"第 {step} 步：{action} → {observation}")

    return AgentRun(
        status="step_limit",
        trace=trace,
        final={"error": f"达到最大步数 {max_steps}"},
        steps=max_steps,
        stop_reason="max_steps",
    )


async def _never(_: dict[str, Any]) -> ToolResult:  # pragma: no cover - 占位，不参与执行
    return ToolResult("finish 由运行时处理")
