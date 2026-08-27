import assert from 'node:assert/strict';
import { test } from 'node:test';

import { SUPPORTED_CITIES, isSupportedCity } from './supportedCities.ts';

test('supported city selectors expose exactly the business-approved cities', () => {
  assert.deepEqual(SUPPORTED_CITIES, [
    'Алматы',
    'Астана',
    'Шымкент',
    'Атырау',
    'Есик',
    'Караганда',
    'Костанай',
    'Семей',
    'Усть-Каменогорск',
    'Павлодар',
    'Актау',
    'Актобе',
  ]);
  assert.equal(SUPPORTED_CITIES.length, 12);
});

test('unsupported historical cities are hidden from new city selection', () => {
  for (const city of ['Кызылорда', 'Петропавловск', 'Талдыкорган', 'Уральск', 'Орал', 'Нур-Султан']) {
    assert.equal(isSupportedCity(city), false, city);
  }
});

