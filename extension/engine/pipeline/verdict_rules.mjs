/**
 * Rules applied to model output before anything is shown — a port of the
 * backend's hard invariants (backend/app/fact_checker.py `_enforce_invariants`
 * and the GateClaim validators in backend/app/models.py).
 *
 * These are what make a verdict trustworthy regardless of which provider
 * produced it, so every provider module routes its output through here:
 *
 *   1. A verdict other than UNVERIFIED with no citations is downgraded.
 *   2. A verdict other than UNVERIFIED whose own evidence rating is not
 *      "strong" is downgraded — a web search always returns something, so
 *      topically adjacent pages must not read as confirmation.
 *   3. Source URLs come only from what the search actually returned; a URL
 *      the model merely wrote down is dropped.
 *   4. The explanation is clamped.
 *
 * Pure (no browser APIs): unit-tested under Node.
 */

import {
  EVIDENCE_LEVELS,
  LABELS,
  MAX_EXPLANATION_CHARS,
  MAX_SOURCES,
  NO_SOURCES_NOTE,
  TOPICS,
  WEAK_EVIDENCE_NOTE,
} from "../generated/prompts.mjs";

/**
 * @typedef {object} Source
 * @property {string} url
 * @property {string|null} title
 */

/**
 * @typedef {object} Verdict
 * @property {"TRUE"|"FALSE"|"MISLEADING"|"UNVERIFIED"} label
 * @property {string} explanation
 * @property {"strong"|"partial"|"none"|null} evidence
 * @property {Source[]} sources
 */

/** Loose match key so a copied URL with or without a trailing slash matches. */
const urlKey = (url) => url.trim().replace(/\/+$/, "");

/**
 * Choose a verdict's sources, taken ONLY from the search output:
 *
 *   1. the URLs the model picked, if the search really returned or cited them;
 *   2. otherwise the URLs cited in the answer text;
 *   3. otherwise the retrieved results, in search order.
 *
 * Deduplicated and capped at MAX_SOURCES. No search output -> no sources.
 *
 * @param {object} found
 * @param {Source[]} [found.retrieved] - every result the search returned
 * @param {Source[]} [found.cited] - results cited in the answer text
 * @param {string[]} [found.picked] - URLs named in the model's JSON
 * @returns {Source[]}
 */
export const selectSources = ({retrieved = [], cited = [], picked = []}) => {
  const index = (sources) => {
    const byKey = new Map();
    for (const source of sources) {
      if (typeof source?.url === "string" && source.url) {
        const key = urlKey(source.url);
        if (!byKey.has(key)) {
          byKey.set(key, {url: source.url, title: source.title ?? null});
        }
      }
    }
    return byKey;
  };
  const citedByKey = index(cited);
  const retrievedByKey = index(retrieved);
  const known = new Map([...citedByKey, ...retrievedByKey]);

  const pickedSources = new Map();
  for (const url of picked) {
    const key = typeof url === "string" ? urlKey(url) : "";
    if (known.has(key)) {
      pickedSources.set(key, known.get(key));
    }
  }
  const chosen = [pickedSources, citedByKey, retrievedByKey].find((group) => group.size > 0);
  return chosen ? [...chosen.values()].slice(0, MAX_SOURCES) : [];
};

/**
 * Validate the model's verdict object and apply the invariants.
 *
 * @param {unknown} payload - parsed model JSON ({label, explanation, evidence})
 * @param {Source[]} sources - from selectSources()
 * @returns {Verdict}
 * @throws {TypeError} when the payload is not a usable verdict
 */
export const finalizeVerdict = (payload, sources) => {
  if (typeof payload !== "object" || payload === null) {
    throw new TypeError("verdict is not an object");
  }
  const rawLabel = String(payload.label ?? "").trim().toUpperCase();
  if (!LABELS.includes(rawLabel)) {
    throw new TypeError(`verdict has an unknown label: ${JSON.stringify(payload.label)}`);
  }
  if (typeof payload.explanation !== "string" || !payload.explanation.trim()) {
    throw new TypeError("verdict has no explanation");
  }
  const rawEvidence = String(payload.evidence ?? "").trim().toLowerCase();
  const evidence = EVIDENCE_LEVELS.includes(rawEvidence) ? rawEvidence : null;

  let label = rawLabel;
  let explanation = payload.explanation.trim();
  if (label !== "UNVERIFIED" && sources.length === 0) {
    label = "UNVERIFIED";
    if (!explanation.includes(NO_SOURCES_NOTE)) {
      explanation = `${explanation} ${NO_SOURCES_NOTE}`;
    }
  }
  if (label !== "UNVERIFIED" && (evidence === "partial" || evidence === "none")) {
    label = "UNVERIFIED";
    if (!explanation.includes(WEAK_EVIDENCE_NOTE)) {
      explanation = `${explanation} ${WEAK_EVIDENCE_NOTE}`;
    }
  }
  if (explanation.length > MAX_EXPLANATION_CHARS) {
    explanation = `${explanation.slice(0, MAX_EXPLANATION_CHARS - 1).trimEnd()}…`;
  }
  return {label, explanation, evidence, sources};
};

/**
 * @typedef {object} GateClaim
 * @property {string} claim_text
 * @property {number} check_worthiness - 0..1
 * @property {string} topic - one of TOPICS
 */

/**
 * Validate the gate model's output. Malformed claims are dropped rather than
 * failing the batch; an unknown topic becomes "other" and the score is
 * clamped to 0..1, as the backend does.
 *
 * @param {unknown} payload - parsed model JSON ({claims: [...]})
 * @returns {GateClaim[]}
 * @throws {TypeError} when there is no claims array at all
 */
export const normalizeGateClaims = (payload) => {
  if (typeof payload !== "object" || payload === null || !Array.isArray(payload.claims)) {
    throw new TypeError("gate output has no claims array");
  }
  const claims = [];
  for (const candidate of payload.claims) {
    const text = typeof candidate?.claim_text === "string" ? candidate.claim_text.trim() : "";
    const score = Number(candidate?.check_worthiness);
    if (!text || !Number.isFinite(score)) {
      continue;
    }
    const topic = String(candidate.topic ?? "").trim().toLowerCase();
    claims.push({
      claim_text: text,
      check_worthiness: Math.min(1, Math.max(0, score)),
      topic: TOPICS.includes(topic) ? topic : "other",
    });
  }
  return claims;
};
