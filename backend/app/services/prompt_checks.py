"""金镜头编译断言：pytest 回归与 RSI 模板验证共用的同一份检查。

返回违规列表（空 = 全过）。任何新断言先加在这里，两边同时生效。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.services import prompts

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "prompt_snapshots"

_HEADERS = {
    "legend": "【参考图",
    "count_check": "【人数复核】",
    "negative": "【负面提示",
    "quality": "【画面质量",
    "directives": "【镜头语言",
    "subjects": "【本镜头主体",
    "state": "【镜头起始状态",
}


def load_snapshots(fixtures_dir: Path | None = None) -> list[tuple[str, dict]]:
    """[(snapshot_name, snapshot_dict)]；expectations.json 是手工必含断言，不充当快照。"""
    directory = fixtures_dir or FIXTURE_DIR
    files = sorted(p for p in directory.glob("*.json") if p.name != "expectations.json")
    result = []
    for path in files:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        extra = _load_expectations(directory).get(path.stem, {}).get("must_contain", [])
        result.append((path.stem, {"snapshot": snapshot, "extra_must_contain": extra}))
    return result


def _load_expectations(directory: Path) -> dict:
    expect_file = directory / "expectations.json"
    if expect_file.exists():
        return json.loads(expect_file.read_text(encoding="utf-8"))
    return {}


def run_compile_assertions(snapshot: dict, extra_must_contain: list[str] | None = None) -> list[str]:
    """对一份冻结输入跑全部断言，返回违规描述（空列表 = 通过）。"""
    name = f"{snapshot.get('project_id')}_s{snapshot.get('shot_id')}_{snapshot.get('kind')}"
    inputs = snapshot["inputs"]
    expect = snapshot["expect"]
    violations: list[str] = []

    composed, blocks = prompts.compose_generation_prompt(**inputs)

    if len(composed) > expect["max_chars"]:
        violations.append(f"{name}: 编译超预算 {len(composed)} > {expect['max_chars']}")

    by_tag = {item["tag"]: item for item in blocks}
    dropped = {tag for tag, item in by_tag.items() if item["chars"] == 0}
    for tag in expect["forbid_dropped"]:
        if tag in dropped:
            violations.append(f"{name}: 关键块 {tag} 被预算裁掉")

    references = inputs.get("references") or []
    if references:
        legend = _block_text(composed, _HEADERS["legend"])
        if len(legend) < 40:
            violations.append(f"{name}: 图例只剩 {len(legend)} 字（截断会分身）")
        else:
            if "张数≠人数" not in legend or "同一角色只画一次" not in legend:
                violations.append(f"{name}: 图例缺少防分身关键句")
            declared = re.findall(r"图(\d+)=", legend)
            if [int(n) for n in declared] != list(range(1, len(references) + 1)):
                violations.append(
                    f"{name}: 图例声明 {declared}，参考图 {len(references)} 张"
                )

    subjects = inputs.get("subjects") or []
    if subjects:
        if "【本镜头主体" not in composed:
            violations.append(f"{name}: 主体绑定块缺失")
        has_continuity_ref = any(str(ref.get("kind")) == "continuity" for ref in references)
        if has_continuity_ref and len(subjects) >= 2:
            if by_tag.get("count_check", {}).get("chars", 0) == 0:
                violations.append(f"{name}: 人数复核块缺失（连续性参考 + 主体≥2）")

    # 半句截断检查（negative 是英文词列表，不适用；衔接块设计上允许整块丢弃）
    for tag in ("quality", "legend", "state"):
        text = _block_text(composed, _HEADERS[tag])
        if text and text.endswith(("，", "；", "、", "：", "…", "-", "/")):
            violations.append(f"{name}: 块 {tag} 疑似被截成半句：…{text[-20:]}")

    for needle in extra_must_contain or []:
        if needle not in composed:
            violations.append(f"{name}: 必含内容缺失：{needle}")

    return violations


def _block_text(composed: str, header: str) -> str:
    match = re.search(re.escape(header) + r"[^【\n]*】(.*?)(?=\n【|\Z)", composed, re.S)
    return match.group(1).strip() if match else ""


# 整块替换的模板必须自带块头，否则替换后成稿结构损坏。
# 负面词不同：它只是 build_negative_block 里的词表片段，块头由函数统一加，
# 内容里再写块头会在成稿里重复一次。
_BLOCK_TEMPLATE_HEADERS = {
    "video_continuity_compact": "【前后镜头衔接】",
}
_NEGATIVE_TEMPLATE_KEYS = ("video_negative_compact", "image_negative_compact")
# 负面词可以改写，但不能丢掉「畸形 / 多手 / 分身」与「文字」两条底线
_NEGATIVE_REQUIRED_GROUPS = (
    ("bad anatomy", "deformed", "bad hands", "extra limbs", "duplicate person"),
    ("text", "watermark"),
)


def validate_template_override(snapshot: dict, key: str, content: str) -> list[str]:
    """覆盖模板的结构性验证：块头/词表底线必须成立，且至少在一个预算档真实进入成稿。

    压缩模板只在预算裁剪档生效（默认档快照碰不到），不扫档的话垃圾模板
    会在默认档断言全过的情况下被放行，运行期才在某次紧预算里注进去。
    """
    violations: list[str] = []
    header = _BLOCK_TEMPLATE_HEADERS.get(key)
    if header and header not in content:
        violations.append(f"{key}: 覆盖模板缺少块头 {header}，会损坏成稿结构")
        return violations
    if key in _NEGATIVE_TEMPLATE_KEYS:
        if "【" in content or "\n" in content:
            violations.append(f"{key}: 负面词是词表片段，不能带块头或换行（成稿会重复块头）")
        lowered = content.casefold()
        for group in _NEGATIVE_REQUIRED_GROUPS:
            if not any(term in lowered for term in group):
                violations.append(
                    f"{key}: 负面词丢掉安全底线，至少保留 {' / '.join(group)} 之一"
                )

    inputs = dict(snapshot["inputs"])
    default_mc = int(inputs.get("max_chars") or 0)
    if default_mc <= 0:
        violations.append(f"{key}: 快照缺少 max_chars，无法验证可达性")
        return violations

    reachable = False
    probe = content.strip()[:24]
    try:
        prompts.set_template_overrides({key: content})
        for max_chars in range(default_mc, 200, -50):
            trial = dict(inputs)
            trial["max_chars"] = max_chars
            composed, _ = prompts.compose_generation_prompt(**trial)
            if probe in composed:
                reachable = True
                break
    finally:
        prompts.set_template_overrides({})
    if not reachable:
        violations.append(
            f"{key}: 覆盖模板在任何预算档都进不了成稿（不可达或被截断）——"
            "紧凑模板讲究短，请压缩到 30 字左右再提"
        )
    return violations


def validate_all_snapshots(
    fixtures_dir: Path | None = None,
    *,
    template_overrides: dict[str, str] | None = None,
) -> dict:
    """跑全部金镜头断言；供 RSI 在模板候选上验证（应用 override → 断言 → 还原）。"""
    from app.services.prompts import get_template_overrides, set_template_overrides

    previous = get_template_overrides()
    try:
        set_template_overrides({**previous, **(template_overrides or {})})
        violations: list[str] = []
        checked = 0
        for name, payload in load_snapshots(fixtures_dir):
            checked += 1
            violations.extend(
                run_compile_assertions(payload["snapshot"], payload["extra_must_contain"])
            )
        # 覆盖模板的结构验证：默认档断言碰不到压缩模板，必须单独扫预算档
        if template_overrides:
            by_kind: dict[str, dict] = {}
            for _, payload in load_snapshots(fixtures_dir):
                by_kind.setdefault(payload["snapshot"]["kind"], payload["snapshot"])
            for key, content in template_overrides.items():
                kind = "video" if key.startswith("video_") else "image"
                snapshot = by_kind.get(kind)
                if snapshot is None:
                    violations.append(f"{key}: 找不到 {kind} 金镜头快照，无法验证")
                    continue
                violations.extend(validate_template_override(snapshot, key, content))
        return {"checked": checked, "violations": violations, "passed": not violations}
    finally:
        set_template_overrides(previous)
