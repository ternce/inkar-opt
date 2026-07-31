import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  CREATE_NEW_TEMPLATE,
  CURRENT_FORMAT_SETTINGS,
  resolveExplicitTemplateSelection,
  resolveInitialRoundingSelection,
  resolveInitialTemplateSelection,
} from './pricingTemplateSelection.ts';

const templates = [
  { id: 10, name: 'Markup A' },
  { id: 20, name: 'Bend A' },
  { id: 30, name: 'No competitor A' },
];

test('price format with applied markup template opens with that template selected', () => {
  const resolved = resolveInitialTemplateSelection({ appliedMarkupTemplateId: 10 }, 'markup', templates);

  assert.equal(resolved.selectedId, '10');
  assert.equal(resolved.mode, 'applied');
});

test('applied bend template is selected automatically', () => {
  const resolved = resolveInitialTemplateSelection({ appliedBendTemplateId: 20 }, 'bend', templates);

  assert.equal(resolved.selectedId, '20');
  assert.equal(resolved.mode, 'applied');
});

test('applied no-competitor template is selected automatically', () => {
  const resolved = resolveInitialTemplateSelection({ appliedNoCompetitorTemplateId: 30 }, 'noCompetitor', templates);

  assert.equal(resolved.selectedId, '30');
  assert.equal(resolved.mode, 'applied');
});

test('applied rounding rule is selected automatically', () => {
  const resolved = resolveInitialRoundingSelection({ appliedRoundingRuleId: 40 }, [{ id: 40, name: 'Round A' }]);

  assert.equal(resolved.selectedId, '40');
  assert.equal(resolved.mode, 'applied');
});

test('new template is not the default when an applied template exists', () => {
  const resolved = resolveInitialTemplateSelection({ appliedMarkupTemplateId: 10 }, 'markup', templates);

  assert.notEqual(resolved.selectedId, CREATE_NEW_TEMPLATE);
});

test('price format without a template shows current format settings', () => {
  const resolved = resolveInitialTemplateSelection({}, 'markup', templates);

  assert.equal(resolved.selectedId, CURRENT_FORMAT_SETTINGS);
  assert.equal(resolved.mode, 'current_format');
});

test('selecting new template explicitly enters create mode', () => {
  assert.equal(resolveExplicitTemplateSelection(CREATE_NEW_TEMPLATE, 10), 'create_new');
});

test('switching price formats resolves each actual applied template independently', () => {
  const first = resolveInitialTemplateSelection({ appliedMarkupTemplateId: 10 }, 'markup', templates);
  const second = resolveInitialTemplateSelection({ appliedMarkupTemplateId: 30 }, 'markup', templates);

  assert.equal(first.selectedId, '10');
  assert.equal(second.selectedId, '30');
});

test('fast switching can ignore stale template data by request identity', () => {
  let currentRequest = 0;
  let selected = '';
  const commit = (requestId: number, appliedMarkupTemplateId: number) => {
    if (requestId !== currentRequest) return;
    selected = resolveInitialTemplateSelection({ appliedMarkupTemplateId }, 'markup', templates).selectedId;
  };

  const firstRequest = ++currentRequest;
  const secondRequest = ++currentRequest;
  commit(secondRequest, 30);
  commit(firstRequest, 10);

  assert.equal(selected, '30');
});

test('selecting a pricing rule is preview mode until apply succeeds', () => {
  const preview = resolveExplicitTemplateSelection('20', 10);

  assert.equal(preview, 'existing_template');
});

test('after apply, the new template becomes selected as applied', () => {
  const resolved = resolveInitialTemplateSelection({ appliedMarkupTemplateId: 20 }, 'markup', templates);

  assert.equal(resolved.selectedId, '20');
  assert.equal(resolved.mode, 'applied');
});

test('existing template edit/copy selection remains existing_template when not applied', () => {
  assert.equal(resolveExplicitTemplateSelection('20', 10), 'existing_template');
});
