from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from conftest import ATTACHMENT, CALLER, ORIGINAL, Harness, result_payload
from kimi_agent_module_api import ToolAttachment, ToolFile, TrustTier
from kimi_agent_module_api.contracts import (
    validate_module_name,
    validate_permissions,
    validate_services,
)
from kimi_agent_module_api.testing import FakeToolFiles, load_context
from pydantic import SecretStr, ValidationError

from kimi_agent_content_provenance import SPEC, TOOL_NAME, create
from kimi_agent_content_provenance.module import ProvenanceModule
from kimi_agent_content_provenance.settings import ProvenanceSettings


def test_spec_and_tool_contract() -> None:
    validate_module_name(SPEC.name)
    validate_permissions(SPEC.name, SPEC.permissions)
    validate_services(SPEC.name, SPEC.dependencies, SPEC.provides, SPEC.consumes)
    load, recorded = load_context(ProvenanceSettings())
    module = create(load)
    tool = recorded.registry.tools[TOOL_NAME]
    assert tool.searchable and not tool.guild_only and tool.untrusted
    assert tool.min_tier == TrustTier.MEMBER and not module.scoped_migrations
    assert SPEC.permissions.tool_files
    assert not SPEC.permissions.discord_actions
    assert [rule.host for rule in SPEC.permissions.http_hosts] == ["api.openai.com"]
    assert SPEC.requires_capabilities == ("tools.files.v1",)
    assert SPEC.settings is not None
    assert {x.field for x in SPEC.settings.exposed} | SPEC.settings.environment_only == set(
        ProvenanceSettings.model_fields
    )
    assert "openai_api_key" in SPEC.settings.environment_only


def test_settings_isolate_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-chat-key")
    monkeypatch.delenv("CONTENT_PROVENANCE_OPENAI_API_KEY", raising=False)
    assert not ProvenanceSettings().openai_api_key.get_secret_value()
    monkeypatch.setenv("CONTENT_PROVENANCE_OPENAI_API_KEY", "module-key")
    assert "module-key" not in repr(ProvenanceSettings())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_file_bytes": 0},
        {"max_file_bytes": 50 * 1024 * 1024 + 1},
        {"timeout_seconds": 0},
        {"timeout_seconds": 121},
    ],
)
def test_setting_bounds(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProvenanceSettings(**kwargs)


@pytest.mark.asyncio
async def test_original_bytes_and_negative_result(harness: Harness) -> None:
    result = await harness.check()
    assert result["summary"] == "No supported OpenAI signals detected."
    assert "does not establish" in result["limitations"]
    assert ORIGINAL in harness.requests[0].content
    assert b'filename="upload.png"' in harness.requests[0].content
    assert harness.files.reads == [("attachment", ATTACHMENT.id, 1024)]
    assert not harness.http.requests and not harness.discord.calls


@pytest.mark.asyncio
async def test_saved_workspace_file_and_personal_chat(harness: Harness) -> None:
    result = await harness.check(
        {"path": "saved.png"},
        caller=replace(CALLER, guild_id=None, channel_id=None, trigger_discord_message_id=None),
    )
    assert "results" in result
    assert harness.files.reads == [("workspace", "saved.png", 1024)]
    assert not harness.discord.calls


@pytest.mark.asyncio
async def test_only_host_admitted_reply_image_is_read(harness: Harness) -> None:
    entry = ToolAttachment("reply:0", "reply-image-1.png", len(ORIGINAL), "image/png", "reply")
    harness.files = FakeToolFiles(
        (entry,), attachment_files={entry.id: ToolFile(entry.filename, entry.media_type, ORIGINAL)}
    )
    assert "results" in await harness.check({"source": "reply"})
    assert (await harness.check())["error"]["code"] == "missing_attachment"
    assert not harness.discord.calls


@pytest.mark.asyncio
async def test_missing_file_port_fails_without_network(harness: Harness) -> None:
    result = json.loads(await harness.module.check({}, CALLER))
    assert result["error"]["code"] == "unavailable"
    assert not harness.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"source": "url"},
        {"filename": 3},
        {"filename": ""},
        {"url": "https://example.com"},
        {"message_id": 999},
        {"path": 3},
        {"path": "saved.png", "source": "current"},
        {"path": "saved.png", "filename": "original.png"},
    ],
)
async def test_ambiguous_or_invalid_arguments(harness: Harness, args: dict[str, Any]) -> None:
    assert (await harness.check(args))["error"]["code"] == "invalid_arguments"
    assert not harness.files.reads and not harness.requests


