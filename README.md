# Kimi content provenance

An optional Kimi application module exposing the searchable member tool
`check_content_provenance`. Ask the bot to check an image or audio attachment for
supported OpenAI provenance signals, or provide a saved workspace path. Image
results include C2PA and SynthID; audio results include SynthID. Model, issuer,
and generation time are returned where available.

**No supported signals detected is not proof of human authorship or authenticity.**
Signals may be stripped or degraded, and this API does not detect all providers
or legacy content. Each signal and its validation state are reported separately.

## Install and enable

Requires Python 3.14+, module API **2.2+**, a Kimi host advertising `tools.files.v1`,
and an OpenAI platform key with access to content provenance checks. ChatGPT OAuth
and the bot's chat-model routing are independent of this feature.

The API 2.2 integration is under review. Upgrade the host and install its matching
API package before enabling this module version. The development lock pins the
matching SDK source commit so CI can test before the package release.

This is a private repository. Authenticate Git using your normal credential
helper; never put a token in a URL.

```console
git clone https://github.com/webhead2oo9/kimi-agent-content-provenance.git
```

From the Kimi `bot/` directory, install into the environment running the bot:

```console
.venv/bin/python -m pip install --editable ./packages/kimi-agent-module-api
.venv/bin/python -m pip install --editable /path/to/kimi-agent-content-provenance
```

Append `content_provenance` to the existing `KIMI_MODULES` list in the selected
instance dotenv (`ENV_FILE`), and add the dedicated secret:

```dotenv
KIMI_MODULES=content_provenance
CONTENT_PROVENANCE_OPENAI_API_KEY=your-platform-api-key
```

To allow startup without this feature if it cannot start, also append its name
to `KIMI_OPTIONAL_MODULES`. Preserve other names in both lists. A missing key
raises at startup; API access is checked on first use, with no startup upload.
Restart with your instance's existing supervisor, check the module's startup
messages, then inspect `/modules status` and `/modules manifest`.

## Use an upload or saved image

Attach a PNG and ask “Check this image's provenance.” The model discovers the
tool through `browse_tools` and selects the same admitted upload surface used by
image generation. Server chat and personal chat are supported wherever the host
admits attachments and exposes the module tool.

```json
{"source": "current", "filename": "example.png"}
```

`source` defaults to `current`. Omit `filename` when exactly one supported file
is attached. Multiple files require an exact unique filename; ambiguous results
list the available names. Duplicate filenames can be selected by their distinct
saved workspace paths instead.

A saved upload or generated image can be checked in a later turn:

```json
{"path": "generated_images/image-example.png"}
```

Use `path` by itself, instead of `source`/`filename`. The host confines reads to
the actual caller's workspace and checks path, symlink, and byte boundaries.

`source: "reply"` selects **reply images admitted into the current turn**, using
host-generated names such as `reply-image-1.png`. Reply images excluded by budgets,
moderation, or history deduplication are unavailable. Reply audio is not on that
surface; attach it to the request or use its saved path. No Discord re-fetch or
fallback to older images occurs. Arbitrary URLs and message IDs are not accepted.

## Configuration and limits

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `CONTENT_PROVENANCE_OPENAI_API_KEY` | empty | Required platform key; environment-only. |
| `CONTENT_PROVENANCE_MAX_FILE_BYTES` | `10485760` | Upload limit; hard maximum 50 MiB. |
| `CONTENT_PROVENANCE_TIMEOUT_SECONDS` | `60` | Whole check deadline; 1–120 seconds. |

Safe overrides may be placed in `<CONFIG_DIR>/modules/content_provenance.md`:

```markdown
---
max_file_bytes: 10485760
timeout_seconds: 60
---
```

Restart after changing deployment settings. There is no module-specific per-guild
settings document; the host's normal activation and tool policy apply. Host file
limits can be lower than the module's configured upload limit.

Supported extensions: `.png`, `.jpg`, `.jpeg`, `.webp`, `.mp3`, `.opus`, `.aac`,
`.flac`, `.wav`, `.pcm`. OpenAI enforces media validity and the **60-second decoded
audio limit**. The module never transcodes or truncates files. Reads preserve file
bytes, including embedded provenance metadata. Workspace files may have been edited
since upload; checks describe the bytes currently stored at the selected path.

One check runs at a time per process; concurrent calls receive a busy error. There
are no automatic upload retries. Rate limits apply an in-memory cooldown (numeric
`Retry-After`, otherwise 60 seconds). Provider failures appear as degraded health,
cleared by a successful check. Invalid, incomplete, or failed responses are errors,
never negative provenance evidence.

## Data and permissions

The module owns **no database tables**, migrations, persistent cache, or files.
It subscribes to no events and registers no commands, jobs, or services. Eval tool
calls are stubbed. It declares `tool_files=True` and no Discord actions.

The host's invocation-scoped reader supplies admitted attachments and caller-owned
workspace files after the normal host admission/staging flow. It never downloads
an unavailable attachment again. The module buffers one bounded file for upload,
does not save it, and does not retain the file reader beyond the handler.

The sole declared external host is `api.openai.com`. The original bytes are sent
to `/v1/content_provenance_checks` using the module's key, with the filename replaced
by `upload` plus its extension. The module owns an async HTTPX client because the
host HTTP port has no multipart upload method. Its endpoint is fixed; redirects
and environment proxies are disabled, responses and timeouts are bounded, errors
are redacted, and the client closes on shutdown.

OpenAI receives any embedded metadata. Provenance checks are **not eligible for
Zero Data Retention**; consult [OpenAI's guide](https://developers.openai.com/api/docs/guides/content-provenance)
and [API data controls](https://developers.openai.com/api/docs/guides/your-data).
This module has no provider-side deletion operation. The host may retain staged
uploads in the caller's workspace, tool results in transcripts, and observability
records under its normal retention and `/privacy` controls. Disabling the module
stops future checks but does not delete those records or provider copies. Include
this upload behavior in the deployment's privacy notice before enabling it.

## Development

```console
uv sync --locked --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python -m pytest -q
uv --preview-features audit-command audit --locked
uv build --no-sources
uv run twine check dist/*
```

Tests use the public SDK's `FakeToolFiles` and an HTTPX mock transport. They do not
contact Discord or OpenAI or require credentials. Host path isolation and staging
are tested in Kimi; this module tests selection, error handling, and API uploads.
