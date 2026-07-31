export const CURRENT_FORMAT_SETTINGS = '__current_format_settings__';
export const CREATE_NEW_TEMPLATE = 'new';

export type TemplateKind = 'markup' | 'bend' | 'noCompetitor';

export type TemplateSummary = {
  id: number;
  name?: string;
};

export type PriceFormatTemplateSettings = {
  appliedMarkupTemplateId?: number | null;
  appliedBendTemplateId?: number | null;
  appliedNoCompetitorTemplateId?: number | null;
  appliedRoundingRuleId?: number | null;
};

export type TemplateEditorMode = 'applied' | 'existing_template' | 'create_new' | 'current_format';

export function appliedTemplateIdForKind(settings: PriceFormatTemplateSettings | null | undefined, kind: TemplateKind) {
  if (!settings) return null;
  if (kind === 'markup') return settings.appliedMarkupTemplateId ?? null;
  if (kind === 'bend') return settings.appliedBendTemplateId ?? null;
  return settings.appliedNoCompetitorTemplateId ?? null;
}

export function resolveInitialTemplateSelection(
  settings: PriceFormatTemplateSettings | null | undefined,
  kind: TemplateKind,
  templates: TemplateSummary[]
) {
  const appliedId = appliedTemplateIdForKind(settings, kind);
  if (!appliedId) {
    return {
      selectedId: CURRENT_FORMAT_SETTINGS,
      mode: 'current_format' as TemplateEditorMode,
      missingAppliedTemplate: false,
    };
  }
  const exists = templates.some((template) => Number(template.id) === Number(appliedId));
  return {
    selectedId: exists ? String(appliedId) : CURRENT_FORMAT_SETTINGS,
    mode: exists ? ('applied' as TemplateEditorMode) : ('current_format' as TemplateEditorMode),
    missingAppliedTemplate: !exists,
  };
}

export function resolveExplicitTemplateSelection(value: string, appliedId?: number | null): TemplateEditorMode {
  if (value === CREATE_NEW_TEMPLATE) return 'create_new';
  if (value === CURRENT_FORMAT_SETTINGS) return 'current_format';
  return Number(value) === Number(appliedId) ? 'applied' : 'existing_template';
}

export function resolveInitialRoundingSelection(settings: PriceFormatTemplateSettings | null | undefined, rules: TemplateSummary[]) {
  const appliedId = settings?.appliedRoundingRuleId ?? null;
  if (!appliedId) {
    return {
      selectedId: CURRENT_FORMAT_SETTINGS,
      mode: 'current_format' as TemplateEditorMode,
      missingAppliedTemplate: false,
    };
  }
  const exists = rules.some((rule) => Number(rule.id) === Number(appliedId));
  return {
    selectedId: exists ? String(appliedId) : CURRENT_FORMAT_SETTINGS,
    mode: exists ? ('applied' as TemplateEditorMode) : ('current_format' as TemplateEditorMode),
    missingAppliedTemplate: !exists,
  };
}
