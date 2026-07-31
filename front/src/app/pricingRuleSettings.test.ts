import assert from 'node:assert/strict';
import { test } from 'node:test';

import { resolvePricingRuleSettings } from './pricingRuleSettings.ts';

test('resolves linked markup, bend, no-competitor and rounding settings', () => {
  const resolved = resolvePricingRuleSettings({
    id: 1,
    code: 'RULE-A',
    roundingRuleId: 4,
    markupTemplateId: 10,
    bendTemplateId: 20,
    noCompetitorTemplateId: 30,
    markupTemplate: { rows: [{ costFrom: 0, costTo: 999, markupPercent: 12 }] },
    bendTemplate: { rows: [{ costFrom: 500, bendPercent: 0.25 }] },
    noCompetitorTemplate: { rows: [{ costFrom: 0, costTo: null, markupPercent: 7 }] },
  });

  assert.equal(resolved.pricingRule, 'RULE-A');
  assert.equal(resolved.roundingRuleId, '4');
  assert.equal(resolved.hasLinkedSettings, true);
  assert.deepEqual(resolved.recommendedMarkups, [{ id: 1, lowerBound: '0', upperBound: '999', markupPercent: '12' }]);
  assert.deepEqual(resolved.bendRanges, [{ id: 1, priceFrom: '500', bendPercent: '0.25' }]);
  assert.deepEqual(resolved.noCompetitorMarkups, [{ id: 1, lowerBound: '0', upperBound: '99999999', markupPercent: '7' }]);
});

test('returns an empty state for a rule without linked settings', () => {
  const resolved = resolvePricingRuleSettings({ id: 2, name: 'Empty rule' });

  assert.equal(resolved.pricingRule, 'Empty rule');
  assert.equal(resolved.roundingRuleId, 'none');
  assert.equal(resolved.hasLinkedSettings, false);
  assert.deepEqual(resolved.recommendedMarkups, []);
  assert.deepEqual(resolved.bendRanges, []);
  assert.deepEqual(resolved.noCompetitorMarkups, []);
  assert.deepEqual(resolved.missingLinkedTemplates, []);
});

test('reports missing linked templates without falling back to another template', () => {
  const resolved = resolvePricingRuleSettings({
    id: 3,
    code: 'MISSING',
    markupTemplateId: 999,
    bendTemplateId: 1000,
  });

  assert.equal(resolved.hasLinkedSettings, false);
  assert.deepEqual(resolved.missingLinkedTemplates, ['recommendedMarkups', 'bendRanges']);
  assert.deepEqual(resolved.recommendedMarkups, []);
  assert.deepEqual(resolved.bendRanges, []);
});
