# AI 视频 / 图片 / 短剧生成平台 —— 平台调研与技术方案

> 数据截止：2026-10　｜　调研范围：GitHub 开源短剧/视频生成项目 + 主流商业平台 + 模型/网关生态
> 目的：归纳整合出「最完善」的实现方式，供决策后进入开发。

---

## 一、执行摘要

1. 当前开源阵营可归纳为 **三种截然不同的产品形态**：
   - **工作台型（业务编排）**：Jellyfish、DramaClaw、ArcReel —— 有后端、有数据库、有工作台，把「剧本→分镜→一致性资产→生成→导出」串成流水线。[cite:1][cite:9][cite:10]
   - **Agent 型（无编排器）**：OpenMontage、Toonflow —— 把「你的 AI 编码助手 / 本地 Agent」当成导演，用 Manifest + Skill 文件驱动生产，无中心化服务。[cite:4][cite:5]
   - **一键型（极简）**：MoneyPrinterTurbo —— 输入主题，自动出片，功能单一但门槛最低。[cite:7]
2. **Key 策略是最大分水岭**，共四种，可组合：
   - A. **用户自带 Key（BYOK）**：Jellyfish / Toonflow 模式；[cite:2][cite:8]
   - B. **统一网关聚合多 Key**：one-api / LiteLLM 模式，一份接口适配 N 家厂商；[cite:17][cite:18]
   - C. **零 Key 通道**：OpenMontage 的本地/免费路径 + Agnes AI 免费额度 + ComfyUI 本地推理；[cite:6][cite:13][cite:22]
   - D. **平台托管 Key（作者付费）**：商业 SaaS（即梦、可灵、Vidu Q3、剪映 Hub）。[cite:19][cite:20][cite:21]
3. **推荐落地方案**：工作台型主干 + 统一模型网关 + 异步任务中心 + 三通道（BYOK / 免费额度 / 本地零 Key）。技术栈推荐 **方案 C：Spring Boot 业务与网关 + Python AI Worker**。
4. 核心护城河不在「调 API」，而在 **资产一致性（角色/场景/道具/服装）**、**任务可靠性（长任务状态机）** 与 **多供应商可插拔** 三件事上。

---

## 二、开源平台横向对比

| 维度 | Jellyfish | OpenMontage | MoneyPrinterTurbo | Toonflow | DramaClaw / ArcReel |
|---|---|---|---|---|---|
| 定位 | 短剧生产工作台 | Agent 视频生产系统 | 一键短视频 | 小说→动画短剧工具 | 通用 AIGC 视频引擎 / 小说转视频 |
| License | Apache-2.0 [cite:2] | AGPL-3.0 [cite:5] | MIT [cite:7] | 开源免费 [cite:8] | 开源（NOASSERTION/Python）[cite:9] |
| 前端 | React 18 + TS + Vite + Ant Design [cite:3] | 无独立前端（Agent 驱动）[cite:5] | Streamlit / WebUI [cite:7] | Electron + Vue 3 [cite:8] | 有工作台 [cite:10] |
| 后端 | FastAPI + MySQL + Redis + RustFS(S3) [cite:3] | Python（无编排器，Agent 即控制循环）[cite:5] | Python | Node.js + Express 5 + SQLite [cite:8] | Python / Claude Agent SDK 多智能体 [cite:10] |
| Key 模式 | 用户自带（多供应商多模型）[cite:2] | 默认零 Key，付费 Key 可选 [cite:6] | 自带（LLM + Pexels + TTS）[cite:7] | 自带（Vercel AI SDK 多厂商）[cite:8] | 自带 + 自定义 OpenAI 兼容供应商 [cite:10] |
| 核心能力 | 剧本拆解、分镜准备、资产一致性、单镜/批量生成、任务中心 [cite:2] | 12 条流水线、52 工具、400+ Skills、质量门禁、预算治理 [cite:6] | 主题→文案→素材→TTS→字幕→BGM→合成 [cite:7] | AI 编剧/智能分镜/角色与视频生成 [cite:8] | 角色提取→分集→剧本 JSON→角色设计图→分镜→视频→FFmpeg 合成 [cite:10] |
| 亮点 | 一致性实体模型 + OpenAPI 代码生成 [cite:2] | 零 Key 真素材纪录片；供应商可换、有评分选择 [cite:5][cite:6] | 53k+ star，门槛最低 [cite:7] | 10k+ star，桌面端跨平台 [cite:8] | 多 Agent 编排 + 剪映草稿导出 [cite:10] |
| 局限 | 模型成本由用户承担 | AGPL 传染性；强依赖编码助手 [cite:5] | 叙事结构弱、无角色一致性 | 桌面端形态，服务化弱 | 生态较散、文档不统一 |

