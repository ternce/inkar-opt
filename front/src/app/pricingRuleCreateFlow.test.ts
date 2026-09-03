import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  NO_COPY_SOURCE,
  NO_FORMAT_PRICING_RULE_SELECTION,
  NEW_PRICING_RULE_SELECTION,
  applyPricingRuleCreateSuccess,
  buildPricingRuleCreatePayload,
  canSubmitPricingRuleCreate,
  defaultCompetitorGapThresholds,
  draftFromCopySource,
  emptyPricingRuleDraft,
  hydratePricingRuleDraft,
  isLatestPricingRuleLoadResponse,
  pricingRuleEditorTargetFromFormatSelection,
  pricingRuleCreateErrorMessage,
  shouldHydrateEditorFromFormatRuleSelection,
  type PricingRuleDraft,
} from './pricingRuleCreateFlow.ts';

const draft = (patch: Partial<PricingRuleDraft> = {}): PricingRuleDraft => ({
  id: 0,
  code: 'NEW',
  name: 'New rule',
  description: '',
  regionScope: '',
  branchScope: '',
  markupTemplateId: null,
  bendTemplateId: null,
  noCompetitorTemplateId: null,
  roundingRuleId: null,
  competitorGapThresholds: defaultCompetitorGapThresholds(),
  isActive: true,
  ...patch,
});

test('create form defaults to no copy source', () => {
  assert.equal(NO_COPY_SOURCE, 'none');
  assert.deepEqual(buildPricingRuleCreatePayload(draft(), NO_COPY_SOURCE), draft());
});

test('selecting a source sends the selected source rule id', () => {
  assert.equal(buildPricingRuleCreatePayload(draft(), '12').copyFromRuleId, 12);
});

test('successful copy selects the new rule and resets the copy source', () => {
  const created = draft({ id: 7, code: 'COPIED' });
  assert.deepEqual(applyPricingRuleCreateSuccess(created), {
    selectedRuleId: '7',
    draft: created,
    copyFromRuleId: NO_COPY_SOURCE,
  });
});

test('double submission is prevented while loading', () => {
  assert.equal(canSubmitPricingRuleCreate(false), true);
  assert.equal(canSubmitPricingRuleCreate(true), false);
});

test('api error detail is displayed', () => {
  assert.equal(pricingRuleCreateErrorMessage({ detail: 'Duplicate' }, 'fallback'), 'Duplicate');
});

test('copy source prefill does not mutate source frontend state', () => {
  const source = draft({ id: 1, code: 'SRC', name: 'Source', markupTemplateId: 10, bendTemplateId: 20, roundingRuleId: 30 });
  const before = { ...source };
  const copiedDraft = draftFromCopySource(draft({ code: 'NEW', name: 'New name' }), source);

  assert.equal(copiedDraft.code, 'NEW');
  assert.equal(copiedDraft.name, 'New name');
  assert.equal(copiedDraft.markupTemplateId, 10);
  assert.deepEqual(source, before);
});

test('new rule draft starts with empty linked settings', () => {
  assert.deepEqual(emptyPricingRuleDraft(), draft({ code: '', name: '' }));
});

test('rule drafts include default competitor gap thresholds', () => {
  const hydrated = hydratePricingRuleDraft({ code: 'GAP', name: 'Gap' });

  assert.deepEqual(
    hydrated.competitorGapThresholds.map((row) => [row.minPrice, row.maxPrice, row.maxGapPercent]),
    [
      [0, 500, 15],
      [500, 2500, 12],
      [2500, 5000, 10],
      [5000, 10000, 8],
      [10000, 25000, 7],
      [25000, null, 5],
    ]
  );
});

test('top selector selecting Rule A opens lower editor through existing rule hydration target', () => {
  const target = pricingRuleEditorTargetFromFormatSelection('1');
  const ruleA = hydratePricingRuleDraft(draft({ id: 1, code: 'A', name: 'Rule A', markupTemplateId: 10, bendTemplateId: 20, noCompetitorTemplateId: 30, roundingRuleId: 40 }));

  assert.deepEqual(target, { formatRuleId: '1', hydrateEditorRuleId: '1' });
  assert.deepEqual(
    [ruleA.id, ruleA.name, ruleA.markupTemplateId, ruleA.bendTemplateId, ruleA.noCompetitorTemplateId, ruleA.roundingRuleId],
    [1, 'Rule A', 10, 20, 30, 40]
  );
});

test('top selector selecting Rule B replaces all Rule A hydrated values', () => {
  const ruleA = hydratePricingRuleDraft(draft({ id: 1, code: 'A', name: 'Rule A', markupTemplateId: 10, bendTemplateId: 20, noCompetitorTemplateId: 30, roundingRuleId: 40 }));
  const ruleB = hydratePricingRuleDraft(draft({ id: 2, code: 'B', name: 'Rule B', markupTemplateId: 11, bendTemplateId: null, noCompetitorTemplateId: 31, roundingRuleId: null }));

  assert.deepEqual(
    [ruleA.markupTemplateId, ruleA.bendTemplateId, ruleA.noCompetitorTemplateId, ruleA.roundingRuleId],
    [10, 20, 30, 40]
  );
  assert.deepEqual(
    [ruleB.markupTemplateId, ruleB.bendTemplateId, ruleB.noCompetitorTemplateId, ruleB.roundingRuleId],
    [11, null, 31, null]
  );
});

