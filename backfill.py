#!/usr/bin/env python3
"""Backfill AI coding-assistant session history into Cognee as distilled knowledge.

Three-phase pipeline (local models only, any OpenAI-compatible server):
  Phase 1 DISTILL: read sessions from the assistant's store (read-only) ->
      persisted normalized transcript -> local LLM -> small markdown lesson
      doc. Junk sessions (empty/aborted/trivial) are skipped, never indexed.
  Phase 2 ADD: add distilled docs to Cognee in idempotent batches.
  Phase 3 COGNIFY: run one bounded, resumable graph-building pipeline.

Connectors: opencode (SQLite) ships first; more ingestion lines planned.
See README.md and the wiki for setup.

Usage:
  export LLM_BASE_URL=http://127.0.0.1:8000 LLM_API_KEY=...
  python3 backfill.py --stats                       # show store counts
  python3 backfill.py --limit 3 --dataset kb_smoke  # smoke test
  python3 backfill.py --dataset knowledge_base      # full run (resumable)

Stdlib only. Progress in progress.json, model inputs in var/transcripts/, and
distilled docs in var/distilled/ (kill and re-run any time with zero loss).
"""
import argparse
import concurrent.futures as cf
import datetime
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

# Any OpenAI-compatible inference server (oMLX, Ollama, LM Studio, vLLM).
# OMLX_* names kept as fallback for existing setups.
LLM_URL = os.environ.get("LLM_BASE_URL",
                         os.environ.get("OMLX_URL", "http://127.0.0.1:8000"))
LLM_KEY = os.environ.get("LLM_API_KEY", os.environ.get("OMLX_API_KEY", ""))
COGNEE_URL = os.environ.get("COGNEE_API_URL", "http://localhost:8010")
DB_PATH = os.path.expanduser(
    os.environ.get("OPENCODE_DB", "~/.local/share/opencode/opencode.db"))
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
PROGRESS_FILE = os.path.join(ROOT_DIR, "progress.json")
VAR_DIR = os.path.join(ROOT_DIR, "var")
# Distilled docs persisted per session: resume-safe (progress.json alone
# cannot rebuild them) and auditable. Gitignored: may contain project facts.
DOCS_DIR = os.path.join(VAR_DIR, "distilled")
TRANSCRIPTS_DIR = os.path.join(VAR_DIR, "transcripts")
SKIPPED_MANIFEST = os.path.join(VAR_DIR, "skipped.json")

DISTILL_MODEL = os.environ.get(
    "DISTILL_MODEL",
    "Qwen3.6-35B-A3B-Uncensored-Heretic-MLX-4bit")  # override with --model
PROMPT_VERSION = os.environ.get("DISTILL_PROMPT_VERSION", "work-history-v2")
DISABLE_THINKING = os.environ.get("DISABLE_THINKING", "true").lower() not in (
    "0", "false", "no", "off")
MAX_TRANSCRIPT_CHARS = 18000  # head+tail sampled: outcomes live at the end
HEAD_CHARS = 6000
MAX_TOOL_OUTPUT = 1500
MAX_TEXT_PART = 4000

DISTILL_SYSTEM = """You turn an AI coding-assistant session into a durable work-history note.
The goal is broader than decision logging: preserve useful evidence of what the user worked on.
Keep: tasks attempted, projects and systems touched, investigations and research performed,
artifacts created or edited, implementation details, decisions and rationale, findings,
successful or failed approaches, outcomes/current state, reusable workflows, gotchas,
error causes and fixes, follow-ups, exact paths, symbols, commands, config, and error strings.
Do not discard a session merely because no decision was made or work was unfinished.
Drop only conversational filler, duplicated raw logs after extracting their meaning,
unresolved chain-of-thought, empty agent handshakes, health checks, and sessions with no
meaningful activity, subject, finding, artifact, or outcome.
Be concrete and factual. Do not invent completion or success.
Output markdown with exactly these sections (omit empty ones):
# <short title>
## What Was Worked On
## Outcome / Current State
## Decisions & Rationale
## Findings & Learnings
## Problems & Fixes
## Files, Systems & Commands
## Follow-ups
Use JUNK only for a pure greeting/ping, empty or duplicate shell, agent availability test,
or a session with genuinely no evidence of work. If uncertain, keep the session.
If and only if the session is junk, output exactly: JUNK
Output ONLY the note (or JUNK). No preamble, no thinking process, no analysis steps."""

