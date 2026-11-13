/**
 * Duplicate-claim suppression — the biggest saver of provider spend, because
 * streamers repeat themselves.
 *
 * A port of the backend's check (backend/app/fact_checker.py `is_duplicate`):
 * a claim is a duplicate when its token-set similarity to any claim already
 * seen this session is at least 85. The similarity is rapidfuzz's
 * `token_set_ratio`, re-implemented here and held to the Python library's
 * values by golden cases in the unit tests, so both engines drop the same
 * repeats.
 *
 * Pure (no browser APIs): unit-tested under Node.
 */

export const DUPLICATE_SIMILARITY_THRESHOLD = 85;

// Python's string.punctuation, which is what the backend strips.
const PUNCTUATION = /[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]/g;

/**
 * Lower-case, strip punctuation, collapse whitespace.
 *
 * @param {string} claim
 * @returns {string}
 */
export const normalizeClaim = (claim) =>
  String(claim ?? "")
    .toLowerCase()
    .replace(PUNCTUATION, "")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");

/** Length of the longest common subsequence of two strings. */
const lcsLength = (first, second) => {
  if (first.length === 0 || second.length === 0) {
    return 0;
  }
  let previous = new Uint16Array(second.length + 1);
  let current = new Uint16Array(second.length + 1);
  for (let i = 1; i <= first.length; i += 1) {
    for (let j = 1; j <= second.length; j += 1) {
      current[j] =
        first[i - 1] === second[j - 1]
          ? previous[j - 1] + 1
          : Math.max(previous[j], current[j - 1]);
    }
    [previous, current] = [current, previous];
  }
  return previous[second.length];
};

/** rapidfuzz `ratio`: similarity from insertions and deletions only, 0–100. */
const indelRatio = (first, second) => {
  const total = first.length + second.length;
  return total === 0 ? 100 : (200 * lcsLength(first, second)) / total;
};

/**
 * rapidfuzz `token_set_ratio` over whitespace-separated tokens: word order
 * and repeated words do not matter, and one text containing all the words of
 * the other scores 100.
 *
 * @param {string} first - already normalised
 * @param {string} second - already normalised
 * @returns {number} 0–100
 */
export const tokenSetRatio = (first, second) => {
  const firstTokens = new Set(first.split(" ").filter(Boolean));
  const secondTokens = new Set(second.split(" ").filter(Boolean));
  if (firstTokens.size === 0 || secondTokens.size === 0) {
    return 0;
  }
  const shared = [...firstTokens].filter((token) => secondTokens.has(token)).sort();
  const onlyFirst = [...firstTokens].filter((token) => !secondTokens.has(token)).sort();
  const onlySecond = [...secondTokens].filter((token) => !firstTokens.has(token)).sort();
  if (shared.length > 0 && (onlyFirst.length === 0 || onlySecond.length === 0)) {
    return 100;
  }
  const sharedText = shared.join(" ");
  const join = (rest) => [sharedText, rest.join(" ")].filter(Boolean).join(" ");
  const firstText = join(onlyFirst);
  const secondText = join(onlySecond);
  const candidates = [indelRatio(firstText, secondText)];
  if (shared.length > 0) {
    candidates.push(indelRatio(sharedText, firstText), indelRatio(sharedText, secondText));
  }
  return Math.max(...candidates);
};

/** The claims already seen in one session. */
export class ClaimMemory {
  constructor() {
    /** @type {string[]} normalised claims, in the order first heard */
    this.seen = [];
  }

  /**
   * Check and register: a novel claim is remembered as a side effect, so
   * the caller can simply drop the duplicates.
   *
   * @param {string} claim
   * @returns {boolean} true when it repeats an earlier claim
   */
  isDuplicate(claim) {
    const normalized = normalizeClaim(claim);
    if (
      this.seen.some(
        (earlier) => tokenSetRatio(normalized, earlier) >= DUPLICATE_SIMILARITY_THRESHOLD
      )
    ) {
      return true;
    }
    this.seen.push(normalized);
    return false;
  }
}
