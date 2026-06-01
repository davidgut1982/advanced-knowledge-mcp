# Automatic Memory Extraction Setup

Lore can automatically extract memorable facts from Hermes conversations using a fast
LLM (Llama 3.1 8B via OpenRouter) and store them in your knowledge base.

## Prerequisites

- A Lore MCP server running with PostgreSQL backend
- An OpenRouter API key (free tier is sufficient)

## Option A: OpenRouter

### 1. Get an OpenRouter API key

1. Sign up at https://openrouter.ai
2. Create a key at https://openrouter.ai/keys
3. Free tier: suitable for personal use (~$0.0001 per extraction call)

## 2. Set the environment variable

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
```

Or add to your `.env` file:
```
OPENROUTER_API_KEY=sk-or-v1-...
```

## 3. Enable in plugin config

In your `plugin.yaml` (or Hermes plugin config):

```yaml
config:
  auto_extract:
    enabled: true
    model: meta-llama/llama-3.1-8b-instruct
    provider_order:
      - Groq        # ~800 tok/s — fastest
      - Together    # fallback
      - Fireworks   # fallback
    confidence_threshold: 0.75   # 0–1; higher = fewer but more reliable memories
    dedup_similarity_threshold: 0.85  # cosine similarity for merge vs new entry
    min_turns: 3                 # skip single-exchange sessions
```

## Option B: Cerebras (recommended if already using Cerebras for Hermes)

Cerebras Cloud runs on wafer-scale chips. With 300+ TPS and 91–99% prompt cache hit rate,
the static extraction system prompt is effectively free after the first call.

### Get a Cerebras API key
1. Sign up at https://cloud.cerebras.ai
2. Create an API key

### Set environment variable
```bash
export CEREBRAS_API_KEY=csk-...
```

### Configure
```yaml
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
1. Sends the conversation turns to Llama 3.1 8B via OpenRouter (Groq backend)
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

Set `enabled: false` or remove `OPENROUTER_API_KEY`. The plugin degrades gracefully —
manual `kb_add` continues to work as before.
