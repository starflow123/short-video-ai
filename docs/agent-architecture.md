# Agent 能力补强方案（2026-10-25）

## 一、现状：是「流水线」，还不是「agent」

当前 LangGraph 只有三条直线（`backend/app/services/pipeline.py:511-555`）：

```
start      → build_characters → generate_breakdown → END
confirmed  → batch_generate_images → END
videos     → batch_generate_videos → END
```

模型调用全是**单次 JSON**（`_json_call`，`pipeline.py:475`；剪辑选段 `editing.py:202`），没有工具调用、没有自检、没有视觉判断。刚上线的「认可」闸门解决的是**人怎么看**，但「谁先替人看一遍」还是空白。

已有可复用的底座：

- ffmpeg 抽帧与切点色差：`export.py:26-108`、`editing.py:81-153`
- 提示词编译预览：`generation.py:1195`（能提前看到分块与裁剪）
- 参考图体检结果：`media_assets.meta.reference_*`（已落库、已上前端）
- 任务顺序链：`dispatch.create_task(enqueue=False)` + `_start_chain`
- 凭证按能力筛选：`resolve_credential(..., "text"|"image"|"video")`，`capabilities` 是逗号分隔字段

## 二、最该补的三个 agent（按 ROI 排序）

### P0 视觉一致性审查 agent（关键帧）

**解决什么**：现在关键帧像不像本人、道具换没换人、有没有分身，全靠用户逐张肉眼判断；`reference_issues` 只能说明「参考图没送到」，不能说明「送到了但画错了」。

**插入点**：

1. 单镜生成：`api/routes/shots.py` 的生成任务链尾，追加一个 `review` 任务；
2. 批量：`_batch_generate_images` 之后新增图节点 `review_images`（或同样挂链尾，不阻塞用户）。

**输入**：生成帧 + 本镜出场角色的定妆照 + `Shot` 的结构化上下文（subjects / props / directives）+ 实际发送的提示词。

**工具**：读取关键帧与资产、发起视觉模型调用、写审查结论、（可选）按建议重绘一次。

**输出（结构化）**：

```json
{
  "verdict": "pass | warn | fail",
  "checks": {
    "face_match": "pass|warn|fail",
    "costume_match": "...",
    "cast_count": "...",
    "prop_holder": "...",
    "duplicate_person": "...",
    "composition": "..."
  },
  "reasons": ["陈默的脸型比定妆照更圆", "画面出现第二个林越"],
  "prompt_patch": "只改画面描述的增量建议，不直接覆盖硬约束"
}
```

**硬约束**：agent **永远不自动点「认可」**；`fail` 只给「按建议重绘」按钮；自动重绘默认关闭，开启后最多 1 次。

**能力接入**：**已实测 DeepSeek 原生多模态可用，不需要新厂商**。

- 2026-10-25 用账号 8 的现有 DeepSeek Key 实测：`GET /models` 返回 `deepseek-flash`、`deepseek-v4-pro`；`deepseek-flash` 接收 base64 图片并正确描述画面（2 人、服装、保温杯），`HTTP 200`。
- 关键参数：`thinking: {"type": "disabled"}`。不开时一次简单看图会花 611 个推理 token 且可能因 `max_tokens` 耗尽返回空内容；关闭后 65 token 出答案。
- 结构化输出：`response_format: {"type": "json_object"}` + 关闭思考，返回合法 JSON（`cast_count / costume_ok / duplicate_person / notes`）。
- 成本量级：一张 1080×1920 关键帧约 633-688 输入 token，一次审查总计约 700-1000 token。
- 实现上只改 `DeepSeekProvider`：`capabilities` 加 `vision`、`models["vision"] = ["deepseek-flash"]`、新增 `generate_vision()`（content 数组 + base64 data URL）；`provider_credentials.capabilities` 需要把存量 DeepSeek 凭证补上 `vision`（或解析时按 provider 能力放行）。**不需要新表、不需要新厂商**。

**数据**：MVP 直接写 `media_assets.meta.visual_review`（与现有 `reference_*` 同路，零迁移）；等要做批量统计/看板时再拆 `agent_findings` 表。

**前端**：关键帧卡片加「AI 初审：通过 / 2 项风险」，点开看原因；失败项直接触发已有的重绘弹窗并预填 `prompt_patch`。

**落地结果（2026-10-25 晚，P0 已完成）**

| 层 | 实现 |
|---|---|
| 厂商 | `providers/base.py` 新增 `generate_vision(prompt, images=[(图注, bytes)])` 占位；`providers/deepseek.py` 加 `vision` 能力、`models["vision"]=["deepseek-flash"]`、base64 data URL、`thinking={"type":"disabled"}`、JSON 模式；文本默认模型收敛到 `deepseek-flash` |
| 凭证 | 迁移 `0011_vision_capability` 给存量 `provider='deepseek'` 凭证追加 `vision`（实测 id 7/8 均变为 `text,vision`）；新凭证默认勾选全部能力 |
| 服务 | `services/agents/visual_qa.py`：取帧 + 本镜角色定妆照（最多 3 张）+ 结构化分镜要求，六项检查，结论归一化后写 `media_assets.meta.visual_review` |
| 链路 | `_handle_image` 在单张关键帧落库后调用 `maybe_review_keyframe`；开关 `VISION_REVIEW_ENABLED`（默认开）关闭时 0 调用；任何失败只记日志，不影响生图 |
| API | `POST /api/shots/{id}/keyframes/{kid}/review`，返回带 `visual_review` 的关键帧；序列化器把结论挂到 `ShotKeyframeRead` |
| 前端 | 关键帧卡片「AI 初审」标签 + Popover 六项明细 + 「按建议重绘」；按钮只预填 `prompt_patch`，仍需用户确认并重新生成 |