SECRET_PATTERNS = [
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"sk-[A-Za-z0-9-_]{10,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{10,}"), "[REDACTED]"),
    (re.compile(r"xox[bcpas]-[A-Za-z0-9-]+"), "[REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S+"),
     r"\1=[REDACTED]"),
    (re.compile(
        r'''(?i)(["']?(?:password|passwd|secret|api[_-]?key|token)["']?'''
        r'''\s*:\s*)["'][^"']+["']'''), r'''\1"[REDACTED]"'''),
    (re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.I), "Bearer [REDACTED]"),
]


def scrub(text):
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def clip_middle(text, limit, head=None):
    """Bound large fields while preserving both request/setup and final outcome."""
    if len(text) <= limit:
        return text
    head = head if head is not None else max(1, limit // 2)
    tail = max(1, limit - head)
    return text[:head] + "\n[... truncated ...]\n" + text[-tail:]


def db():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def session_list(limit=None, offset=0):
    con = db()
    q = ("SELECT s.id, s.title, s.directory, s.time_created, "
         "COUNT(DISTINCT m.id) AS n_messages "
         "FROM session s LEFT JOIN message m ON m.session_id = s.id "
         "GROUP BY s.id ORDER BY s.time_created ASC, s.id ASC")
    if limit is not None:
        q += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    rows = [dict(r) for r in con.execute(q)]
    con.close()
    return rows


def build_transcript(session_id):
    """Compact, junk-reduced transcript from the part table."""
    con = db()
    parts = list(con.execute(
        "SELECT m.time_created AS t, m.data AS md, p.data AS d FROM part p "
        "JOIN message m ON m.id = p.message_id "
        "WHERE p.session_id = ? ORDER BY m.time_created, p.time_created",
        (session_id,)))
    con.close()
    out, substantive = [], 0
    for row in parts:
        try:
            d = json.loads(row["d"])
            md = json.loads(row["md"])
        except Exception:
            continue
        role = md.get("role", "assistant")
        t = d.get("type", "")
        if t in ("step-start", "step-finish"):
            continue
        if t == "text":
            txt = clip_middle(d.get("text") or "", MAX_TEXT_PART).strip()
            if len(txt) > 40:
                substantive += 1
            out.append(f"[{role}] {txt}" if txt else "")
        elif t == "reasoning":
            txt = (d.get("text") or "")[:500].strip()
            if txt:
                out.append(f"[thinking] {txt}")
        elif t == "tool":
            tool = d.get("tool", "?")
            st = d.get("state", {}) or {}
            inp = clip_middle(json.dumps(st.get("input", "")), 400)
            output = (st.get("output") or st.get("metadata", {}).get("output")
                      or "")
            output = clip_middle(output, MAX_TOOL_OUTPUT, head=600)
            if len(inp) > 80 or len(output) > 100:
                substantive += 1
            out.append(f"[tool:{tool} in={inp} out={output}]".strip())
    text = scrub("\n".join(x for x in out if x))
    if len(text) > MAX_TRANSCRIPT_CHARS:
        # head+tail: setup at the start, decisions/outcome at the end
        text = (text[:HEAD_CHARS] + "\n[... middle truncated ...]\n"
                + text[-(MAX_TRANSCRIPT_CHARS - HEAD_CHARS):])
    return text, substantive


def iso_time(milliseconds):
    return datetime.datetime.fromtimestamp(
        milliseconds / 1000, tz=datetime.timezone.utc).isoformat()


def model_input(session):
    transcript, substantive = build_transcript(session["id"])
    header = (
        f"Session: {session.get('title', '?')}\n"
        f"Project directory: {session.get('directory', '?')}\n"
        f"Started: {iso_time(session['time_created'])}\n\n"
    )
    return scrub(header + transcript), substantive


def provenance_header(session):
    return (
        "Source: OpenCode\n"
        f"Session ID: {session['id']}\n"
        f"Started: {iso_time(session['time_created'])}\n"
        f"Project directory: {scrub(session.get('directory') or '')}\n\n"
    )


def with_provenance(session, document):
    if f"Session ID: {session['id']}" in document:
        return document
    return provenance_header(session) + document


def llm_chat(system, user, model=None, max_tokens=1200, timeout=180):
    model = model or DISTILL_MODEL
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens, "temperature": 0.1,
    }
    if DISABLE_THINKING:
        # Hard-disable Qwen-style thinking at the template level. Set
        # DISABLE_THINKING=false if an endpoint rejects provider extensions.
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{LLM_URL}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LLM_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"__ERROR__ {e}"


