---
name: biomedical-search-strategy
description: Builds a formal, reproducible literature search strategy for a biomedical or clinical question: structures the question (PICO, PEO, SPIDER), expands each concept into synonyms, MeSH headings and Chinese terms, and records it so it is rendered into exact syntax for PubMed, Europe PMC, Web of Science, Scopus, Embase, Cochrane, CNKI and Wanfang. Use at the start of any deep literature search, review, or evidence synthesis, before running searches.
---

# Biomedical search strategy

A literature answer is only as good as the search behind it. Typing a question into a search
box finds the papers that happen to use the same words; a structured strategy finds the
papers that are about the same thing. The goal here is recall with control: miss little, and
be able to show exactly what was searched.

## 1. Structure the question

Pick the framework that fits, and state it to yourself before choosing terms.

| Question type | Framework | Elements |
|---|---|---|
| Therapy, prevention, diagnosis | PICO | Population, Intervention, Comparison, Outcome |
| Etiology, risk, prognosis | PEO | Population, Exposure, Outcome |
| Qualitative, mixed methods | SPIDER | Sample, Phenomenon of Interest, Design, Evaluation, Research type |
| Mechanism, basic biology | Entity–process–context | e.g. gene or protein, the process it affects, the tissue or disease |

Search only the elements that define the topic, usually two or three. Outcomes and
comparators are often absent from titles and abstracts, so including them as required
concepts silently drops relevant papers. Leave them out of the search and apply them when
reading.

## 2. Expand each concept

For every concept, gather what different authors would call it:

- Synonyms and near-synonyms: "heart failure", "cardiac failure", "HFpEF".
- Abbreviations and full forms: "GLP-1", "glucagon-like peptide-1".
- Spelling variants: tumor / tumour, anemia / anaemia.
- Drug names: generic, brand, and development code (semaglutide, Ozempic, NN9535).
- Gene and protein names: official symbol, aliases, and the protein name.
- Truncation where word endings vary: `inflammat*`. Avoid short stems that explode (`gen*`).
- MeSH headings, exact as they appear in the MeSH browser. MeSH catches papers whose
  authors used none of your words; free text catches recent papers not yet indexed. Use both.
- Chinese terms (`terms_zh`) for CNKI and Wanfang: the standard Chinese medical term plus
  common variants, e.g. 心力衰竭, 心衰. Chinese databases matter for traditional Chinese
  medicine, regional epidemiology, and trials published only in Chinese.

## 3. Record and check

Call `record_search_strategy` with the framework and concept blocks. Terms inside a concept
are joined with OR; concepts are joined with AND. The tool renders correct syntax for each
database and returns live hit counts for PubMed and Europe PMC.

Read the counts:

- 0 to about 20 hits: too narrow. Remove a concept, add synonyms, or check a MeSH heading
  is spelled exactly.
- Tens of thousands: too broad for a focused question. Add the missing concept, or a
  publication-type or date limit.
- A few hundred to a few thousand is typical for a focused clinical question.

Revise and call the tool again until the counts are sensible. The last recorded strategy
is the one the user sees.

## 4. Search beyond the strategy

The formal strategy is the backbone, not the whole search. Afterwards:

- Run focused variants for sub-questions (mechanism, safety, specific populations).
- Add publication-type filters to find the strongest designs first:
  `AND (meta-analysis[pt] OR systematic review[pt])`, `AND randomized controlled trial[pt]`.
- Follow citations forward from the key papers with `get_citing_papers`.
- Check preprints for work from the last year, and the trial registry for studies that
  are running or finished without publishing.

## What this app can and cannot reach

PubMed, Europe PMC (including preprints and open-access full text), OpenAlex and
ClinicalTrials.gov are searched live. Web of Science, Scopus, Embase, CNKI and Wanfang
are subscription services without open APIs: the rendered queries are for the user to paste
into those sites through their own institutional access. Say so plainly when coverage
matters, for example when Chinese-language evidence is likely to be important.
