from pydantic import BaseModel


class ProviderInfo(BaseModel):
    key: str
    label: str
    capabilities: list[str]
    default_base_url: str
    models: dict[str, list[str]]
    docs_url: str = ""
    description: str = ""