test('top selector only targets editor hydration and does not build an apply or save payload', () => {
  const target = pricingRuleEditorTargetFromFormatSelection('7');

  assert.deepEqual(Object.keys(target).sort(), ['formatRuleId', 'hydrateEditorRuleId']);
  assert.equal(target.formatRuleId, '7');
  assert.equal(target.hydrateEditorRuleId, '7');
});

test('top selector none option does not open editor hydration', () => {
  assert.equal(shouldHydrateEditorFromFormatRuleSelection(NO_FORMAT_PRICING_RULE_SELECTION), false);
  assert.deepEqual(pricingRuleEditorTargetFromFormatSelection(NO_FORMAT_PRICING_RULE_SELECTION), {
    formatRuleId: NO_FORMAT_PRICING_RULE_SELECTION,
    hydrateEditorRuleId: null,
  });
});

test('existing rule hydration preserves all linked settings', () => {
  const hydrated = hydratePricingRuleDraft({
    id: 3,
    code: 'RULE',
    name: 'Rule',
    markupTemplateId: 10,
    bendTemplateId: 20,
    noCompetitorTemplateId: 30,
    roundingRuleId: 40,
  });

  assert.equal(hydrated.markupTemplateId, 10);
  assert.equal(hydrated.bendTemplateId, 20);
  assert.equal(hydrated.noCompetitorTemplateId, 30);
  assert.equal(hydrated.roundingRuleId, 40);
});

test('existing rule hydration preserves partial null linked settings', () => {
  const hydrated = hydratePricingRuleDraft({
    id: 4,
    code: 'PARTIAL',
    name: 'Partial',
    markupTemplateId: 10,
    bendTemplateId: null,
    noCompetitorTemplateId: 30,
  });

  assert.equal(hydrated.markupTemplateId, 10);
  assert.equal(hydrated.bendTemplateId, null);
  assert.equal(hydrated.noCompetitorTemplateId, 30);
  assert.equal(hydrated.roundingRuleId, null);
});

test('switching between existing and new rule drafts updates every linked field', () => {
  const first = hydratePricingRuleDraft(draft({ id: 1, markupTemplateId: 10, bendTemplateId: 20, noCompetitorTemplateId: 30, roundingRuleId: 40 }));
  const cleared = emptyPricingRuleDraft();
  const second = hydratePricingRuleDraft(draft({ id: 2, markupTemplateId: 11, bendTemplateId: null, noCompetitorTemplateId: 31, roundingRuleId: null }));

  assert.deepEqual(
    [first.markupTemplateId, first.bendTemplateId, first.noCompetitorTemplateId, first.roundingRuleId],
    [10, 20, 30, 40]
  );
  assert.deepEqual(
    [cleared.markupTemplateId, cleared.bendTemplateId, cleared.noCompetitorTemplateId, cleared.roundingRuleId],
    [null, null, null, null]
  );
  assert.deepEqual(
    [second.markupTemplateId, second.bendTemplateId, second.noCompetitorTemplateId, second.roundingRuleId],
    [11, null, 31, null]
  );
});

test('explicit lower New Rule selection still clears editor state', () => {
  const loaded = hydratePricingRuleDraft(draft({ id: 1, markupTemplateId: 10, bendTemplateId: 20, noCompetitorTemplateId: 30, roundingRuleId: 40 }));
  const cleared = NEW_PRICING_RULE_SELECTION === 'new' ? emptyPricingRuleDraft() : loaded;

  assert.equal(NEW_PRICING_RULE_SELECTION, 'new');
  assert.deepEqual(cleared, emptyPricingRuleDraft());
});

test('Rule A to Rule B to Rule A hydrates the latest selected rule values', () => {
  const ruleA = draft({ id: 1, name: 'Rule A', markupTemplateId: 10, bendTemplateId: 20, noCompetitorTemplateId: 30, roundingRuleId: 40 });
  const ruleB = draft({ id: 2, name: 'Rule B', markupTemplateId: 11, bendTemplateId: 21, noCompetitorTemplateId: null, roundingRuleId: 41 });
  const sequence = [ruleA, ruleB, ruleA].map(hydratePricingRuleDraft);
  const latest = sequence.at(-1)!;

  assert.deepEqual([latest.id, latest.markupTemplateId, latest.bendTemplateId, latest.noCompetitorTemplateId, latest.roundingRuleId], [1, 10, 20, 30, 40]);
});

test('stale async rule response cannot overwrite the latest selected rule', () => {
  const staleRequestId = 1;
  const latestRequestId = 2;

  assert.equal(isLatestPricingRuleLoadResponse(staleRequestId, latestRequestId), false);
  assert.equal(isLatestPricingRuleLoadResponse(latestRequestId, latestRequestId), true);
});

test('save payload keeps unchanged relationships and isolated selector edits', () => {
  const loaded = hydratePricingRuleDraft(draft({ id: 5, markupTemplateId: 10, bendTemplateId: 20, noCompetitorTemplateId: 30, roundingRuleId: 40 }));
  assert.deepEqual(buildPricingRuleCreatePayload(loaded, NO_COPY_SOURCE), loaded);

  const changed = { ...loaded, bendTemplateId: 21 };
  assert.deepEqual(
    [
      changed.markupTemplateId,
      changed.bendTemplateId,
      changed.noCompetitorTemplateId,
      changed.roundingRuleId,
    ],
    [10, 21, 30, 40]
  );
});
