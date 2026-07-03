import { useEffect, useMemo, useState } from 'react';
import { BarChart3, Download, FileText, GitCompare, Search, Archive, X } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from './ui/dialog';

type PriceListRow = {
  id: number;
  number: string;
  format: string;
  formatName: string;
  branch: string;
  pricingRule: string;
  date: string;
  createdAt: string;
  activationDate: string;
  user: string;
  skuCount: number;
  withCompetitors: number;
  withoutCompetitors: number;
  status: string;
  revision: string;
  comment: string;
};

type AnalyticsSummary = {
  skuTotal: number;
  withCompetitors: number;
  withoutCompetitors: number;
  leftZone: number;
  optimalZone: number;
  rightZone: number;
  noCompetitorRuleApplied?: number;
  averageMarkup: number;
  minPrice: number | null;
  maxPrice: number | null;
  percentileUsage: number;
};

type PriceListCard = PriceListRow & {
  analytics?: {
    summary?: AnalyticsSummary;
    productsWithSubstituteMatches?: number;
  };
};

type CompetitorColumn = {
  id: number | string;
  key: string;
  title: string;
  sourceType: string;
  priceListId?: number | string;
  competitorName?: string;
};

type CompetitorPriceCell = {
  price: number | null;
  sourcePrice?: number | null;
  coefficient?: number;
  sourceName?: string;
  matchedBy?: string;
  isManualMapping?: boolean;
  isSubstitute?: boolean;
};

type PriceItem = {
  sku: string;
  name: string;
  topRank?: number | null;
  isTop?: boolean;
  is_top?: number | string | null;
  globalRating?: number | null;
  localRating?: number | null;
  manufacturer: string;
  stock: number | null;
  cost: number;
  basePrice: number;
  mdc: number;
  bestCompetitorPrice: number | null;
  priceAfterBend: number | null;
  finalPrice: number;
  markupPercent: number | null;
  zone: string | null;
  priceSource: string;
  percentileSource: string;
  pricingReason: string;
  pricingCalculationLog?: string;
  listOverrideLog?: {
    listName: string;
    listCode: string;
    listType: string;
    value: number | null;
    displayValue: string;
    affectedField?: string;
    action: string;
    ambiguous?: boolean;
  } | null;
  pricingRule: string;
  appliedRuleType?: string;
  appliedRuleValue?: number | null;
  appliedListName?: string;
  appliedRuleAmbiguous?: boolean;
  competitorPrices?: Record<string, CompetitorPriceCell>;
  log: Array<{ label: string; value: any; description: string }>;
};

const pricingLogText = (row: PriceItem) => row.pricingCalculationLog || row.pricingReason || 'Причина расчёта не сохранена.';

const pricingLogTone = (row: PriceItem) => {
  if (row.appliedRuleType === 'fixed_price') {
    return 'border-blue-200 bg-blue-50 text-blue-950';
  }
  if (row.bestCompetitorPrice == null || row.priceAfterBend == null) {
    return 'border-red-200 bg-red-50 text-red-950';
  }
  return 'border-blue-200 bg-blue-50 text-blue-950';
};

type CompareRow = {
  sku: string;
  name: string;
  oldPrice: number;
  newPrice: number;
  changePercent: number | null;
  oldZone: string;
  newZone: string;
};

type PriceListsTabProps = {
  formatCode: string;
  initialPriceListNumber?: string;
};

const parseJsonOrNull = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const DASH = '—';

const fmtNumber = (value: unknown) => {
  if (value === null || value === undefined || value === '') return DASH;
  const n = Number(value);
  if (!Number.isFinite(n)) return DASH;
  return n.toLocaleString('ru-RU', { maximumFractionDigits: 2 });
};

const fmtPrice = (value: unknown) => {
  if (value === null || value === undefined || value === '') return DASH;
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return DASH;
  return n.toLocaleString('ru-RU', { maximumFractionDigits: 2 });
};

