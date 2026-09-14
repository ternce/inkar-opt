import { useEffect, useMemo, useState } from 'react';
import { Download, Search } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';

type PriceFormat = {
  id: string;
  name: string;
  code: string;
  branch: string;
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
  skuCount: number;
};

type ReportPayload = {
  items: any[];
  total: number;
  page: number;
  limit: number;
  context?: {
    branch: string;
    priceFormatCode: string;
    priceFormatName: string;
    priceListNumber: string;
    calculatedAtDisplay: string;
    totalCalculated: number;
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

export function ReportsTab({ branch, selectedFormatCode, priceFormats }: ReportsTabProps) {
  const [activeTab, setActiveTab] = useState<ReportType>('rank-1');
  const [branchFilter, setBranchFilter] = useState(branch || '__all__');
  const [formatFilter, setFormatFilter] = useState(selectedFormatCode || '__all__');
  const [priceLists, setPriceLists] = useState<ReportPriceList[]>([]);
  const [priceListId, setPriceListId] = useState('');
  const [payload, setPayload] = useState<ReportPayload | null>(null);
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const limit = 100;

  const branchOptions = useMemo(
    () => Array.from(new Set(priceFormats.map((format) => format.branch).filter(Boolean))).sort((a, b) => a.localeCompare(b, 'ru-RU')),
    [priceFormats]
  );

  const formatOptions = useMemo(
    () => priceFormats.filter((format) => branchFilter === '__all__' || format.branch === branchFilter),
    [branchFilter, priceFormats]
  );

  useEffect(() => {
    setBranchFilter(branch || '__all__');
  }, [branch]);

  useEffect(() => {
    setFormatFilter(selectedFormatCode || '__all__');
  }, [selectedFormatCode]);

  useEffect(() => {
    const loadPriceLists = async () => {
      setError('');
      const params = new URLSearchParams();
      if (branchFilter !== '__all__') params.set('branch', branchFilter);
      if (formatFilter !== '__all__') params.set('format_code', formatFilter);
      const res = await fetch(`/api/reports/price-lists?${params.toString()}`);
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить расчёты');
      const rows = Array.isArray(data) ? data : [];
      setPriceLists(rows);
      setPriceListId((current) => (current && rows.some((row: ReportPriceList) => row.number === current || String(row.id) === current) ? current : rows[0]?.number || ''));
    };
    loadPriceLists().catch((err) => setError(err?.message || 'Не удалось загрузить расчёты'));
  }, [branchFilter, formatFilter]);

  useEffect(() => {
    if (!priceListId) {
      setPayload(null);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const loadReport = async () => {
        setIsLoading(true);
        setError('');
        try {
          const params = new URLSearchParams({
            price_list_id: priceListId,
            page: String(page),
            limit: String(limit),
          });
          if (q.trim()) params.set('q', q.trim());
          const res = await fetch(`/api/reports/${endpointFor(activeTab)}?${params.toString()}`, { signal: controller.signal });
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
  }, [activeTab, page, priceListId, q]);

  useEffect(() => {
    setPage(1);
  }, [activeTab, priceListId, q]);

  const exportExcel = () => {
    if (!priceListId) return;
    const params = new URLSearchParams({ price_list_id: priceListId });
    if (q.trim()) params.set('q', q.trim());
    window.location.href = `/api/reports/${endpointFor(activeTab)}/export.xlsx?${params.toString()}`;
  };

  const totalPages = Math.max(1, Math.ceil((payload?.total || 0) / limit));
  const context = payload?.context;
  const isRank = activeTab === 'rank-1';
  const emptyText = isRank
    ? 'Для выбранного расчёта позиций Ранг 1 не найдено.'
    : 'Для выбранного расчёта снижений от 0,5% не найдено.';

  return (
    <div className="generated-workspace">
      <section className="generated-toolbar">
        <div>
          <div className="eyebrow">Отчёты</div>
          <h3>Аналитические отчёты по результатам расчёта цен</h3>
        </div>
        <Select value={branchFilter} onValueChange={setBranchFilter}>
          <SelectTrigger><SelectValue placeholder="Филиал" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все филиалы</SelectItem>
            {branchOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={formatFilter} onValueChange={setFormatFilter}>
          <SelectTrigger><SelectValue placeholder="Ценовой формат" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все форматы</SelectItem>
            {formatOptions.map((item) => <SelectItem key={item.code} value={item.code}>{item.code}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={priceListId} onValueChange={setPriceListId}>
          <SelectTrigger><SelectValue placeholder="Расчёт / дата" /></SelectTrigger>
          <SelectContent>
            {priceLists.map((item) => (
              <SelectItem key={item.number} value={item.number}>
                {item.number} · {item.date || item.createdAt}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </section>

      {context ? (
        <section className="generated-panel">
          <div className="generated-summary">
            <Metric label="Филиал" value={context.branch || '—'} />
            <Metric label="Ценовой формат" value={context.priceFormatCode || '—'} />
            <Metric label="Расчёт" value={context.priceListNumber || '—'} />
            <Metric label="Дата расчёта" value={context.calculatedAtDisplay || '—'} />
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
              <Button variant="outline" onClick={exportExcel} disabled={!priceListId}>
                <Download className="mr-2 h-4 w-4" />Скачать Excel
              </Button>
            </div>

            <div className="generated-summary">
              {isRank ? (
                <>
                  <Metric label="Всего позиций Ранг 1" value={fmtNumber(payload?.summary?.totalRank1 ?? 0, 0)} />
                  <Metric label="Доля от выбранного расчёта" value={fmtPercent(payload?.summary?.sharePercent ?? 0)} />
                </>
              ) : (
                <>
                  <Metric label="Всего сниженных позиций" value={fmtNumber(payload?.summary?.totalDecreases ?? 0, 0)} />
                  <Metric label="Общая сумма снижения" value={fmtNumber(payload?.summary?.totalDecreaseKzt ?? 0)} />
              <Metric label="Среднее снижение" value={fmtPercent(payload?.summary?.averageDecreasePercent ?? 0)} />
                </>
              )}
            </div>

            {isLoading ? (
              <div className="dashboard-empty">Загрузка отчёта...</div>
            ) : !payload?.items?.length ? (
              <div className="dashboard-empty">{emptyText}</div>
            ) : isRank ? (
              <ReportTable
                columns={['Категория клиента', 'Материал', 'Наименование', 'Производитель', 'ТОП-1500', 'Новая цена', 'Ранг 1']}
                rows={payload.items.map((row) => [row.customerCategory || '—', row.material, row.materialName, row.manufacturer || '—', fmtNumber(row.top1500, 0), fmtNumber(row.newPrice), row.rank])}
              />
            ) : (
              <ReportTable
                columns={['Регион', 'Категория клиента', 'Материал', 'Наименование', 'TOP-1500', 'Новая цена', 'Старая цена', 'Снижение, ₸', 'Снижение, %', 'Производитель']}
                rows={payload.items.map((row) => [row.region || '—', row.customerCategory || '—', row.material, row.name, fmtNumber(row.top1500, 0), fmtNumber(row.newPrice), fmtNumber(row.oldPrice), fmtNumber(row.decreaseKzt), fmtPercent(row.decreasePercent), row.manufacturer || '—'])}
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
    <div className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ReportTable({ columns, rows }: { columns: string[]; rows: Array<Array<string | number>> }) {
  return (
    <div className="table-scroll">
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
