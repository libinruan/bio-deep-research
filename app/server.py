"""Web server: starts research runs, streams their progress, and stores threads on disk."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, entities, export, usage
from .registry import Registry
from .sources import SourceError

ROOT = Path(__file__).resolve().parent.parent
THREADS = ROOT / "data" / "threads"
STATIC = Path(__file__).resolve().parent / "static"
THREADS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Bio Deep Research")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Run:
    """A turn in progress: its event log, and a way for listeners to wait for more."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.changed = asyncio.Condition()
        self.done = False
        self.task: asyncio.Task[None] | None = None

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()


runs: dict[str, Run] = {}            # turn id -> live run
registries: dict[str, Registry] = {}  # thread id -> evidence registry


# ----------------------------------------------------------------------------- storage

def _path(thread_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", thread_id):
        raise HTTPException(404, "Unknown thread")
    return THREADS / f"{thread_id}.json"


def load_thread(thread_id: str) -> dict[str, Any]:
    path = _path(thread_id)
    if not path.exists():
        raise HTTPException(404, "Unknown thread")
    return json.loads(path.read_text())


def save_thread(thread: dict[str, Any]) -> None:
    reg = registries.get(thread["id"])
    if reg is not None:
        thread["registry"] = reg.to_dict()
    tmp = _path(thread["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(thread, ensure_ascii=False))
    tmp.replace(_path(thread["id"]))


def registry_for(thread: dict[str, Any]) -> Registry:
    if thread["id"] not in registries:
        registries[thread["id"]] = Registry.from_dict(thread.get("registry") or {})
    return registries[thread["id"]]


def public(thread: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in thread.items() if k not in ("registry", "session_id", "session_cost", "entities")}
    out["compare_of"] = thread.get("compare_of")
    for turn in out["turns"]:
        turn["live"] = turn["id"] in runs and not runs[turn["id"]].done
        if turn["status"] == "running" and not turn["live"]:
            turn.update(status="error", error="This run was interrupted (the server restarted).")
    return out


async def bill(kind: str, result: dict[str, Any], *, thread_id: str | None = None,
               seconds: float | None = None) -> float | None:
    """Record one model call and return what it really cost, in dollars.

    On Anthropic's own endpoint the harness's figure is correct and is used as-is. On any
    other provider it is priced at Claude's rates, so the cost is recomputed from the token
    counts and that provider's published prices.
    """
    engine = agent.engine()
    tokens = (result.get("tokens") or usage.tokens_from(None))
    price = await usage.price_for(engine["model"], engine["base_url"], engine["third_party"])
    if price is not None:
        cost, basis = price.cost(tokens), price.source
    elif not engine["third_party"]:
        cost, basis = result.get("harness_cost"), "harness (Anthropic rates)"
    else:
        cost, basis = None, "unknown — set BDR_PRICE_IN and BDR_PRICE_OUT to price this engine"
    usage.record(kind=kind, engine=engine["model"], tokens=tokens, cost=cost,
                 basis=basis, seconds=seconds, thread_id=thread_id)
    return cost


# ----------------------------------------------------------------------------- running

async def execute(thread: dict[str, Any], turn: dict[str, Any], run: Run, want_audit: bool) -> None:
    registry = registry_for(thread)
    searches_before = len(registry.search_log)

    def emit(event: dict[str, Any]) -> None:
        if event["type"] in ("tool", "tool_result", "status"):
            turn["activity"].append({**event, "t": round(time.time() - turn["started"], 1)})
        run.emit(event)

    try:
        emit({"type": "status", "text": "Searching the literature"})
        result = await agent.research(
            registry, emit, turn["question"], mode=turn["mode"], hypotheses=turn["hypotheses"],
            session_id=thread.get("session_id"), year_from=turn.get("year_from"), year_to=turn.get("year_to"),
        )
        turn["engine"] = agent.engine()["model"]
        turn["third_party"] = agent.engine()["third_party"]
        result["usage"]["real_cost"] = await bill(
            "research", result["usage"], thread_id=thread["id"], seconds=result["usage"]["seconds"])
        thread["session_id"] = result["session_id"]
        # A resumed session reports its running total, so charge this turn only the difference.
        total = result["usage"]["cost_usd"]
        if total is not None:
            result["usage"]["cost_usd"] = max(0.0, total - thread.get("session_cost", 0.0))
            thread["session_cost"] = total
        emit({"type": "status", "text": "Verifying citations against retrieved records"})
        final = await registry.finalize(result["report"])
        turn.update(
            status="done", report=final["report"], references=final["references"], problems=final["problems"],
            strategy=registry.strategy if turn["mode"] == "deep" else None,
            search_log=registry.search_log[searches_before:], usage=result["usage"],
            records_seen=len(registry.records),
        )
        save_thread(thread)
        run.emit({"type": "final", "turn": turn})

        if want_audit and final["references"]:
            emit({"type": "status", "text": "Auditing each claim against its cited source"})
            try:
                turn["audit"] = await agent.audit(registry, result["report"])
                turn["audit"]["real_cost"] = await bill(
                    "audit", turn["audit"].get("usage") or {}, thread_id=thread["id"])
                numbers = {ref["key"]: ref["n"] for ref in final["references"]}
                for issue in turn["audit"]["issues"]:
                    rec = registry.get(issue["citation"])
                    issue["n"] = numbers.get(rec["key"]) if rec else None
            except Exception as exc:  # the report stands on its own if the audit fails
                turn["audit"] = {"error": str(exc)}
            run.emit({"type": "audit", "audit": turn["audit"]})
    except asyncio.CancelledError:
        turn.update(status="cancelled", error="Stopped.")
        run.emit({"type": "error", "message": "Stopped."})
    except Exception as exc:
        turn.update(status="error", error=str(exc))
        run.emit({"type": "error", "message": str(exc)})
    finally:
        turn["finished"] = time.time()
        if turn["status"] == "running":
            turn["status"] = "error"
        save_thread(thread)
        run.done = True
        run.emit({"type": "done"})


class ResearchRequest(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    mode: str = Field(default="quick", pattern="^(quick|deep)$")
    hypotheses: bool = False
    audit: bool = True
    year_from: int | None = Field(default=None, ge=1800, le=2100)
    year_to: int | None = Field(default=None, ge=1800, le=2100)
    thread_id: str | None = None
    compare_of: str | None = None


@app.post("/api/research")
async def start_research(req: ResearchRequest) -> dict[str, str]:
    if req.thread_id:
        thread = load_thread(req.thread_id)
        if any(t["id"] in runs and not runs[t["id"]].done for t in thread["turns"]):
            raise HTTPException(409, "This thread already has a run in progress.")
    else:
        title = req.question.strip()[:120]
        thread = {"id": uuid.uuid4().hex[:12], "title": f"Re-run · {title}" if req.compare_of else title,
                  "created": time.time(), "session_id": None, "turns": [],
                  "compare_of": req.compare_of}
    turn = {
        "id": uuid.uuid4().hex[:12], "question": req.question.strip(), "mode": req.mode, "hypotheses": req.hypotheses,
        "year_from": req.year_from, "year_to": req.year_to, "status": "running", "started": time.time(),
        "activity": [], "report": "", "references": [], "problems": [], "audit": None, "strategy": None,
        "search_log": [], "usage": None, "error": None,
    }
    thread["turns"].append(turn)
    save_thread(thread)
    run = runs[turn["id"]] = Run()
    run.task = asyncio.create_task(execute(thread, turn, run, req.audit))
    return {"thread_id": thread["id"], "turn_id": turn["id"]}


@app.get("/api/runs/{turn_id}/events")
async def run_events(turn_id: str, start: int = 0) -> StreamingResponse:
    run = runs.get(turn_id)
    if run is None:
        raise HTTPException(404, "No live run with that id")

    async def stream():
        i = start
        while True:
            while i < len(run.events):
                yield f"id: {i}\ndata: {json.dumps(run.events[i], ensure_ascii=False)}\n\n"
                i += 1
            if run.done:
                return
            async with run.changed:
                try:
                    await asyncio.wait_for(run.changed.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{turn_id}/cancel")
async def cancel_run(turn_id: str) -> dict[str, bool]:
    run = runs.get(turn_id)
    if run and run.task and not run.done:
        run.task.cancel()
        return {"cancelled": True}
    return {"cancelled": False}


# ----------------------------------------------------------------------------- threads

@app.get("/api/threads")
async def list_threads() -> list[dict[str, Any]]:
    out = []
    for path in THREADS.glob("*.json"):
        t = json.loads(path.read_text())
        out.append({"id": t["id"], "title": t["title"], "created": t["created"], "turns": len(t["turns"]),
                    "mode": t["turns"][0]["mode"] if t["turns"] else "quick"})
    return sorted(out, key=lambda t: -t["created"])


@app.get("/api/threads/{thread_id}")
async def get_thread(thread_id: str) -> dict[str, Any]:
    return public(load_thread(thread_id))


class DeleteRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


@app.post("/api/threads/delete")
async def delete_threads(req: DeleteRequest) -> dict[str, Any]:
    """Delete several saved searches, skipping any with a run still going."""
    deleted, skipped = [], []
    for thread_id in dict.fromkeys(req.ids):
        try:
            thread = load_thread(thread_id)
        except HTTPException:
            continue  # already gone
        if any(t["id"] in runs and not runs[t["id"]].done for t in thread["turns"]):
            skipped.append({"id": thread_id, "title": thread["title"]})
            continue
        _path(thread_id).unlink(missing_ok=True)
        registries.pop(thread_id, None)
        deleted.append(thread_id)
    return {"deleted": deleted, "skipped": skipped}


@app.delete("/api/threads/{thread_id}")
async def delete_thread(thread_id: str) -> dict[str, bool]:
    thread = load_thread(thread_id)
    if any(t["id"] in runs and not runs[t["id"]].done for t in thread["turns"]):
        raise HTTPException(409, "Stop the run in progress before deleting this thread.")
    _path(thread_id).unlink()
    registries.pop(thread_id, None)
    return {"deleted": True}


# ----------------------------------------------------------------------------- export

def _turn_or_404(thread: dict[str, Any], turn_id: str) -> dict[str, Any]:
    turn = next((t for t in thread["turns"] if t["id"] == turn_id), None)
    if turn is None:
        raise HTTPException(404, "Unknown turn")
    return turn


@app.get("/api/threads/{thread_id}/turns/{turn_id}/print")
async def print_view(thread_id: str, turn_id: str) -> HTMLResponse:
    """The standalone page behind the HTML and PDF exports; also readable on its own."""
    thread = load_thread(thread_id)
    turn = _turn_or_404(thread, turn_id)
    return HTMLResponse(export.to_html(thread, turn, engine=agent.engine()["model"]))


EXPORTS = {
    "md": ("text/markdown; charset=utf-8", "md"),
    "bib": ("application/x-bibtex; charset=utf-8", "bib"),
    "ris": ("application/x-research-info-systems; charset=utf-8", "ris"),
    "html": ("text/html; charset=utf-8", "html"),
    "pdf": ("application/pdf", "pdf"),
}


@app.get("/api/threads/{thread_id}/turns/{turn_id}/export")
async def export_turn(request: Request, thread_id: str, turn_id: str, format: str = "md") -> Response:
    thread = load_thread(thread_id)
    turn = _turn_or_404(thread, turn_id)
    if format not in EXPORTS:
        raise HTTPException(400, f"Unknown export format. Choose one of: {', '.join(EXPORTS)}")
    media_type, ext = EXPORTS[format]
    refs = turn["references"]

    if format == "pdf":
        url = str(request.url_for("print_view", thread_id=thread_id, turn_id=turn_id))
        try:
            body: str | bytes = await export.to_pdf(url)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    elif format == "html":
        body = export.to_html(thread, turn, engine=agent.engine()["model"])
    elif format == "bib":
        body = "\n\n".join(export.to_bibtex(r) for r in refs)
    elif format == "ris":
        body = "\n\n".join(export.to_ris(r) for r in refs)
    else:
        body = export.to_markdown(turn)

    slug = re.sub(r"[^a-z0-9]+", "-", turn["question"].lower())[:50].strip("-") or "research"
    return Response(content=body, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{slug}.{ext}"'})


# ----------------------------------------------------------------------------- comparing runs

def first_turn(thread: dict[str, Any]) -> dict[str, Any]:
    done = [t for t in thread["turns"] if t["status"] == "done"]
    if not done:
        raise HTTPException(409, "That run has no finished answer to compare.")
    return done[0]


LEAD_CHARS = 2000


def _trim(text: str) -> str:
    """Cut at a paragraph or sentence boundary, so Markdown is never left half-open."""
    if len(text) <= LEAD_CHARS:
        return text
    head = text[:LEAD_CHARS]
    for boundary in ("\n\n", ". ", "\n"):
        cut = head.rfind(boundary)
        if cut > LEAD_CHARS // 2:
            return head[:cut + (1 if boundary == ". " else 0)].rstrip() + "\n\n…"
    return head.rstrip() + "…"


def lead_section(report: str) -> str:
    """The report's opening section — its bottom line — for a side-by-side view."""
    body = export.split_title({"report": report, "question": ""})[1]
    parts = re.split(r"^## ", body, flags=re.M)
    for part in parts[1:]:
        text = part.split("\n", 1)[1].strip() if "\n" in part else ""
        if len(text) > 120:
            return _trim(text)
    return _trim(body)


@app.post("/api/threads/{thread_id}/rerun")
async def rerun(thread_id: str) -> dict[str, str]:
    """Ask the same question again in a fresh session, for comparison."""
    source = load_thread(thread_id)
    turn = first_turn(source)
    # compare_of is set as the thread is created: the run task holds its own copy of the
    # thread and would overwrite a field written after it started.
    return await start_research(ResearchRequest(
        question=turn["question"], mode=turn["mode"], hypotheses=turn["hypotheses"],
        audit=bool(turn.get("audit")), year_from=turn.get("year_from"), year_to=turn.get("year_to"),
        compare_of=thread_id,
    ))


@app.get("/api/compare")
async def compare(a: str, b: str) -> dict[str, Any]:
    """What two runs of the same question found, and where their sources differ."""
    threads = {key: load_thread(ident) for key, ident in (("a", a), ("b", b))}
    runs = {}
    for key, thread in threads.items():
        turn = first_turn(thread)
        runs[key] = {
            "thread_id": thread["id"], "turn_id": turn["id"], "question": turn["question"],
            "engine": turn.get("engine") or "unrecorded", "mode": turn["mode"],
            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(turn["started"])),
            "sources": len(turn["references"]), "usage": turn.get("usage"),
            "lead": lead_section(turn["report"]),
            "audit_issues": len((turn.get("audit") or {}).get("issues") or []),
            "problems": len(turn.get("problems") or []),
        }

    def by_key(thread: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {r["key"]: r for r in first_turn(thread)["references"]}

    refs_a, refs_b = by_key(threads["a"]), by_key(threads["b"])
    shared = sorted(set(refs_a) & set(refs_b))
    union = set(refs_a) | set(refs_b)

    def listing(refs: dict[str, dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
        return [{"key": k, "title": refs[k]["title"], "year": refs[k]["year"],
                 "design": refs[k].get("design"), "url": refs[k]["url"]} for k in keys]

    return {
        "same_question": runs["a"]["question"].strip() == runs["b"]["question"].strip(),
        "runs": runs,
        "sources": {
            "shared": len(shared),
            "overlap": round(len(shared) / len(union), 3) if union else 0.0,
            "only_a": listing(refs_a, sorted(set(refs_a) - set(refs_b))),
            "only_b": listing(refs_b, sorted(set(refs_b) - set(refs_a))),
            "shared_sample": listing(refs_a, shared[:12]),
        },
    }


@app.post("/api/compare/summary")
async def compare_summary(a: str, b: str) -> dict[str, Any]:
    """A model pass over both reports: agreements, real disagreements, coverage gaps."""
    pair = []
    for ident in (a, b):
        thread = load_thread(ident)
        turn = first_turn(thread)
        pair.append({"engine": turn.get("engine") or "unrecorded",
                     "date": time.strftime("%Y-%m-%d", time.localtime(turn["started"])),
                     "sources": len(turn["references"]), "report": turn["report"]})
    try:
        out = await agent.compare(pair[0], pair[1])
    except agent.ResearchError as exc:
        raise HTTPException(503, str(exc)) from exc
    out["real_cost"] = await bill("compare", out.get("usage") or {})
    return out


ENTITY_CACHE_VERSION = 3


@app.get("/api/threads/{thread_id}/entities")
async def thread_entities(thread_id: str) -> dict[str, Any]:
    """Genes, drugs and variants named in the papers this thread retrieved."""
    thread = load_thread(thread_id)
    cached = thread.get("entities")
    if cached and cached.get("version") == ENTITY_CACHE_VERSION:
        return cached
    pmids = [r["pmid"] for r in (thread.get("registry") or {}).get("records", []) if r.get("pmid")]
    try:
        built = await entities.build(pmids)
    except SourceError as exc:
        raise HTTPException(503, f"Entity lookup failed: {exc}") from exc
    built["version"] = ENTITY_CACHE_VERSION
    thread["entities"] = built
    save_thread(thread)
    return built


@app.get("/api/engine/check")
async def engine_check() -> dict[str, Any]:
    """Ask the configured model one trivial question, so a provider setup can be verified."""
    return await agent.check_engine()


@app.get("/api/usage")
async def usage_summary() -> dict[str, Any]:
    """Spend for this hour, today, since the last reset, and all time."""
    engine = agent.engine()
    price = await usage.price_for(engine["model"], engine["base_url"], engine["third_party"])
    return {
        **usage.summary(),
        "engine": engine["model"],
        "price": None if price is None else {
            "input": price.inp, "output": price.out,
            "cache_read": price.cache_read, "source": price.source,
        },
    }


@app.post("/api/usage/reset")
async def usage_reset() -> dict[str, Any]:
    """Start the running total again. The history is kept, so daily figures survive."""
    return {"reset_at": usage.reset()}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {**agent.engine(), "skills": agent.skill_names(),
            "sources": ["PubMed", "Europe PMC", "OpenAlex", "ClinicalTrials.gov"]}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
