import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { competitorFreshnessClassName, competitorFreshnessLabel } from './competitorTimestamps.ts';

const NOW = Date.parse('2026-09-07T12:00:00.000Z');
const ORIGINAL_DATE_NOW = Date.now;

before(() => {
  Date.now = () => NOW;
});

after(() => {
  Date.now = ORIGINAL_DATE_NOW;
});

test('old priceDate with fresh lastSuccessAt is current', () => {
  const row = {
    priceDate: '2026-09-04',
    lastSuccessAt: '2026-09-07T06:00:00.000Z',
  };

  assert.equal(competitorFreshnessLabel(row), '\u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u043e');
  assert.equal(competitorFreshnessClassName(row), 'ok');
});

test('checked_unchanged with fresh lastSuccessAt is current', () => {
  const row = {
    priceDate: '2026-09-04',
    lastSuccessAt: '2026-09-07T06:00:00.000Z',
    status: 'checked_unchanged',
  };

  assert.equal(competitorFreshnessLabel(row), '\u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u043e');
  assert.equal(competitorFreshnessClassName(row), 'ok');
});

test('stale lastSuccessAt is stale', () => {
  const row = {
    priceDate: '2026-09-07',
    lastSuccessAt: '2026-09-01T06:00:00.000Z',
  };

  assert.equal(competitorFreshnessLabel(row), '\u0443\u0441\u0442\u0430\u0440\u0435\u043b\u043e');
  assert.equal(competitorFreshnessClassName(row), 'warn');
});

test('timeout and error statuses override a fresh successful check', () => {
  const freshRow = {
    priceDate: '2026-09-07',
    lastSuccessAt: '2026-09-07T06:00:00.000Z',
  };

  assert.equal(competitorFreshnessLabel({ ...freshRow, status: 'timeout' }), '\u0443\u0441\u0442\u0430\u0440\u0435\u043b\u043e');
  assert.equal(competitorFreshnessClassName({ ...freshRow, status: 'timeout' }), 'warn');
  assert.equal(competitorFreshnessLabel({ ...freshRow, status: 'error' }), '\u043e\u0448\u0438\u0431\u043a\u0430');
  assert.equal(competitorFreshnessClassName({ ...freshRow, status: 'error' }), 'bad');
});
