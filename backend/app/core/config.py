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
