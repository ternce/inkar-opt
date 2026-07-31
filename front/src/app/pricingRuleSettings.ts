export type PricingRuleTemplateRow = {
  id?: number;
  costFrom?: number | string | null;
  costTo?: number | string | null;
  markupPercent?: number | string | null;
  bendPercent?: number | string | null;
};

export type PricingRuleTemplate = {
  id?: number | null;
  rows?: PricingRuleTemplateRow[] | null;
};

export type ResolvedPricingRule = {
  id: number;
  code?: string;
  name?: string;
  markupTemplateId?: number | null;
  bendTemplateId?: number | null;
  noCompetitorTemplateId?: number | null;
  roundingRuleId?: number | null;
  markupTemplate?: PricingRuleTemplate | null;
  bendTemplate?: PricingRuleTemplate | null;
  noCompetitorTemplate?: PricingRuleTemplate | null;
};

export type ResolvedMarkupRow = {
  id: number;
  lowerBound: string;
  upperBound: string;
  markupPercent: string;
};

export type ResolvedBendRow = {
  id: number;
  priceFrom: string;
  bendPercent: string;
};

export type ResolvedPricingRuleSettings = {
  pricingRule: string;
  roundingRuleId: string;
  recommendedMarkups: ResolvedMarkupRow[];
  noCompetitorMarkups: ResolvedMarkupRow[];
  bendRanges: ResolvedBendRow[];
  hasLinkedSettings: boolean;
  missingLinkedTemplates: string[];
};

const valueText = (value: number | string | null | undefined, fallback = '') =>
  value === null || value === undefined ? fallback : String(value);

const markupRows = (rows: PricingRuleTemplateRow[] | null | undefined): ResolvedMarkupRow[] =>
  (Array.isArray(rows) ? rows : []).map((row, index) => ({
    id: index + 1,
    lowerBound: valueText(row.costFrom, '0'),
    upperBound: valueText(row.costTo, '99999999'),
    markupPercent: valueText(row.markupPercent),
  }));

const bendRows = (rows: PricingRuleTemplateRow[] | null | undefined): ResolvedBendRow[] =>
  (Array.isArray(rows) ? rows : []).map((row, index) => ({
    id: index + 1,
    priceFrom: valueText(row.costFrom, '0'),
    bendPercent: valueText(row.bendPercent),
  }));

export function resolvePricingRuleSettings(rule: ResolvedPricingRule): ResolvedPricingRuleSettings {
  const recommendedMarkups = markupRows(rule.markupTemplate?.rows);
  const noCompetitorMarkups = markupRows(rule.noCompetitorTemplate?.rows);
  const bendRanges = bendRows(rule.bendTemplate?.rows);
  const missingLinkedTemplates = [
    rule.markupTemplateId && !rule.markupTemplate ? 'recommendedMarkups' : '',
    rule.bendTemplateId && !rule.bendTemplate ? 'bendRanges' : '',
    rule.noCompetitorTemplateId && !rule.noCompetitorTemplate ? 'noCompetitorMarkups' : '',
  ].filter(Boolean);

  return {
    pricingRule: String(rule.code || rule.name || ''),
    roundingRuleId: rule.roundingRuleId ? String(rule.roundingRuleId) : 'none',
    recommendedMarkups,
    noCompetitorMarkups,
    bendRanges,
    hasLinkedSettings:
      recommendedMarkups.length > 0 ||
      noCompetitorMarkups.length > 0 ||
      bendRanges.length > 0 ||
      Boolean(rule.roundingRuleId),
    missingLinkedTemplates,
  };
}
