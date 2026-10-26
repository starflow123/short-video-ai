# Agent 能力补强方案（2026-10-25）

> 更新（2026-10-25 晚）：除 P0 关键帧初审、P1 切点审查两个单次审查器外，已落地 5 个带工具循环的 agent——关键帧修复、导演自检、失败诊断、切点修复、素材整理；7 类运行（含两个单次审查器）统一写 `agent_runs` 审计表并提供查询接口。人工认可闸门未改变。

## 一、现状：流水线 + 有界 agent 循环

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

## 二、已落地能力（按上线顺序）

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
    "composition": "...",
    "style_match": "..."
  },
  "reasons": ["陈默的脸型比定妆照更圆", "画面出现第二个林越"],
  "prompt_patch": "只改画面描述的增量建议，不直接覆盖硬约束"
}
```

`style_match`（2026-10-25 补）只判**画法类别**是否与项目视觉风格一致（真人照片 / 手绘动画 / 3D / 水墨…），不判美丑；项目没填画风时该项归一化为 `pass`，不参与结论。加这一项的原因见「画风漂移」一节：定妆照被画成真人照片时，其余六项全 `pass`，审查器完全看不见最严重的问题。

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

### P1.5 修复 agent（真闭环，2026-10-25 已落地）

前两个 agent 都是「单次审查器」：一次模型调用、一段 JSON、结束。修复 agent 补上了真正的工具循环：

```
读初审失败项（observation）
  → 模型决策：patch_prompt / redraw / review_candidate / escalate / finish
  → 执行 Python 工具 → 观察结果回灌
  → 复审通过 → finish(repaired)；修不好 → 再改一轮或 escalate
