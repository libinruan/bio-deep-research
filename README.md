# Bio Deep Research

A local search engine for biomedical research questions. You ask a question; an agent
builds a search strategy, searches the literature, reads abstracts and open-access full
text, and writes an answer in which every citation has been checked against the database
record it came from. Deep mode adds a structured evidence report, new hypotheses with
testable predictions, and an independent audit of each claim against its source.

## Run it

```bash
./run.sh          # then open http://127.0.0.1:8790
```

The agent runs on the Claude Agent SDK and uses your existing Claude Code login, so no API
key is needed. If you are not logged in, run `claude` once and log in. Optional settings
are in `.env.example`.

To rebuild the environment from scratch: `conda env create -p ./.conda -f environment.yml`.

## What it does

| Mode | What happens | Typical time |
|---|---|---|
| Quick answer | A few targeted searches, a short sourced answer | under a minute |
| Deep research | Formal search strategy, multi-database search, citation chasing, full-text reading, structured report | several minutes |
| + Propose new hypotheses | Adds hypotheses with reasoning, predictions, and the most direct test | |
| + Audit claims | A second, independent pass checks each cited claim against the source record | |

Follow-up questions in a thread reuse everything already retrieved.

### Sources

Searched live: **PubMed**, **Europe PMC** (including bioRxiv/medRxiv preprints and
open-access full text), **OpenAlex**, and **ClinicalTrials.gov**.

**Web of Science, Scopus, Embase, CNKI (知网) and Wanfang (万方)** are subscription
services with no open API, so the app cannot search them. Instead, deep research records a
formal strategy (concept blocks with synonyms, MeSH headings and Chinese terms) and renders
it into the exact query syntax for each of those databases, under the **Search strategy**
tab, ready to paste into the site through your institutional access.

### How citations are kept honest

1. Every record a tool returns goes into a per-thread evidence registry (`app/registry.py`).
2. The agent cites by identifier: `[PMID:…]`, `[DOI:…]`, `[NCT…]`.
3. After the report is written, each identifier is checked against the registry. The
   reference list is built from database metadata, never from model output.
4. An identifier that no tool returned is looked up. If it exists it is marked "not
   retrieved in this search"; if it does not exist it is shown as **unverified**.
5. Retracted papers and preprints are flagged on the source card and in a banner.
6. The optional audit compares each claim with the abstract of its source and flags claims
   that are contradicted, overstated, numerically different, or off topic.

The audit reads abstracts, so it cannot confirm details that appear only in a paper's full
text. It reduces citation errors; it does not replace reading the key papers.

## Layout

```
app/
  sources.py    clients for PubMed, Europe PMC, OpenAlex, ClinicalTrials.gov
  registry.py   evidence registry, citation verification and numbering
  strategy.py   renders concept blocks into per-database query syntax
  tools.py      the agent's tools (in-process MCP server)
  prompts.py    system prompt, mode instructions, audit prompt
  agent.py      Claude Agent SDK runner: research turn and audit pass
  server.py     FastAPI server: runs, live event stream, threads, exports
  static/       the web UI
agent_home/.claude/skills/   skills the agent can load
data/threads/                saved searches (JSON)
```

## Skills

The agent loads skills on demand. Seven come from
[K-Dense scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills)
(MIT), pinned to a reviewed commit (see `agent_home/.claude/skills/VENDORED.md`):
literature-review, paper-lookup, database-lookup, citation-management,
hypothesis-generation, scientific-brainstorming, scientific-critical-thinking.
`biomedical-search-strategy` is this project's own.

To add another skill, copy its folder into `agent_home/.claude/skills/` and restart.

## Safety boundaries

- The agent has no shell, no file writing, and no web browsing. Its only tools are the
  literature tools, skill loading, and reading files inside the skills folder (enforced by
  a hook in `app/agent.py`). Skill scripts therefore never execute.
- Retrieved paper text is treated as data, and there is nothing for injected instructions
  to act on beyond running more literature searches.
- The server binds to 127.0.0.1 and has no authentication. Do not expose it to a network.
- This is a research tool. It does not give medical advice.
