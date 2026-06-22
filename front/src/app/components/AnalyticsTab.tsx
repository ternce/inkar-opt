import { useEffect, useMemo, useState } from 'react';
import { Bar, BarChart, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Download, ExternalLink, Search } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from './ui/dialog';

type PriceListRow = {
  number: string;
  format: string;
  branch: string;
  date: string;
  activationDate?: string;
  skuCount: number;
  status: string;
};

type AnalyticsPayload = {
  priceList: PriceListRow;
  summary: Record<string, number>;
  zones: Array<{ code: string; label: string; count: number; percent: number; change: number }>;
  charts: {
    zoneDistribution: Array<{ code: string; label: string; count: number; percent: number }>;
    markupHistogram: Array<{ label: string; count: number }>;
    competitorUsage: Array<{ source: string; count: number }>;
    percentileUsage: Array<{ label: string; count: number }>;
    noCompetitors: Array<{ label: string; count: number }>;
    topChangedProducts: Array<{ sku: string; name: string; oldPrice: number; newPrice: number; changePercent: number; zone: string | null }>;
  };
  repricing: Record<string, number>;
  rightZoneReasons?: Record<string, number>;
};

type ResultItem = {
  sku: string;
  name: string;
  manufacturer?: string;
  basePrice: number;
  finalPrice: number;
  bestCompetitorPrice: number | null;
  markupPercent: number | null;
  priceAfterBend: number | null;
  zone: string | null;
  priceSource: string;
  appliedSourceName?: string;
  percentileSource?: string;
  usedPercentile?: boolean;
  usedSubstitute?: boolean;
  appliedListIds?: string;
  pricingReason: string;
  pricingCalculationLog?: string;
  listOverrideLog?: {
    listName: string;
    listCode: string;
    listType: string;
    displayValue: string;
    action: string;
    ambiguous?: boolean;
  } | null;
  pricingRule: string;
  log?: Array<{ label: string; value: any; description: string }>;
};

type Props = {
  branch?: string;
  selectedFormatCode?: string;
  initialPriceListNumber?: string;
  onNavigate?: (section: any) => void;
};

const zoneColors: Record<string, string> = {
  left: '#0f766e',
  optimal: '#2563eb',
  right: '#c2410c',
};

const parseJson = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const fmt = (value: unknown) => {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('ru-RU', { maximumFractionDigits: 2 });
};

const pct = (part: unknown, total: unknown) => {
  const p = Number(part);
  const t = Number(total);
  if (!Number.isFinite(p) || !Number.isFinite(t) || t <= 0) return '0';
  return ((p / t) * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 });
};

const rightReasonLabels: Record<string, string> = {
  right_due_to_mdc_floor: 'ПП: MDC floor',
  right_due_to_chosen_higher_competitor: 'ПП: chosen higher competitor',
  right_due_to_universal_override: 'ПП: universal override',
  right_other: 'ПП: other',
};

const changePercent = (row: ResultItem) => {
  if (!row.basePrice) return null;
  return ((Number(row.finalPrice || 0) - Number(row.basePrice || 0)) / Number(row.basePrice)) * 100;
};

const hasUniversalList = (row: ResultItem) => {
  const raw = String(row.appliedListIds || '').trim();
  return Boolean(raw && raw !== '[]' && raw !== 'null' && raw !== 'None');
};

const zoneLabel = (zone: string | null | undefined) => {
  if (zone === 'left') return 'Левое плечо: ниже конкурента';
  if (zone === 'optimal') return 'ЗЛ';
  if (zone === 'right') return 'ПП';
  return '—';
};

const rowChangeClass = (row: ResultItem) => {
  const value = changePercent(row);
  if (value === null || Math.abs(value) <= 0.01) return 'neutral';
  if (Math.abs(value) >= 20) return 'critical';
  return value > 0 ? 'up' : 'down';
};

