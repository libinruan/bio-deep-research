# Bio Deep Research

A local search engine for biomedical research questions. You ask a question; an agent
builds a search strategy, searches the literature, reads abstracts and open-access full
text, and writes an answer in which every citation has been checked against the database
record it came from. Deep mode adds a structured evidence report, new hypotheses with
testable predictions, and an independent audit of each claim against its source.

Three things to know before you start:

- It runs **entirely on your machine** — your questions and reports never leave it, except
  as queries to the literature APIs.
- It **costs real money per run**, because it drives a commercial model. See
  [What it costs](#what-it-costs).
- It listens on **localhost with no login**. See [Running it safely](#running-it-safely).

## What it does

| Mode | What happens | Typical time |
|---|---|---|
| Quick answer | A few targeted searches, a short sourced answer | under a minute |
| Deep research | Formal search strategy, multi-database search, citation chasing, full-text reading, structured report | several minutes |
| + Propose new hypotheses | Adds hypotheses with reasoning, predictions, and the most direct test | |
| + Audit claims | A second, independent pass checks each cited claim against the source record | |

Follow-up questions in a thread reuse everything already retrieved.

Saved searches are listed in the sidebar. Hover one for a **×** to delete it, which asks once
before it goes. **Select** turns on checkboxes for clearing several at once, with **All** to
take the lot; a search with a run still going is kept and named rather than deleted.

## Requirements

| | Why |
|---|---|
| Linux or macOS | Developed on Linux. macOS should work; Windows is untested. |
| [uv](https://docs.astral.sh/uv/) | Builds the environment and fetches Python 3.12 if you don't have it. One binary: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| A model — either [Claude Code](https://code.claude.com/docs) installed and logged in, **or** an API key for any Anthropic-compatible endpoint | The agent runs on the Claude Agent SDK, which launches the `claude` CLI as a subprocess. See [Choosing a model engine](#choosing-a-model-engine) for the key-based route. |
| Outbound HTTPS | PubMed and PubTator3 (NCBI), Europe PMC (EBI), OpenAlex, ClinicalTrials.gov. Every search is live; there is no offline cache. |
| Chrome or Chromium — *optional* | Only for **PDF** export. Markdown, HTML, BibTeX and RIS need nothing extra. |

If you take the Claude Code route, the `claude` binary must be on your `PATH` — the SDK finds
it with `which claude`. The app still starts without it and fails on the first run instead, so
check it beforehand rather than wondering what broke.

## Install

```bash
git clone https://github.com/libinruan/bio-deep-research.git
cd bio-deep-research
cp .env.example .env
```

There is no install step: `./run.sh` builds the environment from `uv.lock` the first time it
runs, fetching Python 3.12 if your system lacks it. It lands in `./.venv` *inside* the
checkout, so nothing is activated and uninstalling is `rm -rf` on the directory. Every
dependency is pinned by `uv.lock`, down to transitive ones, so you get the same versions
this was tested against.

Every setting in `.env` is optional, but set this one:

```bash
BDR_CONTACT_EMAIL=you@example.org
```

NCBI's usage policy expects a contact address on automated traffic, and OpenAlex routes
requests carrying one into a faster pool. It goes to those two services and nowhere else.
`.env` is sourced by bash, so quote any value containing spaces, `#` or `$`.

To run a one-off command in the project environment, use `uv run python …` — or
`uv sync` to build it without starting the server.

## Choosing a model engine

By default the app uses Claude through your Claude Code login, and no key is needed. To use
another provider instead, point it at that provider's **Anthropic-compatible** endpoint:

```bash
# OpenRouter — one key, hundreds of models, no per-region setup
BDR_BASE_URL=https://openrouter.ai/api
BDR_MODEL=z-ai/glm-5.3
BDR_AUTH_TOKEN=sk-or-...

# or a provider directly
BDR_BASE_URL=https://api.z.ai/api/anthropic   # or https://api.moonshot.ai/anthropic
BDR_MODEL=glm-5.2                             # or kimi-k3[1m]
BDR_AUTH_TOKEN=...
```

`BDR_BASE_URL` is the endpoint's base, with **no `/v1`** on the end — the harness appends
`/v1/messages` itself. A URL ending in `/v1`, or in a provider's OpenAI-compatible path such
as `/compatible-mode/v1`, is the wrong one: that is a different protocol, not a different
spelling. **Test connection** in the sidebar names the mistake if you make it.

Setting `BDR_BASE_URL` changes four things automatically: every model tier is pinned to
`BDR_MODEL` (background work otherwise asks the endpoint for a Claude model it does not
serve), an empty `ANTHROPIC_API_KEY` is sent so a Claude Code login on the same machine cannot
quietly take precedence and bill the wrong account, the Claude-only `effort` setting is
dropped, and the audit asks for JSON in the prompt rather than relying on enforced schemas.
The sidebar footer shows the active engine.

Verified end to end on OpenRouter with `z-ai/glm-5.3`: routing, quick mode, the literature
tools, citation verification, the audit and exports all work. Deep-mode report quality on a
non-Claude engine has not been benchmarked — use **Run again** and the comparison view to
judge that on your own questions.

## First run

```bash
./run.sh          # then open http://127.0.0.1:8790
```

1. The sidebar footer names the active engine. Press **Test connection** — it asks the
   endpoint directly, before the harness is involved, so a wrong key or address comes back in
   about a second carrying the provider's own message rather than as a timeout.
2. Ask something small in **Quick answer** mode. It finishes in under a minute and costs
   cents. You should get a report with numbered citations and a populated Sources panel.
3. Only then try **Deep research**. Read the next section first.

<kbd>Ctrl</kbd>+<kbd>C</kbd> stops the server. Saved searches persist in `data/threads/` and
come back on restart.

## What it costs

The app is free; the model is not. Every run bills whichever engine you configured. Measured
on this project:

| Run | Engine | Rough cost |
|---|---|---|
| Quick answer | `z-ai/glm-5.3` via OpenRouter | ~US$0.03 |
| Deep research | `claude-opus-5-5` | ~US$5 |
| Deep research + **Audit claims** | `claude-opus-5-5` | ~US$7 |

All figures in this README and in the app are **US dollars**, the currency providers bill in.

Deep mode makes 30 or more tool calls across many turns with a long context, and the audit
re-reads every cited abstract; those two options dominate the bill. Treat the figures as an
order of magnitude, not a quote — cost scales with how much literature a question pulls in.
On a Claude subscription, runs draw against your usage limits rather than a card.

### Tracking what you actually spend

The **Spend** panel in the sidebar totals this hour, today, since your last reset, and all
time, with a bar per day for the last fortnight. **Reset** restarts the running total and
keeps the history, so the daily figures survive it.

Figures are recomputed from token counts and your provider's own published prices, because
the harness reports every run at Anthropic's rates whatever endpoint was used — on one
measured GLM-5.3 search it said US$0.195 for work that actually cost US$0.0077. Prices come from
OpenRouter's live list; for a provider with no machine-readable prices, set `BDR_PRICE_IN`
and `BDR_PRICE_OUT` in `.env` (US dollars per million tokens) and those are used instead. On a
Claude login the harness's own figure is correct and is used as-is.

These are estimates from token counts, not billed amounts — check your provider's dashboard
for the authoritative number. Every call is logged to `data/usage.jsonl`, which is gitignored
along with the rest of `data/`.

## Running it safely

**There is no login. Anyone who can reach the port can run searches that bill your account
and read every report you have saved.** The server binds to `127.0.0.1` for that reason.

- Don't change the bind address, don't put it behind a tunnel or port-forward, and don't run
  it where other people can reach loopback. Adding authentication is out of scope.
- To use it from another machine, forward the port over SSH:
  `ssh -L 8790:127.0.0.1:8790 you@host`. Authentication then happens at the SSH layer and the
  app is untouched.
- `data/threads/*.json` holds the full text of every report and every abstract retrieved, in
  plaintext. It is gitignored, but not protected or encrypted. Delete the directory to wipe
  your history.
- Model credentials live in `.env`, which is gitignored. Keep it that way.

## Using it

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

Clicking a citation walks the Sources panel to that reference, switching to the Sources tab
if another is open and outlining the entry. Only the panel moves, so the sentence being read
stays put. It also opens **Where this comes from**: the claim it is attached to, the source
it points at, and the passages of that source closest to the claim, with the shared wording
highlighted. The match is lexical, weighted towards shared numbers — effect sizes, sample
sizes and percentages — so it points at a passage rather than judging that the passage
supports the claim. The audit pass is what judges that, and anything it flagged for that
citation is shown alongside. When nothing in the abstract shares wording, it says so.

Every answer exports from the bar at its head or foot: **PDF** and **HTML** (citations
become superscripts that jump to the reference list, which links on to each paper),
**Markdown** (each citation links straight to the paper), and **BibTeX** / **RIS** for a
reference manager. PDF export needs Chrome or Chromium; the other four formats need nothing.

## Sources

Searched live: **PubMed**, **Europe PMC** (including bioRxiv/medRxiv preprints and
open-access full text), **OpenAlex**, and **ClinicalTrials.gov**.

**Web of Science, Scopus, Embase, CNKI (知网) and Wanfang (万方)** are subscription
services with no open API, so the app cannot search them. Instead, deep research records a
formal strategy (concept blocks with synonyms, MeSH headings and Chinese terms) and renders
it into the exact query syntax for each of those databases, under the **Search strategy**
tab, ready to paste into the site through your institutional access.

## How citations are kept honest

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

## Limitations

- **Open-access bias.** Only the four sources above are searched. Full-text reading covers
  open-access articles only; everything else is abstract-only.
- **The audit reads abstracts.** It cannot check a figure that appears only in a paper's
  methods or supplement.
- **Passage matching is lexical, not semantic.** "Where this comes from" points at the
  nearest-wording passage; it does not judge that the passage supports the claim.
- **Third-party engines are best-effort.** Anthropic does not support routing this harness to
  non-Claude models. Quick mode is verified; deep-mode quality is not benchmarked.
- **On macOS, PDF export** looks for Chrome in `/Applications` as well as on `PATH`; if you
  installed it elsewhere, that one export will report it missing.
- **One process, one person.** No job queue, no multi-user support, no storage beyond JSON
  files on disk.
- **Not medical advice.** A tool for reading literature, nothing else.

## Layout

```
app/
  sources.py    clients for PubMed, Europe PMC, OpenAlex, ClinicalTrials.gov
  registry.py   evidence registry, citation verification and numbering
  strategy.py   renders concept blocks into per-database query syntax
  entities.py   gene/drug/variant marking via PubTator3, verified against NCBI
  tools.py      the agent's tools (in-process MCP server)
  prompts.py    system prompt, mode instructions, audit and comparison prompts
  agent.py      Claude Agent SDK runner: research turn, audit, comparison
  export.py     Markdown, HTML, PDF, BibTeX and RIS output
  server.py     FastAPI server: runs, live event stream, threads, exports
  usage.py      per-call cost accounting, priced from the provider's own rates
  static/       the web UI (vendor/ holds marked and DOMPurify)
agent_home/.claude/skills/   skills the agent can load
data/threads/                saved searches (JSON, gitignored)
```

## Skills

The agent loads skills on demand. Seven come from
[K-Dense scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills)
(MIT — see `agent_home/.claude/skills/K-DENSE-LICENSE.md`), pinned to a reviewed commit:
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
- Reports and retrieved abstracts are stored unencrypted under `data/threads/`.
- This is a research tool. It does not give medical advice.

## Licence and attribution

MIT — see [LICENSE](LICENSE).

Bundled third-party code, each under its own licence, is listed in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md):

- seven agent skills from [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills) (MIT)
- [marked](https://github.com/markedjs/marked) (MIT) and
  [DOMPurify](https://github.com/cure53/DOMPurify) (Apache-2.0 / MPL-2.0) in `app/static/vendor/`

Literature records come from NCBI/PubMed, Europe PMC, OpenAlex and ClinicalTrials.gov at
query time. That content is not part of this project and is governed by each provider's own
terms of use.