**验收证据**

- `python -m pytest -q` → **48 passed**（新增 8 项：请求体、结论归一化、空镜规则、开关关闭、失败吞掉、提示词内容）。
- 迁移 head `0011_vision_capability`；`provider_credentials` id 7/8 → `text,vision`。
- 真实调用（项目 34 历史帧，`deepseek-flash`，1.2-3.7 秒/次）：
  - 镜头 97（双人+道具）→ `fail`（脸型与定妆照不符），`reference_sheets=2`；
  - 镜头 98（空镜）→ `pass`（修正后，人物项按「不适用」处理）；
  - 镜头 99（双人+道具）→ `fail`（道具归属 + 构图）；
  - 镜头 103（单人+道具）→ `fail`（服装 + 道具）；
  - 审查前后 `approved_at` 不变（API 实测返回 `APPROVED True`），证明只给建议。
- 生产前端 bundle 已包含「AI 初审 / 按建议重绘 / 视觉（读图审查）」；因无浏览器登录态，未做登录后点击验收。

**已知边界**

- 只对「单张落库」自动审查；一次出 3 张（起幅/中段/落幅）不逐张调用，需手动点 AI 初审（控制成本）。
- 同一张图两次审查可能给出不同结论（镜头 97 首次脸 fail、复跑脸 pass 但构图 fail）——模型判断有波动，所以只做建议、不做闸门。
- 未做自动重绘、未做项目级开关（当前为全局环境变量）。


### P1 连续性审查 agent（视频 → 成片前）

**落地结果（2026-10-25 晚，P1 已完成）**

| 层 | 实现 |
|---|---|
| 服务 | `services/agents/continuity.py`：ffmpeg 抽「上一镜尾帧（`-sseof` 定位文件末尾）+ 下一镜首帧（0.05s）」；若下一镜有已认可关键帧，作为图3一起比对；720 宽压 token |
| 检查项 | `same_person / costume_continuity / scene_continuity / lighting_continuity / keyframe_match`；姿态突变、景别/机位切换明确不算问题；明确换场的场景/光线给 pass；轻微手位/表情差异不算 warn |
| 落库 | 结论写下一镜视频素材 `media_assets.meta.continuity_review`，带 `from/to_shot_id`、`from/to_media_id`、`advisory: true`；视频没变时复用上次结论（手动「重跑」强制刷新） |
| 链路 | 导出任务 `_handle_export` 在真正拼接前调用 `maybe_review_project`：**只出报告，不阻断导出**；单切点失败/模型不可用只记 `skipped`，导出照常；整项目预算 150 秒，超时跳过剩余切点 |
| 开关 | `CONTINUITY_REVIEW_ENABLED`（默认开）；关闭后 0 额外调用 |
| API | `POST /api/shots/{id}/continuity`：重跑本镜与上一镜之间的切点（第一镜返回 400）；`ShotRead.continuity_review` 随分镜详情返回 |
| 前端 | 分镜详情「视频」卡片：衔接结论 Tag + Popover 五项明细/原因 + 「重跑切点审查」；明确标注 AI 只给建议、不阻断导出 |

**与人工审查的关系（未改变的闸门）**

- 切点审查和关键帧 AI 初审一样，只写 `visual_review` / `continuity_review`，**从不写 `shot_keyframes.approved_at`**；
- 批量生视频仍只消费人工点过「认可」的关键帧（`pipeline.py` 的 `approved_at.isnot(None)` 过滤）；
- 导出的阻断条件仍是「有镜头缺视频」，不是 AI 的 `fail`；P1 当前是 dry-run 阶段。

**验收证据**

- `python -m pytest -q` → **59 passed**（新增 11 项：提示词边界、fail/unknown 归一化、无关键帧不误报、空镜人物项、尾帧 `-sseof`、缺 ffmpeg、单切点失败继续、开关关闭、导出前先审查且顺序正确）。
- 项目 34（4 镜 / 4 视频 / 3 个切点，`deepseek-flash`，每次约 2-4 秒）：
  - 97→98 `pass`、99→103 `pass`、98→99 首次 `warn`（图2 手位与认可帧不同）；收紧「手位差异不算问题」的规则后复跑为 `pass`；
  - 第二次调用返回 `reused=3`，视频未变时未产生新的视觉调用；
  - `POST /api/shots/99/continuity` → HTTP 200；`GET /api/shots/99` 已带 `continuity_review`；审查后关键帧仍 `approved=True`；
  - 第一镜 `POST /api/shots/97/continuity` → HTTP 400「这是第一个镜头，没有可比的切点」。

