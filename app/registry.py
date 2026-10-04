"""Evidence registry: every record a tool returns during a research thread.

The registry is what makes citations trustworthy. The agent cites by identifier
(``[PMID:123]``, ``[DOI:10.x/y]``, ``[NCT01234567]``); ``finalize`` then checks each one
against what was actually retrieved, numbers them, and builds the reference list from
database metadata rather than from anything the model wrote.
"""

from __future__ import annotations

import re
from typing import Any

from . import sources

_ID = r"(?:PMID:\s*\d+|DOI:\s*10\.[^\s\];]+|NCT\d{8})"
CITATION_RE = re.compile(rf"\[({_ID}(?:\s*[;,]\s*{_ID})*)\]", re.I)
_SPLIT_RE = re.compile(r"\s*[;,]\s*(?=PMID:|DOI:|NCT\d)", re.I)

# First match wins, so order is by how much weight the design usually carries.
_DESIGNS = [
    ("meta-analysis", "Meta-analysis"),
    ("systematic review", "Systematic review"),
    ("practice guideline", "Guideline"),
    ("guideline", "Guideline"),
    ("randomized controlled trial", "RCT"),
    ("clinical trial registration", "Trial registration"),
    ("clinical trial", "Clinical trial"),
    ("observational study", "Observational"),
    ("comparative study", "Comparative study"),
    ("case reports", "Case report"),
    ("review", "Review"),
    ("editorial", "Editorial"),
    ("comment", "Comment"),
    ("letter", "Letter"),
    ("preprint", "Preprint"),
]


def design_of(rec: dict[str, Any]) -> str:
    types = " | ".join(rec.get("pub_types") or []).lower()
    for needle, label in _DESIGNS:
        if needle in types:
            return label
    return "Preprint" if rec.get("preprint") else ""


def canonical_key(rec: dict[str, Any]) -> str:
    if rec.get("pmid"):
        return f"PMID:{rec['pmid']}"
    if rec.get("nct"):
        return rec["nct"].upper()
    if rec.get("doi"):
        return f"DOI:{rec['doi']}"
    return f"PMCID:{rec.get('pmcid')}"


def _norm_token(token: str) -> str:
    token = token.strip()
    up = token.upper()
    if up.startswith("PMID:"):
        return "PMID:" + token[5:].strip()
    if up.startswith("DOI:"):
        return "DOI:" + token[4:].strip().lower().rstrip(".")
    if up.startswith("PMC"):
        return "PMCID:" + up.removeprefix("PMCID:")
    if up.isdigit():
        return "PMID:" + up
    return up