> 商业侧参照：即梦（字节，Seedance 入口，中文强、出片快，但长叙事/跨集角色管理弱）、Vidu Q3（原生音视频、多人对白、Q3 Drama 面向短剧）、剪映 Hub（无限画布 + 多轨道，调用即梦/小云雀）。[cite:19][cite:20][cite:21]

---

## 三、四种 API Key 策略（核心决策点）

| 策略 | 说明 | 代表实现 | 优点 | 代价 |
|---|---|---|---|---|
| **A. BYOK 用户自带 Key** | 用户在前端录入自己的 Key，系统只做编排 | Jellyfish / Toonflow [cite:2][cite:8] | 零平台成本、可支持任意厂商、合规清晰 | 用户门槛高、需支持众多厂商差异化字段 |
| **B. 统一网关聚合** | 后端建「模型网关」，一份接口适配 N 家，统一鉴权/计量/重试 | one-api 支持 OpenAI/Azure/Claude/Gemini/DeepSeek/豆包/千问/混元等 [cite:17]；LiteLLM 支持 100+ 端点、虚拟 Key 与预算 [cite:18] | 一次接入到处可用；可做额度、限流、成本核算 | 需维护适配层；视频类厂商协议差异大 |
| **C. 零 Key / 免费额度 / 本地** | 不填 Key 也能出片 | OpenMontage：Piper 离线 TTS + Archive.org/NASA/Wikimedia 素材 + WhisperX + Remotion/FFmpeg [cite:6]；Agnes AI 核心模型「无限期免费」、Starter 额度 1,500 请求/5h、4,000 图/天、500 秒视频/天 [cite:12][cite:13]；本地 WAN 2.1 / Hunyuan / CogVideo 免 Key 但吃 GPU [cite:6][cite:22] | 零成本可跑通全流程，转化率高 | 免费额度有限流/版权与画质约束；本地推理需显卡 |
| **D. 平台托管 Key** | 平台方出 Key，按量/订阅收费 | 商业 SaaS [cite:19] | 体验最好 | 平台成本与合规风险 |

**结论：A + B + C 组合，D 作为后期商业化选项。** 即「用户自带 Key 为主 + 网关统一适配 + 免费/本地零 Key 通道兜底」。

---

## 四、模型生态清单（按模态）

### 4.1 文本 / 推理（剧本、解析、结构化）
| 模型 | 提供方 | 接入方式 | 说明 |
|---|---|---|---|
| deepseek-v4-flash / deepseek-v4-pro | DeepSeek | OpenAI 兼容 API（api.deepseek.com） | 主力文案与结构化输出，成本低 [cite:16] |
| agnes-2.5-flash | Agnes AI | `https://apihub.agnes-ai.com/v1`，Bearer Key [cite:11] | 免费额度，支持 Thinking / Function Calling [cite:13] |
| GPT / Claude / Gemini / 智谱 / MiniMax / Qwen / xAI | 多厂商 | 网关统一适配 | Toonflow 已支持此集合，可作参考 [cite:8] |

### 4.2 图片生成
| 模型 | 提供方 | 计费 | 说明 |
|---|---|---|---|
| agnes-image-2.1-flash | Agnes AI（Echo） | 核心模型免费，输出分辨率输出当前免费 [cite:13] | 文生图/图生图，出图快 [cite:10] |
| flux-schnell / z-image-turbo / krea-2 | MagicHour | 5–10 credits 起，free 档可用 [cite:14] | 免费档低价起步 |
| nano-banana 系列 / seedream 系列 / gpt-image / qwen-image | MagicHour | 50–150 credits [cite:14] | 高质量/可编辑，付费档 |
| FLUX / Imagen / Grok / Recraft / 本地 Diffusion | OpenMontage | 部分免费 | 供应商可插拔 [cite:6] |

