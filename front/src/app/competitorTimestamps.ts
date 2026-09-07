export type CompetitorTimestampRow = {
  priceDate?: string | null;
  lastSuccessAt?: string | null;
  lastCheckedAt?: string | null;
  lastUpdatedAt?: string | null;
  updatedAt?: string | null;
  sourceUpdatedAt?: string | null;
  status?: string | null;
};

const EMPTY = '—';

const parseTime = (value?: string | null) => {
  const time = value ? Date.parse(value) : NaN;
  return Number.isFinite(time) ? time : null;
};

const SUCCESS_STATUSES = new Set(['updated', 'ok', 'success', 'success_zero_items']);
const UNCHANGED_STATUSES = new Set(['checked_unchanged']);
const ERROR_STATUSES = new Set(['failed', 'error', 'stale']);
const RECENT_SUCCESS_MS = 2 * 86400000;

const CHECK_CURRENT_LABEL = '\u0410\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u043e';
const CHECK_UNCHANGED_LABEL = '\u0410\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u043e, \u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439';
const CHECK_TIMEOUT_LABEL = '\u0422\u0430\u0439\u043c-\u0430\u0443\u0442 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438';
const CHECK_ERROR_LABEL = '\u041e\u0448\u0438\u0431\u043a\u0430 \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f';
const CHECK_EMPTY_LABEL = '\u041d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445';
const LAST_CHECK_PREFIX = '\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430';

export const competitorPriceDate = (row: CompetitorTimestampRow) => row.priceDate || '';

export const competitorLastSuccessfulCheck = (row: CompetitorTimestampRow) =>
  row.lastSuccessAt || row.lastUpdatedAt || row.lastCheckedAt || '';

export const competitorLastDataReplacement = (row: CompetitorTimestampRow) => row.updatedAt || '';

export const competitorSourceTimestamp = (row: CompetitorTimestampRow) => row.sourceUpdatedAt || '';

export const formatLocalDate = (value?: string | null) => {
  if (!value) return EMPTY;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleDateString('ru-RU');
};

export const formatLocalDateTime = (value?: string | null) => {
  if (!value) return EMPTY;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('ru-RU');
};

export const competitorFreshnessLabel = (row: CompetitorTimestampRow) => {
  const status = String(row.status || '').split(';', 1)[0].trim().toLowerCase();
  if (status === 'timeout') return CHECK_TIMEOUT_LABEL;
  if (ERROR_STATUSES.has(status)) return CHECK_ERROR_LABEL;

  const marker = competitorLastSuccessfulCheck(row);
  const time = parseTime(marker);
  if (time === null) return CHECK_EMPTY_LABEL;
  if (UNCHANGED_STATUSES.has(status) && Date.now() - time <= RECENT_SUCCESS_MS) return CHECK_UNCHANGED_LABEL;
  if ((SUCCESS_STATUSES.has(status) || !status) && Date.now() - time <= RECENT_SUCCESS_MS) return CHECK_CURRENT_LABEL;
  return `${LAST_CHECK_PREFIX}: ${formatLocalDate(marker)}`;
};

export const competitorFreshnessClassName = (row: CompetitorTimestampRow) => {
  const label = competitorFreshnessLabel(row);
  if (label === CHECK_CURRENT_LABEL || label === CHECK_UNCHANGED_LABEL) return 'ok';
  if (label === CHECK_TIMEOUT_LABEL) return 'warn';
  if (label === CHECK_ERROR_LABEL) return 'bad';
  return '';
};

export const usefulSourceTimestamp = (row: CompetitorTimestampRow) => {
  const source = competitorSourceTimestamp(row);
  if (!source) return '';
  if (source === row.priceDate || source === row.updatedAt || source === row.lastSuccessAt || source === row.lastCheckedAt) {
    return '';
  }
  return source;
};
