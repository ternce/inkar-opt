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
  const status = String(row.status || '').toLowerCase();
  if (status === 'timeout' || status === 'stale') return '\u0443\u0441\u0442\u0430\u0440\u0435\u043b\u043e';
  if (status === 'failed' || status === 'error') return '\u043e\u0448\u0438\u0431\u043a\u0430';

  const marker = competitorLastSuccessfulCheck(row);
  const time = parseTime(marker);
  if (time === null) return '\u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445';
  const ageDays = (Date.now() - time) / 86400000;
  return ageDays <= 2 ? '\u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u043e' : '\u0443\u0441\u0442\u0430\u0440\u0435\u043b\u043e';
};

export const competitorFreshnessClassName = (row: CompetitorTimestampRow) => {
  const label = competitorFreshnessLabel(row);
  if (label === '\u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u043e') return 'ok';
  if (label === '\u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445') return '';
  if (label === '\u043e\u0448\u0438\u0431\u043a\u0430') return 'bad';
  return 'warn';
};

export const usefulSourceTimestamp = (row: CompetitorTimestampRow) => {
  const source = competitorSourceTimestamp(row);
  if (!source) return '';
  if (source === row.priceDate || source === row.updatedAt || source === row.lastSuccessAt || source === row.lastCheckedAt) {
    return '';
  }
  return source;
};
