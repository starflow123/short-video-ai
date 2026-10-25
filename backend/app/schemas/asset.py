from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

ASSET_KINDS = ("actor", "scene", "prop", "costume")


class AssetCreate(BaseModel):
    """新建生产资产：角色 / 场景 / 道具 / 服装。"""

    kind: str = Field(description="actor / scene / prop / costume")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    prompt: str | None = Field(default=None, description="稳定的外观描述，跨镜头原样复用")
    tags: list[str] | None = None
    attributes: dict | None = None
    media_id: int | None = Field(default=None, description="参考图（media_assets.id）")
    project_id: int | None = None
    is_global: bool = False


class AssetUpdate(BaseModel):
    kind: str | None = None
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    prompt: str | None = None
    tags: list[str] | None = None
    attributes: dict | None = None
    media_id: int | None = None
    project_id: int | None = None
    is_global: bool | None = None


class AssetFromMedia(BaseModel):
    """把一张已生成的图片素材直接存为资产（复用其外观）。"""

    media_id: int
    kind: str = "actor"
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    prompt: str | None = None
    project_id: int | None = None
    is_global: bool = False


class AssetGenerateRequest(BaseModel):
    """给资产直接生成参考图：输入描述词出图，出的图自动挂成该资产的参考图。

    之后在分镜里关联这个资产时，参考图会作为「图像参考」一起送给模型，
    让同一个角色 / 场景在不同镜头里长得一样。
    """

    prompt: str | None = Field(default=None, description="覆盖描述词；留空则用资产自身的 prompt")
    credential_id: int | None = None
    provider: str | None = None
    model: str | None = None
    negative_prompt: str | None = None
    n: int = Field(default=1, ge=1, le=4)
    aspect_ratio: str | None = None
    size: str | None = None
    project_id: int | None = Field(default=None, description="不传则用资产所属项目")
    use_reference: bool = Field(
        default=True,
        description="是否基于资产已有参考图修改（换装但保脸）。有参考图时默认开启，关闭则纯文生图重画",
    )
    reference_media_id: int | None = Field(
        default=None, description="额外参考图（素材库 media id），如某套服装的实物图"
    )


class AssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int | None = None
    kind: str
    name: str
    description: str | None = None
    prompt: str | None = None
    tags: list[str] | None = None
    attributes: dict | None = None
    media_id: int | None = None
    source: str
    is_global: bool
    created_at: datetime
    # 冗余展示字段
    media_url: str | None = None
    project_title: str | None = None
