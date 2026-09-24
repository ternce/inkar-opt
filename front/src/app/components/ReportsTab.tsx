import { useEffect, useMemo, useState } from 'react';
import { CheckSquare, Download, Search, Square, X } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';

type PriceFormat = {
  id: string | number;
  name: string;
  code: string;
  branch: string;
  sapCategory?: string;
};

type ReportType = 'rank-1' | 'decreases';

type ReportPriceList = {
  id: number | string;
  number: string;
  format: string;
  formatName?: string;
  branch: string;
  date: string;
  createdAt: string;
  activationDate: string;
  skuCount: number;
};

type ReportContextOption = {
  priceFormat: PriceFormat;
  priceLists: ReportPriceList[];
  availableActivationDates?: string[];
  latestPriceListsByActivationDate?: ReportPriceList[];
  latestPriceList?: ReportPriceList | null;
};

type AppliedContext = {
  priceFormatId: number | string;
};

type ReportPayload = {
  items: any[];
  total: number;
  page: number;
  limit: number;
  context?: {
    branch: string;
    activationDate?: string;
    selectedFormatCount: number;
    contexts: Array<{
      priceFormatId: number | string;
      priceFormatCode: string;
      priceFormatName: string;
      priceListNumber: string;
      customerCategory: string;
      calculatedAtDisplay: string;
      totalCalculated: number;
      previousPriceListNumber?: string;
      currentActivationDate?: string;
      currentCreatedAt?: string;
      previousActivationDate?: string;
      previousCreatedAt?: string;
    }>;
  };
  summary?: Record<string, number>;
};

type ReportsTabProps = {
  branch: string;
  selectedFormatCode: string;
  priceFormats: PriceFormat[];
};

const parseJsonOrNull = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const fmtNumber = (value: unknown, maximumFractionDigits = 2) => {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('ru-RU', { maximumFractionDigits });
};

