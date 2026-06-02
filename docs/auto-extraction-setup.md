# Automatic Memory Extraction Setup

Lore can automatically extract memorable facts from Hermes conversations using a fast
LLM and store them in your knowledge base.

The default provider is **OpenRouter**, used as the transport but hard-pinned to Cerebras
via `provider_order: ["Cerebras"]` (which becomes `provider.only` in the request). Talking
to Cerebras Cloud directly is also supported as an alternative.

## Prerequisites

- A Lore MCP server running with PostgreSQL backend
- An OpenRouter API key (default) **or** a Cerebras API key

## Option A: OpenRouter (default, pinned to Cerebras)

OpenRouter is used as the transport, but the request is hard-pinned to Cerebras via
`provider.only` so it fails loudly rather than silently routing to a different backend.

### 1. Get an OpenRouter API key
1. Sign up at https://openrouter.ai
2. Create a key at https://openrouter.ai/keys

### 2. Set the environment variable

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
```

Or add to your `.env` file:
```
OPENROUTER_API_KEY=sk-or-v1-...
```

### 3. Enable in plugin config

In your `plugin.yaml` (or Hermes plugin config):

```yaml
config:
  auto_extract:
    enabled: true
    model: meta-llama/llama-3.1-8b-instruct
    provider_order:
      - Cerebras    # hard-pinned via "only" — will not fall through to other providers
    confidence_threshold: 0.75
    dedup_similarity_threshold: 0.85
    min_turns: 3
```

No `provider` key is needed — `openrouter` is the default. The `provider_order` defaults
to `["Cerebras"]` if omitted.

Note: `provider_order` maps to `provider.only` in the OpenRouter request body, not
`provider.order`. This means the request will return an error rather than silently
routing to Groq, Together, or Fireworks if Cerebras is unavailable.

## Option B: Cerebras (direct)

If you prefer to talk to Cerebras Cloud directly instead of through OpenRouter, set
`provider: cerebras`. Cerebras Cloud runs on wafer-scale chips. With 300+ TPS and 91–99%
prompt cache hit rate, the static extraction system prompt is effectively free after the
first call.

### 1. Get a Cerebras API key
1. Sign up at https://cloud.cerebras.ai
2. Create an API key

### 2. Set the environment variable

```bash
export CEREBRAS_API_KEY=csk-...
```

### 3. Enable in plugin config

```yaml
config:
  auto_extract:
    enabled: true
    provider: cerebras
    model: gpt-oss-120b
    confidence_threshold: 0.75
    dedup_similarity_threshold: 0.12
    min_turns: 3
```

## How it works

At the end of each session, Lore asynchronously:
1. Sends the conversation turns to a fast model via OpenRouter pinned to Cerebras (or Cerebras direct)
2. Extracts facts, preferences, goals, events, and system facts
3. Deduplicates against your existing KB using vector similarity
4. Writes new entries or updates existing ones

Extracted entries are tagged `source:auto-extracted` so you can inspect or bulk-remove them:

```
# View auto-extracted memories
kb_search(query="...", topic="auto-memory")

# All auto-extracted entries have tag: source:auto-extracted
```

## Tuning

| Config | Effect |
|--------|--------|
| `confidence_threshold: 0.9` | Only very explicit facts saved |
| `confidence_threshold: 0.6` | More memories, more noise |
| `dedup_similarity_threshold: 0.9` | Stricter dedup, more new entries |
| `dedup_similarity_threshold: 0.7` | Aggressive dedup, fewer entries |
| `min_turns: 1` | Extract from every session |

## Review mode (pending queue)

When `review_mode: true`, new auto-extracted entries land in `topic="auto-memory-pending"`
instead of being written directly to `auto-memory`. Merged entries (updates to existing KB
facts) bypass the queue and are applied directly.

To review pending entries:
  kb_list(topic="auto-memory-pending")

To approve an entry (promote to main KB):
  kb_update(kb_id="kb_xxx", topic="auto-memory", tags=["source:auto-extracted", "type:preference"])

To reject an entry:
  kb_delete(kb_id="kb_xxx")

Recall and search do not include `auto-memory-pending` entries by default — they are invisible
to the agent until approved.

## Disabling

Set `enabled: false` or remove the relevant API key (`CEREBRAS_API_KEY` or
`OPENROUTER_API_KEY`). The plugin degrades gracefully — manual `kb_add` continues to work
as before.
