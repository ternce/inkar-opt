export type CompetitorAssignmentSourceRow = {
  id: string;
  sourceId: string | number;
  sourceType: string;
  sourceKey?: string;
  sourceName: string;
  name?: string;
  region: string;
  branchName: string;
  competitorName: string;
  accountId?: string;
  accountLogin: string;
  priceDate: string;
  updatedAt?: string;
  sourceUpdatedAt?: string;
  lastCheckedAt?: string;
  lastSuccessAt?: string;
  lastUpdatedAt?: string;
  generatedAt?: string;
  itemsCount: number;
  skuCount?: number;
  status?: string;
  refreshStatus?: string;
  lastRefreshStatus?: string;
  coefficient?: number;
  priceCoefficient?: number;
  active?: boolean;
  isSelected?: boolean;
  isPercentile?: boolean;
  eligibleForPricing?: boolean;
  pricingEligibilityReason?: string;
  rowType?: 'physical_plk' | 'percentile_config' | string;
  assignmentKind?: 'physical' | 'percentile_config' | string;
  percentile?: number;
};

const EMIT_DISPLAY_NAMES: Record<string, string> = {
  '1052': 'Эмити Интернешнл Алматы',
  '1076': 'Эмити Интернешнл Астана',
  '1106': 'Эмити Интернешнл Актау',
  '1107': 'Эмити Интернешнл Шымкент',
  '1108': 'Эмити Интернешнл Костанай',
  '1111': 'Эмити Интернешнл Павлодар',
  '1114': 'Эмити Интернешнл Уральск',
  '1140': 'Эмити Интернешнл Талдыкорган',
  '1149': 'Эмити Интернешнл Петропавловск',
};

export const emitDisplayFallback = (sourceKey?: string, value?: string) => {
  const text = String(value || '').trim();
  const match = String(sourceKey || '').trim().match(/^emit:(\d+)$/i);
  const mapped = match ? EMIT_DISPLAY_NAMES[match[1]] : '';
  if (!mapped) return text;
  return !text || text === sourceKey || /^emit:?\s*\d+$/i.test(text) || /^emit international\s+\d+$/i.test(text) ? mapped : text;
};

export const normalizeCompetitorAssignmentSource = (row: any): CompetitorAssignmentSourceRow => {
  const sourceKey = String(row.sourceKey || row.id || '');
  const sourceName = emitDisplayFallback(sourceKey, String(row.sourceName || row.name || row.displayName || ''));
  const branchName = emitDisplayFallback(sourceKey, String(row.branchName || row.region || ''));
  const competitorName = emitDisplayFallback(sourceKey, String(row.competitorName || row.competitor || row.supplier || ''));
  return {
    id: String(row.id ?? row.sourceId ?? ''),
    sourceId: row.sourceId ?? row.id ?? '',
    sourceType: String(row.sourceType || 'manual'),
    sourceKey,
    sourceName,
    name: emitDisplayFallback(sourceKey, String(row.name || row.sourceName || '')),
    region: branchName,
    branchName,
    competitorName,
    accountId: String(row.accountId || ''),
    accountLogin: String(row.accountLogin || row.accountId || ''),
    priceDate: String(row.priceDate || row.generatedAt || ''),
    updatedAt: String(row.updatedAt || ''),
    sourceUpdatedAt: String(row.sourceUpdatedAt || ''),
    lastCheckedAt: String(row.lastCheckedAt || ''),
    lastSuccessAt: String(row.lastSuccessAt || ''),
    lastUpdatedAt: String(row.lastUpdatedAt || ''),
    generatedAt: String(row.generatedAt || ''),
    itemsCount: Number(row.itemsCount ?? row.skuCount ?? 0),
    skuCount: Number(row.skuCount ?? row.itemsCount ?? 0),
    status: String(row.status || row.refreshStatus || row.lastRefreshStatus || row.last_refresh_status || ''),
    refreshStatus: String(row.refreshStatus || row.status || row.lastRefreshStatus || row.last_refresh_status || ''),
    lastRefreshStatus: String(row.lastRefreshStatus || row.last_refresh_status || row.refreshStatus || row.status || ''),
    coefficient: Number(row.priceCoefficient ?? row.coefficient ?? 1),
    priceCoefficient: Number(row.priceCoefficient ?? row.coefficient ?? 1),
    active: Boolean(row.active ?? row.isSelected ?? true),
    isSelected: Boolean(row.isSelected),
    eligibleForPricing: row.eligibleForPricing !== false,
    pricingEligibilityReason: String(row.pricingEligibilityReason || ''),
    rowType: String(row.rowType || (row.sourceType === 'percentile' || row.isPercentile ? 'percentile_config' : 'physical_plk')),
    assignmentKind: String(row.assignmentKind || (row.sourceType === 'percentile' || row.isPercentile ? 'percentile_config' : 'physical')),
    isPercentile: row.sourceType === 'percentile' || Boolean(row.isPercentile),
    percentile: row.percentile != null ? Number(row.percentile) : undefined,
  };
};