def distill(session, persist_transcript=True):
    sid = session["id"]
    transcript, substantive = model_input(session)
    if substantive < 1 or len(transcript) < 200:
        return None, "junk: too little substance"
    if persist_transcript:
        save_text_atomic(transcript_path(sid), transcript)
    doc = llm_chat(DISTILL_SYSTEM, transcript)
    if doc.startswith("__ERROR__"):
        return None, f"distill-error: {doc}"
    if doc.strip() == "JUNK":
        return None, "junk: model judged no meaningful work history"
    if len(doc) < 80:
        return None, "distill-error: unexpectedly short model output"
    return with_provenance(session, scrub(doc)), None


def cognee_post(path, fields, files=None, timeout=600):
    """Multipart POST to Cognee API (stdlib). fields: {k: str|list}."""
    boundary = "----backfill%d" % int(time.time() * 1000)
    buf = b""
    for k, v in fields.items():
        vals = v if isinstance(v, list) else [v]
        for item in vals:
            buf += (f"--{boundary}\r\nContent-Disposition: form-data; "
                    f"name=\"{k}\"\r\n\r\n{item}\r\n").encode()
    body = buf + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{COGNEE_URL}{path}", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}: {e.read()[:300]}"
    except Exception as e:
        return None, str(e)[:200]


def cognee_get(path, timeout=30):
    try:
        with urllib.request.urlopen(f"{COGNEE_URL}{path}",
                                    timeout=timeout) as r:
            return json.load(r), None
    except Exception as e:
        return None, str(e)[:200]


