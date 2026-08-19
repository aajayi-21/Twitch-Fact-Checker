// Small pure helpers shared by the console and the overlay.

/** Clamp a numeric query param; non-numeric input falls back. */
export const clampNum = (value, low, high, fallback) => {
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? Math.min(high, Math.max(low, parsed))
    : fallback;
};

/** Seconds -> "m:ss" (floor at 0). */
export const mmss = (seconds) => {
  const total = Math.max(0, Math.floor(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
};

/** Seconds -> "1:42:10" when >= 1h else "42:10". */
export const hms = (seconds) => {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
};

/**
 * Registrable-ish domain of an http(s) URL, or null.
 *
 * Ported guard from the extension overlay: http(s) ONLY, so a junk or
 * javascript: URL from a hostile source never renders as a chip.
 */
export const domainOf = (url) => {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return parsed.hostname.replace(/^www\./, "");
  } catch {
    return null;
  }
};

/** Distinct source domains, capped; ["nasa.gov", "+1"] style collapse. */
export const sourceDomains = (sources, cap = 2) => {
  const seen = [];
  for (const source of sources || []) {
    const domain = domainOf(source.url);
    if (domain && !seen.includes(domain)) seen.push(domain);
  }
  const shown = seen.slice(0, cap);
  const extra = seen.length - shown.length;
  return { shown, extra };
};

/** "21:18" local wall-clock from an ISO string. */
export const wallClock = (iso) => {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
};
