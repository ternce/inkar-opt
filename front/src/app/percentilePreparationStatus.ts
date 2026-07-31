export type PercentilePreparation = {
  priceFormatId?: number;
  status?: string;
  message?: string;
  lastError?: string;
  rowsCount?: number;
  startedAt?: string;
  completedAt?: string;
  failedAt?: string;
  sourceRefreshedAt?: string;
  jobId?: string;
};

export const percentilePreparationStatusText = (status?: string) => {
  if (status === 'ready') return 'Готово';
  if (status === 'pending') return 'Ожидает подготовки';
  if (status === 'processing') return 'Подготовка';
  if (status === 'failed') return 'Ошибка';
  if (status === 'stale') return 'Нужна повторная подготовка';
  return 'Не настроено';
};

export const percentilePreparationClassName = (status?: string) => {
  if (status === 'ready') return 'ok';
  if (status === 'failed' || status === 'stale') return 'bad';
  return 'warn';
};

export const shouldPollPercentilePreparation = (status?: string) => status === 'pending' || status === 'processing';

export const canRetryPercentilePreparation = (status?: string) => status === 'failed' || status === 'stale';

export const percentilePreparationMessage = (prep?: PercentilePreparation | null) => {
  const status = prep?.status || 'not_configured';
  if (prep?.message) return prep.message;
  if (prep?.lastError) return prep.lastError;
  if (status === 'ready') return `Строк персентилей: ${Number(prep?.rowsCount || 0).toLocaleString('ru-RU')}`;
  if (status === 'pending') return 'Подготовка запланирована. Старые строки не будут заменены до успешного завершения.';
  if (status === 'processing') return 'Персентили рассчитываются из сохранённых данных источников.';
  if (status === 'failed') return 'Подготовка завершилась ошибкой. Можно повторить после проверки источников.';
  if (status === 'stale') return 'Настройки или источник изменились. Запустите повторную подготовку.';
  return 'Для выбранного формата настройки персентилей ещё не заданы';
};
