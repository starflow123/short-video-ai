"""一键回归：宿主机全量单测 + 可选容器内真实场景验收。

    # 只跑单测（默认）
    python backend/scripts/regression.py

    # 单测 + 容器内场景（教训库沉淀→注入、台账导出）
    python backend/scripts/regression.py --live --project 49 --user 8

    # 额外审一遍现有金镜头（不花生成额度；项目需已有回归快照）
    python backend/scripts/regression.py --live --eval-rejudge

场景脚本都会自己清理测试数据；dataset 导出写到容器 /tmp，不落业务库。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DEPLOY = BACKEND.parent / "deploy"


def _run(command: list[str], cwd: Path) -> int:
    print(f"\n$ {' '.join(command)}  (cwd={cwd})", flush=True)
    completed = subprocess.run(command, cwd=str(cwd))
    return completed.returncode


def _running_services() -> set[str]:
    completed = subprocess.run(
        ["docker", "compose", "ps", "--status", "running", "--services"],
        cwd=str(DEPLOY),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        print(completed.stderr.strip(), file=sys.stderr)
        return set()
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def main(args: argparse.Namespace) -> int:
    failures: list[str] = []

    if not args.skip_tests:
        code = _run([sys.executable, "-m", "pytest", "-q"], BACKEND)
        if code != 0:
            failures.append("pytest")
    if not args.live:
        return 1 if failures else 0

    services = _running_services()
    missing = {"backend", "worker"} - services
    if missing:
        print(f"FAILED: 容器未运行：{', '.join(sorted(missing))}（先 docker compose up -d）")
        return 1

    compose = ["docker", "compose", "exec", "-T", "backend", "python"]

    code = _run(
        compose
        + [
            "/app/scripts/scenario_knowledge.py",
            "--user-id",
            str(args.user),
            "--project",
            str(args.project),
        ],
        DEPLOY,
    )
    if code != 0:
        failures.append("scenario_knowledge")

    code = _run(
        compose
        + [
            "/app/scripts/export_dataset.py",
            "--project",
            str(args.project),
            "--out",
            "/tmp/regression_dataset.jsonl",
        ],
        DEPLOY,
    )
    if code != 0:
        failures.append("export_dataset")

    if args.eval_rejudge:
        code = _run(
            compose
            + [
                "/app/scripts/run_golden_eval.py",
                "--project",
                str(args.project),
                "--user",
                str(args.user),
                "--mode",
                "rejudge",
            ],
            DEPLOY,
        )
        if code != 0:
            failures.append("golden_eval_rejudge")

    print("\n==== 回归汇总 ====")
    print("FAILED: " + ", ".join(failures) if failures else "ALL PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="同时跑容器内真实场景")
    parser.add_argument("--skip-tests", action="store_true", help="跳过宿主机 pytest")
    parser.add_argument("--project", type=int, default=49)
    parser.add_argument("--user", type=int, default=8)
    parser.add_argument(
        "--eval-rejudge",
        action="store_true",
        help="容器内再跑一遍金镜头复审（不耗生成额度）",
    )
    raise SystemExit(main(parser.parse_args()))
