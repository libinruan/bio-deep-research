"""Turning a finished turn into something that leaves the app: Markdown, HTML or PDF.

Citations stay clickable everywhere. In Markdown each bracketed number links straight to
the paper; in HTML and PDF it links to the reference list, which links on to the paper.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

CITE_LINK = re.compile(r"\[\[(\d+)\]\]\(#ref-(\d+)\)")
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def _doi_url(doi: str) -> str:
    return f"https://doi.org/{doi}"


def reference_links(ref: dict[str, Any]) -> list[tuple[str, str]]:
    """The identifiers of a reference, each with the page it points at."""
    out = []
    if ref.get("pmid"):
        out.append((f"PMID {ref['pmid']}", f"https://pubmed.ncbi.nlm.nih.gov/{ref['pmid']}/"))
    if ref.get("doi"):
        out.append((f"doi:{ref['doi']}", _doi_url(ref["doi"])))
    if ref.get("nct"):
        out.append((ref["nct"], f"https://clinicaltrials.gov/study/{ref['nct']}"))
    if ref.get("pmcid") and not ref.get("pmid"):
        out.append((ref["pmcid"], f"https://www.ncbi.nlm.nih.gov/pmc/articles/{ref['pmcid']}/"))
    return out


def split_title(turn: dict[str, Any]) -> tuple[str, str]:
    """Deep reports open with their own title; use it, and keep the question beneath it."""
    report = turn["report"].lstrip()
    if report.startswith("# "):
        title, _, body = report.partition("\n")
        return title[2:].strip(), body.lstrip()
    return turn["question"], report


def _byline(ref: dict[str, Any]) -> str:
    authors = ref.get("authors") or []
    return ", ".join(authors[:3]) + (" et al" if len(authors) > 3 else "")


def _flags(ref: dict[str, Any]) -> list[str]:
    out = [f for f in (ref.get("design"),) if f]
    if ref.get("preprint"):
        out.append("preprint, not peer reviewed")
    if ref.get("retracted"):
        out.append("RETRACTED")
    return out


# --------------------------------------------------------------------------- Markdown

def _markdown_reference(ref: dict[str, Any]) -> str:
    who = _byline(ref)
    ids = "; ".join(f"[{label}]({url})" for label, url in reference_links(ref))
    flags = f" _({'; '.join(_flags(ref))})_" if _flags(ref) else ""
    title = (ref.get("title") or "").rstrip(".")
    journal = " ".join(x for x in (ref.get("journal"), str(ref.get("year") or "")) if x)
    parts = [f"{ref['n']}.", f"{who}." if who else "", f"{title}.", f"{journal}." if journal else "", ids, flags]
    return " ".join(p for p in parts if p)


def to_markdown(turn: dict[str, Any]) -> str:
    """The report with every citation linked to the paper it cites."""
    by_number = {ref["n"]: ref for ref in turn["references"]}

    def link(m: re.Match[str]) -> str:
        ref = by_number.get(int(m.group(1)))
        return f"[[{m.group(1)}]]({ref['url']})" if ref and ref.get("url") else f"[{m.group(1)}]"

    title, body = split_title(turn)
    parts = [f"# {title}"]
    if title != turn["question"]:
        parts.append(f"> {turn['question']}")
    parts.append(CITE_LINK.sub(link, body))

    if turn["references"]:
        parts += ["## References", "\n".join(_markdown_reference(r) for r in turn["references"])]

    problems = turn.get("problems") or []
    if problems:
        parts += ["## Citation notes",
                  "\n".join(f"- **{p['citation']}**: {p['problem']}" for p in problems)]

    audit = turn.get("audit") or {}
    if audit.get("issues"):
        rows = [f"- **{i['verdict']}** [{i.get('n') or i['citation']}] — {i['claim']}\n  - {i['explanation']}"
                for i in audit["issues"]]
        parts += [f"## Audit ({audit.get('citations_checked', 0)} citations checked)", "\n".join(rows)]

    strategy = turn.get("strategy")
    if strategy:
        blocks = []
        for db in strategy["databases"]:
            hits = f" — {db['hits']:,} hits" if db.get("hits") is not None else ""
            blocks.append(f"**{db['database']}**{hits}\n\n```\n{db['query']}\n```")
        parts += ["## Search strategy", "\n\n".join(blocks)]

    return "\n\n".join(parts) + "\n"


# --------------------------------------------------------------------------- HTML / PDF

_PRINT_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 36px 48px; max-width: 46em; background: #fff; color: #16202a;
  font: 11.5pt/1.55 Georgia, "Times New Roman", serif; }
h1 { font-size: 20pt; line-height: 1.25; margin: 0 0 6px; }
h2 { font-size: 14pt; margin: 26px 0 8px; border-bottom: 1px solid #d9d6cd; padding-bottom: 4px; }
h3 { font-size: 12pt; margin: 18px 0 6px; }
p, li { orphans: 3; widows: 3; }
.meta { font: 9.5pt/1.5 system-ui, sans-serif; color: #5d6b76; margin: 0 0 24px; }
.meta span + span::before { content: " · "; }
a { color: #0b6b62; }
sup a { text-decoration: none; font-size: 8.5pt; padding: 0 1px; }
table { border-collapse: collapse; width: 100%; font: 9.5pt/1.45 system-ui, sans-serif; margin: 12px 0; }
th, td { border: 1px solid #d9d6cd; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #f3f1ea; }
code, pre { background: #f3f1ea; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 9pt; }
pre { padding: 8px 10px; white-space: pre-wrap; word-break: break-word; border-radius: 4px; }
blockquote { margin: 6px 0; color: #5d6b76; font-style: italic; }
blockquote.question { border-left: 3px solid #0b6b62; padding: 6px 12px; margin: 0 0 22px;
  font: italic 10.5pt/1.5 system-ui, sans-serif; }
.note { border-left: 3px solid #c8922a; background: #fdf6e7; padding: 8px 12px; margin: 14px 0;
  font: 10pt/1.5 system-ui, sans-serif; }
.note.bad { border-color: #a3261d; background: #fbe9e6; }
ol.refs { font: 10pt/1.5 system-ui, sans-serif; padding-left: 22px; }
ol.refs li { margin-bottom: 7px; }
ol.refs .by { color: #5d6b76; }
.flag { font-size: 8.5pt; text-transform: uppercase; letter-spacing: .04em; color: #8a5a00; }
.flag.bad { color: #a3261d; font-weight: 700; }
h2, h3 { break-after: avoid; page-break-after: avoid; }
table, pre, blockquote, li { break-inside: avoid; page-break-inside: avoid; }
@page { margin: 16mm 14mm; }
"""