### 4.3 视频生成
| 模型 | 提供方 | 计费（credits/秒） | 说明 |
|---|---|---|---|
| agnes-video-v2.0 / agnes-video-2.5-flash | Agnes AI（Pavo） | 2.5-flash 当前限时免费 [cite:12] | 文生/图生/首尾帧，异步轮询 [cite:10] |
| ltx-2.5 / minimax-h3 | MagicHour | 24–72/秒，**free 档可用（480p）** [cite:14] | 推荐起步模型 |
| kling-3.0 / seedance-2.5 / veo3.1 / wan-3.0 / sora-2 | MagicHour | 48–576/秒（付费档）[cite:14] | 高画质 |
| WAN 2.1 / Hunyuan / CogVideo / LTX-Video | 本地 | 免 Key，吃 GPU [cite:6][cite:22] | 零 Key 通道 |
| Vidu Q3 / 可灵 / 即梦 Seedance | 国内商业 | 按量 | 原生音视频、多人对白 [cite:20] |

### 4.4 语音 / 音乐 / 后期
- TTS：Piper（免费离线）、ElevenLabs、Google TTS（700+ 音色）、OpenAI TTS、Kling TTS [cite:6]
- 音乐：Suno、ElevenLabs Music / SFX [cite:6]
- 后期：FFmpeg（编码/字幕烧录）、Video Stitch、Audio Mix、Color Grade、升频(Real-ESRGAN)、去背(rembg)、口型(Wav2Lip/MuseTalk)、转录(WhisperX)、场景检测、CLIP/BLIP 理解 [cite:6]
- 导出：FFmpeg 成片 + **剪映草稿导出**（ArcReel 支持）[cite:10]

> MagicHour 计费换算：Creator $19/12,000 credits、Pro $39/25,000、Business $99/70,000；失败不扣费；免费档 480p。[cite:14][cite:15]

---

## 五、短剧生产工作流（多家归纳后的「完整链路」）

```
小说/剧本输入
  → ①剧本解析（角色/场景/道具/服装/对白抽取）
  → ②分集规划（Episode Planning）
  → ③分镜拆解（Storyboard / Shot Breakdown）
  → ④资产一致性（角色设计图、场景/道具参考图）
  → ⑤镜头准备（关键帧/首帧、镜头级 Prompt）
  → ⑥生成执行（图生视频 / 文生视频 / 批量）
  → ⑦配音 TTS + 字幕 + 音乐音效
  → ⑧合成导出（FFmpeg / Remotion / 剪映草稿）
```

- Jellyfish 的关键贡献：把 **④资产一致性** 与 **⑥-⑦异步任务中心** 提升为一等公民，并用 `prepared` 与 `generating` 两态区分「已就绪」与「正在生成」。[cite:2]
- OpenMontage 的关键贡献：**阶段化流水线 + 人审门禁 + 预算治理**（research→proposal→script→scene_plan→assets→edit→compose），以及「零 Key 也能真素材成片」。[cite:5][cite:6]
- ArcReel 的关键贡献：**角色设计图保证跨镜头一致性** + **多 Agent 编排** + **自定义 OpenAI 兼容供应商**。[cite:10]

---

## 六、推荐系统架构

