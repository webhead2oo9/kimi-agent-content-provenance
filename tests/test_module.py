from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from conftest import ATTACHMENT, CALLER, ORIGINAL, REF, URL, Harness, message, result_payload
from kimi_agent_module_api import TrustTier
from kimi_agent_module_api.contracts import (
    HttpResponse,
    MessageRef,
    validate_module_name,
    validate_permissions,
    validate_services,
)
from kimi_agent_module_api.testing import load_context
from pydantic import SecretStr, ValidationError

from kimi_agent_content_provenance import SPEC, TOOL_NAME, create
from kimi_agent_content_provenance.module import ProvenanceModule
from kimi_agent_content_provenance.settings import ProvenanceSettings


def test_spec_and_tool_contract() -> None:
    validate_module_name(SPEC.name)
    validate_permissions(SPEC.name, SPEC.permissions)
    validate_services(SPEC.name, SPEC.dependencies, SPEC.provides, SPEC.consumes)
    assert SPEC.api_version == 2
    load, recorded = load_context(ProvenanceSettings())
    module = create(load)
    assert not module.scoped_migrations
    tool = recorded.registry.tools[TOOL_NAME]
    assert tool.searchable and tool.guild_only and tool.untrusted
    assert tool.min_tier == TrustTier.MEMBER
    assert recorded.labels[TOOL_NAME] == "Checking content provenance"
    assert SPEC.settings is not None
    assert {x.field for x in SPEC.settings.exposed} | SPEC.settings.environment_only == set(
        ProvenanceSettings.model_fields
    )
    assert "openai_api_key" in SPEC.settings.environment_only
    assert not SPEC.permissions.event_topics
    assert not SPEC.permissions.raw_bot and not SPEC.permissions.raw_storage


def test_settings_isolate_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-chat-key")
    monkeypatch.delenv("CONTENT_PROVENANCE_OPENAI_API_KEY", raising=False)
    assert not ProvenanceSettings().openai_api_key.get_secret_value()
    monkeypatch.setenv("CONTENT_PROVENANCE_OPENAI_API_KEY", "module-key")
    settings = ProvenanceSettings()
    assert settings.openai_api_key.get_secret_value() == "module-key"
    assert "module-key" not in repr(settings)


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
    request = harness.requests[0]
    assert ORIGINAL in request.content
    assert b'filename="upload.png"' in request.content
    assert b"Content-Type: image/png" in request.content
    assert request.headers["authorization"] == "Bearer test-key"
    assert str(request.url) == "https://api.openai.com/v1/content_provenance_checks"
    assert harness.http.requests == [("GET", URL, {})]


@pytest.mark.asyncio
async def test_each_signal_kept_independently(harness: Harness) -> None:
    harness.response = result_payload(detected=True)
    result = await harness.check()
    assert result["results"][0]["outcome"] == "detected"
    assert result["results"][1]["outcome"] == "not_detected"
    assert result["summary"] == "Supported OpenAI provenance signal detected."


@pytest.mark.asyncio
async def test_reply_target_is_fetched_from_verified_trigger(harness: Harness) -> None:
    harness.discord.messages[REF] = message(attachments=(), reply=31)
    reply_ref = MessageRef(10, 20, 31)
    harness.discord.messages[reply_ref] = message(reply_ref, author=2)
    result = await harness.check({"source": "reply"})
    assert "results" in result
    assert len(harness.discord.calls_for("fetch_message")) == 2


@pytest.mark.asyncio
async def test_thread_snapshot_parent_metadata(harness: Harness) -> None:
    harness.discord.messages[REF] = message(replace(REF, parent_channel_id=99))
    result = await harness.check(caller=replace(CALLER, channel_id=99, thread_id=20))
    assert "results" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller",
    [
        replace(CALLER, guild_id=None),
        replace(CALLER, guild_id=999),
        replace(CALLER, channel_id=None),
        replace(CALLER, trigger_discord_message_id=None),
    ],
)
async def test_unsupported_context_never_downloads(harness: Harness, caller: Any) -> None:
    assert (await harness.check(caller=caller))["error"]["code"] == "unsupported_context"
    assert not harness.http.requests and not harness.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [{"source": "url"}, {"filename": 3}, {"filename": ""}, {"url": URL}, {"message_id": 999}],
)
async def test_untrusted_arguments_cannot_widen_source(
    harness: Harness, args: dict[str, Any]
) -> None:
    assert (await harness.check(args))["error"]["code"] == "invalid_arguments"
    assert not harness.discord.calls and not harness.requests


