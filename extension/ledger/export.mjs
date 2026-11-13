/**
 * Turning ledger rows into files the viewer can keep: CSV for spreadsheets,
 * JSON for everything. Pure — the app does the actual download.
 */

export const EXPORT_FORMAT_VERSION = 1;

const CSV_COLUMNS = Object.freeze([
  ["checked_at", (verdict) => verdict.checked_at],
  ["platform", (verdict) => verdict.platform],
  ["channel", (verdict) => verdict.channel_name ?? verdict.channel_id],
  ["stream_title", (verdict) => verdict.stream_title],
  ["stream_url", (verdict) => verdict.stream_url],
  ["topic", (verdict) => verdict.topic],
  ["label", (verdict) => verdict.label],
  ["claim", (verdict) => verdict.claim_text],
  ["explanation", (verdict) => verdict.explanation],
  ["sources", (verdict) => (verdict.sources ?? []).map((source) => source.url).join(" ")],
  ["your_rating", (verdict, feedback) => feedback?.rating],
  ["your_corrected_label", (verdict, feedback) => feedback?.corrected_label],
  ["provider", (verdict) => verdict.provider],
  ["model", (verdict) => verdict.model],
  ["latency_ms", (verdict) => verdict.latency_ms],
  ["verdict_id", (verdict) => verdict.id],
]);

/**
 * One CSV cell.
 *
 * Claims are transcribed speech — text this app does not control. A cell
 * that begins with "=", "+", "-" or "@" is run as a formula by spreadsheet
 * programs, so such cells are prefixed with an apostrophe, which makes them
 * plain text. Numbers are left alone.
 *
 * @param {unknown} value
 * @returns {string}
 */
export const csvCell = (value) => {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? String(value) : "";
  }
  let cell = String(value);
  if (/^[=+\-@\t\r]/.test(cell)) {
    cell = `'${cell}`;
  }
  return /[",\r\n]/.test(cell) ? `"${cell.replaceAll('"', '""')}"` : cell;
};

/**
 * @param {object[]} verdicts - in the order they should appear
 * @param {Map<string, object>} [feedbackByVerdict]
 * @returns {string} CSV text with a header row and CRLF line endings
 */
export const verdictsToCsv = (verdicts, feedbackByVerdict = new Map()) => {
  const header = CSV_COLUMNS.map(([name]) => name).join(",");
  const lines = verdicts.map((verdict) =>
    CSV_COLUMNS.map(([, valueOf]) =>
      csvCell(valueOf(verdict, feedbackByVerdict.get(verdict.id)))
    ).join(",")
  );
  return `${[header, ...lines].join("\r\n")}\r\n`;
};

/**
 * The whole ledger (or a filtered part of it) as one JSON document.
 *
 * @param {object} ledger
 * @param {object[]} [ledger.sessions]
 * @param {object[]} [ledger.claims]
 * @param {object[]} [ledger.verdicts]
 * @param {object[]} [ledger.feedback]
 * @param {object[]} [ledger.usage]
 * @param {object} meta
 * @param {string} meta.exportedAt - ISO time
 * @param {string} meta.appVersion
 * @returns {string} pretty-printed JSON
 */
export const ledgerToJson = (
  {sessions = [], claims = [], verdicts = [], feedback = [], usage = []},
  {exportedAt, appVersion}
) =>
  `${JSON.stringify(
    {
      format: "live-stream-fact-checker-export",
      format_version: EXPORT_FORMAT_VERSION,
      exported_at: exportedAt,
      app_version: appVersion,
      sessions,
      claims,
      verdicts,
      feedback,
      usage,
    },
    null,
    2
  )}\n`;

/**
 * @param {string} stem - e.g. "fact-checks"
 * @param {string} extension - without the dot
 * @param {Date} [now]
 * @returns {string} e.g. "fact-checks-2026-11-12.csv"
 */
export const exportFileName = (stem, extension, now = new Date()) => {
  const pad = (number) => String(number).padStart(2, "0");
  const day = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  return `${stem}-${day}.${extension}`;
};
