# Kimi content provenance

An optional Kimi application module exposing the searchable member tool
`check_content_provenance`. Ask the bot to check an image or audio attachment for
supported OpenAI provenance signals. It returns C2PA and SynthID results for images,
and SynthID results for audio, including model, issuer, and generation time where
the API supplies them.

**No supported signals detected is not proof of human authorship or authenticity.**
OpenAI signals may be stripped or degraded, and this API does not detect all AI
providers or legacy content. An invalid C2PA manifest is not reliable evidence.
The tool preserves each signal's outcome and validation state independently.

## Install and enable

Requires Python 3.14+, Kimi's module API 2.1+, and an OpenAI platform API key with
access to content provenance checks. ChatGPT OAuth and the bot's chat-model routing
are independent of this feature. This is a private repository: authenticate Git
with your normal credential helper before cloning; never put a token in a URL.

```console
git clone https://github.com/webhead2oo9/kimi-agent-content-provenance.git
```

From the Kimi `bot/` directory, install into the environment that runs the bot:

```console
.venv/bin/python -m pip install --editable /path/to/kimi-agent-content-provenance
```

Append `content_provenance` to your existing `KIMI_MODULES` list in the selected
instance dotenv (`ENV_FILE`), and add the dedicated secret:

```dotenv
KIMI_MODULES=content_provenance
CONTENT_PROVENANCE_OPENAI_API_KEY=your-platform-api-key
```

To allow the bot to start if this feature cannot start, also append its name to
`KIMI_OPTIONAL_MODULES`. Both lists must retain your other enabled modules. A missing
key raises at startup; an optional module then appears disabled. API permissions
are checked on first use, without a startup upload. Restart your instance using
its existing supervisor, check the module's composed/migrated/started log entries,
then run `/modules status` and `/modules manifest`.

Attach a PNG to a server message and ask: “Check this image's provenance.” You can
also reply to a message with an attachment and ask to check that attachment. The
model discovers the tool through `browse_tools`. Example arguments:

```json
{"source": "reply", "filename": "example.png"}
```

`source` defaults to `current`. `filename` can be omitted when exactly one supported
attachment exists. Multiple matching files require an exact, unique filename.
The reply source is confined to the same channel as the requesting message.
The module verifies the requesting message's author and rechecks the caller's
channel access. It uses the original Discord CDN attachment, preserving its bytes;
it does not resize, re-encode, or remove metadata.

No arbitrary URLs, message IDs, workspace paths, embeds, DMs, or personal `/chat`
attachments are accepted. It does not scan messages in the background or subscribe
to events. It registers no slash commands, jobs, or services. Tool calls on eval
surfaces are stubbed.

## Configuration and limits

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `CONTENT_PROVENANCE_OPENAI_API_KEY` | empty | Required platform key; environment-only secret. |
| `CONTENT_PROVENANCE_MAX_FILE_BYTES` | `10485760` | Upload limit in bytes; hard maximum 50 MiB. |
| `CONTENT_PROVENANCE_TIMEOUT_SECONDS` | `60` | Whole check deadline, including download; 1–120 seconds. |

Safe deployment overrides can also be supplied in
`<CONFIG_DIR>/modules/content_provenance.md`:

```markdown
---
max_file_bytes: 10485760
timeout_seconds: 60
---
```

There is no module-specific per-guild settings document. The host's normal module
activation and tool policy apply. Restart after changing this module's deployment
settings so its client and limits are reconstructed.

Supported filename extensions: `.png`, `.jpg`, `.jpeg`, `.webp`, `.mp3`, `.opus`,
`.aac`, `.flac`, `.wav`, `.pcm`. Media validity and the **60-second decoded audio
limit** are enforced by OpenAI. The module does not decode or truncate audio.
Malformed files and failed requests return errors, never negative provenance results.

Only one check runs at a time per bot process; other calls receive a busy response.
There are no automatic upload retries. Rate limits return a clear error and apply
an in-memory cooldown (numeric `Retry-After`, otherwise 60 seconds). Authentication
and provider failures appear as degraded health, cleared by a successful check.
Unsupported or incomplete API response schemas fail explicitly.

## Data and external services

The module owns **no database tables**, migrations, persistent cache, or files.
Attachment bytes are buffered in memory for one bounded request and are not saved
by this module. It fetches only the requesting message and, when explicitly
selected, its reply target. It does not observe messages or events not addressed
to the bot except that selected reply target.

Declared external hosts:

- `cdn.discordapp.com`: original attachment download through Kimi's bounded HTTP port.
- `api.openai.com`: file upload to `/v1/content_provenance_checks` with the module's
  platform key. The original filename is replaced with `upload` plus its extension.

The current module HTTP port has no multipart upload method, so the module owns
a small async HTTPX client for the fixed OpenAI endpoint. It disables redirects
and environment proxies, bounds its response and timeout, redacts upstream errors,
and closes on shutdown. It imports no host internals and requests no raw host handles.

OpenAI receives the selected original file, including any embedded metadata.
Content provenance checks are **not eligible for Zero Data Retention**. Consult
[OpenAI's content provenance guide](https://developers.openai.com/api/docs/guides/content-provenance)
and [API data controls](https://developers.openai.com/api/docs/guides/your-data)
for provider retention. The module has no provider-side deletion operation.
Tool arguments/results and the final reply may be retained by Kimi's normal
conversation and observability systems; their existing retention and `/privacy`
deletion controls apply. Disabling the module stops future checks and does not
delete host transcripts or copies held by Discord or OpenAI. Include this upload
behavior in your deployment's privacy notice before enabling the feature.

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

Tests use the public SDK fakes and an HTTPX mock transport. They never contact
Discord or OpenAI and require no credentials. The dependency lock uses the published
module API; it has no dependency on a sibling checkout.

The API integration follows the
[official endpoint reference](https://developers.openai.com/api/reference/python/resources/content_provenance_checks/methods/create).
