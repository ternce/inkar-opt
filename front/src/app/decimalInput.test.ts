import assert from 'node:assert/strict';
import { test } from 'node:test';

import { parseDecimalInput, parseOptionalDecimalInput, parseRequiredDecimalInput } from './decimalInput.ts';

test('parses dot and comma decimal input to the same numeric value', () => {
  const cases: Array<[string, number]> = [
    ['1', 1],
    ['1.5', 1.5],
    ['1,5', 1.5],
    ['0,25', 0.25],
    ['0.25', 0.25],
    ['10,75', 10.75],
    ['10.75', 10.75],
    ['100', 100],
  ];

  for (const [input, expected] of cases) {
    assert.equal(parseDecimalInput(input), expected);
    assert.equal(parseRequiredDecimalInput(input, 'value'), expected);
  }
});

test('allows optional empty decimal fields', () => {
  assert.equal(parseOptionalDecimalInput('', 'value'), null);
  assert.equal(parseOptionalDecimalInput('   ', 'value'), null);
});

test('rejects malformed decimal input', () => {
  for (const input of ['1,2,3', '1.2.3', '1,2.3', 'abc', '--1', '']) {
    assert.equal(Number.isFinite(parseDecimalInput(input)), false, input);
  }
});
