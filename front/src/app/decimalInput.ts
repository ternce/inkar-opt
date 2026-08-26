export type DecimalParseOptions = {
  allowEmpty?: boolean;
};

export function parseDecimalInput(value: string | number | null | undefined, options: DecimalParseOptions = {}): number | null {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null;
  }

  const text = String(value ?? '').trim();
  if (!text) return options.allowEmpty ? null : Number.NaN;

  const commaCount = (text.match(/,/g) || []).length;
  const dotCount = (text.match(/\./g) || []).length;
  if (commaCount + dotCount > 1) return Number.NaN;

  if (!/^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)$/.test(text)) return Number.NaN;

  const parsed = Number(text.replace(',', '.'));
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

export function parseRequiredDecimalInput(value: string | number | null | undefined, fieldName: string): number {
  const parsed = parseDecimalInput(value);
  if (parsed === null || !Number.isFinite(parsed)) {
    throw new Error(`${fieldName}: некорректное число`);
  }
  return parsed;
}

export function parseOptionalDecimalInput(value: string | number | null | undefined, fieldName: string): number | null {
  const parsed = parseDecimalInput(value, { allowEmpty: true });
  if (parsed !== null && !Number.isFinite(parsed)) {
    throw new Error(`${fieldName}: некорректное число`);
  }
  return parsed;
}