```
┌──────────────────────────────────────────────────────────┐
│  前端工作台 (Web)  React + TS + Vite + Ant Design          │
│  项目/分集/剧本 · 分镜台 · 资产库 · 任务中心 · Key 管理      │
└───────────────┬──────────────────────────────────────────┘
                │ REST / SSE
┌───────────────▼──────────────────────────────────────────┐
│  业务与编排层 (Spring Boot 3)                              │
│  用户/Key 管理 · 项目/剧本/分镜 · 资产一致性 · 任务编排       │
│  计量与额度 · 审计日志                                     │
└───────┬───────────────────────────┬──────────────────────┘
        │                           │
┌───────▼─────────────┐   ┌─────────▼──────────────────────┐
│ 统一模型网关          │   │ Python AI Worker (FastAPI/Celery)│
│ Provider Adapter     │   │ 本地推理编排(ComfyUI/WAN/Hunyuan) │
│ DeepSeek/Agnes/      │   │ 语音(Piper/TTS) · 转录(WhisperX)  │
│ MagicHour/OpenAI/... │   │ FFmpeg 合成 · 素材抓取            │
│ 重试/降级/配额/成本    │   │ 长任务执行与回调                  │
└───────┬─────────────┘   └─────────┬──────────────────────┘
        │                           │
┌───────▼───────────────────────────▼──────────────────────┐
│ 基础设施：PostgreSQL + Redis(队列/缓存) + S3/MinIO/RustFS   │
└──────────────────────────────────────────────────────────┘
```

### 关键设计点
1. **Provider Adapter 统一抽象**：文本 / 图片 / 视频 / 语音 四类接口，每家厂商一个适配器，屏蔽字段差异（如 Agnes 接口与 OpenAI「类似但路径/字段不同」，需适配层；可参考 O4OpenAI 的思路做协议转换）。[cite:10]
2. **异步任务中心（状态机）**：`PENDING → RUNNING → SUCCEEDED / FAILED / CANCELED`；支持取消、重试、断点恢复（ArcReel 的 `resume=true` 思路）。视频类均为异步轮询。[cite:1][cite:10]
3. **一致性资产库**：`character / scene / prop / costume` 实体 + 参考图，镜头层复用，减少「角色漂移」。[cite:2]
4. **Prompt 模板中心**：按模态分类的模板与默认模型设置。[cite:2]
5. **三通道 Key 解析**：`用户自有 Key → 平台免费额度(Agnes) → 本地零 Key`，按优先级与可用性降级。
6. **预算与门禁**：成本预估、单次/单项目上限、阶段人工确认（可借鉴 OpenMontage 的门禁与预算治理）。[cite:5][cite:6]

---

## 七、技术栈选型（三方案）

| 方案 | 构成 | 优势 | 劣势 | 适配度 |
|---|---|---|---|---|
| **A. 全 Python 主线** | FastAPI + PostgreSQL + Redis + Celery + React/TS/Vite/antd | 与 Jellyfish/DramaClaw 生态一致；AI 库最全；上手快 | 与既有 Java 偏好不符 | 最贴 AI 生态 [cite:3] |
| **B. 全 Java 主线** | Spring Boot 3 + PostgreSQL + Redis + RabbitMQ/Kafka + React | 业务/网关稳健、团队熟悉 | AI/本地推理需大量外部进程桥接 | 符合既有偏好 |
| **C. 混合（推荐）** | **Spring Boot 3 业务+网关** ＋ **Python AI Worker**（FastAPI/Celery）＋ React 前端 | 业务用 Java（稳、可维护），AI 用 Python（生态全、本地推理顺） | 多语言运维成本 | **推荐** |

> 前端统一建议：**React 18 + TypeScript + Vite + Ant Design**（与 Jellyfish 一致，且符合既有技术栈），并使用 OpenAPI 自动生成前端类型与请求代码。[cite:2][cite:3]

---

## 八、功能模块清单（MVP → 进阶）

**MVP（第一版）**
1. 用户与 Key 管理：多厂商多 Key 录入、连通性测试、默认模型设置
2. 项目 / 分集 / 剧本 / 分镜 CRUD
3. 文本生成：DeepSeek 主力（剧本、拆解、结构化）
4. 图片生成：Agnes + MagicHour 适配器，单图/批量
5. 视频生成：图生视频（Agnes / MagicHour），异步任务 + 轮询
6. 统一任务中心：状态、进度、取消、重试
7. 文件与媒体库：上传/生成物预览、复用

**进阶**
8. 资产一致性库（角色/场景/道具/服装）+ 角色设计图
9. 语音与字幕（Piper/TTS + WhisperX）+ 背景音乐
10. FFmpeg 一键合成 + 剪映草稿导出
11. 零 Key 通道：本地 ComfyUI/WAN + 免费素材库(Archive.org/Pexels)
12. 网关计量：额度、限流、成本看板、审计日志
13. 阶段化流水线与人审门禁（OpenMontage 式）