export function AnalyticsTab({ branch = '', selectedFormatCode = '', initialPriceListNumber = '', onNavigate }: Props) {
  const [priceLists, setPriceLists] = useState<PriceListRow[]>([]);
  const [payload, setPayload] = useState<AnalyticsPayload | null>(null);
  const [priceListId, setPriceListId] = useState('');
  const [branchFilter, setBranchFilter] = useState(branch || '__all__');
  const [formatFilter, setFormatFilter] = useState(selectedFormatCode || '__all__');
  const [activationDateFilter, setActivationDateFilter] = useState('');
  const [zoneDrilldown, setZoneDrilldown] = useState<string | null>(null);
  const [items, setItems] = useState<any[]>([]);
  const [resultItems, setResultItems] = useState<ResultItem[]>([]);
  const [resultTotal, setResultTotal] = useState(0);
  const [resultPage, setResultPage] = useState(1);
  const [selectedItem, setSelectedItem] = useState<ResultItem | null>(null);
  const [search, setSearch] = useState('');
  const [itemSearch, setItemSearch] = useState('');
  const [onlyChanged, setOnlyChanged] = useState(false);
  const [onlyNoCompetitors, setOnlyNoCompetitors] = useState(false);
  const [onlyPercentile, setOnlyPercentile] = useState(false);
  const [onlyUniversalLists, setOnlyUniversalLists] = useState(false);
  const [directionFilter, setDirectionFilter] = useState('__all__');
  const [zoneFilter, setZoneFilter] = useState('__all__');
  const [ruleFilter, setRuleFilter] = useState('__all__');
  const [sourceFilter, setSourceFilter] = useState('__all__');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [lastInitialOpened, setLastInitialOpened] = useState('');

  const branchOptions = useMemo(
    () => Array.from(new Set(priceLists.map((row) => row.branch).filter(Boolean))).sort((a, b) => a.localeCompare(b, 'ru')),
    [priceLists]
  );
  const formatOptions = useMemo(
    () => Array.from(new Set(priceLists.map((row) => row.format).filter(Boolean))).sort(),
    [priceLists]
  );
  const selectablePriceLists = useMemo(
    () => activationDateFilter ? priceLists.filter((row) => row.activationDate === activationDateFilter) : priceLists,
    [activationDateFilter, priceLists]
  );

  const loadPriceLists = async () => {
    const params = new URLSearchParams();
    if (branchFilter !== '__all__') params.set('branch', branchFilter);
    if (formatFilter !== '__all__') params.set('format_code', formatFilter);
    const res = await fetch(`/api/generated-price-lists?${params.toString()}`);
    const text = await res.text();
    const data = parseJson(text);
    if (res.ok) {
      setPriceLists(Array.isArray(data) ? data : []);
      if (!priceListId && Array.isArray(data) && data[0]?.number) setPriceListId(data[0].number);
    }
  };

  const loadAnalytics = async () => {
    if (activationDateFilter && !priceListId) {
      setPayload(null);
      return;
    }
    setIsLoading(true);
    setError('');
    try {
      const params = new URLSearchParams();
      if (priceListId) params.set('price_list_id', priceListId);
      if (!priceListId && branchFilter !== '__all__') params.set('branch', branchFilter);
      if (!priceListId && formatFilter !== '__all__') params.set('format_code', formatFilter);
      const res = await fetch(`/api/price-list-analytics?${params.toString()}`);
      const text = await res.text();
      const data = parseJson(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить аналитику');
      setPayload(data);
    } catch (e: any) {
      setPayload(null);
      setError(e?.message || 'Ошибка загрузки аналитики');
    } finally {
      setIsLoading(false);
    }
  };

  const openZone = async (zone: string) => {
    if (!payload?.priceList?.number) return;
    setZoneDrilldown(zone);
    const params = new URLSearchParams({ zone, page: '1', page_size: '100' });
    if (search.trim()) params.set('q', search.trim());
    const res = await fetch(`/api/generated-price-lists/${encodeURIComponent(payload.priceList.number)}/items?${params.toString()}`);
    const text = await res.text();
    const data = parseJson(text);
    setItems(res.ok && Array.isArray(data?.items) ? data.items : []);
  };

  const loadResultItems = async () => {
    if (!payload?.priceList?.number) {
      setResultItems([]);
      return;
    }
    const params = new URLSearchParams({ page: String(resultPage), page_size: '500', zone: '__all__', sort: 'name' });
    if (itemSearch.trim()) params.set('q', itemSearch.trim());
    const res = await fetch(`/api/generated-price-lists/${encodeURIComponent(payload.priceList.number)}/items?${params.toString()}`);
    const text = await res.text();
    const data = parseJson(text);
    setResultItems(res.ok && Array.isArray(data?.items) ? data.items : []);
    setResultTotal(res.ok ? Number(data?.total || 0) : 0);
  };

  useEffect(() => {
    void loadPriceLists();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [branchFilter, formatFilter]);

  useEffect(() => {
    void loadAnalytics();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [priceListId, branchFilter, formatFilter, activationDateFilter]);

  useEffect(() => {
    setResultPage(1);
  }, [priceListId]);

  useEffect(() => {
    if (!activationDateFilter) return;
    const first = selectablePriceLists[0]?.number || '';
    setPriceListId(first);
  }, [activationDateFilter, selectablePriceLists]);

  useEffect(() => {
    void loadResultItems();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload?.priceList?.number, resultPage]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadResultItems(), 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemSearch]);

  useEffect(() => {
    if (!initialPriceListNumber || initialPriceListNumber === lastInitialOpened) return;
    setLastInitialOpened(initialPriceListNumber);
    setPriceListId(initialPriceListNumber);
  }, [initialPriceListNumber, lastInitialOpened]);

  const exportAnalytics = (fmtType: 'csv' | 'xlsx') => {
    const params = new URLSearchParams();
    if (payload?.priceList?.number) params.set('price_list_id', payload.priceList.number);
    window.location.href = `/api/price-list-analytics/export.${fmtType}?${params.toString()}`;
  };

  const summary = payload?.summary || {};
  const repricing = payload?.repricing || {};
  const sourceOptions = useMemo(
    () => Array.from(new Set(resultItems.map((row) => row.priceSource).filter(Boolean))).sort((a, b) => a.localeCompare(b, 'ru')),
    [resultItems]
  );
  const ruleOptions = useMemo(
    () => Array.from(new Set(resultItems.map((row) => row.pricingRule).filter(Boolean))).sort((a, b) => a.localeCompare(b, 'ru')),
    [resultItems]
  );
  const filteredItems = useMemo(() => {
    return resultItems.filter((row) => {
      const change = changePercent(row);
      if (onlyChanged && (change === null || Math.abs(change) <= 0.01)) return false;
      if (onlyNoCompetitors && row.bestCompetitorPrice !== null) return false;
      if (onlyPercentile && !row.usedPercentile) return false;
      if (onlyUniversalLists && !hasUniversalList(row)) return false;
      if (directionFilter === 'up' && (change === null || change <= 0.01)) return false;
      if (directionFilter === 'down' && (change === null || change >= -0.01)) return false;
      if (zoneFilter !== '__all__' && row.zone !== zoneFilter) return false;
      if (ruleFilter !== '__all__' && row.pricingRule !== ruleFilter) return false;
      if (sourceFilter !== '__all__' && row.priceSource !== sourceFilter) return false;
      return true;
    });
  }, [directionFilter, onlyChanged, onlyNoCompetitors, onlyPercentile, onlyUniversalLists, resultItems, ruleFilter, sourceFilter, zoneFilter]);

  return (
    <div className="business-workspace analytics-workspace">
      <div className="business-toolbar">
        <div>
          <h2>Итоги ЦО</h2>
          <p>Результаты переоценки: изменения цен, зоны, источники, персентили, универсальные списки и причины расчета.</p>
        </div>
        <div className="business-actions">
          <Button variant="outline" onClick={() => exportAnalytics('csv')} disabled={!payload}><Download className="h-4 w-4 mr-2" />CSV</Button>
          <Button variant="outline" onClick={() => exportAnalytics('xlsx')} disabled={!payload}><Download className="h-4 w-4 mr-2" />XLSX</Button>
          <Button onClick={() => onNavigate?.('pricelists')}><ExternalLink className="h-4 w-4 mr-2" />Открыть прайс</Button>
        </div>
      </div>

      <div className="business-filters">
        <Select value={branchFilter} onValueChange={(value) => { setBranchFilter(value); setPriceListId(''); }}>
          <SelectTrigger><SelectValue placeholder="Филиал" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все филиалы</SelectItem>
            {branchOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={formatFilter} onValueChange={(value) => { setFormatFilter(value); setPriceListId(''); }}>
          <SelectTrigger><SelectValue placeholder="ЦФ" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все ЦФ</SelectItem>
            {formatOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
          </SelectContent>
        </Select>
        <Input
          type="date"
          value={activationDateFilter}
          onChange={(e) => {
            setActivationDateFilter(e.target.value);
            setPriceListId('');
          }}
          title="Дата действия"
        />
        <Select value={priceListId || '__latest__'} onValueChange={(value) => setPriceListId(value === '__latest__' ? '' : value)}>
          <SelectTrigger><SelectValue placeholder="Прайс-лист" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__latest__">Последний по фильтру</SelectItem>
            {selectablePriceLists.map((row) => <SelectItem key={row.number} value={row.number}>{row.number} · {row.activationDate || row.date}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>

      {error && <div className="business-alert bad">{error}</div>}
      {!payload && !error && <div className="empty-state">Выберите прайс-лист для аналитики</div>}

      {payload && (
        <>
          <section className="business-panel">
            <div className="panel-head">
              <h3>{payload.priceList.number}</h3>
              <span>{payload.priceList.branch} · {payload.priceList.format} · {payload.priceList.date}</span>
            </div>
            <div className="metric-grid analytics">
              <div><span>Всего товаров</span><strong>{fmt(summary.skuTotal)}</strong></div>
              <div><span>Цена изменилась</span><strong>{fmt(repricing.changedCount)}</strong></div>
              <div><span>Повышение цены</span><strong>{fmt(repricing.increasedCount)}</strong></div>
              <div><span>Снижение цены</span><strong>{fmt(repricing.decreasedCount)}</strong></div>
              <div><span>Без изменений</span><strong>{fmt(repricing.unchangedCount)}</strong></div>
              <div><span>Без конкурентной цены</span><strong>{fmt(summary.withoutCompetitors)}</strong></div>
              <div><span>Применена логика без конкурентов</span><strong>{fmt(summary.noCompetitorRuleApplied)}</strong></div>
              <div><span>Percentile</span><strong>{fmt(summary.percentileUsage)}</strong></div>
              <div><span>Substitute</span><strong>{fmt(summary.substituteUsage || repricing.withSubstitute || 0)}</strong></div>
              <div><span>Универсальные списки</span><strong>{fmt(repricing.withUniversalLists)}</strong></div>
              <div><span>Левое плечо: ниже конкурента</span><strong>{fmt(summary.leftZone)}</strong></div>
              <div><span>ЗЛ</span><strong>{fmt(summary.optimalZone)}</strong></div>
              <div><span>ПП</span><strong>{fmt(summary.rightZone)}</strong></div>
            </div>
            <p className="text-sm text-gray-600 mt-3">Зона рассчитывается только при наличии первой цены конкурента. Для товаров без конкурентной цены отображается «—».</p>
          </section>

          <div className="business-grid two-columns">
            <section className="business-panel">
              <div className="panel-head"><h3>Зоны</h3><span>Левое плечо / ЗЛ / ПП</span></div>
              <div className="zone-cards">
                {payload.zones.map((zone) => (
                  <button key={zone.code} className={`zone-card ${zone.code}`} onClick={() => void openZone(zone.code)}>
                    <span>{zone.label}</span>
                    <strong>{fmt(zone.count)}</strong>
                    <small>{fmt(zone.percent)}% · изменение {fmt(zone.change)}</small>
                  </button>
                ))}
              </div>
              <div className="metric-grid compact mt-4">
                {Object.entries(payload.rightZoneReasons || {}).map(([key, value]) => (
                  <div key={key}>
                    <span>{rightReasonLabels[key] || key}</span>
                    <strong>{fmt(value)}</strong>
                  </div>
                ))}
              </div>
              <div className="chart-box">
                <ResponsiveContainer width="100%" height={260}>
                  <PieChart>
                    <Pie data={payload.charts.zoneDistribution} dataKey="count" nameKey="label" innerRadius={58} outerRadius={95}>
                      {payload.charts.zoneDistribution.map((entry) => <Cell key={entry.code} fill={zoneColors[entry.code] || '#64748b'} />)}
                    </Pie>
                    <Tooltip />
                  </PieChart>
                </ResponsiveContainer>
              </div>
            </section>

            <section className="business-panel">
              <div className="panel-head"><h3>Итоги переоценки</h3><span>по сравнению с базовой ценой</span></div>
              <div className="metric-grid compact">
                <div><span>Средняя наценка</span><strong>{fmt(summary.averageMarkup)}%</strong></div>
                <div><span>Средний прогиб</span><strong>{fmt(repricing.averageBendPercent)}%</strong></div>
                <div><span>% изменений</span><strong>{pct(repricing.changedCount, summary.skuTotal)}%</strong></div>
                <div><span>% без конкурентной цены</span><strong>{pct(summary.withoutCompetitors, summary.skuTotal)}%</strong></div>
                <div><span>% percentile</span><strong>{pct(summary.percentileUsage, summary.skuTotal)}%</strong></div>
                <div><span>% списков</span><strong>{pct(repricing.withUniversalLists, summary.skuTotal)}%</strong></div>
                <div><span>Max increase</span><strong>{fmt(repricing.maxIncrease)}%</strong></div>
                <div><span>Max decrease</span><strong>{fmt(repricing.maxDecrease)}%</strong></div>
              </div>
            </section>
          </div>

          <div className="business-grid two-columns">
            <section className="business-panel">
              <div className="panel-head"><h3>Гистограмма наценки</h3></div>
              <div className="chart-box">
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart data={payload.charts.markupHistogram}>
                    <XAxis dataKey="label" />
                    <YAxis />
                    <Tooltip />
                    <Bar dataKey="count" fill="#2563eb" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </section>

            <section className="business-panel">
              <div className="panel-head"><h3>Использование конкурентов</h3></div>
              <div className="chart-box">
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart data={payload.charts.competitorUsage} layout="vertical" margin={{ left: 80 }}>
                    <XAxis type="number" />
                    <YAxis dataKey="source" type="category" width={120} />
                    <Tooltip />
                    <Bar dataKey="count" fill="#0f766e" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </section>
          </div>

          <div className="business-grid two-columns">
            <section className="business-panel">
              <div className="panel-head"><h3>Percentile / без конкурентной цены</h3></div>
              <div className="split-mini-charts">
                <ResponsiveContainer width="50%" height={220}>
                  <BarChart data={payload.charts.percentileUsage}>
                    <XAxis dataKey="label" /><YAxis /><Tooltip /><Bar dataKey="count" fill="#7c3aed" />
                  </BarChart>
                </ResponsiveContainer>
                <ResponsiveContainer width="50%" height={220}>
                  <BarChart data={payload.charts.noCompetitors}>
                    <XAxis dataKey="label" /><YAxis /><Tooltip /><Bar dataKey="count" fill="#c2410c" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </section>

            <section className="business-panel">
              <div className="panel-head"><h3>Top changed products</h3></div>
              <div className="table-scroll compact">
                <table className="business-table">
                  <thead><tr><th>SKU</th><th>Наименование</th><th>Старая</th><th>Новая</th><th>Изм. %</th><th>Зона</th></tr></thead>
                  <tbody>
                    {payload.charts.topChangedProducts.map((row) => (
                      <tr key={row.sku}>
                        <td>{row.sku}</td>
                        <td>{row.name}</td>
                        <td>{fmt(row.oldPrice)}</td>
                        <td>{fmt(row.newPrice)}</td>
                        <td>{fmt(row.changePercent)}%</td>
                        <td><span className={`zone-badge ${row.zone}`}>{row.zone}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          </div>

          <section className="business-panel">
            <div className="panel-head">
              <h3>Таблица результатов</h3>
              <span>{filteredItems.length} из {resultItems.length} строк загруженной детализации</span>
            </div>
            <div className="business-filters">
              <div className="business-search">
                <Search className="h-4 w-4" />
                <Input placeholder="Товар или SKU" value={itemSearch} onChange={(e) => { setItemSearch(e.target.value); setResultPage(1); }} />
              </div>
              <Select value={directionFilter} onValueChange={setDirectionFilter}>
                <SelectTrigger><SelectValue placeholder="Изменение" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all__">Все изменения</SelectItem>
                  <SelectItem value="up">Только повышение</SelectItem>
                  <SelectItem value="down">Только снижение</SelectItem>
                </SelectContent>
              </Select>
              <Select value={zoneFilter} onValueChange={setZoneFilter}>
                <SelectTrigger><SelectValue placeholder="Зона" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all__">Все зоны</SelectItem>
                  {['left', 'optimal', 'right'].map((zone) => <SelectItem key={zone} value={zone}>{zoneLabel(zone)}</SelectItem>)}
                </SelectContent>
              </Select>
              <Select value={ruleFilter} onValueChange={setRuleFilter}>
                <SelectTrigger><SelectValue placeholder="Правило" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all__">Все правила</SelectItem>
                  {ruleOptions.map((rule) => <SelectItem key={rule} value={rule}>{rule}</SelectItem>)}
                </SelectContent>
              </Select>
              <Select value={sourceFilter} onValueChange={setSourceFilter}>
                <SelectTrigger><SelectValue placeholder="Источник" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all__">Все источники</SelectItem>
                  {sourceOptions.map((source) => <SelectItem key={source} value={source}>{source}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant={onlyChanged ? 'default' : 'outline'} size="sm" onClick={() => setOnlyChanged((value) => !value)}>Только измененные</Button>
              <Button variant={onlyNoCompetitors ? 'default' : 'outline'} size="sm" onClick={() => setOnlyNoCompetitors((value) => !value)}>Без конкурентной цены</Button>
              <Button variant={onlyPercentile ? 'default' : 'outline'} size="sm" onClick={() => setOnlyPercentile((value) => !value)}>Percentile</Button>
              <Button variant={onlyUniversalLists ? 'default' : 'outline'} size="sm" onClick={() => setOnlyUniversalLists((value) => !value)}>Универсальные списки</Button>
            </div>
            <div className="table-scroll compact">
              <table className="business-table">
                <thead>
                  <tr>
                    <th>Товар</th>
                    <th>Старая цена</th>
                    <th>Новая цена</th>
                    <th>Изм. %</th>
                    <th>Competitor price</th>
                    <th>Pricing reason</th>
                    <th>Zone</th>
                    <th>Percentile</th>
                    <th>Source</th>
                    <th>Applied rule</th>
                    <th>Universal list effect</th>
                    <th>Без конкурентной цены</th>
                    <th>Substitute</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredItems.slice(0, 250).map((row) => {
                    const change = changePercent(row);
                    const noCompetitors = row.bestCompetitorPrice === null;
                    return (
                      <tr key={row.sku} className={rowChangeClass(row)} onClick={() => setSelectedItem(row)}>
                        <td><strong>{row.sku}</strong><div>{row.name}</div><small>{row.manufacturer || ''}</small></td>
                        <td>{fmt(row.basePrice)}</td>
                        <td>{fmt(row.finalPrice)}</td>
                        <td>{change === null ? '—' : `${fmt(change)}%`}</td>
                        <td>{row.bestCompetitorPrice === null ? '—' : fmt(row.bestCompetitorPrice)}</td>
                        <td className="max-w-80">{row.pricingReason || '—'}</td>
                        <td>{row.zone ? <span className={`zone-badge ${row.zone}`}>{zoneLabel(row.zone)}</span> : '—'}</td>
                        <td>{row.usedPercentile ? row.percentileSource || 'Да' : '—'}</td>
                        <td>{row.priceSource || '—'}</td>
                        <td>{row.pricingRule || '—'}</td>
                        <td>{hasUniversalList(row) ? 'Да' : '—'}</td>
                        <td>{noCompetitors ? <span className="status-pill warn">Нет</span> : '—'}</td>
                        <td>{row.usedSubstitute ? 'Да' : '—'}</td>
                      </tr>
                    );
                  })}
                  {!filteredItems.length && <tr><td colSpan={13} className="empty-cell">Нет строк по выбранным фильтрам</td></tr>}
                </tbody>
              </table>
            </div>
            <div className="generated-pagination">
              <Button variant="outline" size="sm" onClick={() => setResultPage((page) => Math.max(1, page - 1))} disabled={resultPage <= 1}>Назад</Button>
              <span>Страница {resultPage} · показано {resultItems.length} из {fmt(resultTotal)}</span>
              <Button variant="outline" size="sm" onClick={() => setResultPage((page) => page + 1)} disabled={resultPage * 500 >= resultTotal}>Далее</Button>
            </div>
          </section>
        </>
      )}

      <Dialog open={Boolean(selectedItem)} onOpenChange={(open) => !open && setSelectedItem(null)}>
        <DialogContent className="max-w-3xl">
          <DialogHeader><DialogTitle>Объяснение цены</DialogTitle></DialogHeader>
          {selectedItem ? (
            <div className="space-y-4">
              <div>
                <h3 className="text-lg font-semibold text-gray-900">{selectedItem.sku} · {selectedItem.name}</h3>
                <p className="text-sm text-gray-600">{selectedItem.manufacturer || 'Производитель не указан'}</p>
              </div>
              <div className="metric-grid compact">
                <div><span>Старая цена</span><strong>{fmt(selectedItem.basePrice)}</strong></div>
                <div><span>Новая цена</span><strong>{fmt(selectedItem.finalPrice)}</strong></div>
                <div><span>Изменение</span><strong>{changePercent(selectedItem) === null ? '—' : `${fmt(changePercent(selectedItem))}%`}</strong></div>
                <div><span>Зона</span><strong>{zoneLabel(selectedItem.zone)}</strong></div>
              </div>
              <div className="rounded-md border border-gray-200 p-3 text-sm">
                <div><strong>Правило:</strong> {selectedItem.pricingRule || '—'}</div>
                <div><strong>Источник:</strong> {selectedItem.priceSource || '—'}</div>
                <div><strong>Percentile:</strong> {selectedItem.usedPercentile ? selectedItem.percentileSource || 'использован' : 'не использовался'}</div>
                <div><strong>Универсальные списки:</strong> {hasUniversalList(selectedItem) ? `сработали (${selectedItem.appliedListIds})` : 'не применялись'}</div>
                <div><strong>Substitute:</strong> {selectedItem.usedSubstitute ? 'использован' : 'не использовался'}</div>
              </div>
              <div className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900">
                <strong className="block mb-1">Лог #1 — причина расчёта цены</strong>
                {selectedItem.pricingCalculationLog || selectedItem.pricingReason || 'Причина расчета не сохранена.'}
              </div>
              {selectedItem.listOverrideLog ? (
                <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
                  <strong className="block mb-1">Лог #2 — Lists Management</strong>
                  <div>Позиция найдена в списке:</div>
                  <div>Название: {selectedItem.listOverrideLog.listName || '—'}</div>
                  <div>Код: {selectedItem.listOverrideLog.listCode || '—'}</div>
                  <div>Тип: {selectedItem.listOverrideLog.listType}</div>
                  <div>Значение: {selectedItem.listOverrideLog.displayValue}</div>
                  <div className="mt-1 font-medium">{selectedItem.listOverrideLog.action}</div>
                  {selectedItem.listOverrideLog.ambiguous ? <div className="mt-1 font-semibold">Статус: правило требует бизнес-подтверждения.</div> : null}
                </div>
              ) : null}
              {selectedItem.log?.length ? (
                <div className="table-scroll compact">
                  <table className="business-table">
                    <thead><tr><th>Шаг</th><th>Значение</th><th>Описание</th></tr></thead>
                    <tbody>{selectedItem.log.map((row, index) => <tr key={`${row.label}-${index}`}><td>{row.label}</td><td>{String(row.value ?? '—')}</td><td>{row.description}</td></tr>)}</tbody>
                  </table>
                </div>
              ) : null}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(zoneDrilldown)} onOpenChange={(open) => !open && setZoneDrilldown(null)}>
        <DialogContent className="max-w-5xl">
          <DialogHeader><DialogTitle>Drilldown зоны {zoneDrilldown}</DialogTitle></DialogHeader>
          <div className="business-search full">
            <Search className="h-4 w-4" />
            <Input placeholder="Поиск товара в зоне" value={search} onChange={(e) => setSearch(e.target.value)} />
            <Button variant="outline" onClick={() => zoneDrilldown && void openZone(zoneDrilldown)}>Найти</Button>
          </div>
          <div className="table-scroll compact">
            <table className="business-table">
              <thead><tr><th>SKU</th><th>Наименование</th><th>Финальная цена</th><th>Наценка</th><th>Источник</th><th>Причина</th></tr></thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.sku}>
                    <td>{row.sku}</td>
                    <td>{row.name}</td>
                    <td>{fmt(row.finalPrice)}</td>
                    <td>{fmt(row.markupPercent)}%</td>
                    <td>{row.priceSource || '—'}</td>
                    <td>{row.pricingReason || '—'}</td>
                  </tr>
                ))}
                {!items.length && <tr><td colSpan={6} className="empty-cell">Нет товаров для выбранной зоны</td></tr>}
              </tbody>
            </table>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
