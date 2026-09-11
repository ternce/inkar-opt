import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  buildProductCatalogCandidateUrl,
  buildProductCatalogMappingPayload,
  canConfirmProductCatalogMapping,
} from './productCatalogMapping.ts';

const row = { productId: 3, status: 'review', platform: 'provisor' };
const candidate = {
  platform: 'provisor',
  itemId: 44,
  sourceExternalKey: '984',
  sourceMatchKey: 'provisor:984',
  sourceName: 'External source',
  sourceManufacturer: 'Maker',
  confidence: 91,
};

test('review row with selected candidate can be confirmed without selectedProduct', () => {
  assert.equal(canConfirmProductCatalogMapping(row, candidate, false), true);
});

test('candidate changes keep product-catalog confirmation enabled', () => {
  const otherCandidate = { ...candidate, itemId: 45, sourceExternalKey: '985', sourceMatchKey: 'provisor:985' };

  assert.equal(canConfirmProductCatalogMapping(row, otherCandidate, false), true);
});

test('product-catalog mapping payload uses selected row product id', () => {
  const payload = buildProductCatalogMappingPayload(row, candidate, 'provisor');

  assert.deepEqual(payload, {
    platform: 'provisor',
    status: 'mapped',
    itemId: 44,
    sourceExternalKey: '984',
    sourceMatchKey: 'provisor:984',
    sourceName: 'External source',
    sourceManufacturer: 'Maker',
    sourceDosageForm: undefined,
    sourceNormalizedName: undefined,
    ourProductId: 3,
    confidence: 91,
  });
});

test('product-catalog confirmation stays disabled without external source key', () => {
  assert.equal(canConfirmProductCatalogMapping(row, { platform: 'provisor' }, false), false);
});

test('rejected row cannot be confirmed', () => {
  assert.equal(canConfirmProductCatalogMapping({ ...row, status: 'rejected' }, candidate, false), false);
});

test('row candidate loading uses dedicated product-catalog candidate endpoint', () => {
  assert.equal(
    buildProductCatalogCandidateUrl(6177, 'provisor', 'INKAR', true),
    '/api/competitors/code-mappings/product-catalog/6177/candidates?platform=provisor&limit=5&format_code=INKAR',
  );
});
