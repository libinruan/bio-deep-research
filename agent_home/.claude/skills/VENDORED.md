# Vendored skills

The skills below are copied unmodified from
[K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills)
(MIT, see `K-DENSE-LICENSE.md`), pinned at commit
`154988403bb5a18e9d3c0ce4e6d5e2e4b184a298` (2026-10-03):

literature-review, paper-lookup, database-lookup, citation-management,
hypothesis-generation, scientific-brainstorming, scientific-critical-thinking

They are not auto-updated. To update, re-copy from a reviewed commit and change the hash above.
The research agent can read these skills but has no shell, so their bundled scripts never
run inside the app; retrieval goes through the app's own tools in `app/tools.py`.

`biomedical-search-strategy` is this project's own skill.
