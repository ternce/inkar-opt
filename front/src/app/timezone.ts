export const APP_TIME_ZONE = 'Asia/Qyzylorda';

export const formatDateTimeKz = (value?: string | null) => {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('ru-RU', { timeZone: APP_TIME_ZONE });
};

export const formatDateKz = (value?: string | null) => {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleDateString('ru-RU', { timeZone: APP_TIME_ZONE });
};
