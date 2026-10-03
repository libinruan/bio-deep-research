"""Turn concept blocks into syntax-correct search strings for each database.

The agent supplies the concepts (synonyms, MeSH headings, Chinese terms); the rendering
here is deterministic so that field tags and boolean syntax are right for each platform.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote


def _q(term: str, quote_char: str = '"') -> str:
    term = term.strip().strip('"').strip("'")
    return f"{quote_char}{term}{quote_char}"


def _uniq(terms: list[str]) -> list[str]:
    """De-duplicate ignoring case, so a MeSH heading doesn't repeat a free-text term."""
    seen: dict[str, str] = {}
    for t in terms:
        seen.setdefault(t.strip().lower(), t)
    return list(seen.values())


def _or(parts: list[str], op: str = " OR ") -> str:
    return op.join(parts)


def render(concepts: list[dict[str, Any]], year_from: int | None = None, year_to: int | None = None) -> list[dict[str, Any]]:
    """Returns one entry per database: name, query, how to run it, and whether the app can run it."""
    blocks = [
        {
            "terms": [t for t in (c.get("terms") or []) if t.strip()],
            "mesh": [t for t in (c.get("mesh") or []) if t.strip()],
            "zh": [t for t in (c.get("terms_zh") or []) if t.strip()],
        }
        for c in concepts
    ]
    blocks = [b for b in blocks if b["terms"] or b["mesh"]]
    y0, y1 = year_from, year_to
    has_years = bool(y0 or y1)
    lo, hi = y0 or 1900, y1 or date.today().year

    pubmed = " AND ".join(
        "(" + _or([f"{_q(m)}[MeSH Terms]" for m in b["mesh"]] + [f"{_q(t)}[Title/Abstract]" for t in b["terms"]]) + ")"
        for b in blocks
    )
    if has_years:
        pubmed += f' AND ("{lo}"[Date - Publication] : "{hi if y1 else 3000}"[Date - Publication])'

    def plain(b: dict[str, Any]) -> str:
        return _or([_q(t) for t in _uniq(b["terms"] + b["mesh"])])

    epmc = " AND ".join(f"({plain(b)})" for b in blocks)
    if has_years:
        epmc += f" AND PUB_YEAR:[{lo} TO {hi}]"

    wos = " AND ".join(f"TS=({plain(b)})" for b in blocks)
    if has_years:
        wos += f" AND PY=({lo}-{hi})"

    scopus = " AND ".join(f"TITLE-ABS-KEY({plain(b)})" for b in blocks)
    if y0:
        scopus += f" AND PUBYEAR > {y0 - 1}"
    if y1:
        scopus += f" AND PUBYEAR < {y1 + 1}"

    embase = " AND ".join("(" + _or([f"{_q(t, chr(39))}:ti,ab,kw" for t in _uniq(b["terms"] + b["mesh"])]) + ")" for b in blocks)
    if has_years:
        embase += f" AND [{lo}-{hi}]/py"

    cochrane = " AND ".join(f"({plain(b)}):ti,ab,kw" for b in blocks)

    def zh_terms(b: dict[str, Any]) -> list[str]:
        return b["zh"] or b["terms"]

    cnki = " AND ".join("SU=(" + _or([_q(t, "'") for t in zh_terms(b)], "+") + ")" for b in blocks)
    wanfang = " AND ".join("主题:(" + _or([_q(t) for t in zh_terms(b)], " OR ") + ")" for b in blocks)
    year_note = f" Set the publication-year filter to {lo}–{hi} in the search form." if has_years else ""

    return [
        {"database": "PubMed", "query": pubmed, "live": True,
         "url": f"https://pubmed.ncbi.nlm.nih.gov/?term={quote(pubmed)}",
         "note": "Searched live by this app."},
        {"database": "Europe PMC", "query": epmc, "live": True,
         "url": f"https://europepmc.org/search?query={quote(epmc)}",
         "note": "Searched live by this app. Includes bioRxiv/medRxiv preprints."},
        {"database": "Web of Science", "query": wos, "live": False,
         "url": "https://www.webofscience.com/wos/woscc/advanced-search",
         "note": "Subscription database. Paste into Advanced Search."},
        {"database": "Scopus", "query": scopus, "live": False,
         "url": "https://www.scopus.com/search/form.uri?display=advanced",
         "note": "Subscription database. Paste into Advanced document search."},
        {"database": "Embase", "query": embase, "live": False,
         "url": "https://www.embase.com/",
         "note": "Subscription database (Embase.com syntax). Add Emtree terms with /exp after checking them in the Emtree browser."},
        {"database": "Cochrane Library", "query": cochrane, "live": False,
         "url": "https://www.cochranelibrary.com/advanced-search/search-manager",
         "note": "Paste into Search Manager." + year_note},
        {"database": "CNKI 知网", "query": cnki, "live": False,
         "url": "https://kns.cnki.net/kns8s/AdvSearch",
         "note": "Subscription database. Paste into 专业检索 (professional search)." + year_note},
        {"database": "Wanfang 万方", "query": wanfang, "live": False,
         "url": "https://s.wanfangdata.com.cn/advanced-search/paper",
         "note": "Subscription database. Paste into 专业检索 (professional search)." + year_note},
    ]
