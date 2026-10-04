"use strict";

/* Finding the part of a source that backs a claim.
 *
 * Entirely local and lexical: it scores each sentence of the abstract against the sentence
 * the citation sits in, weighting rare terms and, heavily, shared numbers — effect sizes,
 * sample sizes and percentages are what biomedical claims hang on, and they match precisely.
 * This points the reader at a passage. It is not a judgement that the passage supports the
 * claim; the audit pass is what judges that. */

const STOPWORDS = new Set(
  ("the a an and or of in to for with by on at from as that this these those was were is are be been being has have had "
  + "not no nor but if then than so such it its their there here when while after before during both each other more most "
  + "all any we our us they them he she his her you your i study studies patients patient results result conclusions "
  + "conclusion background methods method objective objectives aim aims purpose significant significantly compared "
  + "compare versus vs between among also may might can could would should will shall do does did done using use used "
  + "show shows showed shown found find finds including include included however therefore thus overall respectively "
  + "per were which who whom whose what why how about into over under above below out up down only just very much many "
  + "few less least well also new").split(" "));

// Keeps decimals, percentages and ranges intact; they are the highest-signal tokens here.
const TERM_RE = /[a-z][a-z-]{2,}|\d+(?:[.,]\d+)*%?/g;

function terms(text) {
  const found = (text || "").toLowerCase().match(TERM_RE) || [];
  return found.filter((t) => !STOPWORDS.has(t));
}

/* Structured abstracts label their parts (BACKGROUND:, METHODS:); treat a label as a break
 * so a label never ends up glued to the sentence that follows it. */
function sentences(text) {
  return (text || "")
    .replace(/\s+/g, " ")
    .replace(/\b([A-Z][A-Z &]{3,}):\s*/g, "\n$1: ")
    .split(/\n|(?<=[.!?])\s+(?=[A-Z(\[])/)
    .map((s) => s.trim())
    .filter((s) => s.length > 25);
}

const MIN_SCORE = 0.55;

/** The passages of `source` closest to `claim`, best first. */
function bestPassages(claim, source, limit = 2) {
  const pool = sentences(source);
  if (!pool.length) return [];
  const wanted = new Set(terms(claim));
  if (!wanted.size) return [];

  const perSentence = pool.map((s) => new Set(terms(s)));
  const docFreq = new Map();
  for (const set of perSentence) {
    for (const t of set) docFreq.set(t, (docFreq.get(t) || 0) + 1);
  }

  const n = pool.length;
  const scored = pool.map((sentence, i) => {
    let score = 0;
    let hits = 0;
    for (const t of perSentence[i]) {
      if (!wanted.has(t)) continue;
      hits += 1;
      const idf = Math.log(1 + n / docFreq.get(t));
      score += /\d/.test(t) ? idf * 3 : idf;
    }
    // Normalise by length so a long sentence does not win on volume alone.
    return { sentence, hits, score: score / Math.sqrt(perSentence[i].size || 1) };
  });

  return scored
    .filter((x) => x.score >= MIN_SCORE && x.hits >= 2)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit);
}

/** Which words of the passage to highlight: the ones the claim also used. */
function sharedTerms(claim, passage) {
  const wanted = new Set(terms(claim));
  return new Set(terms(passage).filter((t) => wanted.has(t)));
}

const Evidence = { bestPassages, sharedTerms, sentences, terms };

if (typeof module !== "undefined" && module.exports) {
  module.exports = Evidence;
}