export const percentileToCompetitorAssignmentSource = (row: any): CompetitorAssignmentSourceRow =>
  normalizeCompetitorAssignmentSource({
    id: row.id,
    sourceId: row.id,
    sourceKey: row.sourceKey || row.id,
    sourceType: 'percentile',
    sourceName: row.name || `${row.region || 'Регион'} - Эмити - Персентиль ${row.percentile}`,
    region: row.region,
    branchName: row.region,
    competitorName: row.competitor || 'Эмити',
    accountLogin: `Персентиль ${row.percentile}`,
    priceDate: row.generatedAt,
    lastSuccessAt: row.generatedAt,
    lastCheckedAt: row.generatedAt,
    updatedAt: row.generatedAt,
    sourceUpdatedAt: row.generatedAt,
    generatedAt: row.generatedAt,
    itemsCount: row.skuCount,
    skuCount: row.skuCount,
    eligibleForPricing: row.eligibleForPricing,
    pricingEligibilityReason: row.pricingEligibilityReason,
    percentile: row.percentile,
    isPercentile: true,
  });

const isPercentileConfig = (row: CompetitorAssignmentSourceRow) =>
  row.sourceType === 'percentile' || row.isPercentile || row.assignmentKind === 'percentile_config' || row.rowType === 'percentile_config';

export const physicalCompetitorSourceIdentity = (row: CompetitorAssignmentSourceRow) => {
  if (isPercentileConfig(row)) return '';
  const id = row.sourceId || row.id;
  return id ? `${row.sourceType}:${String(id)}` : '';
};

const sourceIdentity = (row: CompetitorAssignmentSourceRow) => {
  const physicalIdentity = physicalCompetitorSourceIdentity(row);
  if (physicalIdentity) return physicalIdentity;
  return `${row.sourceType}:${String(row.sourceId || row.id || row.sourceKey || row.sourceName)}`;
};

export const mergeAvailableCompetitorAssignmentSources = ({
  globalPriceLists,
  percentileSources,
  assignments,
}: {
  globalPriceLists: any[];
  percentileSources: any[];
  assignments: CompetitorAssignmentSourceRow[];
}) => {
  const assignedPhysicalIds = new Set(assignments.map(physicalCompetitorSourceIdentity).filter(Boolean));
  const seen = new Set<string>();
  const rows = [
    ...globalPriceLists.map(normalizeCompetitorAssignmentSource).filter((row) => !isPercentileConfig(row)),
    ...percentileSources.map(percentileToCompetitorAssignmentSource),
  ];

  return rows.flatMap((row) => {
    const identity = sourceIdentity(row);
    if (seen.has(identity)) return [];
    seen.add(identity);
    return [{ ...row, isSelected: assignedPhysicalIds.has(physicalCompetitorSourceIdentity(row)) }];
  });
};