**已知边界**

- 模型判断有波动（同一切点两次结论可能不同），所以只做建议；是否重跑镜头仍由人决定。
- 当前在导出时同步跑，项目很大时导出前会多等一会儿（150 秒预算封顶）；后续可挪成独立任务。
- 只抽首/尾各一帧，片中的动作中途崩坏、口型、跳帧不在检查范围。

**原设计说明（保留）**

**解决什么**：切点色差已经由 `export.py` 的色彩匹配处理；但「上一镜结尾的人/衣服/场景」和「下一镜开头」是否接得上，目前没人检查，正是已知的「姿态突变不接力」边界。

**工具**：ffmpeg 抽每个视频首帧/尾帧（复用 `export.py` 的采样方式）、视觉模型比对相邻镜头、读取镜头邻居与认可关键帧。

**成本**：每个切点 2 帧 + 1 次视觉调用；一集 10 镜约 9 次，可控。

### P2 导演 agent（规划 / 拆解自检）

**插入点**：`_build_characters` 与 `_generate_breakdown` 之间、以及生成方案后展示给用户确认之前。

**循环**：草稿 → 自评 → 修订，最多 2 轮。评审维度：镜头数是否符合目标、时长是否超预算、时间轴是否首尾相接、对白是否被覆盖、角色/道具是否与档案冲突、JSON 结构是否完整。

**输出**：不直接落库，先给「评审意见 + 修订稿」，仍然由用户确认后才拆解。这样不改变现有「两步式：先规划、用户确认后拆解」的产品语义。

## 三、不建议做成 LLM agent 的部分

| 环节 | 原因 | 建议 |
|---|---|---|
| 503/429 重试与厂商降级 | 现有策略是确定性的、可测试的；LLM 只会增加不确定性 | 保持规则引擎，最多让 agent 解释失败原因 |
| 提示词最终编译 | `compose_generation_prompt` 是预算与硬约束的唯一真源，LLM 重写会破坏保护块 | agent 只产出字段级 patch，必须再过编译与校验 |
| 资产绑定 / 去重 | 姓名与 ID 规则优先，错绑代价高 | agent 只给建议，不直接写库 |
| 剪辑选段 | `editing.py` 已有 AI 选段 + 确定性 fit/pad | 维持现状，最多加一个「为什么选这几段」的解释 |

## 四、Agent runtime 最小设计

新增 `backend/app/services/agents/`：

```
runtime.py     # 有界循环：max_steps、token/时间预算、结构化输出、审计
tools.py       # 白名单工具：读镜头/资产/帧、预览提示词、建重绘任务
visual_qa.py   # P0 关键帧审查
continuity.py  # P1 切点审查
director.py    # P2 方案自评
```

运行方式：

- 关键帧审查做成 `kind="review"` 任务，挂在现有顺序链尾，不阻塞用户继续操作；
- 导出前审查必须是同步门（或让导出任务等待审查链完成），否则会先导出再报错；
- 所有 agent 动作写 `agent_runs`（kind / target / model / tokens / steps / verdict / status），任务中心可见、可审计；
- 每个项目一个开关 + 每次运行成本上限；关闭时行为与现在完全一致。

## 五、实施顺序与验收

**Phase 1 — P0 视觉审查（建议先做）**

- 接入 1 个视觉模型（用你已有 Key 的厂商优先），失败/未配置时自动跳过；
- 先只做「只读审查 + UI 提示」，不自动重绘、不阻止任何操作；
- 验收：项目 34 的 10 张历史帧里，能标出 2 张 `reference_used=false` 的降级帧；误报率可接受（人工抽查 10 张，误 fail ≤ 2）；关闭开关时 0 额外调用、0 额外任务。

**Phase 2 — P1 连续性**

- 先 dry-run 出报告（不阻止导出），拿项目 34 的 4 个切点校准阈值；
- 稳定后再把 `fail` 接成导出闸门。

**Phase 3 — P2 导演自检**

- 在方案确认弹窗里多一块「AI 预审」，不影响现有确认流程；
- 验收：同一剧本重跑，镜头数/时长覆盖不下降，拆解不再丢镜。

## 六、风险与成本

- **视觉凭证已具备**：现有 DeepSeek Key 即可（`deepseek-flash`）；如果不想把帧外发给第三方，再考虑本地 Qwen-VL/Ollama。
- **模型名要更新**：DeepSeek `/models` 里已没有 `deepseek-v4-flash` / `deepseek-chat`，当前 registry 的默认文本模型仍是旧名（但旧名会路由到 V4.1-Flash）。接 P0 时顺手把默认模型改成 `deepseek-flash`，避免哪天别名下线。
- **误报会消耗信任**：第一版只做建议，不做阻断；阈值用真实项目校准。
- **循环失控**：max_steps、token 上限、超时、每集成本上限，缺一不可。
- **隐私**：帧会发给 DeepSeek，与现有图片/视频厂商同级；在凭证页说明。
- **不要一次上四个 agent**：先 P0，拿到「审查结论与人工判断一致率」的数据，再决定 P1/P2 的投入。
