"""Genes, drugs, diseases and variants named in the papers a thread retrieved.

The vocabulary is not guessed from the report text. It comes from PubTator3, NCBI's
annotation of the very abstracts that were cited, so every highlighted term is one the
cited literature actually used, carrying a normalised identifier.

PubTator3 does make normalisation errors — it has resolved "MET" to NCBI gene 79811
(SLTM) — so gene mappings are checked against NCBI's own record for that gene before
being offered as a link.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import Any

from .sources import EUTILS, SourceError, _get, _ncbi_params, _ncbi_throttle

PUBTATOR = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api"

# Species annotations are mostly "patients" and "human": noise in a biomedical report.
WANTED_TYPES = ("Gene", "Chemical", "Disease", "Variant")

# Surface forms too generic to be worth marking up.
SKIP_FORMS = {
    "patient", "patients", "human", "humans", "men", "women", "mice", "mouse", "rat", "rats",
    "disease", "diseases", "disorder", "disorders", "syndrome", "death", "deaths", "toxicity",
    "pain", "drug", "drugs", "agent", "agents", "therapy", "treatment", "infection", "tumor",
    "tumour", "tumors", "tumours", "mutation", "mutations", "variant", "variants",
}

RS_RE = re.compile(r"RS#:(\d+)")
MAX_FORMS = 600
MAX_WORDS = 4          # longer spans are phrases PubTator marked, not names to highlight
MAX_FORM_CHARS = 42

# A form shaped like a canonical gene symbol must *be* this gene's symbol. NCBI lists "Met"
# among the aliases of SLTM, so allowing aliases here would link every "MET" in a cancer
# report to the wrong gene.
CANONICAL_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{1,6}$")
WORD_RE = re.compile(r"[a-z]{4,}")


def _shares_wording(form: str, name: str) -> bool:
    """Does the text share a word with the name PubTator resolved it to?"""
    return bool(set(WORD_RE.findall(form.lower())) & set(WORD_RE.findall(name.lower())))


def _usable(form: str) -> bool:
    return len(form) <= MAX_FORM_CHARS and len(form.split()) <= MAX_WORDS


def _worth_marking(form: str, checked: bool) -> bool:
    """A bare lowercase word is only worth marking when its mapping held up.

    These are where PubTator's misses land — "events" resolved to Cardiovascular Diseases,
    "tumours1,2" picked up from a citation marker — while verified ones are real names such
    as "sotorasib". Symbols, abbreviations and multi-word names keep their own case or
    spacing and are left alone.
    """
    return checked or " " in form or any(c.isupper() for c in form)


def _url(kind: str, identifier: str) -> str | None:
    if kind == "Gene" and identifier.isdigit():
        return f"https://www.ncbi.nlm.nih.gov/gene/{identifier}"
    if identifier.startswith("MESH:"):
        return f"https://meshb.nlm.nih.gov/record/ui?ui={identifier[5:]}"
    if kind == "Variant":
        m = RS_RE.search(identifier)
        return f"https://www.ncbi.nlm.nih.gov/snp/rs{m.group(1)}" if m else None
    return None


def _label(kind: str) -> str:
    return {"Gene": "gene", "Chemical": "drug or chemical", "Disease": "disease", "Variant": "variant"}[kind]


async def _annotate(pmids: list[str]) -> list[dict[str, Any]]:
    """PubTator3 annotations for up to 100 PubMed records."""
    await _ncbi_throttle.wait()
    resp = await _get(f"{PUBTATOR}/publications/export/biocjson",
                      {"pmids": ",".join(pmids)}, source="PubTator3")
    try:
        docs = resp.json().get("PubTator3", [])
    except ValueError as exc:
        raise SourceError(f"PubTator3 returned unreadable JSON ({exc})") from exc
    return [a for d in docs for passage in d.get("passages", []) for a in passage.get("annotations", [])]


async def _gene_records(gene_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Official symbol and aliases for NCBI gene ids, used to check PubTator's mapping."""
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(gene_ids), 200):
        batch = gene_ids[i:i + 200]
        await _ncbi_throttle.wait()
        try:
            resp = await _get(f"{EUTILS}/esummary.fcgi",
                              _ncbi_params(db="gene", id=",".join(batch), retmode="json"), source="NCBI Gene")
            result = resp.json().get("result", {})
        except (SourceError, ValueError):
            continue
        for uid in result.get("uids", []):
            rec = result.get(uid) or {}
            aliases = {a.strip().lower() for a in (rec.get("otheraliases") or "").split(",") if a.strip()}
            symbol = (rec.get("name") or "").strip()
            if symbol:
                aliases.add(symbol.lower())
            out[uid] = {"symbol": symbol, "aliases": aliases, "description": rec.get("description") or ""}
    return out


