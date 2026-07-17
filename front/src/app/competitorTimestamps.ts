export type CompetitorTimestampRow = {
  priceDate?: string | null;
  lastSuccessAt?: string | null;
  lastCheckedAt?: string | null;
  updatedAt?: string | null;
  sourceUpdatedAt?: string | null;
};

const EMPTY = '—';

const parseTime = (value?: string | null) => {
  const time = value ? Date.parse(value) : NaN;
  return Number.isFinite(time) ? time : null;
};

export const competitorPriceDate = (row: CompetitorTimestampRow) => row.priceDate || '';

export const competitorLastSuccessfulCheck = (row: CompetitorTimestampRow) =>
  row.lastSuccessAt || row.lastCheckedAt || '';

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
  const marker = competitorLastSuccessfulCheck(row);
  const time = parseTime(marker);
  if (time === null) return 'нет данных';
  const ageDays = (Date.now() - time) / 86400000;
  return ageDays <= 2 ? 'актуально' : 'устарело';
};

export const competitorFreshnessClassName = (row: CompetitorTimestampRow) => {
  const label = competitorFreshnessLabel(row);
  if (label === 'актуально') return 'ok';
  if (label === 'нет данных') return '';
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
