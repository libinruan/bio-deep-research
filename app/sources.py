"""Clients for the open biomedical literature APIs.

Every search function returns ``(total_hits, [record, ...])`` where a record is a plain
dict in one normalised shape (see ``_record``), whatever the upstream source was.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
OPENALEX = "https://api.openalex.org"
CTGOV = "https://clinicaltrials.gov/api/v2"

TOOL_NAME = "bio-deep-research"
CONTACT_EMAIL = os.environ.get("BDR_CONTACT_EMAIL", "")
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(40.0, connect=15.0),
            headers={"User-Agent": f"{TOOL_NAME}/0.1 ({CONTACT_EMAIL or 'local research tool'})"},
            follow_redirects=True,
        )
    return _client


class SourceError(RuntimeError):
    """An upstream API failed in a way the agent should be told about."""


class _Throttle:
    """Spaces calls so we stay under a per-second request limit."""

    def __init__(self, per_second: float):
        self._gap = 1.0 / per_second
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self._gap - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


# NCBI allows 3 requests/s without a key and 10/s with one.
_ncbi_throttle = _Throttle(9.0 if NCBI_API_KEY else 2.5)


async def _get(url: str, params: dict[str, Any], *, source: str, retries: int = 2) -> httpx.Response:
    last = ""
    for attempt in range(retries + 1):
        try:
            resp = await client().get(url, params=params)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                return resp
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
        await asyncio.sleep(1.5 * (attempt + 1))
    raise SourceError(f"{source} request failed ({last})")


def _record(**kw: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "pmid": None, "pmcid": None, "doi": None, "nct": None,
        "title": "", "authors": [], "journal": "", "year": None,
        "abstract": "", "pub_types": [], "cited_by": None,
        "open_access": None, "preprint": False, "retracted": False,
        "species": [], "url": "", "sources": [], "kind": "paper",
    }
    rec.update(kw)
    rec["title"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", rec["title"] or "")).strip()
    if rec["doi"]:
        rec["doi"] = re.sub(r"^https?://(dx\.)?doi\.org/", "", rec["doi"].strip(), flags=re.I).lower()
    return rec


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


# --------------------------------------------------------------------------- PubMed

def _ncbi_params(**kw: Any) -> dict[str, Any]:
    p = {"tool": TOOL_NAME, **kw}
    if CONTACT_EMAIL:
        p["email"] = CONTACT_EMAIL
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    return p


def _parse_pubmed_article(art: ET.Element) -> dict[str, Any]:
    cit = art.find("MedlineCitation")
    article = cit.find("Article") if cit is not None else None
    if cit is None or article is None:
        return {}
    pmid = _text(cit.find("PMID"))

    parts = []
    for ab in article.findall("Abstract/AbstractText"):
        label = ab.get("Label")
        txt = _text(ab)
        if txt:
            parts.append(f"{label}: {txt}" if label else txt)

    authors = []
    for au in article.findall("AuthorList/Author"):
        last, init = _text(au.find("LastName")), _text(au.find("Initials"))
        name = f"{last} {init}".strip() or _text(au.find("CollectiveName"))
        if name:
            authors.append(name)

    year = None
    for path in ("Journal/JournalIssue/PubDate/Year", "Journal/JournalIssue/PubDate/MedlineDate", "ArticleDate/Year"):
        m = re.search(r"(19|20)\d{2}", _text(article.find(path)))
        if m:
            year = int(m.group(0))
            break

    pub_types = [_text(pt) for pt in article.findall("PublicationTypeList/PublicationType")]
    mesh = [_text(d) for d in cit.findall("MeshHeadingList/MeshHeading/DescriptorName")]
    species = [s for s in ("Humans", "Animals") if s in mesh]

    doi = pmcid = None
    for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi":
            doi = _text(aid)
        elif aid.get("IdType") == "pmc":
            pmcid = _text(aid)

    # A retraction shows up either as a publication type or as a linked retraction notice.
    retracted = "Retracted Publication" in pub_types or any(
        c.get("RefType") == "RetractionIn" for c in cit.findall("CommentsCorrectionsList/CommentsCorrections")
    )

    return _record(
        pmid=pmid, pmcid=pmcid, doi=doi,
        title=_text(article.find("ArticleTitle")),
        authors=authors,
        journal=_text(article.find("Journal/ISOAbbreviation")) or _text(article.find("Journal/Title")),
        year=year,
        abstract="\n".join(parts),
        pub_types=[p for p in pub_types if p != "Journal Article"],
        preprint="Preprint" in pub_types,
        retracted=retracted,
        species=species,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        sources=["PubMed"],
    )


async def pubmed_fetch(pmids: list[str]) -> list[dict[str, Any]]:
    pmids = [p for p in dict.fromkeys(str(p).strip() for p in pmids) if p.isdigit()]
    if not pmids:
        return []
    await _ncbi_throttle.wait()
    resp = await _get(f"{EUTILS}/efetch.fcgi", _ncbi_params(db="pubmed", id=",".join(pmids), retmode="xml"), source="PubMed")
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise SourceError(f"PubMed returned unparseable XML ({exc})") from exc
    by_id = {}
    for art in root.findall("PubmedArticle"):
        rec = _parse_pubmed_article(art)
        if rec:
            by_id[rec["pmid"]] = rec
    return [by_id[p] for p in pmids if p in by_id]


async def pubmed_search(
    query: str, max_results: int = 20, sort: str = "relevance",
    year_from: int | None = None, year_to: int | None = None,
) -> tuple[int, list[dict[str, Any]], str]:
    """Returns (total, records, how PubMed translated the query)."""
    params = _ncbi_params(db="pubmed", term=query, retmode="json", retmax=max_results,
                          sort="pub_date" if sort == "date" else "relevance")
    if year_from or year_to:
        params.update(datetype="pdat", mindate=str(year_from or 1800), maxdate=str(year_to or 3000))
    await _ncbi_throttle.wait()
    resp = await _get(f"{EUTILS}/esearch.fcgi", params, source="PubMed")
    res = resp.json().get("esearchresult", {})
    if "ERROR" in res:
        raise SourceError(f"PubMed rejected the query: {res['ERROR']}")
    records = await pubmed_fetch(res.get("idlist", []))
    return int(res.get("count", 0)), records, res.get("querytranslation", "")


# --------------------------------------------------------------------------- Europe PMC

def _epmc_record(r: dict[str, Any]) -> dict[str, Any]:
    src = r.get("source", "")
    pub_types = (r.get("pubTypeList") or {}).get("pubType") or []
    if isinstance(pub_types, str):
        pub_types = [pub_types]
    pub_types = [p.title() if p.islower() else p for p in pub_types]
    pmid = r.get("pmid") or (r.get("id") if src == "MED" else None)
    doi = r.get("doi")
    if pmid:
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    elif doi:
        url = f"https://doi.org/{doi}"
    else:
        url = f"https://europepmc.org/article/{src}/{r.get('id')}"
    year = r.get("pubYear")
    journal = ((r.get("journalInfo") or {}).get("journal") or {}).get("isoabbreviation") or \
        (r.get("bookOrReportDetails") or {}).get("publisher") or ""
    if src == "PPR":
        journal = journal or "Preprint"
    return _record(
        pmid=str(pmid) if pmid else None, pmcid=r.get("pmcid"), doi=doi,
        title=(r.get("title") or "").rstrip("."),
        authors=[a.strip() for a in (r.get("authorString") or "").rstrip(".").split(",") if a.strip()],
        journal=journal,
        year=int(year) if year and str(year).isdigit() else None,
        abstract=re.sub(r"<[^>]+>", "", r.get("abstractText") or ""),
        pub_types=[p for p in pub_types if p.lower() not in ("journal article", "research-article")],
        cited_by=r.get("citedByCount"),
        open_access=r.get("isOpenAccess") == "Y",
        preprint=src == "PPR",
        retracted=any("retract" in p.lower() for p in pub_types),
        url=url, sources=["Europe PMC"],
    )


async def europepmc_search(
    query: str, max_results: int = 20, sort: str = "relevance",
    year_from: int | None = None, year_to: int | None = None, preprints_only: bool = False,
) -> tuple[int, list[dict[str, Any]]]:
    q = f"({query})"
    if year_from or year_to:
        q += f" AND PUB_YEAR:[{year_from or 1800} TO {year_to or 3000}]"
    if preprints_only:
        q += " AND SRC:PPR"
    params = {"query": q, "format": "json", "resultType": "core", "pageSize": max_results}
    if sort == "date":
        params["sort"] = "P_PDATE_D desc"
    elif sort == "cited":
        params["sort"] = "CITED desc"
    resp = await _get(f"{EPMC}/search", params, source="Europe PMC")
    data = resp.json()
    results = (data.get("resultList") or {}).get("result") or []
    return int(data.get("hitCount", 0)), [_epmc_record(r) for r in results]


async def europepmc_citing(pmid: str, max_results: int = 25) -> tuple[int, list[dict[str, Any]]]:
    """Papers that cite the given PubMed article, most cited first."""
    return await europepmc_search(f"CITES:{pmid}_MED", max_results=max_results, sort="cited")


_SKIP_SECTIONS = ("reference", "acknowledg", "funding", "contribution", "disclosure", "competing interest",
                  "online content", "peer review", "footnote", "associated data", "untitled section",
                  "conflict of interest", "supplementary", "abbreviation", "data availability")


def _section_text(sec: ET.Element) -> str:
    chunks = []
    for el in sec.iter():
        if el.tag == "p":
            txt = re.sub(r"\s+", " ", _text(el))
            if txt:
                chunks.append(txt)
    return "\n".join(chunks)


async def europepmc_fulltext(pmcid: str) -> list[tuple[str, str]]:
    """Open-access full text as [(section title, text)], empty if not in the OA subset."""
    pmcid = pmcid.upper() if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
    # Europe PMC answers 404 or, for many non-open-access articles, a plain 500. Retry once
    # in case the 500 is transient, then treat it as "no full text".
    resp = None
    for attempt in range(2):
        try:
            resp = await client().get(f"{EPMC}/{pmcid}/fullTextXML")
        except httpx.HTTPError as exc:
            if attempt == 1:
                raise SourceError(f"Europe PMC full text request failed ({exc})") from exc
        else:
            if resp.status_code == 200:
                break
            if resp.status_code == 404 or attempt == 1:
                return []
        await asyncio.sleep(1.0)
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise SourceError(f"Europe PMC returned unparseable full text ({exc})") from exc
    body = root.find(".//body")
    if body is None:
        return []
    sections = []
    for sec in body.findall("sec"):
        title = _text(sec.find("title")) or "Untitled section"
        if any(s in title.lower() for s in _SKIP_SECTIONS):
            continue
        text = _section_text(sec)
        if text:
            sections.append((title, text))
    if not sections:  # some articles have paragraphs directly under <body>
        text = _section_text(body)
        if text:
            sections.append(("Body", text))
    return sections


async def resolve_pmcid(pmid: str) -> str | None:
    _, recs = await europepmc_search(f"EXT_ID:{pmid} AND SRC:MED", max_results=1)
    return recs[0]["pmcid"] if recs else None


# --------------------------------------------------------------------------- OpenAlex

def _openalex_abstract(inv: dict[str, list[int]] | None) -> str:
    if not inv:
        return ""
    words = sorted((pos, w) for w, positions in inv.items() for pos in positions)
    return " ".join(w for _, w in words)


def _openalex_record(w: dict[str, Any]) -> dict[str, Any]:
    ids = w.get("ids") or {}
    pmid = (ids.get("pmid") or "").rstrip("/").split("/")[-1] or None
    pmcid = (ids.get("pmcid") or "").rstrip("/").split("/")[-1] or None
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    wtype = w.get("type") or ""
    doi = w.get("doi")
    return _record(
        pmid=pmid, pmcid=pmcid, doi=doi,
        title=w.get("title") or "",
        authors=[(a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])[:30]],
        journal=src.get("display_name") or "",
        year=w.get("publication_year"),
        abstract=_openalex_abstract(w.get("abstract_inverted_index")),
        pub_types=[wtype.title()] if wtype and wtype != "article" else [],
        cited_by=w.get("cited_by_count"),
        open_access=(w.get("open_access") or {}).get("is_oa"),
        preprint=wtype == "preprint",
        retracted=bool(w.get("is_retracted")),
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else (doi or w.get("id") or ""),
        sources=["OpenAlex"],
    )


async def openalex_search(
    query: str, max_results: int = 20, sort: str = "relevance",
    year_from: int | None = None, year_to: int | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    params: dict[str, Any] = {"search": query, "per-page": max_results}
    if filters:
        params["filter"] = ",".join(filters)
    if sort == "cited":
        params["sort"] = "cited_by_count:desc"
    elif sort == "date":
        params["sort"] = "publication_date:desc"
    if CONTACT_EMAIL:
        params["mailto"] = CONTACT_EMAIL
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    resp = await _get(f"{OPENALEX}/works", params, source="OpenAlex")
    data = resp.json()
    return int((data.get("meta") or {}).get("count", 0)), [_openalex_record(w) for w in data.get("results", [])]


# --------------------------------------------------------------------------- ClinicalTrials.gov

def _trial_record(s: dict[str, Any]) -> dict[str, Any]:
    p = s.get("protocolSection") or {}
    ident = p.get("identificationModule") or {}
    status = p.get("statusModule") or {}
    design = p.get("designModule") or {}
    nct = ident.get("nctId")
    phases = design.get("phases") or []
    interventions = [i.get("name", "") for i in (p.get("armsInterventionsModule") or {}).get("interventions") or []]
    primary = [o.get("measure", "") for o in (p.get("outcomesModule") or {}).get("primaryOutcomes") or []]
    start = (status.get("startDateStruct") or {}).get("date") or ""
    summary = (p.get("descriptionModule") or {}).get("briefSummary") or ""
    lines = [
        f"Status: {status.get('overallStatus', 'unknown')}",
        f"Phase: {', '.join(phases) or 'n/a'}; study type: {design.get('studyType', 'n/a')}",
        f"Enrollment: {(design.get('enrollmentInfo') or {}).get('count', 'n/a')}",
        f"Conditions: {', '.join((p.get('conditionsModule') or {}).get('conditions') or []) or 'n/a'}",
        f"Interventions: {', '.join(interventions) or 'n/a'}",
        f"Primary outcomes: {'; '.join(primary) or 'n/a'}",
        f"Has posted results: {'yes' if s.get('hasResults') else 'no'}",
        f"Summary: {summary}",
    ]
    return _record(
        nct=nct, kind="trial",
        title=ident.get("briefTitle") or ident.get("officialTitle") or "",
        authors=[((p.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}).get("name", "")],
        journal="ClinicalTrials.gov",
        year=int(start[:4]) if start[:4].isdigit() else None,
        abstract="\n".join(lines),
        pub_types=["Clinical Trial Registration", *phases, status.get("overallStatus", "")],
        url=f"https://clinicaltrials.gov/study/{nct}",
        sources=["ClinicalTrials.gov"],
    )


async def trials_search(query: str, max_results: int = 15, status: list[str] | None = None) -> tuple[int, list[dict[str, Any]]]:
    params: dict[str, Any] = {"query.term": query, "pageSize": max_results, "countTotal": "true"}
    if status:
        params["filter.overallStatus"] = ",".join(status)
    resp = await _get(f"{CTGOV}/studies", params, source="ClinicalTrials.gov")
    data = resp.json()
    return int(data.get("totalCount", 0)), [_trial_record(s) for s in data.get("studies", [])]


async def trial_fetch(nct: str) -> dict[str, Any] | None:
    try:
        resp = await client().get(f"{CTGOV}/studies/{nct}")
    except httpx.HTTPError:
        return None
    return _trial_record(resp.json()) if resp.status_code == 200 else None
