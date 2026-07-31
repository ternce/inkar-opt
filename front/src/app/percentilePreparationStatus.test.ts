import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  canRetryPercentilePreparation,
  percentilePreparationClassName,
  percentilePreparationMessage,
  percentilePreparationStatusText,
  shouldPollPercentilePreparation,
} from './percentilePreparationStatus.ts';

test('marks ready percentile preparation as non-polling and successful', () => {
  assert.equal(percentilePreparationStatusText('ready'), 'Готово');
  assert.equal(percentilePreparationClassName('ready'), 'ok');
  assert.equal(shouldPollPercentilePreparation('ready'), false);
  assert.equal(canRetryPercentilePreparation('ready'), false);
  assert.match(percentilePreparationMessage({ status: 'ready', rowsCount: 12 }), /12/);
});

test('polls only active preparation statuses', () => {
  assert.equal(shouldPollPercentilePreparation('pending'), true);
  assert.equal(shouldPollPercentilePreparation('processing'), true);
  assert.equal(shouldPollPercentilePreparation('failed'), false);
  assert.equal(shouldPollPercentilePreparation('stale'), false);
});

test('allows retry for failed and stale statuses', () => {
  assert.equal(canRetryPercentilePreparation('failed'), true);
  assert.equal(canRetryPercentilePreparation('stale'), true);
  assert.equal(canRetryPercentilePreparation('pending'), false);
  assert.equal(percentilePreparationClassName('failed'), 'bad');
});

test('shows empty state for unconfigured percentile preparation', () => {
  assert.equal(percentilePreparationStatusText('not_configured'), 'Не настроено');
  assert.match(percentilePreparationMessage({ status: 'not_configured' }), /ещё не заданы/);
});
