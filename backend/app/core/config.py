"""应用配置：全部通过环境变量 / .env 注入。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "AI Video Studio"
    debug: bool = False

    secret_key: str = "change-me"
    encryption_key: str = ""
    access_token_expire_minutes: int = 10080

    cors_origins: str = "http://localhost:7788"

    database_url: str = "postgresql+asyncpg://aivideo:aivideo@localhost:5432/aivideo"
    redis_url: str = "redis://localhost:6379/0"

    media_root: str = "/data/media"
    media_url_prefix: str = "/media"
    # 若部署在可公网访问的域名下，配置该项后 Agnes 等「仅接受 URL」的厂商
    # 也能直接使用本地素材；留空则仅 Magic Hour 的图生视频可用。
    public_media_base_url: str = ""
    max_upload_mb: int = 512
    generation_timeout: int = 900

    # 关键帧视觉初审：开启后在生图任务末尾顺带调一次视觉模型（只给建议、不阻断）。
    # 关掉后不产生任何额外调用与任务，行为与接入前完全一致。
    vision_review_enabled: bool = True

    # 切点连续性审查：导出前比对相邻镜头尾帧/首帧（人 / 服装 / 场景 / 光线）。
    # 只出报告、不阻断导出；视频没变时复用上次结论，关掉后 0 额外调用。
    continuity_review_enabled: bool = True

    # 关键帧修复 agent：初审失败后自动改提示词 → 重绘 → 复审的最大重绘轮数。
    # 候选帧只追加、不替换旧帧，也永远不会被自动认可；人工认可仍是唯一闸门。
    agent_repair_max_rounds: int = 2

    # 失败诊断 agent：任务失败后自动挂一条诊断任务（只分析、不重跑）。
    # 默认关闭重跑；用户在任务详情里显式开启后，agent 才能换凭证重跑一次。
    agent_diagnosis_enabled: bool = True

    # 切点修复 agent：对连续性审查失败项给出提示词修正建议。
    # 重绘下一镜视频默认关闭——视频重绘成本高，需要在请求里显式开启。
    cut_repair_rerender_enabled: bool = False

    # 首次部署可留空（允许自由注册）；设置后注册必须携带同样的邀请码
    registration_invite_code: str = ""
    # 媒体地址签名有效期：厂商拉取参考图用的临时链接
    media_signature_ttl_seconds: int = 86400

    # 允许访问内网地址的厂商 base_url（本地 Ollama / 局域网服务才需要开启）
    allow_private_outbound: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
