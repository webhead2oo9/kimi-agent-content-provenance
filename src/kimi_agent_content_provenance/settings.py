"""Settings prepared by Kimi from the selected instance environment."""

from __future__ import annotations

from kimi_agent_module_api import ModuleSetting, ModuleSettingsDefinition
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProvenanceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTENT_PROVENANCE_", extra="ignore")

    openai_api_key: SecretStr = SecretStr("")
    max_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    timeout_seconds: float = Field(default=60, ge=1, le=120)


SETTINGS = ModuleSettingsDefinition(
    name="content_provenance",
    label="Content provenance",
    model=ProvenanceSettings,
    exposed=(
        ModuleSetting("max_file_bytes", "Maximum upload bytes", minimum=1),
        ModuleSetting("timeout_seconds", "Check timeout in seconds", minimum=1),
    ),
    environment_only=frozenset({"openai_api_key"}),
)
