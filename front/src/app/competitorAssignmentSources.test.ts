import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  mergeAvailableCompetitorAssignmentSources,
  physicalCompetitorSourceIdentity,
} from './competitorAssignmentSources.ts';

const plk = (id: number, name: string) => ({
  id,
  sourceType: 'provisor',
  sourceKey: 'ambiguous-source-key',
  name,
  competitorName: name,
  branchName: 'Актау',
  accountLogin: 'account',
  itemsCount: 12,
});

test('globally available but unassigned PLK appears in assignment UI pool', () => {
  const rows = mergeAvailableCompetitorAssignmentSources({
    globalPriceLists: [plk(101, 'Стофарм средняя цена (Актау)')],
    percentileSources: [],
    assignments: [],
  });

  assert.equal(rows.length, 1);
  assert.equal(rows[0].sourceName, 'Стофарм средняя цена (Актау)');
  assert.equal(rows[0].isSelected, false);
});

test('assigned PLK appears selected based on physical source id', () => {
  const rows = mergeAvailableCompetitorAssignmentSources({
    globalPriceLists: [plk(101, 'Стофарм средняя цена (Актау)')],
    percentileSources: [],
    assignments: [
      {
        ...plk(101, 'Assigned row copy'),
        id: 'assignment-row-id',
        sourceId: 101,
      },
    ],
  });

  assert.equal(rows.length, 1);
  assert.equal(rows[0].isSelected, true);
  assert.equal(physicalCompetitorSourceIdentity(rows[0]), 'provisor:101');
});

test('unassigned PLK stays unselected even with matching source key', () => {
  const rows = mergeAvailableCompetitorAssignmentSources({
    globalPriceLists: [plk(101, 'Стофарм средняя цена (Актау)')],
    percentileSources: [],
    assignments: [
      {
        ...plk(202, 'Different physical PLK'),
        sourceId: 202,
      },
    ],
  });

  assert.equal(rows.length, 1);
  assert.equal(rows[0].sourceKey, 'ambiguous-source-key');
  assert.equal(rows[0].isSelected, false);
});

test('selected-only endpoint no longer defines the available pool', () => {
  const rows = mergeAvailableCompetitorAssignmentSources({
    globalPriceLists: [plk(101, 'Global unassigned PLK')],
    percentileSources: [],
    assignments: [
      {
        ...plk(202, 'Assigned but absent from global pool'),
        sourceId: 202,
      },
    ],
  });

  assert.deepEqual(rows.map((row) => row.sourceName), ['Global unassigned PLK']);
  assert.deepEqual(rows.map((row) => row.isSelected), [false]);
});
