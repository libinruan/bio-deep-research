# Third-party notices

This project is MIT licensed (see `LICENSE`). It also redistributes the
third-party code listed below, each under its own licence. None of this is
fetched at install time — these files are committed in-tree, which is why their
notices travel with the repository.

## Agent skills — `agent_home/.claude/skills/`

Seven skills — literature-review, paper-lookup, database-lookup,
citation-management, hypothesis-generation, scientific-brainstorming and
scientific-critical-thinking — are copied unmodified from
[K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills),
pinned at commit `154988403bb5a18e9d3c0ce4e6d5e2e4b184a298`.

MIT, Copyright (c) 2025 K-Dense Inc. Full text:
[`agent_home/.claude/skills/K-DENSE-LICENSE.md`](agent_home/.claude/skills/K-DENSE-LICENSE.md).
Provenance and the update procedure:
[`agent_home/.claude/skills/VENDORED.md`](agent_home/.claude/skills/VENDORED.md).

`biomedical-search-strategy` is this project's own skill, MIT under `LICENSE`
like the rest of the repository.

## Browser libraries — `app/static/vendor/`

| File | Library | Version | Licence |
|---|---|---|---|
| `marked.min.js` | [marked](https://github.com/markedjs/marked) | 12.0.2 | MIT — [`LICENSE-marked.txt`](app/static/vendor/LICENSE-marked.txt) |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.1.6 | Apache-2.0 **or** MPL-2.0 — [`LICENSE-DOMPurify.txt`](app/static/vendor/LICENSE-DOMPurify.txt) |

Note that DOMPurify is **not** MIT: it is dual-licensed under Apache-2.0 and
MPL-2.0, and you may take it under either. Both files are unmodified upstream
minified builds. Details in
[`app/static/vendor/VENDORED.md`](app/static/vendor/VENDORED.md).

## Not covered here

Python packages declared in `pyproject.toml` and pinned in `uv.lock` —
claude-agent-sdk, FastAPI, uvicorn, httpx, pydantic and their dependencies —
are fetched at setup time rather than redistributed here, and carry their own
licences in the installed environment.

## Literature data

Records the app displays come from NCBI/PubMed, Europe PMC, OpenAlex and
ClinicalTrials.gov at query time. That content is not part of this project and
is governed by each provider's own terms of use.