class Registry:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}  # canonical key -> record
        self._alias: dict[str, str] = {}              # any known identifier -> canonical key
        self.search_log: list[dict[str, Any]] = []
        self.strategy: dict[str, Any] | None = None

    # -- storage ------------------------------------------------------------

    def _aliases(self, rec: dict[str, Any]) -> list[str]:
        out = []
        if rec.get("pmid"):
            out.append(f"PMID:{rec['pmid']}")
        if rec.get("doi"):
            out.append(f"DOI:{rec['doi']}")
        if rec.get("nct"):
            out.append(rec["nct"].upper())
        if rec.get("pmcid"):
            out.append(f"PMCID:{rec['pmcid'].upper()}")
        return out

    def add(self, rec: dict[str, Any]) -> dict[str, Any]:
        """Insert or merge a record; returns the stored record (with its ``key``)."""
        existing_key = next((self._alias[a] for a in self._aliases(rec) if a in self._alias), None)
        if existing_key is None:
            rec = dict(rec)
            rec["key"] = canonical_key(rec)
            rec["design"] = design_of(rec)
            rec.setdefault("full_text_read", False)
            self.records[rec["key"]] = rec
            stored = rec
        else:
            stored = self.records[existing_key]
            for field in ("pmid", "pmcid", "doi", "nct", "title", "journal", "year", "url"):
                if not stored.get(field) and rec.get(field):
                    stored[field] = rec[field]
            if len(rec.get("abstract") or "") > len(stored.get("abstract") or ""):
                stored["abstract"] = rec["abstract"]
            if len(rec.get("authors") or []) > len(stored.get("authors") or []):
                stored["authors"] = rec["authors"]
            for field in ("pub_types", "species", "sources"):
                stored[field] = list(dict.fromkeys([*(stored.get(field) or []), *(rec.get(field) or [])]))
            if rec.get("cited_by") is not None:
                stored["cited_by"] = max(stored.get("cited_by") or 0, rec["cited_by"])
            if stored.get("open_access") is None:
                stored["open_access"] = rec.get("open_access")
            stored["retracted"] = bool(stored.get("retracted") or rec.get("retracted"))
            stored["preprint"] = bool(stored.get("preprint") and rec.get("preprint"))
            stored["design"] = design_of(stored)
        for alias in self._aliases(stored):
            self._alias.setdefault(alias, stored["key"])
        return stored

    def get(self, token: str) -> dict[str, Any] | None:
        key = self._alias.get(_norm_token(token))
        return self.records.get(key) if key else None

    def log_search(self, **entry: Any) -> None:
        self.search_log.append(entry)

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"records": list(self.records.values()), "search_log": self.search_log, "strategy": self.strategy}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Registry":
        reg = cls()
        for rec in data.get("records", []):
            reg.records[rec["key"]] = rec
            for alias in reg._aliases(rec):
                reg._alias.setdefault(alias, rec["key"])
        reg.search_log = data.get("search_log", [])
        reg.strategy = data.get("strategy")
        return reg

    # -- citation resolution ------------------------------------------------

    async def _resolve_unknown(self, token: str) -> dict[str, Any] | None:
        """The agent cited something no tool returned. Check whether it exists at all."""
        try:
            if token.startswith("PMID:"):
                found = await sources.pubmed_fetch([token[5:]])
            elif token.startswith("DOI:"):
                _, found = await sources.europepmc_search(f'DOI:"{token[4:]}"', max_results=1)
                found = [r for r in found if r.get("doi") == token[4:]]
            else:
                trial = await sources.trial_fetch(token)
                found = [trial] if trial else []
        except sources.SourceError:
            return None
        if not found:
            return None
        rec = self.add(found[0])
        rec["from_memory"] = True
        return rec

    async def finalize(self, report: str) -> dict[str, Any]:
        """Replace identifier citations with numbered links and build the reference list."""
        order: list[str] = []
        problems: list[dict[str, str]] = []
        resolved: dict[str, dict[str, Any] | None] = {}

        tokens = []
        for m in CITATION_RE.finditer(report):
            tokens.extend(_norm_token(t) for t in _SPLIT_RE.split(m.group(1)))
        for token in dict.fromkeys(tokens):
            rec = self.get(token) or await self._resolve_unknown(token)
            resolved[token] = rec
            if rec is None:
                problems.append({"citation": token, "problem": "Not found in any database. Treat the claim as unsupported."})
            else:
                if rec.get("from_memory"):
                    problems.append({"citation": token, "problem": "Cited from the model's memory rather than retrieved during this search. The paper exists, but the agent did not read it in this session."})
                if rec.get("retracted"):
                    problems.append({"citation": token, "problem": "This publication has been retracted."})

        def render(m: re.Match[str]) -> str:
            out = []
            for token in (_norm_token(t) for t in _SPLIT_RE.split(m.group(1))):
                rec = resolved.get(token)
                if rec is None:
                    out.append(f"**[unverified: {token}]**")
                    continue
                if rec["key"] not in order:
                    order.append(rec["key"])
                n = order.index(rec["key"]) + 1
                out.append(f"[[{n}]](#ref-{n})")
            return "".join(out)

        body = CITATION_RE.sub(render, report)
        references = [{**self.records[key], "n": i + 1} for i, key in enumerate(order)]
        return {"report": body, "references": references, "problems": problems}
