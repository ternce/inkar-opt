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

test('old priceDate with recent success is current', () => {
  const row = {
    priceDate: '2026-09-04',
    lastSuccessAt: '2026-09-07T06:00:00.000Z',
  };

  assert.equal(competitorFreshnessLabel(row), '\u0410\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u043e');
  assert.equal(competitorFreshnessClassName(row), 'ok');
});

test('old lastSuccessAt with no error shows a neutral factual check label', () => {
  const row = {
    priceDate: '2026-09-02',
    lastSuccessAt: '2026-09-02T06:00:00.000Z',
  };

  assert.match(competitorFreshnessLabel(row), /^\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430: /);
  assert.doesNotMatch(competitorFreshnessLabel(row), /\u0423\u0441\u0442\u0430\u0440\u0435\u043b\u043e|\u0443\u0441\u0442\u0430\u0440\u0435\u043b\u043e/);
  assert.equal(competitorFreshnessClassName(row), '');
});

test('checked_unchanged with recent lastSuccessAt is current without changes', () => {
  const row = {
    priceDate: '2026-09-04',
    lastSuccessAt: '2026-09-07T06:00:00.000Z',
    status: 'checked_unchanged',
  };

  assert.equal(competitorFreshnessLabel(row), '\u0410\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u043e, \u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439');
  assert.equal(competitorFreshnessClassName(row), 'ok');
});

test('timeout shows a check timeout warning regardless of a fresh successful check', () => {
  const freshRow = {
    priceDate: '2026-09-07',
    lastSuccessAt: '2026-09-07T06:00:00.000Z',
  };

  assert.equal(competitorFreshnessLabel({ ...freshRow, status: 'timeout' }), '\u0422\u0430\u0439\u043c-\u0430\u0443\u0442 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438');
  assert.equal(competitorFreshnessClassName({ ...freshRow, status: 'timeout' }), 'warn');
});

test('failed error and stale statuses show refresh errors', () => {
  const freshRow = {
    priceDate: '2026-09-07',
    lastSuccessAt: '2026-09-07T06:00:00.000Z',
  };

  for (const status of ['failed', 'error', 'stale']) {
    assert.equal(competitorFreshnessLabel({ ...freshRow, status }), '\u041e\u0448\u0438\u0431\u043a\u0430 \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f');
    assert.equal(competitorFreshnessClassName({ ...freshRow, status }), 'bad');
  }
});
