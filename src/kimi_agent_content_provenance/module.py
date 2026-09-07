"""On-demand provenance checks over files admitted by the Kimi host."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any

from kimi_agent_module_api import FileAccessError, ModuleRuntimeContext, ModuleToolContext, ToolFile
from kimi_agent_module_api.contracts import ScopedModuleMigration

from kimi_agent_content_provenance.client import ProvenanceClient, ProvenanceError
from kimi_agent_content_provenance.settings import ProvenanceSettings

log = logging.getLogger(__name__)
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".opus": "audio/ogg",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".pcm": "audio/pcm",
}


def media_type_for(file: ToolFile) -> tuple[str, str]:
    extension = PurePosixPath(file.filename).suffix.lower()
    media_type = MEDIA_TYPES.get(extension)
    if media_type is None:
        raise ProvenanceError(
            "unsupported_format",
            "Supported formats: PNG, JPEG, WebP, MP3, Opus, AAC, FLAC, WAV, PCM.",
        )
    return media_type, extension


class ProvenanceModule:
    scoped_migrations: Sequence[ScopedModuleMigration] = ()

    def __init__(self, settings: ProvenanceSettings) -> None:
        self._settings = settings
        self._ctx: ModuleRuntimeContext | None = None
        self._client: ProvenanceClient | None = None
        self._busy = asyncio.Lock()

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        key = self._settings.openai_api_key.get_secret_value().strip()
        if not key:
            raise ValueError("CONTENT_PROVENANCE_OPENAI_API_KEY is required.")
        self._client = ProvenanceClient(key, self._settings.timeout_seconds)
        self._ctx = ctx
        ctx.health.report(
            "healthy", "Ready for on-demand checks; API access is checked on first use."
        )

    async def close(self) -> None:
        self._ctx = None
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def check(self, arguments: dict[str, Any], caller: ModuleToolContext) -> str:
        ctx, client = self._ctx, self._client
        if ctx is None or client is None:
            return self._error("unavailable", "The content provenance module is not running.")
        if caller.files is None:
            return self._error(
                "unavailable", "The host did not provide file access for this invocation."
            )
        source = arguments.get("source", "current")
        filename = arguments.get("filename")
        path = arguments.get("path")
        if (
            set(arguments) - {"source", "filename", "path"}
            or source not in ("current", "reply")
            or (
                filename is not None
                and (not isinstance(filename, str) or not filename or len(filename) > 1024)
            )
            or (path is not None and (not isinstance(path, str) or not path or len(path) > 4096))
            or (path is not None and ("source" in arguments or "filename" in arguments))
        ):
            return self._error(
                "invalid_arguments",
                "Select an attachment with source/filename, or supply one saved workspace path.",
            )
        if self._busy.locked():
            return self._error("busy", "Another provenance check is running. Try again shortly.")
        async with self._busy:
            try:
                async with asyncio.timeout(self._settings.timeout_seconds):
                    if path is not None:
                        file = await caller.files.read_workspace(
                            path, max_bytes=self._settings.max_file_bytes
                        )
                    else:
                        matches = [
                            item
                            for item in caller.files.attachments
                            if item.source == source
                            and (
                                item.filename == filename
                                if filename is not None
                                else PurePosixPath(item.filename).suffix.lower() in MEDIA_TYPES
                            )
                        ]
                        if not matches:
                            raise ProvenanceError(
                                "missing_attachment",
                                "No supported attachment was admitted from that source. Attach a file to your request, or provide its saved workspace path.",
                            )
                        if len(matches) != 1:
                            names = ", ".join(item.filename for item in matches)
                            raise ProvenanceError(
                                "ambiguous_attachment",
                                "Select one attachment by its exact, unique filename, or use its saved path. Available: "
                                + names[:2048],
                            )
                        entry = matches[0]
                        if entry.unavailable_reason:
                            raise ProvenanceError(
                                "unavailable_attachment",
                                "The host did not admit or save this attachment. No check was made.",
                            )
                        file = await caller.files.read_attachment(
                            entry.id, max_bytes=self._settings.max_file_bytes
                        )
                    media_type, extension = media_type_for(file)
                    if not file.data or len(file.data) > self._settings.max_file_bytes:
                        raise ProvenanceError(
                            "file_size", "The file is empty or exceeds the upload limit."
                        )
                    result = await client.check(file.data, media_type, "upload" + extension)
                    ctx.health.report("healthy", key="openai")
                    return json.dumps({"filename": file.filename, **result.as_dict()})
            except FileAccessError as exc:
                return self._error(exc.code, str(exc))
            except ProvenanceError as exc:
                if exc.code in {
                    "authentication_failed",
                    "access_denied",
                    "unavailable",
                    "rate_limited",
                    "upstream_error",
                    "network_error",
                    "invalid_response",
                }:
                    ctx.health.report("degraded", str(exc), key="openai")
                return self._error(exc.code, str(exc))
            except TimeoutError:
                return self._error(
                    "timeout", "The provenance check timed out. No result is available."
                )
            except Exception:
                log.warning("Content provenance check failed while accessing its source")
                return self._error(
                    "source_error", "The source file could not be checked. No result is available."
                )

    @staticmethod
    def _error(code: str, message: str) -> str:
        return json.dumps({"error": {"code": code, "message": message}})
