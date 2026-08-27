import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  NO_COPY_SOURCE,
  applyPricingRuleCreateSuccess,
  buildPricingRuleCreatePayload,
  canSubmitPricingRuleCreate,
  draftFromCopySource,
  emptyPricingRuleDraft,
  hydratePricingRuleDraft,
  pricingRuleCreateErrorMessage,
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