def _collect(annotations: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for ann in annotations:
        infons = ann.get("infons") or {}
        kind, identifier = infons.get("type"), infons.get("identifier")
        text = (ann.get("text") or "").strip()
        if kind not in WANTED_TYPES or len(text) < 3:
            continue
        # Unnormalised annotations carry no usable id and would otherwise collapse
        # unrelated mentions into one entry.
        if not identifier or identifier in ("-", "None") or (kind != "Gene" and not infons.get("name")):
            continue
        if text.lower() in SKIP_FORMS:
            continue
        entry = grouped.setdefault((kind, identifier), {
            "type": kind, "label": _label(kind), "id": identifier,
            "name": infons.get("name") or "", "forms": Counter(), "mentions": 0,
        })
        entry["forms"][text] += 1
        entry["mentions"] += 1
        if not entry["name"] and infons.get("name"):
            entry["name"] = infons["name"]
    return grouped


async def build(pmids: list[str]) -> dict[str, Any]:
    """Vocabulary for a thread: one entry per entity, each with the forms it appeared as."""
    pmids = [p for p in dict.fromkeys(pmids) if p and str(p).isdigit()]
    if not pmids:
        return {"entities": [], "papers": 0}

    batches = [pmids[i:i + 100] for i in range(0, len(pmids), 100)]
    results = await asyncio.gather(*(_annotate(b) for b in batches), return_exceptions=True)
    annotations = [a for r in results if not isinstance(r, BaseException) for a in r]
    failures = [str(r) for r in results if isinstance(r, BaseException)]
    if not annotations and failures:
        raise SourceError(failures[0])

    grouped = _collect(annotations)
    genes = await _gene_records([ident for (kind, ident) in grouped if kind == "Gene" and ident.isdigit()])

    entities = []
    for (kind, identifier), entry in grouped.items():
        forms = [f for f in sorted(entry["forms"], key=len, reverse=True) if _usable(f)]
        counts = entry["forms"]
        if not forms:
            continue
        checked, note = False, ""
        if kind == "Gene":
            record = genes.get(identifier)
            if not record:
                continue  # unverifiable gene id: do not offer a link to it
            entry["name"] = record["symbol"] or entry["name"]
            entry["description"] = record["description"]
            symbol = record["symbol"].lower()
            forms = [f for f in forms
                     if (f.lower() == symbol) or
                        (not CANONICAL_SYMBOL.match(f) and f.lower() in record["aliases"])]
            if not forms:
                continue
            checked = True
            note = f"Checked against NCBI gene {identifier}."
        else:
            # Nothing authoritative to check these against, so say whether the text and the
            # name PubTator resolved it to actually share wording.
            checked = any(_shares_wording(f, entry["name"]) for f in forms)
            note = ("" if checked else
                    f"PubTator3 resolved this to \u201c{entry['name']}\u201d, which shares no wording with the text. Treat it as a suggestion.")
        forms = [f for f in forms if _worth_marking(f, checked)]
        if not forms:
            continue
        entities.append({**entry, "forms": forms, "counts": counts, "checked": checked,
                         "note": note, "url": _url(kind, identifier)})

    # A form belongs to whichever entity actually used it most. Without this, an entity that
    # merely mentions "KRAS" once in passing can claim the form from the KRAS gene itself.
    rank = {"Gene": 0, "Variant": 1, "Chemical": 2, "Disease": 3}
    owner: dict[str, dict[str, Any]] = {}
    for entity in entities:
        for form in entity["forms"]:
            key = form.lower()
            claim = (entity["counts"][form], -rank[entity["type"]], entity["mentions"])
            held = owner.get(key)
            if held is None or claim > held["claim"]:
                owner[key] = {"claim": claim, "entity": entity}

    for entity in entities:
        entity["forms"] = [f for f in entity["forms"] if owner[f.lower()]["entity"] is entity]

    # Longest form first, so "KRAS G12C" is matched before "KRAS".
    kept = [e for e in entities if e["forms"]]
    kept.sort(key=lambda e: (-max(len(f) for f in e["forms"]), -e["mentions"]))
    total = 0
    trimmed = []
    for entity in kept:
        trimmed.append({k: v for k, v in entity.items() if k != "counts"})
        total += len(entity["forms"])
        if total >= MAX_FORMS:
            break

    return {"entities": trimmed, "papers": len(pmids)}
