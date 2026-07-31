import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  NO_COPY_SOURCE,
  applyPricingRuleCreateSuccess,
  buildPricingRuleCreatePayload,
  canSubmitPricingRuleCreate,
  draftFromCopySource,
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
