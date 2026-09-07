from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio
from kimi_agent_module_api import (
    ModuleCapabilities,
    ModuleRuntimeContext,
    ModuleToolContext,
    ToolAttachment,
    ToolFile,
    TrustTier,
)
from kimi_agent_module_api.testing import (
    FakeDiscordActions,
    FakeEvents,
    FakeHealth,
    FakeHttp,
    FakeInteractions,
    FakeScheduler,
    FakeServiceRegistry,
    FakeToolFiles,
    FakeTrust,
    MemoryStorage,
    RecordingToolRegistry,
    load_context,
)
from pydantic import SecretStr

from kimi_agent_content_provenance import SPEC, TOOL_NAME, create
from kimi_agent_content_provenance.client import ProvenanceClient
from kimi_agent_content_provenance.module import ProvenanceModule
from kimi_agent_content_provenance.settings import ProvenanceSettings

ORIGINAL = b"\x89PNG\r\n\x1a\noriginal bytes including provenance metadata"
ATTACHMENT = ToolAttachment(
    "current:0", "original.png", len(ORIGINAL), "image/png", workspace_path="saved.png"
)
CALLER = ModuleToolContext(
    1, "Alice", 10, 20, None, TrustTier.MEMBER, trigger_discord_message_id=30
)


def result_payload(*, detected: bool = False, audio: bool = False) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if not audio:
        results.append(
            {
                "type": "c2pa",
                "outcome": "detected" if detected else "not_detected",
                "validation_state": "trusted" if detected else "not_present",
                "issuer": "OpenAI" if detected else None,
                "model": None,
                "generated_at": None,
            }
        )
    results.append(
        {"type": "synthid", "outcome": "not_detected", "model": None, "generated_at": None}
    )
    return {"object": "content_provenance_check", "created_at": 123, "results": results}


@dataclass
class Harness:
    module: ProvenanceModule
    ctx: ModuleRuntimeContext
    discord: FakeDiscordActions
    http: FakeHttp
    health: FakeHealth
    registry: RecordingToolRegistry
    files: FakeToolFiles
    requests: list[httpx.Request] = field(default_factory=list)
    response: dict[str, Any] = field(default_factory=result_payload)
    status: int = 200

    def upstream(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.response)

    async def check(
        self, args: dict[str, Any] | None = None, caller: ModuleToolContext = CALLER
    ) -> dict[str, Any]:
        result = json.loads(
            await self.registry.tools[TOOL_NAME].handler(
                args or {}, replace(caller, files=self.files)
            )
        )
        assert isinstance(result, dict)
        return result


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    settings = ProvenanceSettings(openai_api_key=SecretStr("test-key"), max_file_bytes=1024)
    load, recorded = load_context(settings)
    module = create(load)
    discord = FakeDiscordActions(SPEC.name, SPEC.permissions.discord_actions)
    http = FakeHttp()
    health = FakeHealth()
    async with MemoryStorage.open(SPEC.name) as storage:
        ctx = ModuleRuntimeContext(
            module_name=SPEC.name,
            is_guild_active=lambda guild: guild == 10,
            current_config_dir=lambda: tmp_path,
            capabilities=ModuleCapabilities(frozenset(), False, True),
            events=FakeEvents(SPEC.name),
            scheduler=FakeScheduler(),
            storage=storage,
            health=health,
            discord=discord,
            interactions=FakeInteractions(SPEC.name),
            http=http,
            services=FakeServiceRegistry(),
            trust=FakeTrust(),
        )
        await module.start(ctx)
        assert module._client is not None
        await module._client.close()
        files = FakeToolFiles(
            (ATTACHMENT,),
            attachment_files={ATTACHMENT.id: ToolFile("original.png", "image/png", ORIGINAL)},
            workspace_files={"saved.png": ToolFile("original.png", "image/png", ORIGINAL)},
        )
        harness = Harness(module, ctx, discord, http, health, recorded.registry, files)
        module._client = ProvenanceClient(
            "test-key", 60, transport=httpx.MockTransport(harness.upstream)
        )
        try:
            yield harness
        finally:
            await module.close()
