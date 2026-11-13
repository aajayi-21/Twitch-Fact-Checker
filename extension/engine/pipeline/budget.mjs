/**
 * Spending limits for the browser engine.
 *
 * The viewer pays their AI provider directly, per call, and a stream can run
 * for hours. Two caps, both on by default, stop a session left running from
 * becoming a surprise bill: a number of fact-checks per day, and a dollar
 * amount per calendar month. Either one reaching its limit stops ALL
 * provider calls (claim detection included — it costs money too) until the
 * day or month rolls over or the viewer raises the limit.
 *
 * Pure: unit-tested under Node.
 */

export const DEFAULT_BUDGETS = Object.freeze({
  // A fact-check costs about a cent, so this is roughly $1.50 a day at most.
  dailyChecks: 150,
  monthlyUsd: 10,
});

/** Tell the viewer once they have used this share of a limit. */
export const WARN_FRACTION = 0.8;

export const BUDGET_STATE = Object.freeze({OK: "ok", WARN: "warn", STOP: "stop"});

const limitOf = (value) => (Number.isFinite(value) && value > 0 ? value : null);

/**
 * @param {unknown} stored
 * @returns {{dailyChecks: number, monthlyUsd: number}} 0 means "no limit"
 */
export const normalizeBudgets = (stored) => {
  const record = stored && typeof stored === "object" ? stored : {};
  const clean = (value, fallback, max) =>
    Number.isFinite(value) && value >= 0 ? Math.min(value, max) : fallback;
  return {
    dailyChecks: Math.round(clean(record.dailyChecks, DEFAULT_BUDGETS.dailyChecks, 100_000)),
    monthlyUsd: clean(record.monthlyUsd, DEFAULT_BUDGETS.monthlyUsd, 100_000),
  };
};

/**
 * Where spending stands against the limits.
 *
 * @param {{dailyChecks: number, monthlyUsd: number}} budgets
 * @param {{checksToday: number, spendThisMonthUsd: number}} spent
 * @returns {{state: string, limit: "daily"|"monthly"|null, message: string|null}}
 */
export const budgetStatus = (budgets, spent) => {
  const checks = [
    {
      limit: "daily",
      used: spent.checksToday,
      cap: limitOf(budgets.dailyChecks),
      reached: (cap) => `Today's limit of ${cap} fact-checks has been reached.`,
      near: (cap, used) => `${used} of today's ${cap} fact-checks used.`,
    },
    {
      limit: "monthly",
      used: spent.spendThisMonthUsd,
      cap: limitOf(budgets.monthlyUsd),
      reached: (cap) => `This month's spending limit of $${cap.toFixed(2)} has been reached.`,
      near: (cap, used) => `$${used.toFixed(2)} of this month's $${cap.toFixed(2)} limit spent.`,
    },
  ].filter((check) => check.cap !== null);

  const over = checks.find((check) => check.used >= check.cap);
  if (over) {
    return {
      state: BUDGET_STATE.STOP,
      limit: over.limit,
      message: `${over.reached(over.cap)} Checking is paused; raise the limit in Settings to continue.`,
    };
  }
  const close = checks.find((check) => check.used >= check.cap * WARN_FRACTION);
  if (close) {
    return {state: BUDGET_STATE.WARN, limit: close.limit, message: close.near(close.cap, close.used)};
  }
  return {state: BUDGET_STATE.OK, limit: null, message: null};
};
