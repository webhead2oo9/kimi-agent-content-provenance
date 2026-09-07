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
        "file bytes to OpenAI. Supports admitted current/reply attachments or a saved workspace path. "
        "Audio must be at most 60 seconds. A negative check does not prove human authorship or "
        "authenticity, and this does not detect every AI provider. Never use for automatic scanning.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": ["string", "null"],
                    "description": "Saved caller-workspace path, including uploads or generated images from earlier turns. Set source and filename to null when using path.",
                    "minLength": 1,
                    "maxLength": 4096,
                },
                "source": {
                    "type": ["string", "null"],
                    "enum": ["current", "reply", None],
                    "description": "current (default): admitted uploads; reply: admitted reply images. Set to null when using path.",
                },
                "filename": {
                    "type": ["string", "null"],
                    "description": "Exact attachment filename. Required when several supported files are attached. Set to null when using path or selecting a single attachment.",
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
        guild_only=False,
        untrusted=True,
    )
    ctx.register_tool_labels({TOOL_NAME: "Checking content provenance"})
    ctx.declare_surface_tools("eval_stub", (TOOL_NAME,))
    return module


SPEC = ModuleSpec(
    name="content_provenance",
    version="0.2.1",
    api_version=2,
    create=create,
    settings=SETTINGS,
    requires_capabilities=("tools.files.v1",),
    permissions=ModulePermissions(
        tool_files=True,
        http_hosts=(HttpHostRule("api.openai.com"),),
    ),
)

__all__ = ["SPEC"]
