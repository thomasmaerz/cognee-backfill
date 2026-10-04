# cognee-backfill

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/docker-composed-2496ED?logo=docker&logoColor=white)
![Cognee](https://img.shields.io/badge/memory-cognee_1.x-7C3AED)
![Kuzu](https://img.shields.io/badge/graph-kuzu-FF6F00)
![LanceDB](https://img.shields.io/badge/vectors-lancedb-00B588)
![SQLite](https://img.shields.io/badge/store-sqlite-003B57?logo=sqlite&logoColor=white)
![OpenAI-compatible](https://img.shields.io/badge/LLM-openai_compatible-10A37F?logo=openai&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)
![Stdlib](https://img.shields.io/badge/connector_deps-stdlib_only-blue)

Turn years of AI coding-assistant history into a **distilled, junk-free knowledge graph** — running **100% on local models**. No cloud, no API bills, no raw-transcript indexing.

Your assistant's past sessions hold decisions, fixes, and hard-won gotchas. But backfilling them naively (embed every message!) produces a bloated, noisy, expensive mess. `cognee-backfill` does it the disciplined way: **distill first, remember only what's worth keeping.**

```
┌──────────────┐   read-only    ┌───────────────────┐  distilled notes  ┌────────────────┐
│  assistant    │ ───────────▶ │  Phase 1: DISTILL │ ─────────────────▶ │ Phase 2:       │
│  history DB   │  transcripts │  local LLM filter │   (junk skipped)   │ REMEMBER into  │
│ (opencode… )  │  + scrub     │  decisions/fixes  │                    │ Cognee graph   │
└──────────────┘              └───────────────────┘                    └───────┬────────┘
                                                                              │ entities,
                                                                              │ relations,
                                                                              │ vectors
                                                                              ▼
                                                                     ┌────────────────┐
                                                                     │ recall: graph  │
                                                                     │ + hybrid search│
                                                                     └────────────────┘
```

## Why not just index everything?

| Naive backfill | This pipeline |
|---|---|
| Embeds 100% of messages, incl. greetings, aborted attempts, log dumps | LLM distills each session to decisions / patterns / gotchas / errors |
| Reasoning traces and tool output pollute retrieval | Head+tail sampling keeps outcomes; junk sessions skipped pre-LLM |
| Secrets leak into the store | Regex scrubbing before anything leaves the DB |
| One giant uninterrupted job | Crash-safe: progress + docs on disk, resume any time with zero loss |

## Quick start

**Prereqs:** Docker, Python 3.10+, and a local OpenAI-compatible server ([oMLX](https://github.com/jundot/omlx), Ollama, LM Studio, or vLLM) with a chat model and an embedding model loaded.

```bash
git clone https://github.com/thomasmaerz/cognee-backfill.git
cd cognee-backfill
cp .env.example .env   # fill in LLM_BASE_URL + LLM_API_KEY
docker compose up -d   # Cognee API :8010, web UI :3010
export LLM_BASE_URL=http://127.0.0.1:8000 LLM_API_KEY=...

# 1. Look at what you have
python3 backfill.py --stats

# 2. Smoke test (isolated dataset, delete after)
python3 backfill.py --limit 3 --dataset kb_smoke --recall "what was decided about X?"

# 3. Full run (resumable — Ctrl-C any time, re-run to continue)
nohup python3 backfill.py --dataset knowledge_base > backfill.log 2>&1 &
./monitor.sh &   # hourly snapshots -> monitor.log

# 4. When extraction completes: enrich + verify
python3 backfill.py --dataset knowledge_base --improve --recall "your test question"
```

Browse the graph at `http://localhost:3010` (sign in with the local-dev
`default_user@example.com` / `default_password` — localhost only).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | `http://127.0.0.1:8000` | OpenAI-compatible inference server |
| `LLM_API_KEY` | — | Server API key (required) |
| `DISTILL_MODEL` / `--model` | Qwen3.6-35B-A3B (MLX 4-bit) | Distill LLM; any strong instruction model works |
| `COGNEE_API_URL` | `http://localhost:8010` | Cognee API |
| `OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | Source DB (read-only access, never written) |
| `--concurrency` | `4` | Parallel distill workers (match to your GPU headroom) |
| `--limit` / `--offset` | — | Slice the session list (testing, sharding) |

Server-side model choice lives in `docker-compose.yml` (`LLM_MODEL`,
`EMBEDDING_MODEL`, `LLM_ARGS`, instructor mode). See the wiki:
**Configuration** for the full matrix, including the reasoning-model
thinking-preamble fix.

## Live capture (going forward)

The backfill covers history. For new sessions, install Cognee's OpenCode
memory plugin so every session is captured, recalled, and bridged into the
same dataset automatically:

```bash
# build @cognee/cognee-opencode from
# https://github.com/topoteretes/cognee-integrations (integrations/opencode)
```

```json
{ "plugin": [["file:/path/to/cognee-opencode",
  { "baseUrl": "http://localhost:8010", "datasetName": "knowledge_base" }]] }
```

Restart OpenCode after adding it. Details in the wiki: **Live capture**.

## Connectors roadmap

- [x] **OpenCode** (`session`/`message`/`part` SQLite schema) — `backfill.py`
- [ ] Claude Code (`~/.claude/projects` JSONL)
- [ ] Cursor / Windsurf transcripts
- [ ] Continue.dev / Cline exports
- [ ] Generic JSONL (`--input transcript.jsonl` — bring your own extractor)

Each connector implements: **extract (read-only) → distill → submit**.
Want to add one? Wiki: **Adding a connector**.

## Troubleshooting (short version)

- **Extraction stuck `pending` / validation errors** → reasoning model emitting
  chain-of-thought into strict JSON calls. Fix is documented and default-on:
  `chat_template_kwargs.enable_thinking=false` (connector) + `LLM_ARGS`
  (server). Wiki: **Thinking-preamble**.
- **Server LLM timeouts under load** → `COGNEE_SKIP_CONNECTION_TEST=true`
  is already set; reduce `--concurrency`, close competing sessions.
- **UI "Cannot connect to backend"** → `CORS_ALLOWED_ORIGINS`/`UI_APP_URL`
  must match the exact browser origin. Already set for `:3010` in compose.

## License

MIT — see [LICENSE](LICENSE). Built on [Cognee](https://github.com/topoteretes/cognee) (Apache-2.0).
