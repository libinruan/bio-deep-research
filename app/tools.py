"""The research agent's tools, served to it as an in-process MCP server.

One server is built per research thread so every tool writes into that thread's registry.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import sources, strategy
from .registry import Registry

SERVER = "bio"
Emit = Callable[[dict[str, Any]], None]

_YEARS = {
    "year_from": {"type": "integer", "description": "Earliest publication year (inclusive)."},
    "year_to": {"type": "integer", "description": "Latest publication year (inclusive)."},
}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _format(rec: dict[str, Any], abstract_chars: int = 3500) -> str:
    authors = rec.get("authors") or []
    who = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    tags = [t for t in (
        rec.get("design"),
        "PREPRINT (not peer reviewed)" if rec.get("preprint") else "",
        "RETRACTED" if rec.get("retracted") else "",
        "/".join(rec.get("species") or []),
        f"cited by {rec['cited_by']}" if rec.get("cited_by") is not None else "",
        f"full text available ({rec['pmcid']})" if rec.get("pmcid") and rec.get("open_access") is not False else "",
    ) if t]
    abstract = rec.get("abstract") or "(no abstract available)"
    if len(abstract) > abstract_chars:
        abstract = abstract[:abstract_chars] + " …[truncated]"
    return (
        f"### [{rec['key']}] {rec.get('title', '')}\n"
        f"{who} — {rec.get('journal', '')} {rec.get('year') or ''}\n"
        f"{' | '.join(tags)}\n{abstract}"
    )


def build_server(registry: Registry, emit: Emit):
    """Returns (mcp server config, list of fully qualified tool names)."""

    def ok(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    def fail(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    def guarded(label: str, fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]):
        """Report the call to the UI and turn upstream failures into tool errors the agent can read."""

        async def run(args: dict[str, Any]) -> dict[str, Any]:
            emit({"type": "tool", "name": label, "input": args})
            try:
                return await fn(args)
            except sources.SourceError as exc:
                emit({"type": "tool_result", "name": label, "summary": str(exc), "is_error": True})
                return fail(f"{exc}. Try again, simplify the query, or use another source.")

        return run

    def results(label: str, source: str, query: str, total: int, recs: list[dict[str, Any]], extra: str = "") -> dict[str, Any]:
        stored = [registry.add(r) for r in recs]
        registry.log_search(source=source, query=query, total=total, returned=len(stored))
        emit({"type": "tool_result", "name": label, "summary": f"{total:,} hits, {len(stored)} retrieved",
              "records": [{"key": r["key"], "title": r["title"], "year": r["year"]} for r in stored]})
        if not stored:
            return ok(f"{source}: 0 results for this query.{extra} Broaden the terms or drop a concept.")
        head = f"{source}: {total:,} total hits; showing {len(stored)}.{extra} Cite a record by the identifier in its heading.\n\n"
        return ok(head + "\n\n".join(_format(r) for r in stored))

    def cap(args: dict[str, Any], default: int, ceiling: int) -> int:
        return max(1, min(int(args.get("max_results") or default), ceiling))

    # ------------------------------------------------------------------ searches

    async def search_pubmed(args):
        total, recs, translation = await sources.pubmed_search(
            args["query"], cap(args, 20, 50), args.get("sort", "relevance"), args.get("year_from"), args.get("year_to"))
        note = f" PubMed interpreted the query as: {translation}" if translation else ""
        return results("search_pubmed", "PubMed", args["query"], total, recs, note)

    async def search_europe_pmc(args):
        total, recs = await sources.europepmc_search(
            args["query"], cap(args, 20, 50), args.get("sort", "relevance"),
            args.get("year_from"), args.get("year_to"), bool(args.get("preprints_only")))
        return results("search_europe_pmc", "Europe PMC", args["query"], total, recs)

    async def search_openalex(args):
        total, recs = await sources.openalex_search(
            args["query"], cap(args, 20, 50), args.get("sort", "relevance"), args.get("year_from"), args.get("year_to"))
        return results("search_openalex", "OpenAlex", args["query"], total, recs)

    async def search_clinical_trials(args):
        total, recs = await sources.trials_search(args["query"], cap(args, 15, 40), args.get("status"))
        return results("search_clinical_trials", "ClinicalTrials.gov", args["query"], total, recs)

    async def get_citing_papers(args):
        pmid = str(args["pmid"]).replace("PMID:", "").strip()
        total, recs = await sources.europepmc_citing(pmid, cap(args, 20, 50))
        return results("get_citing_papers", "Europe PMC citations", f"CITES:{pmid}", total, recs)

    async def get_paper_details(args):
        recs = await sources.pubmed_fetch([str(p).replace("PMID:", "") for p in args["pmids"]][:50])
        stored = [registry.add(r) for r in recs]
        emit({"type": "tool_result", "name": "get_paper_details", "summary": f"{len(stored)} records"})
        if not stored:
            return ok("None of those PMIDs exist in PubMed.")
        return ok("\n\n".join(_format(r, abstract_chars=6000) for r in stored))

    async def get_full_text(args):
        ident = str(args["id"]).strip()
        rec = registry.get(ident)
        pmcid = ident if ident.upper().startswith("PMC") else (rec or {}).get("pmcid")
        if not pmcid and ident.replace("PMID:", "").isdigit():
            pmcid = await sources.resolve_pmcid(ident.replace("PMID:", ""))
        if not pmcid:
            emit({"type": "tool_result", "name": "get_full_text", "summary": "no open-access full text"})
            return ok(f"No PubMed Central full text is available for {ident}. Rely on the abstract and say so.")
        sections = await sources.europepmc_fulltext(pmcid)
        if not sections:
            emit({"type": "tool_result", "name": "get_full_text", "summary": "no open-access full text"})
            return ok(f"{pmcid} is not in the open-access subset, so its full text cannot be retrieved. Rely on the abstract and say so.")
        wanted = [s.lower() for s in (args.get("sections") or [])]
        chosen = [(t, x) for t, x in sections if not wanted or any(w in t.lower() for w in wanted)]
        if not chosen:
            titles = ", ".join(t for t, _ in sections)
            return ok(f"No section matched {wanted}. Available sections: {titles}")
        budget = max(2000, min(int(args.get("max_chars") or 30000), 80000))
        out, used = [], 0
        for title, text in chosen:
            room = budget - used
            if room <= 0:
                out.append(f"## {title}\n[omitted: character budget reached; request this section by name]")
                continue
            out.append(f"## {title}\n{text[:room]}" + (" …[truncated]" if len(text) > room else ""))
            used += min(len(text), room)
        if rec is not None:
            rec["full_text_read"] = True
        emit({"type": "tool_result", "name": "get_full_text", "summary": f"{pmcid}: {len(chosen)} sections, {used:,} chars"})
        cite = rec["key"] if rec else ident
        return ok(f"Full text of [{cite}] ({pmcid}). Sections available: {', '.join(t for t, _ in sections)}\n\n" + "\n\n".join(out))

    # ------------------------------------------------------------------ strategy

    async def record_search_strategy(args):
        rendered = strategy.render(args["concepts"], args.get("year_from"), args.get("year_to"))
        if not rendered[0]["query"]:
            return fail("No usable concepts: each concept needs at least one term or MeSH heading.")

        async def count(entry):
            try:
                if entry["database"] == "PubMed":
                    entry["hits"], _, _ = await sources.pubmed_search(entry["query"], max_results=0)
                elif entry["database"] == "Europe PMC":
                    entry["hits"], _ = await sources.europepmc_search(entry["query"], max_results=1)
            except sources.SourceError as exc:
                entry["error"] = str(exc)

        await asyncio.gather(*(count(e) for e in rendered if e["live"]))
        registry.strategy = {
            "framework": args.get("framework") or {},
            "concepts": args["concepts"],
            "year_from": args.get("year_from"), "year_to": args.get("year_to"),
            "databases": rendered,
        }
        emit({"type": "strategy", "strategy": registry.strategy})
        emit({"type": "tool_result", "name": "record_search_strategy", "summary": f"{len(rendered)} database queries built"})
        lines = []
        for e in rendered:
            hits = f" — {e['hits']:,} hits" if "hits" in e else (f" — ERROR: {e['error']}" if "error" in e else "")
            lines.append(f"{e['database']}{hits}:\n{e['query']}")
        return ok(
            "Search strategy recorded and shown to the user. PubMed and Europe PMC can be run with the search tools; "
            "the others are for the user to paste into subscription databases.\n"
            "If a hit count is 0 or implausibly large, revise the concepts and call this tool again.\n\n" + "\n\n".join(lines)
        )

    # ------------------------------------------------------------------ registration

    sort_rel_date = {"type": "string", "enum": ["relevance", "date"], "description": "Default relevance."}
    sort_all = {"type": "string", "enum": ["relevance", "date", "cited"], "description": "Default relevance. 'cited' ranks by citation count."}
    n = lambda d, c: {"type": "integer", "description": f"Default {d}, maximum {c}."}  # noqa: E731

    defs = [
        ("search_pubmed",
         "Search PubMed (MEDLINE). Accepts full PubMed syntax: field tags such as [Title/Abstract], [MeSH Terms], [Publication Type], "
         "boolean AND/OR/NOT, and phrases in quotes. Returns abstracts with study design, species, preprint and retraction flags. "
         "The best source for clinical and peer-reviewed biomedical literature.",
         _schema({"query": {"type": "string"}, "max_results": n(20, 50), "sort": sort_rel_date, **_YEARS}, ["query"]),
         search_pubmed),
        ("search_europe_pmc",
         "Search Europe PMC: PubMed plus PubMed Central full text, bioRxiv/medRxiv preprints, patents and guidelines. Plain keywords "
         "search the full text of open-access papers, so it finds details that are not in abstracts. Supports AND/OR/NOT, quoted "
         "phrases and fields such as TITLE:, ABSTRACT:, AUTH:. Returns citation counts. Set preprints_only to see the newest "
         "unreviewed work. With sort='cited', keep the query specific or generic highly cited papers will dominate.",
         _schema({"query": {"type": "string"}, "max_results": n(20, 50), "sort": sort_all, **_YEARS,
                  "preprints_only": {"type": "boolean"}}, ["query"]),
         search_europe_pmc),
        ("search_openalex",
         "Search OpenAlex, a cross-disciplinary index of over 250 million works. Takes natural-language keywords (no field tags). "
         "Use it for topics on the edge of biomedicine (engineering, chemistry, computation, social science) and for finding the "
         "most-cited work on a topic with sort='cited'.",
         _schema({"query": {"type": "string"}, "max_results": n(20, 50), "sort": sort_all, **_YEARS}, ["query"]),
         search_openalex),
        ("search_clinical_trials",
         "Search ClinicalTrials.gov for registered studies: status, phase, enrollment, interventions, primary outcomes, and whether "
         "results are posted. Use it to see what is being tested now and which trials have not reported.",
         _schema({"query": {"type": "string"}, "max_results": n(15, 40),
                  "status": {"type": "array", "items": {"type": "string", "enum": [
                      "RECRUITING", "NOT_YET_RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED"]}}},
                 ["query"]),
         search_clinical_trials),
        ("get_citing_papers",
         "List papers that cite a given PubMed article, most cited first. Use it to follow a key finding forward in time: "
         "replications, contradictions, and later reviews.",
         _schema({"pmid": {"type": "string"}, "max_results": n(20, 50)}, ["pmid"]),
         get_citing_papers),
        ("get_paper_details",
         "Fetch full PubMed records (untruncated abstract, publication types, retraction status) for specific PMIDs. Use it to "
         "check a paper you know of but have not retrieved, before citing it.",
         _schema({"pmids": {"type": "array", "items": {"type": "string"}}}, ["pmids"]),
         get_paper_details),
        ("get_full_text",
         "Read the open-access full text of a paper from PubMed Central, by PMID or PMCID. Use it for the papers your conclusions "
         "rest on: effect sizes, methods, limitations and subgroup results are often missing from abstracts. Pass `sections` "
         "(for example ['results', 'discussion']) to read only part of a long paper. Many papers are not open access; the tool says so.",
         _schema({"id": {"type": "string", "description": "PMID or PMCID."},
                  "sections": {"type": "array", "items": {"type": "string"}, "description": "Case-insensitive substrings of section titles."},
                  "max_chars": {"type": "integer", "description": "Default 30000, maximum 80000."}}, ["id"]),
         get_full_text),
        ("record_search_strategy",
         "Record the formal search strategy and render it into exact query syntax for PubMed, Europe PMC, Web of Science, Scopus, "
         "Embase, Cochrane, CNKI and Wanfang. Give one concept per element of the question (for example population, intervention, "
         "outcome); terms within a concept are combined with OR and concepts with AND. Returns live hit counts for PubMed and "
         "Europe PMC so you can check the strategy is neither too narrow nor too broad. The user sees the result and can paste "
         "the other queries into databases this app cannot reach.",
         _schema({
             "framework": {"type": "object", "description": "The structured question, e.g. PICO: {population, intervention, comparison, outcome}, or PEO / SPIDER fields. Free-form string values.",
                           "additionalProperties": {"type": "string"}},
             "concepts": {"type": "array", "items": _schema({
                 "label": {"type": "string", "description": "What this concept block represents, e.g. 'Population: heart failure'."},
                 "terms": {"type": "array", "items": {"type": "string"}, "description": "English free-text synonyms, abbreviations and spelling variants. Use * for truncation."},
                 "mesh": {"type": "array", "items": {"type": "string"}, "description": "Exact MeSH headings."},
                 "terms_zh": {"type": "array", "items": {"type": "string"}, "description": "Chinese terms and synonyms, for CNKI and Wanfang."},
             }, ["label", "terms"])},
             **_YEARS,
         }, ["concepts"]),
         record_search_strategy),
    ]

    tools = [tool(name, desc, schema)(guarded(name, fn)) for name, desc, schema, fn in defs]
    server = create_sdk_mcp_server(name=SERVER, version="0.1.0", tools=tools)
    return server, [f"mcp__{SERVER}__{name}" for name, *_ in defs]
