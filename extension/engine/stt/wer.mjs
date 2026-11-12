/**
 * Word error rate, for judging whether a speech-to-text run is correct.
 *
 * A JavaScript port of the scoring in backend/scripts/compare_stt.py
 * (normalize_words / word_error_rate) so browser and backend runs are scored
 * the same way: lowercase, punctuation stripped, digits spelled out ("450" and
 * "four hundred and fifty" must compare equal — models differ on which they
 * emit), and "and" dropped inside numbers.
 */

const ONES = (
  "zero one two three four five six seven eight nine ten eleven twelve " +
  "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split(" ");
const TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split(" ");
const SCALES = Object.freeze([
  [10 ** 9, "billion"],
  [10 ** 6, "million"],
  [1000, "thousand"],
]);
const NUMBER_WORDS = new Set([
  ...ONES,
  ...TENS.slice(2),
  "hundred",
  ...SCALES.map(([, name]) => name),
]);

// Longest digit run converted to words; anything longer stays as digits.
const MAX_NUMBER_DIGITS = 12;

/**
 * 0 <= value < 10**12 in words, without "and".
 *
 * @param {number} value - non-negative integer
 * @returns {string[]}
 */
export const numberToWords = (value) => {
  if (value < 20) {
    return [ONES[value]];
  }
  if (value < 100) {
    const tens = Math.floor(value / 10);
    const ones = value % 10;
    return ones ? [TENS[tens], ONES[ones]] : [TENS[tens]];
  }
  if (value < 1000) {
    const hundreds = Math.floor(value / 100);
    const rest = value % 100;
    return [ONES[hundreds], "hundred", ...(rest ? numberToWords(rest) : [])];
  }
  for (const [scale, name] of SCALES) {
    if (value >= scale) {
      const head = Math.floor(value / scale);
      const rest = value % scale;
      return [...numberToWords(head), name, ...(rest ? numberToWords(rest) : [])];
    }
  }
  throw new RangeError(`numberToWords: unsupported value ${value}`);
};

/**
 * @param {string} text
 * @returns {string[]} comparable word tokens
 */
export const normalizeWords = (text) => {
  const cleaned = text
    .toLowerCase()
    .replaceAll("%", " percent ")
    .replace(/(?<=\d),(?=\d{3})/g, "");
  const words = [];
  for (const token of cleaned.match(/[a-z0-9']+/g) ?? []) {
    if (/^\d+$/.test(token) && token.length <= MAX_NUMBER_DIGITS) {
      words.push(...numberToWords(Number(token)));
    } else {
      words.push(token.replace(/^'+|'+$/g, ""));
    }
  }
  return words.filter(
    (word, index) =>
      word &&
      !(
        word === "and" &&
        index > 0 &&
        index < words.length - 1 &&
        NUMBER_WORDS.has(words[index - 1]) &&
        NUMBER_WORDS.has(words[index + 1])
      )
  );
};

/**
 * Levenshtein distance over word arrays (two-row dynamic programme).
 *
 * @param {string[]} reference
 * @param {string[]} hypothesis
 * @returns {number}
 */
export const editDistance = (reference, hypothesis) => {
  let previous = Array.from({length: hypothesis.length + 1}, (_, index) => index);
  for (let row = 1; row <= reference.length; row += 1) {
    const current = [row];
    for (let column = 1; column <= hypothesis.length; column += 1) {
      const substitution =
        previous[column - 1] + (reference[row - 1] === hypothesis[column - 1] ? 0 : 1);
      current.push(Math.min(substitution, previous[column] + 1, current[column - 1] + 1));
    }
    previous = current;
  }
  return previous[hypothesis.length];
};

/**
 * Word error rate of `hypothesis` against `reference`: 0 is a perfect match,
 * 1.0 means as many errors as there are reference words (it can exceed 1).
 *
 * @param {string} reference
 * @param {string} hypothesis
 * @returns {number}
 */
export const wordErrorRate = (reference, hypothesis) => {
  const referenceWords = normalizeWords(reference);
  const hypothesisWords = normalizeWords(hypothesis);
  return editDistance(referenceWords, hypothesisWords) / Math.max(1, referenceWords.length);
};
