"""金镜头编译回归：用冻结的输入重编译提示词，跑编译断言。

快照由 backend/scripts/export_eval_snapshots.py 从真实项目导出（容器内跑），
冻结的是「编译输入」而不是输出——编译器代码任何改动都会在这里被复算检验。
断言本体在 app/services/prompt_checks.py（与 RSI 模板验证共用，改一处两边生效）。
历史上抓过的 bug：图例块被预算裁到 17 字导致分身（legend 断言 + forbid_dropped）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.prompt_checks import load_snapshots, run_compile_assertions

CASES = load_snapshots()

pytestmark = pytest.mark.skipif(
    not CASES, reason="还没有快照：容器内跑 export_eval_snapshots.py 导出"
)


@pytest.mark.parametrize("name,payload", CASES, ids=lambda v: v if isinstance(v, str) else "")
def test_frozen_snapshot_compiles_within_guarantees(name: str, payload: dict):
    violations = run_compile_assertions(payload["snapshot"], payload["extra_must_contain"])
    assert violations == [], "\n".join(violations)