def _esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=True)


def _reference_html(ref: dict[str, Any]) -> str:
    ids = "; ".join(f'<a href="{_esc(url)}">{_esc(label)}</a>' for label, url in reference_links(ref))
    flags = "".join(
        f' <span class="flag{" bad" if f == "RETRACTED" else ""}">{_esc(f)}</span>' for f in _flags(ref))
    who = _byline(ref)
    journal = " ".join(x for x in (ref.get("journal"), str(ref.get("year") or "")) if x)
    return (
        f'<li id="ref-{ref["n"]}">'
        f'<a href="{_esc(ref.get("url"))}">{_esc((ref.get("title") or "").rstrip("."))}</a>. '
        f'<span class="by">{_esc(who)}{". " if who else ""}{_esc(journal)}</span>. {ids}{flags}</li>'
    )


def to_html(thread: dict[str, Any], turn: dict[str, Any], engine: str = "") -> str:
    """A standalone page: the same content as the Markdown, styled for reading and printing.

    The report is Markdown, so it is rendered in the page by the libraries the app already
    ships. Headless Chrome runs that script before printing, which is how the PDF is made.
    """
    refs = "".join(_reference_html(r) for r in turn["references"])
    title, body = split_title(turn)
    meta = [turn["mode"] == "deep" and "Deep research" or "Quick answer"]
    if turn.get("hypotheses"):
        meta.append("with hypotheses")
    if turn.get("usage"):
        meta.append(f"{turn['usage']['seconds']}s")
    if engine:
        meta.append(engine)
    meta.append(f"{len(turn['references'])} sources")

    notes = ""
    for problem in turn.get("problems") or []:
        notes += f'<div class="note"><b>{_esc(problem["citation"])}</b>: {_esc(problem["problem"])}</div>'

    audit = turn.get("audit") or {}
    audit_html = ""
    if audit.get("issues"):
        rows = "".join(
            f'<div class="note"><b>{_esc(i["verdict"])}</b> [{_esc(i.get("n") or i["citation"])}]'
            f'<blockquote>{_esc(i["claim"])}</blockquote>{_esc(i["explanation"])}</div>'
            for i in audit["issues"])
        audit_html = f'<h2>Audit</h2><p>{audit.get("citations_checked", 0)} citations checked, ' \
                     f'{len(audit["issues"])} flagged.</p>{rows}'

    strategy_html = ""
    if turn.get("strategy"):
        blocks = ""
        for db in turn["strategy"]["databases"]:
            hits = f" — {db['hits']:,} hits" if db.get("hits") is not None else ""
            blocks += f"<h3>{_esc(db['database'])}{_esc(hits)}</h3><pre>{_esc(db['query'])}</pre>"
        strategy_html = f"<h2>Search strategy</h2>{blocks}"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{_esc(title[:90])}</title>