@pytest.mark.asyncio
async def test_access_revoked(harness: Harness) -> None:
    harness.discord.channel_access[(10, 1, 20)] = False
    assert (await harness.check())["error"]["code"] == "source_access_denied"
    assert not harness.discord.calls_for("fetch_message") and not harness.requests
    assert harness.health.state == "healthy"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [None, message(author=2), message(MessageRef(99, 20, 30)), message(MessageRef(10, 99, 30))],
)
async def test_trigger_must_match_caller_and_scope(harness: Harness, replacement: Any) -> None:
    if replacement is None:
        harness.discord.messages.clear()
    else:
        harness.discord.messages[REF] = replacement
    assert (await harness.check())["error"]["code"] == "invalid_source"
    assert not harness.http.requests and not harness.requests


@pytest.mark.asyncio
async def test_multiple_files_need_unique_selection(harness: Harness) -> None:
    second = replace(ATTACHMENT, filename="second.png")
    harness.discord.messages[REF] = message(attachments=(ATTACHMENT, second))
    assert (await harness.check())["error"]["code"] == "ambiguous_attachment"
    assert not harness.http.requests
    assert "results" in await harness.check({"filename": "second.png"})
    harness.discord.messages[REF] = message(attachments=(ATTACHMENT, ATTACHMENT))
    assert (await harness.check({"filename": "original.png"}))["error"][
        "code"
    ] == "ambiguous_attachment"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/file.png",
        "https://api.openai.com/file.png",
        "http://cdn.discordapp.com/attachments/20/40/a.png",
        "https://user@cdn.discordapp.com/attachments/20/40/a.png",
        "https://cdn.discordapp.com/attachments/99/40/a.png",
        "https://cdn.discordapp.com:443/attachments/20/40/a.png",
        "https://media.discordapp.net/attachments/20/40/a.png",
        "https://cdn.discordapp.com:bad/a.png",
    ],
)
async def test_download_source_is_fixed(harness: Harness, url: str) -> None:
    harness.discord.messages[REF] = message(attachments=(replace(ATTACHMENT, url=url),))
    assert (await harness.check())["error"]["code"] == "invalid_source"
    assert not harness.http.requests and not harness.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, -1, 1025])
async def test_size_rejected_before_download(harness: Harness, size: int) -> None:
    harness.discord.messages[REF] = message(attachments=(replace(ATTACHMENT, size=size),))
    assert (await harness.check())["error"]["code"] == "file_size"
    assert not harness.http.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(404, {}, b""),
        HttpResponse(200, {}, b"truncated"),
        HttpResponse(200, {}, ORIGINAL + b"extra"),
    ],
)
async def test_failed_or_changed_download_is_never_uploaded(
    harness: Harness, response: HttpResponse
) -> None:
    harness.http.routes[URL] = response
    assert (await harness.check())["error"]["code"] == "download_failed"
    assert not harness.requests


@pytest.mark.asyncio
async def test_opus_media_type_and_audio_results(harness: Harness) -> None:
    harness.discord.messages[REF] = message(
        attachments=(replace(ATTACHMENT, filename="clip.opus", content_type="audio/ogg"),)
    )
    harness.response = result_payload(audio=True)
    assert len((await harness.check())["results"]) == 1
    assert b"Content-Type: audio/ogg" in harness.requests[0].content


@pytest.mark.asyncio
async def test_provider_failure_updates_health_and_success_recovers(harness: Harness) -> None:
    harness.status = 401
    result = await harness.check()
    assert result["error"]["code"] == "authentication_failed" and "results" not in result
    assert harness.health.state == "degraded"
    harness.status = 200
    await harness.check()
    assert "openai" not in harness.health.keyed


@pytest.mark.asyncio
async def test_busy_does_not_queue_more_media(harness: Harness) -> None:
    async with harness.module._busy:
        assert (await harness.check())["error"]["code"] == "busy"
    assert not harness.http.requests


@pytest.mark.asyncio
async def test_cancellation_propagates_and_releases_slot(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cancelled(*args: Any, **kwargs: Any) -> HttpResponse:
        raise asyncio.CancelledError

    monkeypatch.setattr(harness.http, "get", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await harness.check()
    assert not harness.module._busy.locked()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_disables_tool(harness: Harness) -> None:
    await harness.module.close()
    await harness.module.close()
    assert (await harness.check())["error"]["code"] == "unavailable"


@pytest.mark.asyncio
async def test_missing_key_fails_start(harness: Harness) -> None:
    module = ProvenanceModule(ProvenanceSettings(openai_api_key=SecretStr(" ")))
    with pytest.raises(ValueError, match="CONTENT_PROVENANCE_OPENAI_API_KEY"):
        await module.start(harness.ctx)
    await module.close()
