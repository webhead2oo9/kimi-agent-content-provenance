"""Searchable content provenance checks through Kimi's public module API."""

from __future__ import annotations

from kimi_agent_module_api import ModuleLoadContext, ModulePermissions, ModuleSpec, TrustTier
from kimi_agent_module_api.contracts import HttpHostRule

from kimi_agent_content_provenance.module import ProvenanceModule
from kimi_agent_content_provenance.settings import SETTINGS, ProvenanceSettings

TOOL_NAME = "check_content_provenance"


def create(ctx: ModuleLoadContext) -> ProvenanceModule:
    module = ProvenanceModule(ctx.settings_for(ProvenanceSettings))
    ctx.registry.register(
        TOOL_NAME,
        "Check an image or audio attachment for supported OpenAI C2PA/SynthID provenance signals. "
        "Use when the user asks to check provenance or whether media is AI-generated. Uploads the "
        "original file to OpenAI. Supports the requesting message or its same-channel reply target. "
        "Audio must be at most 60 seconds. A negative check does not prove human authorship or "
        "authenticity, and this does not detect every AI provider. Never use for automatic scanning.",
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["current", "reply"],
                    "description": "current (default): requesting message; reply: message it replies to.",
                },
                "filename": {
                    "type": "string",
                    "description": "Exact attachment filename. Required when several supported files are attached.",
                    "minLength": 1,
                    "maxLength": 1024,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        module.check,
        min_tier=TrustTier.MEMBER,
        searchable=True,
        guild_only=True,
        untrusted=True,
    )
    ctx.register_tool_labels({TOOL_NAME: "Checking content provenance"})
    ctx.declare_surface_tools("eval_stub", (TOOL_NAME,))
    return module


SPEC = ModuleSpec(
    name="content_provenance",
    version="0.1.0",
    api_version=2,
    create=create,
    settings=SETTINGS,
    permissions=ModulePermissions(
        discord_actions=frozenset({"fetch_message", "can_view_channel"}),
        http_hosts=(HttpHostRule("cdn.discordapp.com"), HttpHostRule("api.openai.com")),
    ),
)

__all__ = ["SPEC"]
