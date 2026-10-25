from pydantic import BaseModel


class SkillInfo(BaseModel):
    """镜头指令 skill 的元信息，供前端展示与选择。"""

    key: str
    label: str
    description: str
    fields: list[str]
