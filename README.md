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

### Using another model provider

The app talks to its model through an Anthropic-compatible endpoint, so providers that offer
one — Kimi (Moonshot), GLM (Z.ai), or a local translation proxy in front of an OpenAI-only
model — can be used instead of Claude. Set three variables in `.env`:

```bash
# OpenRouter — one key, hundreds of models
BDR_BASE_URL=https://openrouter.ai/api
BDR_MODEL=z-ai/glm-5.3
BDR_AUTH_TOKEN=sk-or-...

# or another provider directly
BDR_BASE_URL=https://api.z.ai/api/anthropic   # or https://api.moonshot.ai/anthropic
BDR_MODEL=glm-5.2                             # or kimi-k3[1m]
BDR_AUTH_TOKEN=...
```

`BDR_BASE_URL` is the endpoint's base, with no `/v1` on the end — the harness appends
`/v1/messages` itself. A URL that already ends in `/v1`, or in a provider's OpenAI-compatible
path such as `/compatible-mode/v1`, is the wrong one to put here.

After changing `.env`, restart the app and press **Test connection** in the sidebar. It asks
the endpoint directly before involving the harness, so a wrong key or address comes back in
about a second carrying the provider's own message, rather than as a timeout.

Setting `BDR_BASE_URL` also sends an empty `ANTHROPIC_API_KEY` to the harness. Without that,
a Claude Code login on the same machine can take precedence and the run quietly bills Claude
instead of the provider you configured.

Setting `BDR_BASE_URL` changes three things automatically: every model tier is pinned to
`BDR_MODEL` (background work otherwise asks the endpoint for a Claude model it does not
serve), the Claude-only `effort` setting is dropped, and the audit stops relying on enforced
JSON schemas, asking for JSON in the prompt instead. The sidebar footer shows the active engine.

Caveats worth knowing:

- Anthropic does not support routing this harness to non-Claude models, so treat it as
  best-effort. Skills, the literature tools, the read-confinement hook and citation
  verification are all client-side and keep working.
- Deep research is the demanding case: 30 or more tool calls across many turns with a long
  context. Re-check report quality after switching rather than assuming parity.
- This path is wired and the request routing is verified, but it has not been run end to end
  against a real third-party key.

## What it does

| Mode | What happens | Typical time |
|---|---|---|
| Quick answer | A few targeted searches, a short sourced answer | under a minute |
| Deep research | Formal search strategy, multi-database search, citation chasing, full-text reading, structured report | several minutes |
| + Propose new hypotheses | Adds hypotheses with reasoning, predictions, and the most direct test | |
| + Audit claims | A second, independent pass checks each cited claim against the source record | |

Follow-up questions in a thread reuse everything already retrieved.

### Comparing two runs

**Run again** at the foot of a finished answer asks the same question in a fresh session.
The comparison view then puts the two side by side: their engines, dates and costs, how
much their sources overlap, and which papers only one of them found. **Compare the
conclusions** adds a model pass that reports where they agree, where they genuinely
disagree, and what one covered that the other missed, marking each difference as
substantive, emphasis, or wording only.

This is the honest way to judge a model or a provider. Switching `BDR_BASE_URL` between runs
compares two engines on the same question; running the same question months apart shows what
the literature has added since.

### Reading and exporting an answer

A deep report runs to several thousand words, so the **Outline** tab beside Sources lists its
headings and bold paragraph lead-ins, filters them by name, and marks the section you are
reading. Press <kbd>O</kbd> to open it for whichever answer is in view and again to go back
to Sources, or <kbd>⌘K</kbd> / <kbd>Ctrl</kbd>+<kbd>K</kbd> to open it with the filter
focused.

Genes, drugs and variants named in the retrieved papers are marked in the report, and
hovering one shows what it resolves to and links to the reference record. The vocabulary is
not guessed from the text: it comes from PubTator3's annotation of the cited abstracts, and
every gene is checked against NCBI's own record before it is offered as a link. A dashed
underline means the mapping could not be corroborated, and the card says so. The checkbox in
the sidebar turns the marking off.

Clicking a citation opens **Where this comes from**: the claim it is attached to, the source
it points at, and the passages of that source closest to the claim, with the shared wording
highlighted. The match is lexical, weighted towards shared numbers — effect sizes, sample
sizes and percentages — so it points at a passage rather than judging that the passage
supports the claim. The audit pass is what judges that, and anything it flagged for that
citation is shown alongside. When nothing in the abstract shares wording, it says so.

Every answer exports from the bar at its head or foot: **PDF** and **HTML** (citations
become superscripts that jump to the reference list, which links on to each paper),
**Markdown** (each citation links straight to the paper), and **BibTeX** / **RIS** for a
reference manager. PDF export needs Chrome or Chromium installed.

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
