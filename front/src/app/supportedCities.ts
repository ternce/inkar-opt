export const SUPPORTED_CITIES = [
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
] as const;

export const supportedCitySet = new Set<string>(SUPPORTED_CITIES);

export const isSupportedCity = (value: unknown) =>
  supportedCitySet.has(String(value || '').trim());

