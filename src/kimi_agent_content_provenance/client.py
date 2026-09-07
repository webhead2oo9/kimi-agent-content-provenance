"""Bounded multipart requests to the fixed OpenAI provenance endpoint."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

ENDPOINT = "https://api.openai.com/v1/content_provenance_checks"
MAX_RESPONSE_BYTES = 64 * 1024
LIMITATION = (
    "This checks supported OpenAI provenance signals only. No supported signals detected does "
    "not establish that a file is human-made, authentic, or free of AI generation or modification. "
    "Signals can be removed or degraded; other providers and legacy content may not be detected."
)


class ProvenanceError(Exception):
    """An error whose message is safe to return to the caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CheckResult:
    created_at: int
    results: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        detected = any(result["outcome"] == "detected" for result in self.results)
        return {
            "object": "content_provenance_check",
            "created_at": self.created_at,
            "results": list(self.results),
            "summary": (
                "Supported OpenAI provenance signal detected."
                if detected
                else "No supported OpenAI signals detected."
            ),
            "limitations": LIMITATION,
        }


def parse_result(payload: object, *, is_image: bool) -> CheckResult:
    """Reject incomplete responses instead of turning them into negative evidence."""
    invalid = ProvenanceError("invalid_response", "OpenAI returned an invalid provenance response.")
    if not isinstance(payload, dict) or payload.get("object") != "content_provenance_check":
        raise invalid
    created_at = payload.get("created_at")
    entries = payload.get("results")
    if type(created_at) is not int or created_at < 0 or not isinstance(entries, list):
        raise invalid
    expected = {"c2pa", "synthid"} if is_image else {"synthid"}
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise invalid
        kind = entry.get("type")
        outcome = entry.get("outcome")
        if not isinstance(kind, str) or kind not in expected or kind in seen:
            raise invalid
        if outcome not in ("detected", "not_detected"):
            raise invalid
        seen.add(kind)
        result: dict[str, Any] = {"type": kind, "outcome": outcome}
        fields = (
            ("model", "generated_at", "issuer") if kind == "c2pa" else ("model", "generated_at")
        )
        for name in fields:
            value = entry.get(name)
            if value is not None and (not isinstance(value, str) or len(value) > 1024):
                raise invalid
            result[name] = value
        if kind == "c2pa":
            state = entry.get("validation_state")
            if state not in ("trusted", "valid", "invalid", "not_present"):
                raise invalid
            if outcome == "detected" and state not in ("trusted", "valid"):
                raise invalid
            result["validation_state"] = state
        results.append(result)
    if seen != expected:
        raise invalid
    return CheckResult(created_at, tuple(results))


class ProvenanceClient:
    def __init__(
        self,
        api_key: str,
        timeout_seconds: float,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout = timeout_seconds
        self._clock = clock
        self._retry_at = 0.0
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def check(self, payload: bytes, media_type: str, filename: str) -> CheckResult:
        if self._clock() < self._retry_at:
            raise ProvenanceError(
                "rate_limited", "OpenAI is rate limiting checks. Try again later."
            )
        try:
            async with asyncio.timeout(self._timeout):
                async with self._http.stream(
                    "POST", ENDPOINT, files={"file": (filename, payload, media_type)}
                ) as response:
                    if response.status_code == 429:
                        delay = response.headers.get("retry-after", "60")
                        seconds = (
                            int(delay)
                            if delay.isascii() and delay.isdigit() and len(delay) < 8
                            else 60
                        )
                        self._retry_at = self._clock() + max(1, min(seconds, 86400))
                        raise ProvenanceError(
                            "rate_limited", "OpenAI is rate limiting checks. Try again later."
                        )
                    if response.status_code != 200:
                        code, message = {
                            400: (
                                "invalid_file",
                                "OpenAI rejected this file. Check its format and the 60-second audio limit.",
                            ),
                            401: (
                                "authentication_failed",
                                "The module's OpenAI API key was rejected.",
                            ),
                            403: (
                                "access_denied",
                                "This OpenAI project cannot run provenance checks.",
                            ),
                            404: (
                                "unavailable",
                                "Provenance checks are unavailable to this OpenAI organization.",
                            ),
                            413: ("file_too_large", "OpenAI rejected the file as too large."),
                        }.get(
                            response.status_code,
                            ("upstream_error", "OpenAI could not complete the provenance check."),
                        )
                        raise ProvenanceError(code, message)
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ProvenanceError(
                                "invalid_response", "OpenAI returned an oversized response."
                            )
                        body.extend(chunk)
        except httpx.HTTPError, TimeoutError:
            raise ProvenanceError(
                "network_error", "The OpenAI provenance request failed or timed out."
            ) from None
        try:
            decoded = json.loads(body)
        except ValueError, UnicodeError:
            raise ProvenanceError(
                "invalid_response", "OpenAI returned an invalid provenance response."
            ) from None
        return parse_result(decoded, is_image=media_type.startswith("image/"))