<style>{_PRINT_CSS}</style>
</head><body>
<h1>{_esc(title)}</h1>
<div class="meta">{''.join(f'<span>{_esc(m)}</span>' for m in meta)}</div>
{f'<blockquote class="question">{_esc(turn["question"])}</blockquote>' if title != turn['question'] else ''}
{notes}
<article id="report"></article>
{audit_html}
{'<h2>References</h2><ol class="refs">' + refs + '</ol>' if refs else ''}
{strategy_html}
<script src="/static/vendor/marked.min.js"></script>
<script src="/static/vendor/purify.min.js"></script>
<script>
  const source = {json.dumps(body)};
  const el = document.getElementById("report");
  el.innerHTML = DOMPurify.sanitize(marked.parse(source, {{ gfm: true }}));
  // Citation links are superscript numbers pointing at the reference list.
  for (const a of el.querySelectorAll('a[href^="#ref-"]')) {{
    const sup = document.createElement("sup");
    a.replaceWith(sup);
    sup.append(a);
    a.textContent = a.getAttribute("href").slice(5);
  }}
  document.title = {json.dumps(title[:90])};
  window.__ready = true;
</script>
</body></html>"""


def chrome_path() -> str | None:
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


async def to_pdf(page_url: str) -> bytes:
    """Print the standalone page to PDF with headless Chrome, keeping links clickable."""
    chrome = chrome_path()
    if chrome is None:
        raise RuntimeError(
            "PDF export needs Chrome or Chromium on this machine, and none was found. "
            "Install one, or export Markdown and convert it yourself.")
    with tempfile.TemporaryDirectory(prefix="bdr-pdf-") as tmp:
        out = Path(tmp) / "report.pdf"
        proc = await asyncio.create_subprocess_exec(
            chrome, "--headless", "--disable-gpu", "--no-sandbox",
            f"--user-data-dir={tmp}/profile",
            "--no-pdf-header-footer", "--virtual-time-budget=15000",
            f"--print-to-pdf={out}", page_url,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Chrome took too long to render the PDF.") from None
        if not out.exists() or out.stat().st_size == 0:
            detail = (err or b"").decode(errors="replace").strip().splitlines()
            raise RuntimeError("Chrome produced no PDF. " + (detail[-1] if detail else ""))
        return out.read_bytes()


# --------------------------------------------------------------------------- bibliography

def to_bibtex(ref: dict[str, Any]) -> str:
    first = re.sub(r"[^A-Za-z]", "", ((ref.get("authors") or ["anon"])[0] or "anon").split(" ")[0]) or "anon"
    fields = {
        "title": ref.get("title"), "author": " and ".join(ref.get("authors") or []), "journal": ref.get("journal"),
        "year": ref.get("year"), "doi": ref.get("doi"), "pmid": ref.get("pmid"), "url": ref.get("url"),
        "note": "Retracted" if ref.get("retracted") else ("Preprint" if ref.get("preprint") else None),
    }
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
    kind = "misc" if ref.get("preprint") or ref.get("kind") == "trial" else "article"
    return f"@{kind}{{{first}{ref.get('year') or ''}_{ref['n']},\n{body}\n}}"


def to_ris(ref: dict[str, Any]) -> str:
    lines = ["TY  - " + ("UNPB" if ref.get("preprint") else "JOUR")]
    lines += [f"AU  - {a}" for a in ref.get("authors") or []]
    for tag, key in (("TI", "title"), ("JO", "journal"), ("PY", "year"), ("DO", "doi"), ("AN", "pmid"), ("UR", "url"), ("AB", "abstract")):
        if ref.get(key):
            lines.append(f"{tag}  - {str(ref[key]).replace(chr(10), ' ')}")
    return "\n".join(lines + ["ER  - "])
