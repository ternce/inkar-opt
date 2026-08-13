export const LIST_TYPE_LABELS: Record<string, string> = {
  fixed_price: 'Фиксированная конечная цена (₸)',
  min_price: 'Минимальная цена (₸)',
  max_price: 'Максимальная цена (₸)',
  min_markup: 'Минимальная наценка (%)',
  critical_markup: 'Критическая наценка (%)',
  max_markup: 'Максимальная наценка (%)',
  fixed_markup: 'Фиксированная наценка (%)',
  no_bend: 'Без прогиба',
  percentile_override: 'Переопределение персентиля (%)',
  exclude_from_pricing: 'Исключить из ценообразования',
  memorandum: 'Меморандум, максимальная цена (₸)',
};

const LABEL_TO_CODE = Object.fromEntries(
  Object.entries(LIST_TYPE_LABELS).map(([code, label]) => [label.toLocaleLowerCase('ru-RU'), code])
);

const PRICE_TYPES = new Set(['fixed_price', 'min_price', 'max_price', 'memorandum']);
const MARKUP_TYPES = new Set(['fixed_markup', 'critical_markup', 'min_markup', 'max_markup', 'percentile_override']);

export const listTypeOptions = Object.entries(LIST_TYPE_LABELS);

export const listTypeCode = (value?: string, fallback = 'fixed_price') => {
  const raw = String(value || '').trim();
  if (!raw) return fallback;
  return LABEL_TO_CODE[raw.toLocaleLowerCase('ru-RU')] || raw;
};

export const listTypeLabel = (value?: string, fallback?: string) => {
  const raw = String(value || '').trim();
  if (!raw) return fallback || '';
  const code = listTypeCode(raw, raw);
  return LIST_TYPE_LABELS[code] || LIST_TYPE_LABELS[raw] || fallback || raw;
};

export const listTypeUnit = (value?: string) => {
  const code = listTypeCode(value, '');
  if (PRICE_TYPES.has(code)) return '₸';
  if (MARKUP_TYPES.has(code)) return '%';
  return '';
};

export const listTypeHelper = (value?: string) => {
  switch (listTypeCode(value, '')) {
    case 'fixed_price':
      return [
        'Фиксированная цена — это конечная цена товара в тенге.',
        'Например, значение 5000 означает итоговую цену 5000 ₸.',
        'Отрицательные значения запрещены.',
      ].join('\n');
    case 'critical_markup':
      return [
        'Критическая наценка — процентный параметр расчета.',
        'Например, -2 означает критическую наценку -2%, а не цену -2 ₸.',
      ].join('\n');
    case 'fixed_markup':
      return 'Фиксированная наценка — это процент, а не конечная цена товара.';
    default:
      return '';
  }
};

export const isFixedPriceType = (value?: string) => listTypeCode(value, '') === 'fixed_price';

export const listTypeImpact = (value?: string) => {
  switch (listTypeCode(value, '')) {
    case 'memorandum':
      return 'ограничивает финальную цену регулируемым максимумом';
    case 'fixed_price':
      return 'фиксирует конечную цену выбранных товаров';
    case 'min_price':
      return 'не дает цене опуститься ниже заданного значения';
    case 'max_price':
      return 'ограничивает верхнюю границу цены';
    case 'min_markup':
      return 'контролирует минимальную маржу';
    case 'critical_markup':
      return 'подсвечивает критический уровень маржи';
    case 'max_markup':
      return 'ограничивает максимальную маржу';
    case 'fixed_markup':
      return 'задает фиксированный процент наценки';
    case 'no_bend':
      return 'отключает прогиб от цены конкурента';
    case 'percentile_override':
      return 'задает отдельный персентиль для товаров';
    case 'exclude_from_pricing':
      return 'исключает товары из переоценки';
    default:
      return 'влияет на правило расчета цены';
  }
};