---

## 九、风险与合规

| 风险 | 说明 | 应对 |
|---|---|---|
| AGPL 传染性 | OpenMontage 为 AGPL-3.0，网络分发需开源修改 [cite:5] | 仅借鉴架构思路，不直接内联其代码；或作为可选外部进程 |
| 免费额度限流 | Agnes 免费档有 RPM 限制 [cite:13]；MagicHour 免费档仅 480p [cite:14] | 网关层做降级与队列削峰 |
| 成本失控 | 视频按秒计费，易超支 [cite:14] | 成本预估 + 预算上限 + 门禁 |
| 本地推理硬件 | WAN/Hunyuan/CogVideo 吃 GPU [cite:6] | 独立 Worker，按需启用 |
| 内容合规 | 生成内容需审核 | 预留审核钩子与留痕 |

---

## 十、结论与待决事项

- **形态**：工作台型 → 建议以 Jellyfish 式「剧本→分镜→一致性→生成→导出」为主干。
- **Key**：A（BYOK）+ B（网关）+ C（零 Key/免费/本地）三通道组合。
- **栈**：方案 C（Spring Boot + Python Worker + React）。
- **待用户拍板**：技术栈方案、首版范围(MVP 边界)、Key 通道优先级、是否支持本地零 Key。

---

## Sources / 参考来源

[cite:1] Jellyfish 官网（AI 短剧工厂）— https://forget-c.github.io/Jellyfish/
[cite:2] Forget-C/Jellyfish GitHub README — https://github.com/Forget-C/Jellyfish
[cite:3] Jellyfish 技术栈解析（AIBit）— https://aibit.im/fr/article/jellyfish-ai-short-drama-factory-full-production-suite
[cite:4] OpenMontage 官网 — https://openmontage.video/
[cite:5] OpenMontage 架构解析（creeta news, 2026-08）— https://news.creeta.com/en/openmontage-agentic-video-no-orchestrator/
[cite:6] OpenMontage 能力清单（agents-lib）— https://agents-lib.com/agents/openmontage-video-production
[cite:7] harry0703/MoneyPrinterTurbo — https://github.com/harry0703/MoneyPrinterTurbo
[cite:8] HBAI-Ltd/Toonflow-app 技术栈 — https://gitee.com/rushuai/Toonflow-app ／ https://toonflow.net/
[cite:9] dramaclaw/dramaclaw — https://topairepos.com/repos/dramaclaw/dramaclaw
[cite:10] Agnes AI + ArcReel + O4OpenAI 零成本流水线 — https://cloud.tencent.com/developer/article/2684247
[cite:11] Agnes AI API Quickstart — https://wiki.agnes-ai.com/en/docs/quickstart
[cite:12] Agnes AI Token Plan — https://wiki.agnes-ai.com/en/docs/tokenplan
[cite:13] Agnes AI FAQ（免费额度）— https://wiki.agnes-ai.com/en/docs/faqs
[cite:14] Magic Hour API Models & Credit Costs — https://docs.magichour.ai/api-reference/models
[cite:15] Magic Hour API Billing — https://docs.magichour.ai/billing/overview
[cite:16] DeepSeek API 模型与价格 — https://api-docs.deepseek.com/zh-cn/quick_start/pricing
[cite:17] songquanpeng/one-api — https://github.com/songquanpeng/one-api
[cite:18] LiteLLM AI Gateway — https://www.litellm.ai/
[cite:19] AI 视频工具剧情创作能力分析（IT之家）— https://www.ithome.com/1/001/396.htm
[cite:20] AI 短剧生成平台按「生产任务」选型（Vidu Q3）— https://m.sohu.com/a/1076814793_122862766/
[cite:21] 夹在字节缝隙中的 AI 融资明星（剪映 Hub）— http://m.toutiao.com/group/7697540869284446758/
[cite:22] ComfyUI Wan2.1 Video 教程 — https://docs.comfy.org/zh/tutorials/video/wan/wan-video