const fmtPercent = (value: unknown) => {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return `${(n * 100).toLocaleString('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
};

const endpointFor = (reportType: ReportType) => (reportType === 'rank-1' ? 'rank-1' : 'decreases');

const requestBody = (branchFilter: string, activationDate: string, contexts: AppliedContext[], q: string, page: number, limit: number) => ({
  branch: branchFilter,
  activationDate,
  contexts,
  q: q.trim(),
  page,
  limit,
});

const contextId = (item: ReportContextOption) => String(item.priceFormat.id);

const formatActivationDate = (value: string) => {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  return match ? `${match[3]}.${match[2]}.${match[1]}` : value;
};

const contextBaseLabel = (item: ReportContextOption) => {
  const category = String(item.priceFormat.sapCategory || '').trim();
  if (category) return /^кат(?:\s|\.|$)/iu.test(category) ? category : `Кат ${category}`;
  return item.priceFormat.name || item.priceFormat.code;
};

const downloadNameFromDisposition = (value: string | null, fallback: string) => {
  if (!value) return fallback;
  const match = value.match(/filename\*=UTF-8''([^;]+)/i);
  return match ? decodeURIComponent(match[1]) : fallback;
};

export function ReportsTab({ branch, selectedFormatCode, priceFormats }: ReportsTabProps) {
  const [activeTab, setActiveTab] = useState<ReportType>('rank-1');
  const [branchFilter, setBranchFilter] = useState(branch || priceFormats[0]?.branch || '');
  const [activationDate, setActivationDate] = useState('');
  const [contextOptions, setContextOptions] = useState<ReportContextOption[]>([]);
  const [selectedFormatIds, setSelectedFormatIds] = useState<string[]>([]);
  const [appliedContexts, setAppliedContexts] = useState<AppliedContext[]>([]);
  const [contextsBranch, setContextsBranch] = useState('');
  const [payload, setPayload] = useState<ReportPayload | null>(null);
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [isLoadingContexts, setIsLoadingContexts] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [error, setError] = useState('');
  const limit = 100;

  const branchOptions = useMemo(
    () => Array.from(new Set(priceFormats.map((format) => format.branch).filter(Boolean))).sort((a, b) => a.localeCompare(b, 'ru-RU')),
    [priceFormats]
  );

  const availableContextOptions = useMemo(
    () => contextOptions.filter((item) => item.priceLists.some((priceList) => priceList.activationDate === activationDate)),
    [activationDate, contextOptions]
  );

  const availableContextIds = useMemo(() => new Set(availableContextOptions.map(contextId)), [availableContextOptions]);

  const selectedContexts = useMemo(
    () => selectedFormatIds.filter((formatId) => availableContextIds.has(formatId)).map((priceFormatId) => ({ priceFormatId })),
    [availableContextIds, selectedFormatIds]
  );

  const contextsReady = useMemo(
    () => contextsBranch === branchFilter && appliedContexts.every((item) => availableContextIds.has(String(item.priceFormatId))),
    [appliedContexts, availableContextIds, branchFilter, contextsBranch]
  );

  const quickContextLabels = useMemo(() => {
    const labels = new Map<string, string>();
    const baseLabels = availableContextOptions.map((item) => [contextId(item), contextBaseLabel(item)] as const);
    const counts = baseLabels.reduce<Record<string, number>>((acc, [, label]) => {
      acc[label] = (acc[label] || 0) + 1;
      return acc;
    }, {});
    availableContextOptions.forEach((item) => {
      const id = contextId(item);
      const base = contextBaseLabel(item);
      labels.set(id, counts[base] > 1 ? `${base} (${item.priceFormat.code})` : base);
    });
    return labels;
  }, [availableContextOptions]);

  const toggleFormat = (formatId: string) => {
    setSelectedFormatIds((current) => (current.includes(formatId) ? current.filter((id) => id !== formatId) : [...current, formatId]));
  };

  const selectAll = () => setSelectedFormatIds(availableContextOptions.map(contextId));
  const clearAll = () => setSelectedFormatIds([]);

  const togglePreviewContext = (item: ReportContextOption) => {
    const id = contextId(item);
    if (!item.priceLists.some((priceList) => priceList.activationDate === activationDate)) return;
    toggleFormat(id);
  };

  useEffect(() => {
    if (branch) setBranchFilter(branch);
  }, [branch]);

  useEffect(() => {
    setContextOptions([]);
    setSelectedFormatIds([]);
    setAppliedContexts([]);
    setContextsBranch('');
    setPayload(null);
    setPage(1);
    if (!branchFilter) {
      setIsLoadingContexts(false);
      return;
    }
    const controller = new AbortController();
    const loadContexts = async () => {
      setError('');
      setIsLoadingContexts(true);
      setPayload(null);
      const res = await fetch(`/api/reports/contexts?branch=${encodeURIComponent(branchFilter)}`, { signal: controller.signal });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить параметры отчёта');
      const rows: ReportContextOption[] = Array.isArray(data) ? data : [];
      if (controller.signal.aborted) return;
      setContextOptions(rows);
      setContextsBranch(branchFilter);

      const dates = Array.from(new Set(rows.flatMap((row) => row.availableActivationDates || row.priceLists.map((item) => item.activationDate)).filter(Boolean))).sort().reverse();
      const targetDate = dates.includes(activationDate) ? activationDate : dates[0] || '';
      setActivationDate(targetDate);
      const rowsForDate = rows.filter((row) => row.priceLists.some((item) => item.activationDate === targetDate));
      const idsWithLists = rowsForDate.map((row) => String(row.priceFormat.id));
      const preferred = rowsForDate.find((row) => row.priceFormat.code === selectedFormatCode);
      const defaultIds = preferred ? [String(preferred.priceFormat.id), ...idsWithLists.filter((id) => id !== String(preferred.priceFormat.id))] : idsWithLists;
      setSelectedFormatIds(defaultIds);
      setAppliedContexts(defaultIds.map((priceFormatId) => ({ priceFormatId })));
      setPage(1);
    };
    loadContexts()
      .catch((err) => {
        if (err?.name !== 'AbortError') setError(err?.message || 'Не удалось загрузить параметры отчёта');
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoadingContexts(false);
      });
    return () => controller.abort();
  }, [branchFilter, selectedFormatCode]);

  useEffect(() => {
    if (isLoadingContexts || !branchFilter || !activationDate || !appliedContexts.length || !contextsReady) {
      setPayload(null);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const loadReport = async () => {
        setIsLoading(true);
        setError('');
        try {
          const res = await fetch(`/api/reports/${endpointFor(activeTab)}/query`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody(branchFilter, activationDate, appliedContexts, q, page, limit)),
            signal: controller.signal,
          });
          const text = await res.text();
          const data = parseJsonOrNull(text);
          if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить отчёт');
          setPayload(data);
        } finally {
          setIsLoading(false);
        }
      };
      loadReport().catch((err) => {
        if (err?.name !== 'AbortError') setError(err?.message || 'Не удалось загрузить отчёт');
      });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [activeTab, activationDate, appliedContexts, branchFilter, contextsReady, isLoadingContexts, page, q]);

  useEffect(() => {
    setPage(1);
  }, [activeTab, appliedContexts, q]);

  const applySelection = () => {
    setAppliedContexts(selectedContexts);
    setPage(1);
  };

  const changeActivationDate = (value: string) => {
    setActivationDate(value);
    const formatIds = contextOptions
      .filter((item) => item.priceLists.some((priceList) => priceList.activationDate === value))
      .map(contextId);
    setSelectedFormatIds(formatIds);
    setAppliedContexts(formatIds.map((priceFormatId) => ({ priceFormatId })));
    setPage(1);
  };

  const exportExcel = async () => {
    if (isLoadingContexts || !branchFilter || !activationDate || !appliedContexts.length || !contextsReady) return;
    setIsExporting(true);
    setError('');
    try {
      const res = await fetch(`/api/reports/${endpointFor(activeTab)}/export.xlsx`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody(branchFilter, activationDate, appliedContexts, q, 1, limit)),
      });
      if (!res.ok) {
        const text = await res.text();
        const data = parseJsonOrNull(text);
        throw new Error(data?.detail || text || 'Не удалось выгрузить Excel');
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = downloadNameFromDisposition(res.headers.get('Content-Disposition'), activeTab === 'rank-1' ? 'rank-1.xlsx' : 'decreases.xlsx');
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      setError(err?.message || 'Не удалось выгрузить Excel');
    } finally {
      setIsExporting(false);
    }
  };

  const totalPages = Math.max(1, Math.ceil((payload?.total || 0) / limit));
  const isRank = activeTab === 'rank-1';
  const emptyText = isLoadingContexts
    ? 'Загружаем параметры отчёта...'
    : !availableContextOptions.length
      ? 'Для филиала нет доступных ценовых форматов с расчётами.'
      : !appliedContexts.length
    ? 'Выберите ценовые форматы и примените параметры отчёта.'
    : isRank
      ? 'Для выбранных расчётов позиций Ранг 1 не найдено.'
      : 'Для выбранных расчётов снижений не найдено.';

  return (
    <div className="generated-workspace reports-workspace">
      <section className="generated-panel reports-params-panel">
        <div className="card-title-row">
          <div>
            <div className="eyebrow">Отчёты</div>
            <h3>Параметры отчёта</h3>
          </div>
          <Button onClick={applySelection} disabled={!selectedContexts.length}>
            Применить
          </Button>
        </div>

        <div className="reports-params-grid">
          <label className="reports-field">
            <span>Филиал</span>
            <Select value={branchFilter} onValueChange={setBranchFilter}>
              <SelectTrigger><SelectValue placeholder="Филиал" /></SelectTrigger>
              <SelectContent>
                {branchOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
              </SelectContent>
            </Select>
          </label>

          <label className="reports-field">
            <span>Дата действия</span>
            <Input type="date" value={activationDate} onChange={(event) => changeActivationDate(event.target.value)} />
          </label>

          <div className="reports-format-picker">
            <div className="reports-field-label">
              <span>Категория / ценовой формат</span>
              <div className="reports-inline-actions">
                <Button type="button" variant="ghost" size="sm" onClick={selectAll}><CheckSquare className="mr-1 h-4 w-4" />Выбрать все</Button>
                <Button type="button" variant="ghost" size="sm" onClick={clearAll}><Square className="mr-1 h-4 w-4" />Очистить</Button>
              </div>
            </div>
            <div className="reports-format-list">
              {contextOptions.map((item) => {
                const id = String(item.priceFormat.id);
                const latestForDate = item.priceLists.find((priceList) => priceList.activationDate === activationDate);
                const disabled = !latestForDate;
                return (
                  <label key={id} className={`reports-format-row ${disabled ? 'disabled' : ''}`}>
                    <input type="checkbox" checked={selectedFormatIds.includes(id)} disabled={disabled} onChange={() => toggleFormat(id)} />
                    <span>
                      <strong>{item.priceFormat.name || item.priceFormat.code}</strong>
                      <em>{item.priceFormat.code} · {item.priceFormat.sapCategory || 'без SAP категории'} · {latestForDate?.number || 'нет расчёта на дату'}</em>
                    </span>
                  </label>
                );
              })}
              {!contextOptions.length ? <div className="dashboard-empty">Для филиала нет доступных ценовых форматов.</div> : null}
            </div>
          </div>
        </div>

        <div className="reports-calculation-list">
          {selectedFormatIds.map((formatId) => {
            const option = contextOptions.find((item) => String(item.priceFormat.id) === formatId);
            if (!option) return null;
            const latestForDate = option.priceLists.find((item) => item.activationDate === activationDate);
            if (!latestForDate) return null;
            return (
              <div key={formatId} className="reports-calculation-row">
                <span>{option.priceFormat.code}</span>
                <strong>{latestForDate.number} · {formatActivationDate(latestForDate.activationDate)} · {fmtNumber(latestForDate.skuCount, 0)} SKU</strong>
              </div>
            );
          })}
        </div>
      </section>

      {availableContextOptions.length ? (
        <div className="reports-inline-actions">
          {availableContextOptions.map((item) => {
            const id = contextId(item);
            const active = selectedFormatIds.includes(id);
            return (
              <Button key={id} type="button" size="sm" variant={active ? 'default' : 'outline'} onClick={() => togglePreviewContext(item)}>
                {active ? '✓ ' : ''}{quickContextLabels.get(id) || contextBaseLabel(item)}
              </Button>
            );
          })}
        </div>
      ) : null}

      {payload?.context ? (
        <section className="generated-panel reports-context-panel">
          <div className="generated-summary reports-summary">
            <Metric label="Филиал" value={payload.context.branch || '—'} />
            <Metric label="Дата действия" value={formatActivationDate(payload.context.activationDate || activationDate) || '—'} />
            <Metric label="Форматов" value={fmtNumber(payload.context.selectedFormatCount || 0, 0)} />
            <Metric label={isRank ? 'Ранг 1' : 'Снижений'} value={fmtNumber(isRank ? payload.summary?.totalRank1 ?? 0 : payload.summary?.totalDecreases ?? 0, 0)} />
            <Metric label={isRank ? 'Доля' : 'Среднее снижение'} value={isRank ? fmtPercent(payload.summary?.sharePercent ?? 0) : fmtPercent(payload.summary?.averageDecreasePercent ?? 0)} />
          </div>
          <div className="reports-context-list">
            {payload.context.contexts.map((item) => (
              <div key={`${item.priceFormatId}-${item.priceListNumber}`} className="reports-context-card">
                <strong>{item.priceFormatName || item.priceFormatCode}</strong>
                <span>{item.customerCategory || '—'} · {item.priceListNumber} · действует с {formatActivationDate(item.currentActivationDate || activationDate)} · создан {item.calculatedAtDisplay || '—'}</span>
                {item.previousPriceListNumber ? <em>Предыдущий расчёт: {item.previousPriceListNumber}</em> : null}
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {error ? <div className="dashboard-alert">{error}</div> : null}

      <Tabs value={activeTab} onValueChange={(value) => setActiveTab(value as ReportType)} className="w-full">
        <TabsList className="w-full justify-start border-b border-gray-200 rounded-none h-auto p-0 bg-transparent">
          <TabsTrigger value="rank-1" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">Ранг 1</TabsTrigger>
          <TabsTrigger value="decreases" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">Снижение</TabsTrigger>
        </TabsList>

        <TabsContent value={activeTab} className="m-0 pt-4">
          <section className="generated-panel">
            <div className="card-title-row">
              <div className="generated-search">
                <Search className="h-4 w-4" />
                <Input value={q} onChange={(event) => setQ(event.target.value)} placeholder="Поиск по материалу, названию, производителю" />
              </div>
              <div className="reports-inline-actions">
                {q ? <Button variant="ghost" onClick={() => setQ('')}><X className="mr-2 h-4 w-4" />Очистить</Button> : null}
                <Button variant="outline" onClick={exportExcel} disabled={isLoadingContexts || !appliedContexts.length || !contextsReady || isExporting}>
                  <Download className="mr-2 h-4 w-4" />Excel
                </Button>
              </div>
            </div>

            {isLoading ? (
              <div className="dashboard-empty">Формируем отчёт...</div>
            ) : !payload?.items?.length ? (
              <div className="dashboard-empty">{emptyText}</div>
            ) : isRank ? (
              <ReportTable
                columns={['Ценовой формат', 'Категория клиента', 'Материал', 'Наименование', 'Производитель', 'ТОП-1500', 'Новая цена', 'Ранг 1']}
                rows={payload.items.map((row) => [row.priceFormatName || row.priceFormatCode || '—', row.customerCategory || '—', row.material, row.materialName, row.manufacturer || '—', fmtNumber(row.top1500, 0), fmtNumber(row.newPrice), row.rank])}
              />
            ) : (
              <ReportTable
                columns={['Ценовой формат', 'Регион', 'Категория клиента', 'Материал', 'Наименование', 'ТОП-1500', 'Новая цена', 'Старая цена', 'Снижение, ₸', 'Снижение, %', 'Производитель']}
                rows={payload.items.map((row) => [row.priceFormatName || row.priceFormatCode || '—', row.region || '—', row.customerCategory || '—', row.material, row.name, fmtNumber(row.top1500, 0), fmtNumber(row.newPrice), fmtNumber(row.oldPrice), fmtNumber(row.decreaseKzt), fmtPercent(row.decreasePercent), row.manufacturer || '—'])}
              />
            )}

            <div className="generated-pagination">
              <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>Назад</Button>
              <span>{page} / {totalPages} · {fmtNumber(payload?.total ?? 0, 0)} поз.</span>
              <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage((value) => Math.min(totalPages, value + 1))}>Вперёд</Button>
            </div>
          </section>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric-card generated-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ReportTable({ columns, rows }: { columns: string[]; rows: Array<Array<string | number>> }) {
  return (
    <div className="compact-table-wrap">
      <table className="compact-table">
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => <td key={`${rowIndex}-${cellIndex}`}>{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