def cognee_json_post(path, payload, timeout=600):
    req = urllib.request.Request(
        f"{COGNEE_URL}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}: {e.read()[:1000]}"
    except Exception as e:
        return None, str(e)[:500]


def processing_status(dataset):
    did = dataset_id(dataset)
    if not did:
        return None, None
    status, err = cognee_get(f"/api/v1/datasets/{did}/processing-status")
    return (status if not err else None), did


def latest_cognify_activity(dataset_id_value):
    path = ("/api/v1/activity/pipeline-runs?dataset_id="
            f"{urllib.parse.quote(dataset_id_value)}")
    rows, err = cognee_get(path)
    if err or not isinstance(rows, list):
        return None
    runs = [row for row in rows
            if row.get("pipeline_name") == "cognify_pipeline"]
    if not runs:
        return None
    return max(runs, key=lambda row: row.get("created_at") or "")


def cognee_delete(path, timeout=60):
    req = urllib.request.Request(f"{COGNEE_URL}{path}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (r.read()[:300].decode(errors="replace"), None)
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}: {e.read()[:300]}"
    except Exception as e:
        return None, str(e)[:200]


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {}


def normalize_legacy_progress(progress):
    changed = False
    for sid, entry in progress.items():
        if entry.get("status") not in ("remembered", "remember-failed"):
            continue
        if load_doc(sid):
            entry["status"] = "distilled"
            entry.pop("error", None)
        else:
            entry["status"] = "error"
            entry["reason"] = "legacy backend state had no persisted note"
        changed = True
    return changed


def save_json_atomic(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def save_text_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def save_skipped_manifest(progress, sessions_by_id):
    """Persist a reviewable list; source IDs remain reparsable indefinitely."""
    rows = []
    for sid, entry in progress.items():
        if entry.get("status") != "skipped":
            continue
        session = sessions_by_id.get(sid, {})
        rows.append({
            "session_id": sid,
            "title": entry.get("title") or session.get("title"),
            "directory": session.get("directory"),
            "reason": entry.get("reason"),
            "prompt_version": entry.get("prompt_version"),
        })
    save_json_atomic(SKIPPED_MANIFEST, sorted(rows, key=lambda x: x["session_id"]))


def dataset_record(name):
    data, err = cognee_get("/api/v1/datasets")
    if err:
        return None
    for d in data:
        if d.get("name") == name:
            return d
    return None


def dataset_id(name):
    record = dataset_record(name)
    return record.get("id") if record else None


def dataset_data_count(name):
    did = dataset_id(name)
    if not did:
        return 0
    result, err = cognee_get(f"/api/v1/datasets/{did}/data/count")
    if err:
        return None
    return result.get("count", 0) if isinstance(result, dict) else None


def wait_for_processing(dataset, timeout_s=7 * 24 * 3600, stall_s=1800):
    _, did = processing_status(dataset)
    if not did:
        print("  dataset not found (nothing submitted?)")
        return False
    t0 = time.time()
    last_progress = t0
    last_completed = None
    while time.time() - t0 < timeout_s:
        st, _ = processing_status(dataset)
        if st is None:
            print("  status unavailable; retrying")
        else:
            print(f"  extraction: {st.get('completed')}/{st.get('total')} "
                  f"done ({time.time()-t0:.0f}s elapsed)", flush=True)
            completed = st.get("completed")
            if completed != last_completed:
                last_completed = completed
                last_progress = time.time()
            items = st.get("items", []) or []
            if st.get("total", 0) > 0 and all(
                    x.get("completed") for x in items):
                print("  extraction complete")
                return True
        activity = latest_cognify_activity(did)
        if activity and activity.get("status") == "DATASET_PROCESSING_ERRORED":
            print(f"  extraction failed: {activity.get('error_class') or 'unknown'}",
                  file=sys.stderr)
            return False
        if stall_s and time.time() - last_progress >= stall_s:
            print(f"  extraction stalled for {stall_s}s", file=sys.stderr)
            return False
        time.sleep(60)
    print("  TIMEOUT waiting for extraction; re-run to resume polling")
    return False


def save_progress(p):
    save_json_atomic(PROGRESS_FILE, p)


def doc_path(sid):
    return os.path.join(DOCS_DIR, f"{sid}.md")


def transcript_path(sid):
    return os.path.join(TRANSCRIPTS_DIR, f"{sid}.md")


def load_doc(sid):
    p = doc_path(sid)
    if os.path.exists(p):
        with open(p) as f:
            return f.read()
    return None


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def main():
    ap = argparse.ArgumentParser(description="Backfill OpenCode -> Cognee")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--dataset",
                    default=os.environ.get("COGNEE_DATASET", "opencode_kb"))
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--add-batch-size", type=int, default=100,
                    help="documents per idempotent Cognee add request")
    ap.add_argument("--data-per-batch", type=int, default=4,
                    help="documents Cognee processes concurrently")
    ap.add_argument("--chunks-per-batch", type=int, default=12,
                    help="chunks Cognee processes per task batch")
    ap.add_argument("--processing-timeout", type=int,
                    default=7 * 24 * 3600,
                    help="seconds to wait for the single Cognify run")
    ap.add_argument("--model", default=None,
                    help="distill LLM model id (default: $DISTILL_MODEL)")
    ap.add_argument("--reparse-skipped", action="store_true",
                    help="re-evaluate sessions currently marked skipped")
    ap.add_argument("--reparse-all", action="store_true",
                    help="re-distill all sessions with retained content")
    ap.add_argument("--distill-only", action="store_true",
                    help="phase 1 only, print docs, do not POST to Cognee")
    ap.add_argument("--improve", action="store_true",
                    help="run improve() once after Cognify completes")
    ap.add_argument("--recall", nargs="*", default=None,
                    help="spot-check queries after run")
    args = ap.parse_args()
    global DISTILL_MODEL
    if args.model:
        DISTILL_MODEL = args.model
    print(f"distill model: {DISTILL_MODEL}; prompt: {PROMPT_VERSION}")

    if args.stats:
        con = db()
        for t in ("session", "message", "part"):
            print(t, con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        con.close()
        return

    sessions = session_list(args.limit, args.offset)
    print(f"{len(sessions)} sessions to process -> dataset '{args.dataset}'")
    if args.distill_only:
        # dry run: never touch progress.json or the backend
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(distill, s, False): s for s in sessions}
            for fut in cf.as_completed(futs):
                s = futs[fut]
                doc, reason = fut.result()
                print("=" * 40, s["id"], s.get("title"))
                print((doc[:1500] if doc else f"SKIP: {reason}"))
        return
    progress = load_progress()
    os.makedirs(DOCS_DIR, exist_ok=True)
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    by_id = {s["id"]: s for s in sessions}
    if args.reparse_all and dataset_id(args.dataset):
        print("--reparse-all changes document hashes; use a new dataset or "
              "delete the existing target dataset first", file=sys.stderr)
        return 2
    if normalize_legacy_progress(progress):
        save_progress(progress)
    # Distillation state is independent of Cognee dataset state. Cognee owns
    # ingestion deduplication and per-item processing status.
    def needs_distill(sid):
        entry = progress.get(sid, {})
        st = entry.get("status")
        same_prompt = entry.get("prompt_version") == PROMPT_VERSION
        if args.reparse_all:
            return True
        if st == "skipped":
            return args.reparse_skipped or not same_prompt
        if load_doc(sid):
            return False
        return True
    todo = [s for s in sessions if needs_distill(s["id"])]
    print(f"{len(todo)} remaining after progress.json")
    if todo:
        # Phase 1: distill (parallel, oMLX allows 8 concurrent; use 4)
        t0 = time.time()
        kept = 0
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(distill, s): s for s in todo}
            for i, fut in enumerate(cf.as_completed(futs), 1):
                s = futs[fut]
                doc, reason = fut.result()
                if doc:
                    kept += 1
                    save_text_atomic(doc_path(s["id"]), doc)
                    progress[s["id"]] = {"status": "distilled",
                                         "title": s.get("title"),
                                         "prompt_version": PROMPT_VERSION}
                elif reason and reason.startswith("junk"):
                    if args.reparse_all and os.path.exists(doc_path(s["id"])):
                        os.remove(doc_path(s["id"]))
                    progress[s["id"]] = {"status": "skipped",
                                         "title": s.get("title"),
                                         "reason": reason,
                                         "prompt_version": PROMPT_VERSION}
                else:
                    progress[s["id"]] = {"status": "error",
                                         "title": s.get("title"),
                                         "reason": reason,
                                         "prompt_version": PROMPT_VERSION}
                if i % 10 == 0:
                    save_progress(progress)
                    save_skipped_manifest(progress, by_id)
                    dt = time.time() - t0
                    print(f"  distilled {i}/{len(todo)} "
                          f"({kept} kept) {dt:.0f}s", flush=True)
        save_progress(progress)
        save_skipped_manifest(progress, by_id)
        dt = time.time() - t0
        print(f"Phase 1 done: {kept} kept, "
              f"{len(todo)-kept} skipped, {dt:.0f}s "
              f"({dt/max(len(todo),1):.1f}s/session)")

    # Re-add every persisted note. Cognee's dataset-scoped content hashes make
    # this cheap and idempotent, so client-side backend state is unnecessary.
    docs = []
    legacy_docs = []
    for session in sessions:
        entry = progress.get(session["id"], {})
        if entry.get("status") != "distilled":
            continue
        doc = load_doc(session["id"])
        if not doc:
            continue
        if f"Session ID: {session['id']}" not in doc:
            legacy_docs.append(session["id"])
        if not os.path.exists(transcript_path(session["id"])):
            transcript, _ = model_input(session)
            save_text_atomic(transcript_path(session["id"]), transcript)
        docs.append((session, doc))

    if not docs:
        print("nothing to add")
        return 0

    existing_count = dataset_data_count(args.dataset)
    if existing_count is None:
        print("could not inspect target dataset", file=sys.stderr)
        return 1
    if legacy_docs and existing_count:
        print("persisted notes need the provenance migration, but the target "
              "dataset already contains data; use a new dataset or delete the "
              "existing dataset to avoid duplicate document hashes",
              file=sys.stderr)
        return 2
    if legacy_docs:
        migrated_docs = []
        for session, doc in docs:
            migrated = with_provenance(session, doc)
            save_text_atomic(doc_path(session["id"]), migrated)
            migrated_docs.append((session, migrated))
        docs = migrated_docs

    print(f"Phase 2: adding {len(docs)} docs in batches of "
          f"{args.add_batch_size}")
    t0 = time.time()
    added = 0
    for batch in chunks(docs, args.add_batch_size):
        metadata = [{"session_id": session["id"],
                     "title": scrub(session.get("title") or ""),
                     "directory": scrub(session.get("directory") or ""),
                     "started_at": iso_time(session["time_created"]),
                     "source": "opencode-backfill"}
                    for session, _ in batch]
        _, err = cognee_post("/api/v1/add",
                             {"raw_data": [doc for _, doc in batch],
                              "datasetName": args.dataset,
                              "external_metadata": json.dumps(metadata)},
                             timeout=1800)
        if err:
            print(f"Phase 2 add failed after {added} docs: {err}",
                  file=sys.stderr)
            return 1
        added += len(batch)
        print(f"  added {added}/{len(docs)}", flush=True)
    print(f"Phase 2 done in {time.time()-t0:.0f}s")

    status, did = processing_status(args.dataset)
    items = (status or {}).get("items", []) or []
    already_complete = ((status or {}).get("total", 0) > 0
                        and all(item.get("completed") for item in items))
    activity = latest_cognify_activity(did) if did else None
    active = (activity and
              activity.get("status") == "DATASET_PROCESSING_STARTED")
    state_name = re.sub(r"[^A-Za-z0-9_.-]", "_", args.dataset)
    cognify_state = os.path.join(VAR_DIR, f"cognify-{state_name}.json")
    if already_complete:
        print("Phase 3: all documents already Cognified")
    elif active:
        print(f"Phase 3: monitoring active Cognify run "
              f"{activity.get('pipeline_run_id')}")
        already_complete = wait_for_processing(
            args.dataset, args.processing_timeout, stall_s=1800)
        if not already_complete:
            print("Phase 3: active run failed or stalled; starting a retry")
    if not already_complete:
        print("Phase 3: starting one bounded Cognify pipeline ...")
        result, err = cognee_json_post(
            "/api/v1/cognify",
            {"datasets": [args.dataset],
             "runInBackground": True,
             "dataPerBatch": args.data_per_batch,
             "chunksPerBatch": args.chunks_per_batch,
             "chunkSize": 4096})
        if err:
            print(f"Phase 3 failed: {err}", file=sys.stderr)
            return 1
        save_json_atomic(cognify_state,
                         {"dataset": args.dataset,
                          "started_at": iso_time(int(time.time() * 1000)),
                          "response": result})
    if not already_complete and not wait_for_processing(
            args.dataset, args.processing_timeout, stall_s=1800):
        return 1
    save_json_atomic(cognify_state,
                     {"dataset": args.dataset,
                      "completed_at": iso_time(int(time.time() * 1000))})

    if args.improve:
        marker_name = re.sub(r"[^A-Za-z0-9_.-]", "_", args.dataset)
        marker = os.path.join(VAR_DIR, f"improved-{marker_name}.json")
        current_dataset = dataset_record(args.dataset) or {}
        current_dataset_id = current_dataset.get("id")
        current_dataset_created = current_dataset.get("createdAt")
        marker_data = None
        if os.path.exists(marker):
            try:
                with open(marker) as f:
                    marker_data = json.load(f)
            except (OSError, ValueError):
                marker_data = None
        if (marker_data and marker_data.get("dataset_id") == current_dataset_id
                and marker_data.get("dataset_created_at") == current_dataset_created
                and marker_data.get("status") == "completed"):
            print("improve: already completed")
        else:
            print("running improve() once ...")
            res, err = cognee_json_post(
                "/api/v1/improve",
                {"datasetName": args.dataset,
                 "runInBackground": False,
                 "buildGlobalContextIndex": True},
                timeout=7200)
            if err:
                print(f"improve failed: {err}", file=sys.stderr)
                return 1
            if not isinstance(res, dict) or res.get("status") != "completed":
                print(f"improve did not complete: {res}", file=sys.stderr)
                return 1
            save_json_atomic(marker,
                             {"dataset_id": current_dataset_id,
                              "dataset_created_at": current_dataset_created,
                              "status": "completed",
                              "completed_at": iso_time(int(time.time() * 1000))})
            print("improve:", str(res)[:500])

    if args.recall is not None:
        queries = args.recall or ["What decisions were made?",
                                  "What gotchas came up?"]
        for q in queries:
            body = json.dumps({"query": q, "datasets": [args.dataset]}).encode()
            req = urllib.request.Request(
                f"{COGNEE_URL}/api/v1/recall", data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    ans = json.load(r)
                txt = (ans[0].get("text") if isinstance(ans, list) and ans
                       else str(ans))[:800]
                print(f"\nQ: {q}\nA: {txt}")
            except Exception as e:
                print(f"\nQ: {q}\nERR: {e}")
                return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
