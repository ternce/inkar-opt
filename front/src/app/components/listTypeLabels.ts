export const LIST_TYPE_LABELS: Record<string, string> = {
  fixed_price: 'Фиксированная цена',
  min_price: 'Минимальная цена',
  max_price: 'Максимальная цена',
  min_markup: 'Минимальная наценка',
  critical_markup: 'Критическая наценка',
  max_markup: 'Максимальная наценка',
  no_bend: 'Без прогиба',
  percentile_override: 'Переопределение персентиля',
  exclude_from_pricing: 'Исключить из расчета',
};

LIST_TYPE_LABELS.fixed_markup = 'Фиксированная наценка';

const LABEL_TO_CODE = Object.fromEntries(
  Object.entries(LIST_TYPE_LABELS).map(([code, label]) => [label.toLocaleLowerCase('ru-RU'), code])
);

export const listTypeOptions = Object.entries(LIST_TYPE_LABELS);

export const listTypeLabel = (value?: string, fallback?: string) => {
  const raw = String(value || '').trim();
  if (!raw) return fallback || '';
  const normalized = raw.toLocaleLowerCase('ru-RU');
  const code = LABEL_TO_CODE[normalized] || raw;
  return LIST_TYPE_LABELS[code] || LIST_TYPE_LABELS[raw] || fallback || raw;
};

export const listTypeImpact = (value?: string) => {
  const label = listTypeLabel(value);
  switch (label) {
    case 'Фиксированная цена':
      return 'фиксирует цену выбранных товаров';
    case 'Минимальная цена':
      return 'не дает цене опуститься ниже заданного значения';
    case 'Максимальная цена':
      return 'ограничивает верхнюю границу цены';
    case 'Минимальная наценка':
      return 'контролирует минимальную маржу';
    case 'Критическая наценка':
      return 'подсвечивает критический уровень маржи';
    case 'Максимальная наценка':
      return 'ограничивает максимальную маржу';
    case 'Без прогиба':
      return 'отключает прогиб от цены конкурента';
    case 'Переопределение персентиля':
      return 'задает отдельный персентиль для товаров';
    case 'Исключить из расчета':
      return 'исключает товары из переоценки';
    default:
      return 'влияет на правило расчета цены';
  }
};
