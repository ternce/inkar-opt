import assert from 'node:assert/strict';
import { test } from 'node:test';

import { previewGeneratedPriceFormatCode } from './priceFormatCodeRepair.ts';

test('generated price format code preview uses structured type branch and sequence', () => {
  assert.equal(previewGeneratedPriceFormatCode('ГПЛ', 'Есик', 8), 'ГПЛ_1004_008');
  assert.equal(previewGeneratedPriceFormatCode('ИПЛ', 'Атырау', '2'), 'ИПЛ_1003_002');
});

test('generated price format code preview shows placeholders until structured pieces are complete', () => {
  assert.equal(previewGeneratedPriceFormatCode('ГПЛ', 'Есик', ''), 'ГПЛ_1004_NNN');
  assert.equal(previewGeneratedPriceFormatCode('ИПЛ', 'Unknown', '1'), 'ИПЛ_SAP_001');
});