@pytest.mark.asyncio
async def test_unavailable_attachment_never_refetched(harness: Harness) -> None:
    harness.files = FakeToolFiles((replace(ATTACHMENT, unavailable_reason="Blocked by host"),))
    assert (await harness.check())["error"]["code"] == "unavailable_attachment"
    assert not harness.requests and not harness.files.reads and not harness.discord.calls


@pytest.mark.asyncio
async def test_multiple_files_offer_names_for_selection(harness: Harness) -> None:
    second = replace(ATTACHMENT, id="current:1", filename="second.png")
    file = ToolFile("second.png", "image/png", ORIGINAL)
    harness.files = FakeToolFiles((ATTACHMENT, second), attachment_files={second.id: file})
    result = await harness.check()
    assert result["error"]["code"] == "ambiguous_attachment"
    assert "second.png" in result["error"]["message"]
    assert not harness.requests
    assert "results" in await harness.check({"filename": "second.png"})


@pytest.mark.asyncio
async def test_missing_path_and_host_byte_limit_fail_before_upload(harness: Harness) -> None:
    assert (await harness.check({"path": "other/private.png"}))["error"]["code"] == "unavailable"
    harness.files = FakeToolFiles(
        workspace_files={"big.png": ToolFile("big.png", "image/png", b"x" * 1025)}
    )
    assert (await harness.check({"path": "big.png"}))["error"]["code"] == "too_large"
    assert not harness.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,data,code",
    [("file.txt", b"text", "unsupported_format"), ("file.png", b"", "file_size")],
)
async def test_empty_or_unsupported_file(
    harness: Harness, filename: str, data: bytes, code: str
) -> None:
    harness.files = FakeToolFiles(workspace_files={filename: ToolFile(filename, None, data)})
    assert (await harness.check({"path": filename}))["error"]["code"] == code
    assert not harness.requests


@pytest.mark.asyncio
async def test_audio_media_type_and_results(harness: Harness) -> None:
    harness.files = FakeToolFiles(
        workspace_files={"clip.opus": ToolFile("clip.opus", "audio/ogg", b"audio")}
    )
    harness.response = result_payload(audio=True)
    assert len((await harness.check({"path": "clip.opus"}))["results"]) == 1
    assert b"Content-Type: audio/ogg" in harness.requests[0].content


@pytest.mark.asyncio
async def test_health_recovers_after_success(harness: Harness) -> None:
    harness.status = 401
    assert (await harness.check())["error"]["code"] == "authentication_failed"
    assert harness.health.state == "degraded"
    harness.status = 200
    harness.response = result_payload(detected=True)
    result = await harness.check()
    assert result["results"][0]["outcome"] == "detected"
    assert result["results"][1]["outcome"] == "not_detected"
    assert "openai" not in harness.health.keyed


@pytest.mark.asyncio
async def test_busy_close_and_missing_key(harness: Harness) -> None:
    async with harness.module._busy:
        assert (await harness.check())["error"]["code"] == "busy"
    await harness.module.close()
    await harness.module.close()
    assert (await harness.check())["error"]["code"] == "unavailable"
    module = ProvenanceModule(ProvenanceSettings(openai_api_key=SecretStr(" ")))
    with pytest.raises(ValueError, match="CONTENT_PROVENANCE_OPENAI_API_KEY"):
        await module.start(harness.ctx)
    await module.close()


@pytest.mark.asyncio
async def test_cancelled_file_read_releases_busy_slot(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cancel(attachment_id: str, *, max_bytes: int) -> ToolFile:
        raise asyncio.CancelledError

    monkeypatch.setattr(harness.files, "read_attachment", cancel)
    with pytest.raises(asyncio.CancelledError):
        await harness.check()
    assert not harness.module._busy.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args, expected_read",
    [
        ({"path": "saved.png", "source": None, "filename": None}, ("workspace", "saved.png", 1024)),
        (
            {"path": None, "source": "current", "filename": "original.png"},
            ("attachment", ATTACHMENT.id, 1024),
        ),
        ({"path": None, "source": None, "filename": None}, ("attachment", ATTACHMENT.id, 1024)),
    ],
)
async def test_nullable_selectors(
    harness: Harness, args: dict[str, Any], expected_read: tuple[str, str, int]
) -> None:
    assert "results" in await harness.check(args)
    assert harness.files.reads == [expected_read]
    assert len(harness.requests) == 1


def test_selector_schema_allows_null() -> None:
    load, recorded = load_context(ProvenanceSettings())
    create(load)
    schema = recorded.registry.tools[TOOL_NAME].parameters
    for name in ("path", "source", "filename"):
        assert "null" in schema["properties"][name]["type"]
    assert None in schema["properties"]["source"]["enum"]
