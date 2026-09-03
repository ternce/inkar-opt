export type PricingRuleDraft = {
  id: number;
  code: string;
  name: string;
  description: string;
  regionScope: string;
  branchScope: string;
  markupTemplateId: number | null;
  bendTemplateId: number | null;
  noCompetitorTemplateId: number | null;
  roundingRuleId: number | null;
  competitorGapThresholds: CompetitorGapThreshold[];
  isActive: boolean;
};

export type CompetitorGapThreshold = {
  id?: number;
  minPrice: number;
  maxPrice: number | null;
  maxGapPercent: number | string;
  sortOrder?: number;
};

export const NO_COPY_SOURCE = 'none';
export const NEW_PRICING_RULE_SELECTION = 'new';
export const NO_FORMAT_PRICING_RULE_SELECTION = 'none';

export const defaultCompetitorGapThresholds = (): CompetitorGapThreshold[] => [
  { minPrice: 0, maxPrice: 500, maxGapPercent: 15, sortOrder: 0 },
  { minPrice: 500, maxPrice: 2500, maxGapPercent: 12, sortOrder: 1 },
  { minPrice: 2500, maxPrice: 5000, maxGapPercent: 10, sortOrder: 2 },
  { minPrice: 5000, maxPrice: 10000, maxGapPercent: 8, sortOrder: 3 },
  { minPrice: 10000, maxPrice: 25000, maxGapPercent: 7, sortOrder: 4 },
  { minPrice: 25000, maxPrice: null, maxGapPercent: 5, sortOrder: 5 },
];

export const emptyPricingRuleDraft = (): PricingRuleDraft => ({
  id: 0,
  code: '',
  name: '',
  description: '',
  regionScope: '',
  branchScope: '',
  markupTemplateId: null,
  bendTemplateId: null,
  noCompetitorTemplateId: null,
  roundingRuleId: null,
  competitorGapThresholds: defaultCompetitorGapThresholds(),
  isActive: true,
});

export const hydratePricingRuleDraft = (rule: Partial<PricingRuleDraft>): PricingRuleDraft => ({
  ...emptyPricingRuleDraft(),
  ...rule,
  markupTemplateId: rule.markupTemplateId ?? null,
  bendTemplateId: rule.bendTemplateId ?? null,
  noCompetitorTemplateId: rule.noCompetitorTemplateId ?? null,
  roundingRuleId: rule.roundingRuleId ?? null,
  competitorGapThresholds: Array.isArray(rule.competitorGapThresholds) && rule.competitorGapThresholds.length
    ? rule.competitorGapThresholds.map((row, index) => ({
        id: row.id,
        minPrice: Number(row.minPrice),
        maxPrice: row.maxPrice == null ? null : Number(row.maxPrice),
        maxGapPercent: row.maxGapPercent,
        sortOrder: row.sortOrder ?? index,
      }))
    : defaultCompetitorGapThresholds(),
});

export const buildPricingRuleCreatePayload = (draft: PricingRuleDraft, copyFromRuleId: string) => {
  const payload: Record<string, unknown> = { ...draft };
  if (copyFromRuleId !== NO_COPY_SOURCE) {
    payload.copyFromRuleId = Number(copyFromRuleId);
  }
  return payload;
};

export const draftFromCopySource = (currentDraft: PricingRuleDraft, source: PricingRuleDraft): PricingRuleDraft => ({
  ...currentDraft,
  description: source.description,
  regionScope: source.regionScope,
  branchScope: source.branchScope,
  markupTemplateId: source.markupTemplateId ?? null,
  bendTemplateId: source.bendTemplateId ?? null,
  noCompetitorTemplateId: source.noCompetitorTemplateId ?? null,
  roundingRuleId: source.roundingRuleId ?? null,
  competitorGapThresholds: source.competitorGapThresholds.map((row) => ({ ...row })),
  isActive: source.isActive,
});

export const applyPricingRuleCreateSuccess = (created: PricingRuleDraft) => ({
  selectedRuleId: String(created.id),
  draft: created,
  copyFromRuleId: NO_COPY_SOURCE,
});

export const canSubmitPricingRuleCreate = (isLoading: boolean) => !isLoading;

export const pricingRuleCreateErrorMessage = (data: any, text: string) =>
  data?.detail || text || 'Не удалось сохранить правило';

export const shouldHydrateEditorFromFormatRuleSelection = (value: string) =>
  value !== NO_FORMAT_PRICING_RULE_SELECTION;

export const pricingRuleEditorTargetFromFormatSelection = (value: string) => ({
  formatRuleId: value,
  hydrateEditorRuleId: shouldHydrateEditorFromFormatRuleSelection(value) ? value : null,
});

export const isLatestPricingRuleLoadResponse = (requestId: number, latestRequestId: number) =>
  requestId === latestRequestId;
