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
  isActive: boolean;
};

export const NO_COPY_SOURCE = 'none';

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