const competitorCellTitle = (cell?: CompetitorPriceCell) => {
  if (!cell) return '';
  const parts = [];
  if (cell.matchedBy) parts.push(`matchedBy: ${cell.matchedBy}`);
  if (cell.isManualMapping) parts.push('ручное сопоставление');
  if (cell.isSubstitute) parts.push('замена товара');
  if (cell.coefficient && cell.coefficient !== 1) parts.push(`coefficient: ${cell.coefficient}`);
  return parts.join(' · ');
};

const renderCompetitorCell = (row: PriceItem, column: CompetitorColumn) => {
  const cell = row.competitorPrices?.[column.key];
  if (!cell || cell.price == null || Number(cell.price) <= 0) return DASH;
  const marker = cell.isManualMapping ? ' M' : cell.isSubstitute ? ' S' : '';
  return <span title={competitorCellTitle(cell)}>{fmtNumber(cell.price)}{marker}</span>;
};

const zoneMeta: Record<string, { label: string; title: string; className: string }> = {
  left: { label: 'ЛП', title: 'Цена ниже лучшей цены конкурента', className: 'left' },
  optimal: { label: 'Зона логичности', title: 'Цена в зоне логичности', className: 'optimal' },
  right: { label: 'ПП', title: 'Цена выше зоны логичности', className: 'right' },
  'no-data': { label: 'Зона без цен', title: 'Нет цен конкурентов для сравнения зоны', className: 'no-data' },
};

