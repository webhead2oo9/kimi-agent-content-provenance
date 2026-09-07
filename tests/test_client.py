from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from conftest import ORIGINAL, result_payload

from kimi_agent_content_provenance.client import (
    MAX_RESPONSE_BYTES,
    ProvenanceClient,
    ProvenanceError,
    parse_result,
)


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "invalid_file"),
        (401, "authentication_failed"),
        (403, "access_denied"),
        (404, "unavailable"),
        (413, "file_too_large"),
        (429, "rate_limited"),
        (500, "upstream_error"),
        (302, "upstream_error"),
    ],
)
@pytest.mark.asyncio
async def test_errors_are_redacted_and_never_retried(status: int, code: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, headers={"location": "https://evil.example"}, text="secret-key signed-url"
        )

    client = ProvenanceClient("secret-key", 60, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ProvenanceError) as raised:
            await client.check(ORIGINAL, "image/png", "upload.png")
        assert raised.value.code == code
        assert "secret-key" not in str(raised.value) and "signed-url" not in str(raised.value)
        assert len(requests) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rate_limit_cooldown() -> None:
    now = [100.0]
    calls = [0]

    def respond(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(429, headers={"retry-after": "120"})

    client = ProvenanceClient(
        "test", 60, transport=httpx.MockTransport(respond), clock=lambda: now[0]
    )
    try:
        for _ in range(2):
            with pytest.raises(ProvenanceError, match="rate limiting"):
                await client.check(ORIGINAL, "image/png", "upload.png")
        assert calls[0] == 1
        now[0] += 121
        with pytest.raises(ProvenanceError):
            await client.check(ORIGINAL, "image/png", "upload.png")
        assert calls[0] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not JSON", b"x" * (MAX_RESPONSE_BYTES + 1), b"[]", b"{}"])
async def test_invalid_response_is_not_negative_evidence(body: bytes) -> None:
    client = ProvenanceClient(
        "test", 60, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    )
    try:
        with pytest.raises(ProvenanceError) as raised:
            await client.check(ORIGINAL, "image/png", "upload.png")
        assert raised.value.code == "invalid_response"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_network_failure_is_redacted() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret URL and credentials", request=request)

    client = ProvenanceClient("test", 60, transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(ProvenanceError) as raised:
            await client.check(ORIGINAL, "image/png", "upload.png")
        assert raised.value.code == "network_error"
        assert "secret" not in str(raised.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_cancellation_propagates() -> None:
    async def cancel(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    client = ProvenanceClient("test", 60, transport=httpx.MockTransport(cancel))
    try:
        with pytest.raises(asyncio.CancelledError):
            await client.check(ORIGINAL, "image/png", "upload.png")
    finally:
        await client.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("results", []),
        ("results", [None]),
        ("created_at", True),
        ("created_at", -1),
        ("object", "other"),
    ],
)
def test_malformed_response_fields(field: str, value: Any) -> None:
    payload = result_payload()
    payload[field] = value
    with pytest.raises(ProvenanceError):
        parse_result(payload, is_image=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("outcome", "maybe"),
        ("validation_state", "broken"),
        ("type", "unknown"),
        ("model", {}),
        ("issuer", "x" * 1025),
    ],
)
def test_malformed_signal_fields(field: str, value: Any) -> None:
    payload = result_payload()
    payload["results"][0][field] = value
    with pytest.raises(ProvenanceError):
        parse_result(payload, is_image=True)


def test_duplicate_and_missing_signals_fail_explicitly() -> None:
    payload = result_payload()
    payload["results"][1] = payload["results"][0]
    with pytest.raises(ProvenanceError):
        parse_result(payload, is_image=True)
    with pytest.raises(ProvenanceError):
        parse_result(result_payload(audio=True), is_image=True)


def test_invalid_manifest_cannot_be_detected() -> None:
    payload = result_payload(detected=True)
    payload["results"][0]["validation_state"] = "invalid"
    with pytest.raises(ProvenanceError):
        parse_result(payload, is_image=True)


def test_third_party_manifest_preserves_issuer_without_attribution() -> None:
    payload = result_payload()
    payload["results"][0].update(issuer="Other issuer", validation_state="valid")
    result = parse_result(payload, is_image=True).as_dict()
    assert result["summary"] == "No supported OpenAI signals detected."
    assert result["results"][0]["issuer"] == "Other issuer"
