"""One on-demand check of an attachment from a verified Discord message."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from kimi_agent_module_api import ModuleRuntimeContext, ModuleToolContext
from kimi_agent_module_api.contracts import AttachmentSnapshot, MessageRef, ScopedModuleMigration

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


def same_message(actual: MessageRef, expected: MessageRef) -> bool:
    # A fetched thread message can additionally carry its parent channel ID.
    return (actual.guild_id, actual.channel_id, actual.message_id) == (
        expected.guild_id,
        expected.channel_id,
        expected.message_id,
    )


def select_attachment(
    attachments: tuple[AttachmentSnapshot, ...], filename: str | None
) -> tuple[AttachmentSnapshot, str, str]:
    if filename is not None:
        matches = [item for item in attachments if item.filename == filename]
    else:
        matches = [
            item
            for item in attachments
            if PurePosixPath(item.filename).suffix.lower() in MEDIA_TYPES
        ]
    if not matches:
        raise ProvenanceError(
            "missing_attachment", "Attach a supported image or audio file to the selected message."
        )
    if len(matches) != 1:
        raise ProvenanceError(
            "ambiguous_attachment",
            "Select one attachment by its exact filename; filenames must be unique.",
        )
    attachment = matches[0]
    extension = PurePosixPath(attachment.filename).suffix.lower()
    media_type = MEDIA_TYPES.get(extension)
    if media_type is None:
        raise ProvenanceError(
            "unsupported_format",
            "Supported formats: PNG, JPEG, WebP, MP3, Opus, AAC, FLAC, WAV, PCM.",
        )
    return attachment, media_type, extension


def validate_download(attachment: AttachmentSnapshot, channel_id: int, max_bytes: int) -> None:
    if attachment.size <= 0 or attachment.size > max_bytes:
        raise ProvenanceError("file_size", f"The attachment must contain 1 to {max_bytes} bytes.")
    try:
        parsed = urlsplit(attachment.url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "cdn.discordapp.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and parsed.path.startswith(f"/attachments/{channel_id}/{attachment.attachment_id}/")
        )
    except ValueError:
        valid = False
    if not valid:
        raise ProvenanceError(
            "invalid_source", "The attachment has no supported original Discord download."
        )


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
        if (
            caller.guild_id is None
            or caller.channel_id is None
            or caller.trigger_discord_message_id is None
            or not ctx.is_guild_active(caller.guild_id)
        ):
            return self._error(
                "unsupported_context", "Use this tool from a message in an active server."
            )
        source = arguments.get("source", "current")
        filename = arguments.get("filename")
        if (
            set(arguments) - {"source", "filename"}
            or source not in ("current", "reply")
            or (
                filename is not None
                and (not isinstance(filename, str) or not filename or len(filename) > 1024)
            )
        ):
            return self._error(
                "invalid_arguments", "Use source=current or reply and an optional exact filename."
            )
        if self._busy.locked():
            return self._error("busy", "Another provenance check is running. Try again shortly.")
        async with self._busy:
            try:
                async with asyncio.timeout(self._settings.timeout_seconds):
                    channel_id = caller.thread_id or caller.channel_id
                    if not await ctx.discord.can_view_channel(
                        caller.guild_id, caller.user_id, channel_id
                    ):
                        raise ProvenanceError(
                            "source_access_denied", "You cannot read the source channel."
                        )
                    trigger_ref = MessageRef(
                        caller.guild_id, channel_id, caller.trigger_discord_message_id
                    )
                    trigger = await ctx.discord.fetch_message(trigger_ref)
                    if (
                        trigger is None
                        or not same_message(trigger.ref, trigger_ref)
                        or trigger.author_id != caller.user_id
                    ):
                        raise ProvenanceError(
                            "invalid_source",
                            "The message that requested this check could not be verified.",
                        )
                    message = trigger
                    if source == "reply":
                        if trigger.reply_to_message_id is None:
                            raise ProvenanceError(
                                "missing_reply",
                                "The requesting message is not replying to another message.",
                            )
                        ref = MessageRef(caller.guild_id, channel_id, trigger.reply_to_message_id)
                        reply = await ctx.discord.fetch_message(ref)
                        if reply is None or not same_message(reply.ref, ref):
                            raise ProvenanceError(
                                "missing_reply", "The replied-to message is unavailable."
                            )
                        message = reply
                    attachment, media_type, extension = select_attachment(
                        message.attachments, filename
                    )
                    validate_download(attachment, channel_id, self._settings.max_file_bytes)
                    response = await ctx.http.get(
                        attachment.url,
                        max_bytes=attachment.size,
                        timeout_seconds=self._settings.timeout_seconds,
                    )
                    if response.status != 200 or len(response.body) != attachment.size:
                        raise ProvenanceError(
                            "download_failed",
                            "The original attachment could not be downloaded completely.",
                        )
                    # The host port owns downloads; its v2 API has no multipart POST.
                    # Our client owns only the fixed, declared OpenAI endpoint.
                    result = await client.check(response.body, media_type, "upload" + extension)
                    ctx.health.report("healthy", key="openai")
                    return json.dumps({"filename": attachment.filename, **result.as_dict()})
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
                # Host exceptions can contain signed URLs or other private context.
                log.warning("Content provenance check failed while accessing its source")
                return self._error(
                    "source_error",
                    "The source attachment could not be checked. No result is available.",
                )

    @staticmethod
    def _error(code: str, message: str) -> str:
        return json.dumps({"error": {"code": code, "message": message}})