export function PriceListsTab({ formatCode, initialPriceListNumber = '' }: PriceListsTabProps) {
  const [lists, setLists] = useState<PriceListRow[]>([]);
  const [opened, setOpened] = useState<PriceListCard | null>(null);
  const [items, setItems] = useState<PriceItem[]>([]);
  const [competitorColumns, setCompetitorColumns] = useState<CompetitorColumn[]>([]);
  const [itemsTotal, setItemsTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [selectedItem, setSelectedItem] = useState<PriceItem | null>(null);
  const [compareRows, setCompareRows] = useState<CompareRow[] | null>(null);
  const [branchFilter, setBranchFilter] = useState('__all__');
  const [formatFilter, setFormatFilter] = useState(formatCode || '__all__');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [search, setSearch] = useState('');
  const [itemSearch, setItemSearch] = useState('');
  const [zoneFilter, setZoneFilter] = useState('__all__');
  const [topFilter, setTopFilter] = useState('__all__');
  const [sort, setSort] = useState('name');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastInitialOpened, setLastInitialOpened] = useState('');

  const branchOptions = useMemo(
    () => Array.from(new Set(lists.map((row) => row.branch || '').filter((value) => value !== undefined))).sort((a, b) => a.localeCompare(b, 'ru')),
    [lists]
  );
  const formatOptions = useMemo(
    () => Array.from(new Set(lists.map((row) => row.format).filter(Boolean))).sort(),
    [lists]
  );
  const previousList = useMemo(
    () => opened ? lists.find((row) => row.format === opened.format && row.number !== opened.number) || null : null,
    [lists, opened]
  );
  const summary = opened?.analytics?.summary;

  const loadLists = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (branchFilter !== '__all__') params.set('branch', branchFilter === '__blank__' ? '' : branchFilter);
      if (formatFilter !== '__all__') params.set('format_code', formatFilter);
      if (dateFrom) params.set('date_from', dateFrom);
      if (dateTo) params.set('date_to', dateTo);
      if (search.trim()) params.set('search', search.trim());
      const res = await fetch(`/api/generated-price-lists?${params.toString()}`);
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить прайс-листы');
      setLists(Array.isArray(data) ? data : []);
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки прайс-листов');
    } finally {
      setIsLoading(false);
    }
  };

  const loadCard = async (id: string | number) => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/generated-price-lists/${encodeURIComponent(String(id))}`);
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось открыть прайс-лист');
      setOpened(data);
      setPage(1);
      setCompareRows(null);
    } catch (e: any) {
      setError(e?.message || 'Ошибка открытия прайс-листа');
    } finally {
      setIsLoading(false);
    }
  };

  const loadItems = async () => {
    if (!opened) return;
    setIsLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: '100',
        zone: zoneFilter,
        top_filter: topFilter,
        sort,
      });
      if (itemSearch.trim()) params.set('q', itemSearch.trim());
      const res = await fetch(`/api/generated-price-lists/${encodeURIComponent(opened.number)}/items?${params.toString()}`);
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить товары прайса');
      setItems(Array.isArray(data?.items) ? data.items : []);
      setCompetitorColumns(Array.isArray(data?.competitorColumns) ? data.competitorColumns : []);
      setItemsTotal(Number(data?.total || 0));
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки товаров');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void loadLists();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [branchFilter, formatFilter, dateFrom, dateTo]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadLists(), 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  useEffect(() => {
    void loadItems();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened?.number, page, zoneFilter, topFilter, sort]);

  useEffect(() => {
    if (!initialPriceListNumber || initialPriceListNumber === lastInitialOpened) return;
    setLastInitialOpened(initialPriceListNumber);
    void loadCard(initialPriceListNumber);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPriceListNumber, lastInitialOpened]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setPage(1);
      void loadItems();
    }, 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemSearch]);

  const exportList = (id: string | number, fmt: 'csv' | 'xlsx') => {
    const params = new URLSearchParams();
    if (topFilter !== '__all__') params.set('top_filter', topFilter);
    const suffix = params.toString() ? `?${params.toString()}` : '';
    window.location.href = `/api/generated-price-lists/${encodeURIComponent(String(id))}/export.${fmt}${suffix}`;
  };

  const compareWithPrevious = async () => {
    if (!opened || !previousList) return;
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/generated-price-lists/${encodeURIComponent(opened.number)}/compare/${encodeURIComponent(previousList.number)}`);
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось сравнить прайсы');
      setCompareRows(Array.isArray(data?.items) ? data.items : []);
    } catch (e: any) {
      setError(e?.message || 'Ошибка сравнения');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="generated-workspace">
      <section className="generated-toolbar">
        <div>
          <div className="eyebrow">Сформированные прайс-листы</div>
          <h3>{opened ? opened.number : 'Список результатов'}</h3>
        </div>
        <Select value={branchFilter} onValueChange={setBranchFilter}>
          <SelectTrigger><SelectValue placeholder="Филиал" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все филиалы</SelectItem>
            {branchOptions.map((item) => <SelectItem key={item || '__blank__'} value={item || '__blank__'}>{item || 'Без филиала'}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={formatFilter} onValueChange={setFormatFilter}>
          <SelectTrigger><SelectValue placeholder="ЦФ" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все ЦФ</SelectItem>
            {formatOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
          </SelectContent>
        </Select>
        <Input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
        <Input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
        <div className="generated-search">
          <Search className="h-4 w-4" />
          <Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по номеру, ЦФ" />
        </div>
      </section>

      {error ? <div className="dashboard-alert">{error}</div> : null}

      <section className="generated-panel">
        <div className="card-title-row">
          <h3>Список сформированных прайсов</h3>
          <span className="muted-count">{lists.length} шт.</span>
        </div>
        <CompactTable
          empty="Сформированных прайс-листов пока нет"
          columns={['Номер прайса', 'ЦФ', 'Филиал', 'Правило ЦО', 'Дата формирования', 'Дата активации', 'Пользователь', 'SKU', 'Статус', 'Revision', 'Комментарий', 'Действия']}
          rows={lists.map((row) => [
            <button key={`${row.id}-number`} type="button" className="table-link" onClick={() => loadCard(row.number)}>{row.number}</button>,
            row.format,
            row.branch || 'Без филиала',
            row.pricingRule || '—',
            row.date || '—',
            row.activationDate || '—',
            row.user || '—',
            Number(row.skuCount || 0).toLocaleString('ru-RU'),
            <span key={`${row.id}-status`} className="status-pill ok">{row.status || 'generated'}</span>,
            row.revision || '—',
            row.comment || '—',
            <div key={`${row.id}-actions`} className="generated-actions">
              <Button variant="ghost" size="sm" onClick={() => loadCard(row.number)}>Открыть</Button>
              <Button variant="ghost" size="sm" onClick={() => { void loadCard(row.number); }}>Аналитика</Button>
              <Button variant="ghost" size="sm" onClick={() => exportList(row.number, 'xlsx')}>Экспорт</Button>
              <Button variant="ghost" size="sm" onClick={() => loadCard(row.number)}>Сравнить</Button>
              <Button variant="ghost" size="sm" disabled><Archive className="mr-1 h-4 w-4" />Архив</Button>
            </div>,
          ])}
        />
      </section>

      {opened ? (
        <>
          <section className="generated-card">
            <div className="card-title-row">
              <div>
                <h3>{opened.number}</h3>
                <p>{opened.format} · {opened.branch || 'Без филиала'} · {opened.pricingRule || 'без правила ЦО'}</p>
              </div>
              <div className="generated-actions">
                <Button variant="outline" size="sm" onClick={() => exportList(opened.number, 'csv')}><Download className="mr-2 h-4 w-4" />CSV</Button>
                <Button variant="outline" size="sm" onClick={() => exportList(opened.number, 'xlsx')}><Download className="mr-2 h-4 w-4" />XLSX</Button>
                <Button variant="outline" size="sm" onClick={compareWithPrevious} disabled={!previousList}><GitCompare className="mr-2 h-4 w-4" />Сравнить</Button>
                <Button variant="ghost" size="sm" onClick={() => setOpened(null)}><X className="mr-2 h-4 w-4" />Закрыть</Button>
              </div>
            </div>

            <div className="generated-summary">
              <Metric label="Всего SKU" value={summary?.skuTotal ?? opened.skuCount} />
              <Metric label="С конкурентной ценой" value={summary?.withCompetitors ?? opened.withCompetitors} />
              <Metric label="Без конкурентной цены" value={summary?.withoutCompetitors ?? opened.withoutCompetitors} />
              <Metric label="Применена логика без конкурентов" value={summary?.noCompetitorRuleApplied ?? 0} />
              <Metric label="ЛП" value={summary?.leftZone ?? 0} />
              <Metric label="Зона логичности" value={summary?.optimalZone ?? 0} />
              <Metric label="ПП" value={summary?.rightZone ?? 0} />
              <Metric label="Зона без цен" value={summary?.withoutCompetitors ?? opened.withoutCompetitors} />
              <Metric label="Средняя наценка" value={`${fmtNumber(summary?.averageMarkup)}%`} />
              <Metric label="Диапазон итоговой цены" value={summary?.minPrice != null && summary?.maxPrice != null ? `${fmtNumber(summary.minPrice)}-${fmtNumber(summary.maxPrice)}` : DASH} />
              <Metric label="Использовано персентилей" value={summary?.percentileUsage ?? 0} />
              <Metric label="Использовано замен" value={opened.analytics?.productsWithSubstituteMatches ?? 0} />
            </div>
            <p className="text-sm text-gray-600 mt-3">Зона рассчитывается только при наличии первой цены конкурента. Для товаров без конкурентной цены отображается «Зона без цен».</p>
          </section>

          <section className="generated-panel">
            <div className="card-title-row">
              <div>
                <h3>Товары прайс-листа</h3>
                <p>{items.length} из {itemsTotal} позиций</p>
              </div>
              <div className="generated-filters">
                <div className="generated-search">
                  <Search className="h-4 w-4" />
                  <Input value={itemSearch} onChange={(event) => setItemSearch(event.target.value)} placeholder="SKU или наименование" />
                </div>
                <Select value={zoneFilter} onValueChange={(value) => { setZoneFilter(value); setPage(1); }}>
                  <SelectTrigger><SelectValue placeholder="Зона" /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__all__">Все зоны</SelectItem>
                    {Object.entries(zoneMeta).map(([value, meta]) => <SelectItem key={value} value={value}>{meta.label}</SelectItem>)}
                  </SelectContent>
                </Select>
                <Select value={topFilter} onValueChange={(value) => { setTopFilter(value); setPage(1); }}>
                  <SelectTrigger><SelectValue placeholder="Рейтинг глобальный" /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__all__">Все</SelectItem>
                    <SelectItem value="top">С глобальным рейтингом</SelectItem>
                    <SelectItem value="non_top">Без глобального рейтинга</SelectItem>
                  </SelectContent>
                </Select>
                <Select value={sort} onValueChange={setSort}>
                  <SelectTrigger><SelectValue placeholder="Сортировка" /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="name">Наименование</SelectItem>
                    <SelectItem value="final_desc">Цена по убыванию</SelectItem>
                    <SelectItem value="final_asc">Цена по возрастанию</SelectItem>
                    <SelectItem value="markup_desc">Наценка</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <CompactTable
              empty="По выбранным фильтрам нет товаров"
              columns={['SKU', 'Рейтинг глобальный', 'Рейтинг локальный', 'Наименование', 'Производитель', 'Остаток', 'Себестоимость', 'Базовая цена', 'МДЦ', 'Лучший конкурент', ...competitorColumns.map((column) => column.title || column.key), 'После прогиба', 'Финальная цена', 'Наценка %', 'Зона', 'Источник', 'Персентиль', 'Причина']}
              rows={items.map((row) => [
                row.sku,
                fmtNumber(row.globalRating),
                fmtNumber(row.localRating),
                row.name,
                row.manufacturer || DASH,
                fmtNumber(row.stock),
                fmtNumber(row.cost),
                fmtNumber(row.basePrice),
                fmtNumber(row.mdc),
                fmtPrice(row.bestCompetitorPrice),
                ...competitorColumns.map((column) => renderCompetitorCell(row, column)),
                fmtNumber(row.priceAfterBend),
                <strong key={`${row.sku}-final`}>{fmtNumber(row.finalPrice)}</strong>,
                row.markupPercent != null ? `${fmtNumber(row.markupPercent)}%` : DASH,
                <ZoneBadge key={`${row.sku}-zone`} zone={row.zone} showList={Boolean(row.listOverrideLog)} />,
                row.priceSource || DASH,
                row.percentileSource || DASH,
                <button key={`${row.sku}-log`} type="button" className="table-link max-w-80 text-left" onClick={() => setSelectedItem(row)}>
                  <span className="block text-blue-700">● {pricingLogText(row)}</span>
                  {row.listOverrideLog ? (
                    <span className="mt-1 block text-amber-700">● {row.listOverrideLog.listType} · {row.listOverrideLog.listName} · {row.listOverrideLog.displayValue}</span>
                  ) : null}
                </button>,
              ])}
            />
            <div className="generated-pagination">
              <Button variant="outline" size="sm" onClick={() => setPage((prev) => Math.max(1, prev - 1))} disabled={page <= 1}>Назад</Button>
              <span>Страница {page}</span>
              <Button variant="outline" size="sm" onClick={() => setPage((prev) => prev + 1)} disabled={page * 100 >= itemsTotal}>Далее</Button>
            </div>
          </section>

          {compareRows ? (
            <section className="generated-panel">
              <div className="card-title-row">
                <h3>Сравнение с предыдущим прайсом</h3>
                <span className="muted-count">{previousList?.number}</span>
              </div>
              <CompactTable
                empty="Нет данных для сравнения"
                columns={['SKU', 'Наименование', 'Старая цена', 'Новая цена', 'Изменение %', 'Старая зона', 'Новая зона']}
                rows={compareRows.map((row) => [
                  row.sku,
                  row.name,
                  fmtNumber(row.oldPrice),
                  fmtNumber(row.newPrice),
                  row.changePercent != null ? `${fmtNumber(row.changePercent)}%` : '—',
                  <ZoneBadge key={`${row.sku}-old`} zone={row.oldZone} />,
                  <ZoneBadge key={`${row.sku}-new`} zone={row.newZone} />,
                ])}
              />
            </section>
          ) : null}
        </>
      ) : null}

      <Dialog open={Boolean(selectedItem)} onOpenChange={(open) => !open && setSelectedItem(null)}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>Лог расчёта</DialogTitle>
          </DialogHeader>
          {selectedItem ? (
            <div className="price-log">
              <div className="price-log-head">
                <FileText className="h-5 w-5 text-blue-600" />
                <div>
                  <strong>{selectedItem.sku}</strong>
                  <p>{selectedItem.name}</p>
                </div>
              </div>
              <div className={`rounded-md border p-4 text-sm ${pricingLogTone(selectedItem)}`}>
                <strong className="block mb-2">Лог #1 — причина расчёта цены</strong>
                <p>{selectedItem.pricingCalculationLog || selectedItem.pricingReason || 'Причина расчёта не сохранена.'}</p>
              </div>
              {selectedItem.listOverrideLog ? (
                <div className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
                  <strong className="block mb-2">Лог #2 — Работа со списками</strong>
                  <p>Позиция найдена в списке:</p>
                  <p>Название: {selectedItem.listOverrideLog.listName || '—'}</p>
                  <p>Код: {selectedItem.listOverrideLog.listCode || '—'}</p>
                  <p>Тип: {selectedItem.listOverrideLog.listType}</p>
                  <p>Значение: {selectedItem.listOverrideLog.displayValue}</p>
                  <p>Поле: {selectedItem.listOverrideLog.affectedField || 'Параметр расчёта'}</p>
                  <p className="mt-2 font-medium">{selectedItem.listOverrideLog.action}</p>
                  {selectedItem.listOverrideLog.ambiguous ? <p className="mt-2 font-semibold">Статус: правило требует бизнес-подтверждения.</p> : null}
                </div>
              ) : null}
              {selectedItem.log.map((line) => (
                <div key={line.label} className="price-log-line">
                  <div>
                    <strong>{line.label}</strong>
                    <p>{line.description}</p>
                  </div>
                  <span>{String(line.value ?? '—')}</span>
                </div>
              ))}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      {isLoading ? <div className="text-sm text-gray-500">Загрузка...</div> : null}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: any }) {
  return (
    <div className="generated-metric">
      <span>{label}</span>
      <strong>{value ?? '—'}</strong>
    </div>
  );
}

function ZoneBadge({ zone, showList = false }: { zone: string | null; showList?: boolean }) {
  const meta = zone ? zoneMeta[zone] : undefined;
  if (!meta && !showList) return <span>—</span>;
  return (
    <span className="zone-stack">
      {meta ? <span className={`zone-badge ${meta.className}`} title={meta.title}>{meta.label}</span> : <span>—</span>}
      {showList ? <span className="zone-badge list">Список</span> : null}
    </span>
  );
}

function CompactTable({ columns, rows, empty }: { columns: string[]; rows: any[][]; empty: string }) {
  return (
    <div className="compact-table-wrap">
      <table className="compact-table">
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.length ? rows.map((row, idx) => (
            <tr key={idx}>
              {row.map((cell, cellIdx) => <td key={cellIdx}>{cell}</td>)}
            </tr>
          )) : (
            <tr>
              <td colSpan={columns.length} className="compact-empty">{empty}</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
