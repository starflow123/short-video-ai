# AI Video Studio

AI 短剧 / 视频生成工作台。用户自带 API Key（BYOK），平台负责 **剧本 → AI 拆解分镜 → 角色定妆照 → 镜头关键帧 → 分镜视频 → 拼接导出** 的完整制作链路。

## 功能

- **两阶段分镜拆解**：先让 AI 产出拆解方案（分段、每段镜数、时长、剧情点与理由），用户可改镜数 / 时长后确认，确认后才真正落库，避免「一次 AI 把整集拆歪」。
- **长集数分段拆解**：按确认方案的段落逐次调用模型，合并结果并让相邻镜头继承上一镜的收尾状态，解决单次输出被截断导致丢镜、剧情断线的问题。
- **逐镜智能决策**：每个镜头由模型决定时长、分辨率与画面提示词；提示词按主体 / 道具 / 动作 / 场景 / 风格 / 质量约束分层编译，并做字数预算，超限时按优先级压缩而不丢动作语义。
- **角色一致性**：从剧本抽取角色档案 → 落库为演员资产 → 自动生成定妆照 → 关键帧与分镜视频按本镜出场角色挑参考图；资产库支持跨项目复用同一形象。
- **任务链与容错**：图片、视频、导出串成顺序链，逐个投递；支持取消、失败传播与限流重试，单个环节失败不会拖垮后续链路。
- **多厂商适配**：文本 DeepSeek（兼容任意 OpenAI 接口）、图片 / 视频 Agnes AI 与 Magic Hour。
- **一键成片**：FFmpeg 拼接分镜视频并导出。

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | React 18 + TypeScript + Vite + Ant Design |
| 后端 | FastAPI + SQLAlchemy 2.0 (async) + PostgreSQL + Alembic |
| 异步 | Celery + Redis |
| 存储 | 本地媒体目录（可换 S3/MinIO） |
| 部署 | Docker Compose |

## 快速开始

```bash
cd deploy
cp ../backend/.env.example ../backend/.env   # 首次需生成密钥，见下
docker compose up --build
```

Windows 下也可以直接双击根目录的 `start.bat` / `stop.bat`。

- 前端：http://localhost:7788
- 后端 API 文档：http://localhost:8000/docs（仅绑定 127.0.0.1，前端经 nginx 反代访问）

首次启动前，请在 `backend/.env` 中设置：

```
SECRET_KEY=<随机字符串>
ENCRYPTION_KEY=<Fernet 密钥>
```

生成方式：

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"                       # SECRET_KEY
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"  # ENCRYPTION_KEY
```

## 使用流程

1. 注册 / 登录
2. 「密钥管理」录入自己的 DeepSeek / Agnes / Magic Hour API Key，并测试连通性
3. 新建项目 → 新建分集 → 填写剧本
4. 点「AI 规划拆解方案」，按需调整镜数与时长后确认拆解
5. 拆解完成后自动补角色定妆照 / 道具参考图（可在资产库查看与替换）
6. 校对分镜并确认，批量生成关键帧
7. 批量生成分镜视频（支持首尾帧接力）
8. 在「任务中心」跟踪进度，最后导出成片

## 关键设计

- **拆解先规划后执行**：同步接口只返回方案、不改数据；确认后异步任务才写分镜，用户始终有修改权。
- **角色先于镜头**：档案 → 演员 / 道具资产 → 定妆照 → 关键帧，链路按此顺序串行，后一步才能拿到前一步的参考图。
- **提示词分层 + 预算**：各层独立拼装，压缩时只削低优先级层，保证动作与画质底线；同时适配厂商的字数硬上限。
- **任务链串行**：第 N 镜可引用第 N-1 镜刚落库的关键帧，避免并发造成的参考图缺失与厂商限流。

## 目录结构

```
backend/     FastAPI 服务 + Celery Worker + Provider 适配器
frontend/    React 工作台
deploy/      Docker Compose
docs/        平台调研与技术方案
```

## 说明

- 本项目仅做编排与适配，模型调用均由用户自带的 Key 完成，平台不代管 Key。
- 密钥在数据库中经 Fernet 对称加密存储。
- Agnes 的图生视频仅接受公网可访问的图片地址：若部署在公网域名下，请在 `backend/.env` 中设置 `PUBLIC_MEDIA_BASE_URL`；否则请使用 Magic Hour 生成图生视频（平台会自动上传本地关键帧）。