```

| 层 | 实现 |
|---|---|
| 运行时 | `services/agents/runtime.py`：模型每步只输出 `{"thought","action","args"}`；代码校验工具名、必填参数、单工具次数、昂贵调用预算与连续错误次数；每步写 trace（thought/action/args/ok/observation），失败变成观察而不是拖垮任务 |
| 工具 | `inspect`（只读约束与审查结论）、`patch_prompt`（增量修正，≤200 字）、`redraw`（真实计费重绘）、`review_candidate`（视觉复审）、`escalate`（转人工）、`finish`（结束） |
| 服务 | `services/agents/repair.py`：复用 `_handle_image` 出候选帧；候选帧**追加**为新关键帧，旧帧不替换；`skip_auto_review` 避免生图链路重复扣费 |
| 预算 | `AGENT_REPAIR_MAX_ROUNDS`（默认 2，硬上限 3）＝最多重绘次数；`max_steps=8`；昂贵调用与重绘同额；连续 2 次错误即停 |
| 人工闸门 | 修复 agent **从不写 `approved_at`**，也不改剧情/人数/角色/道具归属；候选帧必须用户对比后自己点「认可」 |
| 任务 | 新任务类型 `kind="repair"`，`generation._handle_repair` 注册进 `_HANDLERS` |
| API | `POST /api/shots/{shot_id}/keyframes/{keyframe_id}/repair`（202，返回任务），可传 `max_rounds` |
| 前端 | 关键帧卡片新增「AI 修复」按钮；任务完成后弹「运行轨迹」：状态 / 步数 / 重绘次数 / 每一轮的判断、工具、参数、观察；明确提示候选帧未替换旧帧 |
| 结果 | `task.result` 含 `status`（repaired / needs_human / escalated / already_pass / review_unavailable / agent_error / step_limit）、`trace`、`initial_review`、`review`、`prompt_patch`、`candidate_keyframe_id/media_id/url` |
| 提示词预算 | 修正说明走 `compose_generation_prompt(repair_patch=...)` 的独立「本次修正」块，与硬约束同级受保护；预算不足时只压正文，避免 patch 被正文裁剪吞掉（现象是 agent 改了提示词但出图没变化） |

**验收证据（2026-10-25）**

- `python -m pytest tests -q` → **66 passed**（修复 agent 6 项：多步工具循环与 trace、未知工具/缺参数恢复、昂贵预算拦截、工具异常降级、修复观察含失败项与硬约束、修正块在预算裁剪下保留）；`npx tsc --noEmit` → 0 错误；`import app.main` → OK。
- 真实闭环（项目 34，镜头 103，`max_rounds=1`，Agnes 出图 + DeepSeek 决策/复审）：
  - 任务 526：`patch_prompt → redraw → review_candidate → patch_prompt → redraw(预算拒绝) → escalate`，6 步，生成候选帧 156 / media 444，结论 `needs_human`；
  - 任务 531（对 156 再修一次）：同样的 6 步轨迹，候选帧 157 / media 448；
  - **修正生效证据**：media 448 的 `meta.prompt_used` 含「【本次修正】」，长度仍为 814 字（正文被压、修正块保留）；第一次实现把 patch 拼在正文尾部时，media 444 的 `prompt_used` 里完全没有 patch——这是本轮修掉的真实 bug；
  - 原帧 62 的 `approved_at` 前后不变（`2026-10-25 05:48:11`），候选帧 `approved_at` 为空：AI 仍未越过人工认可闸门。
- 拆解自动定妆照（临时项目 48，故意用文本凭证 DeepSeek id 8 + `auto_images=true`）：任务 527 `succeeded`，`reference_tasks=3`、`reference_error=None`，3 个定妆照任务 528/529/530 全部 `succeeded`；修复前该参数会把文本凭证带进图片能力解析，直接静默跳过。
- 智能剪辑归属：任务 532（clips 441/442，`project_id=34`）→ media 449，`result.project_id=34`，素材库项目 34 列表可见；旧任务 525 的 media 443 仍以 `project_id=NULL` 存在，通过 `GET /api/media?unlinked=true` 可见（当前 9 个未归属素材）。
- 清理：临时项目 48（媒体/资产/任务）、候选帧 156/157 与 media 444/448/449 已删除；项目 34 恢复为 4 张关键帧，全部仍是原认可时间。
- **未做**：前端登录态下的点击验收（无可用登录会话）；前端以 `tsc` + API 契约 + 生产 bundle 构建通过为准。

**已知边界**

- 决策协议是 JSON 而非厂商 function calling：更稳、可审计，但模型偶尔会输出脏 JSON（连续 2 次即停并保留 trace）。
- 只能修「提示词层面」的缺陷（道具缺失、人数、构图、服装）；参考图没送到、角色没建档这类问题会在观察里提示，但仍需人工处理。
- 修复后不复用初审的 `prompt_patch` 原文，而是让模型针对新的复审失败项改写；两轮仍不过就转人工。
- 「本次修正」是受保护块：极端超预算时只会继续压正文，修正本身不丢；但它仍可能被模型忽略（提示词不是硬约束），所以复审与人工认可都不能省。

### P2 导演自检 agent（规划 / 拆解前）

**落地结果（2026-10-25 晚）**

| 层 | 实现 |
|---|---|
| 服务 | `services/agents/director.py`：先跑确定性校验（段落镜数之和 = 总镜数、段落非空、时长与目标偏差、总镜数上限），干净时 **0 次 LLM**；有问题才进 runtime 修订，最多 2 轮 |
| 工具 | `inspect`（重读方案/剧本/约束）、`validate`（确定性校验）、`revise`（只改段落与镜数，不写分镜正文）、`finish`（clean / revised / needs_human） |
| 接入 | `POST /api/episodes/{id}/breakdown-plan` 的 `review: true`；审查结果随方案返回，**不落分镜、不删旧镜**，用户确认后才走原拆解链路 |
| 输出 | `review.status`（clean / revised / revised_with_warnings / needs_human / agent_error / unavailable）、`issues`、`revisions`、修订后的方案、`trace` |
| 前端 | 拆解方案弹窗里显示自检状态、修订次数、逐条问题；审查不可用时只提示，不挡拆解 |
| 审计 | 每次自检写 `agent_runs(kind=director, target_type=episode)` |

### P2.1 失败诊断 agent（任务失败后）

**解决什么**：失败任务只显示厂商原文报错，用户不知道是密钥、额度、内容审核还是配置问题，常见反应是盲目重试继续烧钱。

| 层 | 实现 |
|---|---|
| 服务 | `services/agents/diagnosis.py`；任务失败后可自动挂诊断（`AGENT_DIAGNOSIS_ENABLED`，默认开），也可在任务页手动点「诊断」 |
| 工具 | `inspect_task`、`inspect_project`（同项目失败任务与本集镜头状态）、`inspect_credentials`（能力与检测结果，**不返回密钥**）、`retry_task`（换凭证重跑一次，昂贵）、`report`、`escalate` |
| 结论 | `category`（credential_missing / auth / quota / timeout / content_rejected / asset_missing / config / provider_error / unknown）、`confidence`、`evidence`（来自工具事实）、`suggestion`、`retry_recommended` |
| 安全 | 默认**只诊断不重跑**；`retry_task` 需请求里显式 `allow_retry=true`，且每次诊断最多 1 次，原任务状态与分镜状态不变 |
| API | `POST /api/tasks/{id}/diagnose`（仅 failed，202 返回诊断任务）、`GET /api/tasks/{id}/diagnosis`（读最近一次结论，不触发调用） |
| 前端 | 失败任务行「诊断」按钮 + 结论弹窗（证据、建议、是否重试、trace）；任务类型显示「AI 诊断」 |

### P2.2 切点修复 agent（连续性 fail 之后）

**解决什么**：P1 切点审查只指出「服装/场景接不上」，用户仍要自己想怎么改视频提示词。这个 agent 把「审查 → 修正 → 可选重拍 → 复审」串成闭环。

| 层 | 实现 |
|---|---|
| 服务 | `services/agents/cut_repair.py`：读 `continuity_review`，先判断是否已 pass（是则 0 调用直接返回）；失败项交给 runtime |
| 工具 | `inspect`、`patch_prompt`（≤240 字视频提示词增量）、`rerender_video`（真实计费重拍，替换现有视频）、`recheck_cut`（重拍后复审）、`report`、`escalate` |
| 双重开关 | 请求 `allow_rerender` **且** 服务端 `CUT_REPAIR_RERENDER_ENABLED=true` 才允许重拍；任一不满足时工具返回拒绝观察，agent 退化为「只给修正建议」 |
| 安全 | 从不自动点认可；重拍会替换本镜视频，因此默认关闭，前端需勾选确认；`recheck_cut` 仍只写 `meta.continuity_review` |
| API | `POST /api/shots/{id}/continuity/repair`（202 返回 `kind=cut_repair` 任务；第一镜 400） |
| 前端 | 分镜详情衔接结论旁「AI 修复切点」：先选是否允许重拍，完成后展示状态、修正内容、前后结论与 trace |

### P2.3 素材整理 agent（资产库只读扫描）

**解决什么**：资产库会积累重名角色、缺参考图的资产、出现在分镜里但没建档的角色/道具、未关联项目的图片；这些正是关键帧「长得不像」的根因，但逐条找很费时间。

| 层 | 实现 |
|---|---|
| 服务 | `services/agents/organize.py`：确定性扫描产出候选建议（`duplicate_asset / missing_media / missing_prompt / unknown_subject / unknown_prop / unlinked_media`），默认 **0 次 LLM** |
| 可选 LLM | `use_llm=true` 时只做「排序 + 人话总结」，通过 `inspect` / `rescan` / `report` 工具，**没有任何写库工具** |
| API | `POST /api/agents/organize-assets?project_id=`，返回 `scan` 统计、`suggestions`、`summary` |
| 前端 | 资产页「AI 整理建议」弹窗：只读警示、LLM 开关、统计标签、建议卡片；不提供一键修复 |
| 审计 | `agent_runs(kind=organize)`，从「按项目」查询时带 `project_id` |

**整理 agent 不做的三件事**：不合并资产、不改名、不给媒体挂项目——这些操作错一次代价高，仍由用户手动完成。

### P2.4 画风一致性：端到端实测查出的四个根因（2026-10-25）

**现象**：新建项目选了「日系手绘动画质感」，拆解后台妆照与关键帧全部出成真人照片；`style_lock=true` 也挡不住。审查器当时六项检查全 `pass`，完全看不见这个问题。

**根因与修复（都在流水线层，不靠重抽卡）**：

| # | 根因 | 修复 |
|---|---|---|
| 1 | 「编译预览」用的 `_style_of` 与真实生成用的 `_project_style_text` 不是同一份，预览里有画风、实际请求里没有 | `generation._style_of` 直接委托 `_project_style_text`，预览与生成同源 |
| 2 | 定妆照走 `flow=asset_reference` 时 `style=None`，项目画风根本没进请求 | `_handle_image` 的 compose 调用统一带 `style=style_text`，定妆照也带画风 |
| 3 | 画风文案只是「质感」描述，模型默认仍出真人照片 | `prompts.style_medium_rule`：非写实画风（动漫/手绘/水墨/油画/卡通/3D）编译成 `【画风硬要求】画法必须是：…` 并把 `photo, photorealistic, live action…` 追加进负面词；「现实 / 复古胶片 / 赛博朋克」不加限制 |
| 4 | 定妆照正文（外观 + 服装 + 构图指令）超过 300 字，被 lean 截断，末尾的「三视图 / 头顶留白 / 灰背景」整段被切掉 | 资产参考图走 `lean=False`（`max_chars=None` 时不再做 300 字截断），并有回归测试锁住这个行为 |

**放大器（不是 bug，但会让画风永久锁死）**：资产页「重新生成」默认 `use_reference=true`，把**上一张错误画风的定妆照**当身份参考图送进图生图——参考图对画法的约束强于文字，于是每次重画都被带回真人照片。产品上保留这个开关（换装不换脸需要它），前端补了提示：画风不对时关掉它。

**Agnes 图生图队列策略**：免费档 429/503 时旧逻辑会在最后一次尝试「摘掉参考图降级为纯文生图」，出一张人物必然不像本人的图还标成功。现改为**只在配置的单一模型上**排队重试 14 次后**显式失败**，错误文案写清「等 1-2 分钟重跑这一张」，不做静默降级——一致性优先于出图成功率。中途换模型（2.5-flash ↔ 2.1-flash）曾经是用来躲开队列拥堵的手段，但两个模型笔触与上色习惯不同，同一批关键帧混用会看出画风漂移（项目 49 实测：8 张里 5 张 2.1、3 张 2.5），2026-10-25 按用户决定彻底取消轮换。单元测试 `test_queue_full_fails_loudly_instead_of_dropping_references` 断言所有重试请求的 `model` 只有一个值；线上实测见 `agent_runs` 第 34 行（repair / `tool_error`，报错文案指向 `agnes-image-2.5-flash` 单模型 14 次）。

**检测通道**：视觉初审补 `style_match`（见 P0 一节），画风不符直接 `fail` 并给「按建议重绘」。

**验证**：`python -m pytest -q` → **95 passed**（新增 4 项：非写实画风编译成画法硬要求 + 照片质感负面词、写实画风不加限制、3D 不被误判成 2D、定妆照构图指令不被 lean 截断）；端到端实测见项目 49（8 镜日系治愈短片）。

### P2.5 关联资产勾选记忆（2026-10-25）

**现象**：「生成关键帧」「生成视频」两个弹窗每次打开都要重新勾选角色 / 场景 / 道具，逐镜确认时重复劳动。

**修复**：`ShotRead` 新增 `last_image_asset_ids` / `last_video_asset_ids`，由 `shots._last_used_asset_ids()` 读本镜头**最近一次成功**生成任务 `params.asset_ids`；前端 `openKeyframeModal` / `openVideoModal` 回填，用户只做增删。两处 `Form.Item` 补提示：留空 = 后端按出场角色自动匹配（并把本次会锁的资产名列出来），显式勾选 = 跳过防分身策略、只用勾选这批。

**两条刻意的边界**：
- 只回传「用户显式选过」的那批。留空时后端走自动绑定 + 防分身参考策略，一旦填上 `asset_ids` 就绕过那套策略，所以不能替用户把自动绑定的资产写回去。
- 失败任务不计入。那批勾选没能出图，恢复它等于把用户推出防分身策略（项目 49 的 shot 216 正是显式勾选后出了分身）。
- 例外：显式勾选里若有**群像定妆照**，仍然摘掉（一张图里好几个人，模型照它的人数复制角色），并把摘了谁写进 `reference_issues` 在界面显示——摘图属于改写本次输入，不能静默。

### P2.6 分身的五个根因：人数把动物算成人、场景判定过严、图例被预算截断（2026-10-25）

**现象**：项目 49（日系治愈短片）shot 215 与 216 的关键帧都画出**两个小满**。自动路径（216）与显式勾选路径（215）都中招，说明不是「勾错资产」而是编译层与参考层的规则问题。

**根因与修复**：

1. **人数把非人形主体算成人**。`subjects` 是「小满 + 三花猫」，编译出来是 `【人数】仅 小满、三花猫 2 人` 与 `【人数硬约束】画面中只有 2 名人物`。模型按「2 名人物」补足了一个人类，猫另外画 → 多出的那个人就是第二个小满。
   `prompts.split_subject_names()` 按姓名 + 外观把主体分成「人物 / 动物」两组：人数硬约束只数人物，动物单独一句「按外观画成动物，不得拟人化，不计入人数」；只有动物的镜头走「本镜头没有人物」分支。
   `build_reference_count_check`（【人数复核】）、`visual_qa`（初审的 `cast_count` 与空镜放宽）、`continuity`（切点审查的空镜判定）三处同步只数人——否则审查器会把正确画面判成缺人、把分身判成合格。
   分类是保守的：姓名带「马/牛/羊/猪/鱼」这类也用作姓氏的字时，必须有外观佐证（四足、肉垫、被毛…）才按动物；外观写了「男性/女性/人类」或姓名是「牛大爷/阿姨」一律按人。
2. **同场景判定过严，上一帧接力从未生效**。`_scene_key()` 要求 location 整串相等，而拆解每次换机位都重写一遍地点（「旧面包店门口的木质门槛前」vs「旧面包店木质橱窗前」），于是 `references = [previous]` 这条最稳的规则在项目 49 的 8 个镜头里一次都没触发，全部退回多张定妆照。
   改为 `generation._same_scene()`：先剥括号与逗号后的补充、剥机位后缀，再按**公共前缀 ≥ 4 字**判同场；两边都没写 location 时沿用旧行为（算同场，不断掉旧数据的接力）。`_previous_shot_reference` 与 `_next_continuation_frame`（视频尾帧接力）都走这条判定。
3. **显式勾选绕过整套策略**：见 P2.5 的群像例外。
4. **场景判定又过松：公共前缀把「大范围」当场景**。首版 `_same_scene` 按逗号前第一段取场景短语，而中文地点串从大到小写——「海边小镇的旧巴士站旁，通向面包店的窄巷口」的真实场景是**窄巷口**，不是巴士站。前缀 ≥4 字于是把 4 号镜（巷口）和 3 号镜（巴士站）判成同场，接力把上一镜的海边长椅构图整张抄了过来（media 495 初审：构图/场景不符）。改为**取逗号后最后一段（最具体的地点）**再比前缀：巴士站与窄巷口判为两场（接力不再抄错环境），「旧面包店门口的木质门槛前」与「旧面包店木质橱窗前」仍判同场（防分身的接力保留）。
5. **参考图图例被预算截成半句话**。820 字预算吃紧时的「极端兜底」压缩顺序里图例排在第二：实测 shot 216 的图例从 ~150 字被截到 **17 字**——模型拿到 4 张参考图却不知道「图几是谁、张数≠人数、同一角色只画一次」，照定妆照把人复制成两个（media 496 初审：可辨认人物 2 人）。两处修复：lean 档图例压成一行（`【参考图】图1=角色「小满」…；张数≠人数…`，约省一半字数，语义不丢）；极端兜底把 `negative`/`quality` 排到图例前面压，图例与人数块保不住完整语义时整块丢弃（前端「这一层没进最终提示词」明确告警），不再截成无意义的半句。

**验证**：`backend/tests/test_duplicate_person_guards.py` 10 条 + `test_providers_agnes.py` 视频 size 归一化 4 条；宿主机 `pytest tests/ -q` → **108 passed**。
重建后 `prompt-preview` 实测：shot 215/216 的图例块分别 67/90 字完整保留（此前 17 字），215 的参考角色退回「小满+三花猫」两张定妆照（不再从巴士站抄环境）。

**已知边界**：拆解阶段的提示词本来就写了「同一场景内的连续镜头必须写同一个 location」，模型没遵守；前缀匹配是补偿而不是根治。更彻底的做法是让拆解为每个镜头输出稳定的 `scene_id`，需要改 schema 与已入库数据，暂缓。

### P2.7 视频分辨率字段不匹配厂商契约（2026-10-25）

Agnes `POST /videos` 只认 `size=720P`（大写枚举，`valid_sizes:["720P"]`）；我们存的是 `720p/1080p` 小写。实测：`1080p` → HTTP 400；`720p` → HTTP 402 `plan_upgrade_required`；`720P` → 建单成功。修复：`agnes.py` 新增 `_video_size()` 把任意写法收敛到厂商枚举，降档时在返回 meta 写 `resolution_note`，经 `serialize.py` 挂到 `ShotRead.video_note`，分镜卡在视频上方用 Alert 显示「Agnes 视频只有 720P 一档，请求的 1080p 已按 720P 出片」——降档必须可见，不静默当高清交付。402 增加中文说明（套餐不支持该档，去 Agnes 后台升级或换 Key）。另用探针确认 `resolution` 不是 /videos 的合法字段（请求它直接 400）。

**已知边界**：拆解阶段的提示词本来就写了「同一场景内的连续镜头必须写同一个 location」，模型没遵守；前缀匹配是补偿而不是根治。更彻底的做法是让拆解为每个镜头输出稳定的 `scene_id`，需要改 schema 与已入库数据，暂缓。

## 三、不建议做成 LLM agent 的部分

| 环节 | 原因 | 建议 |
|---|---|---|
| 503/429 重试与厂商降级 | 现有策略是确定性的、可测试的；LLM 只会增加不确定性 | 保持规则引擎，最多让 agent 解释失败原因。2026-10-25 起：重试预算与模型轮换仍在规则引擎里，但**取消**了「摘掉参考图降级为纯文生图」这条静默降级——它出的图人物一定不像本人 |
| 提示词最终编译 | `compose_generation_prompt` 是预算与硬约束的唯一真源，LLM 重写会破坏保护块 | agent 只产出字段级 patch，必须再过编译与校验 |
| 资产绑定 / 去重 | 姓名与 ID 规则优先，错绑代价高 | agent 只给建议，不直接写库 |
| 剪辑选段 | `editing.py` 已有 AI 选段 + 确定性 fit/pad | 维持现状，最多加一个「为什么选这几段」的解释 |

## 四、Agent runtime 与审计（已落地）

`backend/app/services/agents/`：

```
runtime.py     # 有界循环：工具白名单、必填参数、单工具/昂贵调用预算、连续错误上限、完整 trace
repair.py      # 关键帧修复 agent：inspect / patch_prompt / redraw / review_candidate / escalate
director.py    # 导演自检：确定性校验优先，inspect / validate / revise
diagnosis.py   # 失败诊断：inspect_task / inspect_project / inspect_credentials / retry_task / report / escalate
cut_repair.py  # 切点修复：inspect / patch_prompt / rerender_video / recheck_cut / report / escalate
organize.py    # 素材整理：确定性扫描 + inspect / rescan / report（无写库工具）
audit.py       # record_run：统一写 agent_runs，失败只记日志不拖垮主流程
visual_qa.py   # P0 关键帧审查（单次审查器）
continuity.py  # P1 切点审查（单次审查器）
```

**统一审计表 `agent_runs`（迁移 0012）**

| 字段 | 含义 |
|---|---|
| `kind` | director / diagnose / cut_repair / organize / repair（循环 agent）+ visual_review / continuity_review（单次审查器，`steps=1`） |
| `target_type` + `target_id` | episode / task / shot / project |
| `status` | 各 agent 自己的结论（clean、revised、suggested、already_pass、patch_only、repaired、needs_human、agent_error、review_unavailable 等） |
| `steps` / `stop_reason` / `model` | 运行规模与停止原因 |
| `summary` / `trace` / `error` | 结构化结论、每步 thought/action/args/ok/observation |
| `advisory` | 恒为 true：所有 agent 都只给建议 |

- API：`GET /api/agent-runs?project_id=&kind=&limit=`、`GET /api/agent-runs/{id}`；失败任务另有 `GET /api/tasks/{id}/diagnosis`。
- 早期返回路径也写审计：例如切点修复判定 `already_pass` 时同样落一条 `agent_runs`（E2E 曾发现该路径漏审计，已修）。
- 两个单次审查器同样进审计：`visual_qa` 落 `kind=visual_review`（target=media），`continuity.review_cut` 落 `kind=continuity_review`（target=shot，复用上次结论时不重复记录）；`media_assets.meta` 仍是前端展示的唯一来源。
- 任务类型新增 `diagnose`、`cut_repair`（连同已有 `repair`），在任务中心可按类型筛选。

**运行方式**

- 每个 agent 都是独立任务或独立接口，决策与观察写进 `task.result.trace` 或接口返回的 `trace`；
- 成本上限来自工具 `max_calls` 与 `max_steps`，模型无法越权多调；
- 任何模型不可用、JSON 脏、工具异常都转为观察或 `agent_error` / `unavailable`，降级为人工，不拖垮主链路；
- 批量生视频仍只消费人工认可的帧，agent 只产出候选与建议。

## 五、实施顺序与验收

**已按此顺序完成**：P0 视觉审查 → P1 切点审查 → P1.5 关键帧修复 → P2 导演自检 → P2.1 失败诊断 → P2.2 切点修复 → P2.3 素材整理。

**验收证据（2026-10-25 晚）**

- `python -m pytest tests -q` → **84 passed**；`npx tsc --noEmit` → 0 错误；前端生产构建通过；`import app.main` OK。
- 迁移 `0012_agent_runs` 已应用，`alembic current` = `0012_agent_runs (head)`；backend / worker / frontend 镜像已重建并 force-recreate，`/health` 200。
- 自测项目 34（真实接口，非 mock）：
  - 素材整理：`status=suggested`，1 条建议（`unknown_prop`：教室后门磨砂玻璃），`steps=0`；
  - 切点修复：镜头 98、99 均为 `already_pass`，`steps=0`，未重拍；审计表各落一条；
  - 失败诊断：构造失败任务 536 → 诊断任务 537 `diagnosed`，`steps=4`，`category=auth`，`confidence=high`，`retry_recommended=false`；`GET /api/tasks/536/diagnosis` `found=true`；
  - 导演自检：分集 30 方案 13 镜 / 5 段 / 91 秒，`review.status=clean`、`revisions=0`、`steps=0`，审计已落。
- 项目 34 清理：探针任务 533–537 已删除（剩余 67 条任务），视频仍为 5 个、无新增素材；镜头 97/98 ready、99/103 prepared 为探针前既有状态，未被改变。
- 前端登录态未做点击验收（无可用登录会话，也未注入令牌）：以 `tsc`、API 契约、生产构建通过为准。

**后续（尚未做）**

- `agent_runs` 跨项目统计看板（当前仅按项目列表 + 单条详情，前端可按 kind 筛选）；
- `fail` 是否升级为导出硬闸门——需先积累「审查结论与人工判断一致率」；
- 审查器行数控制：目前每张关键帧初审、每个真实重跑的切点各落一行，量大时再加保留期或按项目开关。

**已确认不需要做**

- 脏 JSON 容错：`prompts.extract_json` 已做代码块剥离、多候选解析与语法修补，`_generate_json` 另有 `attempts=2` 重试；agent 的决策解析走同一入口，无需再造一层。

## 六、风险与成本

- **视觉凭证已具备**：现有 DeepSeek Key 即可（`deepseek-flash`）；如果不想把帧外发给第三方，再考虑本地 Qwen-VL/Ollama。
- **模型名要更新**：DeepSeek `/models` 里已没有 `deepseek-v4-flash` / `deepseek-chat`，当前 registry 的默认文本模型仍是旧名（但旧名会路由到 V4.1-Flash）。接 P0 时顺手把默认模型改成 `deepseek-flash`，避免哪天别名下线。
- **误报会消耗信任**：所有 agent 只做建议，不做阻断；是否重拍/重绘都要人显式开启。
- **循环失控**：max_steps、单工具次数、昂贵调用预算、超时、连续错误即停，缺一不可。
- **隐私**：帧会发给 DeepSeek，与现有图片/视频厂商同级；在凭证页说明。
- **一次上多个 agent 的教训**：先做确定性校验（导演/整理）能显著降低 LLM 调用与误报；真正需要模型的是「语义判断 + 多步修正」，而审计表必须在第一条链路就建好——切点修复的 `already_pass` 漏审计就是事后补的。
