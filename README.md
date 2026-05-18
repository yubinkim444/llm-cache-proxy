# llm-cache-proxy

> **One env var. 60–80% cheaper dev loops.**
> A localhost proxy that caches identical OpenAI and Anthropic API calls on
> disk and replays them for free. Works with every existing tool.

[![PyPI](https://img.shields.io/pypi/v/llm-cache-proxy)](https://pypi.org/project/llm-cache-proxy/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green)]()

---

## Why

You're iterating on a prompt. You run the same call 40 times tweaking
wording. That's 40× the spend on identical requests.

Or: you have a long-running script that re-fetches the same tool definitions
every run during development. Or: your test suite calls the API.

`llm-cache-proxy` sits on localhost, speaks the OpenAI and Anthropic REST
protocols, and caches every successful response in a single SQLite file.
**Identical requests** (same method, path, body) get served from disk —
no network call, no spend.

It works with **every** tool because you only change one env var:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:9001/openai/v1
export ANTHROPIC_BASE_URL=http://127.0.0.1:9001/anthropic
```

Cursor, Claude Code, your scripts, your tests, the OpenAI Python SDK, the
Anthropic SDK — they all start using the cache automatically.

---

## Install &amp; run

```bash
pip install llm-cache-proxy
llm-cache-proxy
# or
uvx llm-cache-proxy
```

Default port: `9001`. Default cache: `~/.cache/llm-cache-proxy/cache.db`.

---

## Use it

In whatever shell launches your tool / script:

```bash
# OpenAI
export OPENAI_BASE_URL=http://127.0.0.1:9001/openai/v1

# Anthropic
export ANTHROPIC_BASE_URL=http://127.0.0.1:9001/anthropic

# now run anything — Cursor, your script, pytest, etc.
```

Responses include a `X-LLM-Cache: HIT|MISS` header so you can see what
happened.

Bypass the cache for a single request:

```bash
curl -H "x-llm-cache-bypass: 1" http://127.0.0.1:9001/openai/v1/chat/completions ...
```

---

## See what you saved

```bash
curl http://127.0.0.1:9001/stats
```

```json
{
  "hits": 312,
  "misses": 87,
  "bytes_served_from_cache": 4_182_404,
  "entries": 87,
  "cached_response_bytes": 1_205_211,
  "by_model": {"gpt-4o": 41, "claude-sonnet-4": 46}
}
```

Clear the cache:

```bash
curl -X DELETE http://127.0.0.1:9001/cache
curl -X DELETE http://127.0.0.1:9001/stats
```

---

## Config (all optional)

| Env var | Default | Description |
|---------|---------|-------------|
| `LLM_CACHE_PORT` | `9001` | Listen port. |
| `LLM_CACHE_HOST` | `127.0.0.1` | Listen host. |
| `LLM_CACHE_DIR` | `~/.cache/llm-cache-proxy` | Where to put the SQLite file. |
| `LLM_CACHE_TTL` | `0` | TTL in seconds (`0` = forever). |
| `LLM_CACHE_TIMEOUT` | `300` | Upstream request timeout. |
| `OPENAI_UPSTREAM` | `https://api.openai.com` | Override the upstream. |
| `ANTHROPIC_UPSTREAM` | `https://api.anthropic.com` | Override the upstream. |

Per-request:
- Header `x-llm-cache-bypass: 1` — skip both read and write for this call.
- Header `x-llm-cache-extra-key: <string>` — add an extra dimension to the
  cache key (e.g., a user id, a session id).

---

## How the cache key is built

```
sha256(method + "|" + path + "|" + body + optional extra_key)
```

Method + path + body is enough to make identical requests collide
deterministically. Headers are *not* included in the default key (so API
key rotation doesn't invalidate the cache) — set `x-llm-cache-extra-key`
if you want extra dimensions.

Only `2xx` responses are cached. Errors always go through.

---

## Caveats

- Streaming responses: when the upstream returns `text/event-stream`, the
  full SSE body is captured and replayed verbatim on cache hit. That works
  but you lose per-token streaming feel.
- Tool / function-calling responses cache fine — the whole completion object
  is one entry.
- Don't expose this proxy to the public internet — it has no auth and your
  API key flows through it.

---

## Companion projects

- **[mcp-rec](https://github.com/yubinkim444/mcp-rec)** — VCR for MCP servers (similar idea, MCP layer).
- **[ai-first-scraper](https://github.com/yubinkim444/ai-first-scraper)** — clean Markdown for LLM agents.

---

## License

MIT © yubinkim444
