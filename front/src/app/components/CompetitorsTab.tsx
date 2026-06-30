import { useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import { ChevronDown, Eye, FileDown, FileUp, Link2, RefreshCw, Search, Trash2, XCircle } from 'lucide-react';
import { Button } from './ui/button';
import { Checkbox } from './ui/checkbox';
import { Input } from './ui/input';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import { ScrollArea } from './ui/scroll-area';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';

type Platform = 'provisor' | 'vidman';
type MappingStatus = 'all' | 'mapped' | 'unmapped' | 'rejected' | 'no_candidates';

type CompetitorSource = {
  id: number;
  sourceName: string;
  sourceType: string;
  branchName?: string;
  competitorName?: string;
  accountLogin?: string;
  accountId?: string;
  priceDate?: string;
  itemsCount: number;
  status?: string;
  updatedAt?: string;
  sourceUpdatedAt?: string;
  refreshStatus?: string;
  refreshMessage?: string;
  lastCheckedAt?: string;
  lastSuccessAt?: string;
  lastUpdatedAt?: string;
  errorSummary?: string;
  visibleForFormatBranch?: boolean;
  branchMatchReason?: string;
  branchMismatchReason?: string;
  filialId?: string;
  name?: string;
};

type ProvisorAccount = {
  id: number;
  login: string;
  status?: string;
  price_list_count?: number;
  last_success_at?: string | null;
};

type PriceListItem = {
  provisor_id: number | null;
  provisor_goods_id?: number | null;
  filial_id: number | null;
  name: string;
  manufacturer?: string | null;
  distributor_goods_name: string;
  distributor_goods_id: string;
  distributor_price: number | null;
  stock: number | null;
  match_type?: string;
  matched_sku?: string;
};

type PercentileSource = {
  id: string;
  name: string;
  region: string;
  competitor: string;
  scope?: string;
  percentile: number;
  skuCount: number;
  sourceCount: number;
  generatedAt: string;
};

type PercentileBrowserRow = {
  productId: number;
  sku: string;
  productName: string;
  manufacturer: string;
  globalRating: number | null;
  localRating: number | null;
  percentiles: Record<string, number | null>;
  branchPrices: Record<string, number | null>;
  competitorCount: number;
  status: string;
  hasPercentile: boolean;
  hasCompetitors: boolean;
};

type PercentileGroup = {
  id: string;
  region: string;
  competitor: string;
  scope?: string;
  name: string;
};

type PercentilePriceColumn = {
  id: number;
  label: string;
};

type PercentileSummary = {
  totalProducts: number;
  productsWithPercentile: number;
  productsWithoutPercentile: number;
  productsWithCompetitors: number;
  productsWithoutCompetitors: number;
  coveragePercent: number;
};

type PercentilePayload = {
  items: PercentileBrowserRow[];
  summary: PercentileSummary;
  total: number;
  page: number;
  pageSize: number;
  pageCount: number;
  percentiles: number[];
  groups: PercentileGroup[];
  selectedRegion: string;
  selectedCompetitor: string;
  priceColumns: PercentilePriceColumn[];
};

type CodeMappingMetric = {
  platform: Platform;
  total: number;
  mapped: number;
  unmapped: number;
  rejected: number;
  noCandidates?: number;
  coveragePercent: number;
  mappingCoveragePercent?: number;
  generatedPricingCoverage?: {
    priceListId: number | null;
    priceListNumber: string;
    total: number;
    withCompetitors: number;
    withoutCompetitors: number;
    coveragePercent: number;
  };
};

type CodeMappingCandidate = {
  itemId?: number | null;
  priceListId?: number | null;
  priceListName?: string;
  platform: Platform;
  matchType?: string;
  matchedSku?: string;
  sourcePrice?: number | null;
  priceDate?: string;
  confidence?: number | null;
  sourceExternalKey?: string | null;
  sourceMatchKey: string;
  sourceName: string;
  sourceManufacturer?: string;
  sourceDosageForm?: string;
  sourceNormalizedName?: string;
};

type CodeMappingRow = CodeMappingCandidate & {
  productId?: number | null;
  status: Exclude<MappingStatus, 'all'>;
  mappingStatus?: Exclude<MappingStatus, 'all'> | 'no_candidates';
  mappingId?: number | null;
  candidatesCount?: number;
  candidates?: CodeMappingCandidate[];
  bestCandidate?: CodeMappingCandidate | null;
  ourProductId?: number | null;
  ourSku?: string;
  ourName?: string;
  ourManufacturer?: string;
};

type MappingPagination = {
  page: number;
  pageSize: number;
  total: number;
  pageCount: number;
};

type ProductSearchRow = {
  productId: number;
  sku: string;
  name: string;
  manufacturer: string;
};

type JobState = {
  id: string;
  status: string;
  progress: number;
  message: string;
  result?: any;
  error?: string;
};

type ProvisorDiagnostics = {
  formatCode: string;
  branch: string;
  visibility: {
    totalProvisorGlobalPool: number;
    visibleForFormatBranch: number;
    hiddenByBranch: number;
    hiddenZeroItems: number;
    hiddenByDedupe: number;
    selectedTotal: number;
    activeTotal: number;
  };
  coverage: {
    totalProducts: number;
    productsWithProvisorGoodsId: number;
    productsWithoutProvisorGoodsId: number;
    activeProvisorPriceLists: Array<{
      id: number;
      sourceKey: string;
      accountLogin: string;
      branchName: string;
      competitorName: string;
      itemsCount: number;
    }>;
    matchTypeDistribution: Array<{ matchType: string; rows: number }>;
    unmatchedRowsWithGoodsId: number;
    unmatchedRowsWhoseGoodsIdExistsInProduct: number;
  };
  referenceFilialCoverage: {
    referenceFilialId: number;
    rowsTotal: number;
    rowsWithGoodsId: number;
    distributorGoodsIdMatchedProducts: number;
    skuNotFound: number;
    topSkuNotFoundExamples: Array<{
      sourceKey: string;
      accountLogin: string;
      goodsId: number | null;
      distributorGoodsId: string;
      name: string;
    }>;
  };
};

type Props = {
  formatCode: string;
};

const parseJsonOrNull = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const fmtNumber = (value: number | null | undefined) => {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return Number(value).toLocaleString('ru-RU');
};

const platformLabel = (platform: Platform) => (platform === 'provisor' ? 'Provisor' : 'Vidman');
const PRICE_LIST_FRESHNESS_MS = 2 * 60 * 60 * 1000;

const parseTime = (value?: string) => {
  const time = value ? Date.parse(value) : NaN;
  return Number.isFinite(time) ? time : null;
};

const refreshStatusLabel = (row: CompetitorSource) => {
  const raw = String(row.refreshStatus || row.status || '').split(';', 1)[0].trim().toLowerCase();
  if (raw === 'updated' || raw === 'ok') return 'Updated with new data';
  if (raw === 'checked_unchanged') return 'Checked, unchanged';
  if (raw === 'success_zero_items') return 'Checked, zero response kept';
  if (raw === 'timeout') return 'Timeout';
  if (raw === 'auth_error') return 'Auth error';
  const lastSuccess = parseTime(row.lastSuccessAt || row.updatedAt || row.lastUpdatedAt);
  if (!lastSuccess || Date.now() - lastSuccess > PRICE_LIST_FRESHNESS_MS) return 'Stale / not checked recently';
  return raw || 'ok';
};

const refreshStatusClass = (row: CompetitorSource) => {
  const label = refreshStatusLabel(row);
  if (label === 'Updated with new data' || label === 'Checked, unchanged' || label === 'Checked, zero response kept') return 'ok';
  if (label === 'Timeout' || label === 'Stale / not checked recently') return 'warn';
  if (label === 'Auth error') return 'bad';
  return '';
};

const statusLabel = (status: MappingStatus) => {
  if (status === 'mapped') return 'Manual/catalog mappings';
  if (status === 'unmapped') return 'Candidates needing manual review';
  if (status === 'rejected') return 'Отклоненные';
  if (status === 'no_candidates') return 'No mapping candidates';
  return 'Все';
};

const rowStatusLabel = (status: CodeMappingRow['status']) => {
  if (status === 'mapped') return 'Manual/catalog mapping';
  if (status === 'rejected') return 'Отклонен';
  return 'Needs manual review';
};

const catalogStatusLabel = (row: CodeMappingRow) => {
  if (row.mappingStatus === 'no_candidates') return 'No mapping candidates';
  return rowStatusLabel(row.status);
};

const catalogStatusClass = (row: CodeMappingRow) => {
  if (row.mappingStatus === 'no_candidates') return '';
  return statusPillClass(row.status);
};

const statusPillClass = (status: CodeMappingRow['status']) => {
  if (status === 'mapped') return 'ok';
  if (status === 'rejected') return 'bad';
  return 'warn';
};

const candidateQueryForRow = (row: CodeMappingRow) =>
  [row.ourName, row.ourManufacturer].filter(Boolean).join(' ').trim();

function PercentileBrowser({
  rows,
  summary,
  total,
  page,
  pageCount,
  groups,
  selectedRegion,
  selectedCompetitor,
  priceColumns,
  percentileNumbers,
  search,
  percentileFilter,
  competitorFilter,
  sort,
  direction,
  onSearch,
  onPercentileFilter,
  onCompetitorFilter,
  onSort,
  onDirection,
  onRegion,
  onCompetitor,
  onPage,
  onExport,
}: {
  rows: PercentileBrowserRow[];
  summary: PercentileSummary;
  total: number;
  page: number;
  pageCount: number;
  groups: PercentileGroup[];
  selectedRegion: string;
  selectedCompetitor: string;
  priceColumns: PercentilePriceColumn[];
  percentileNumbers: number[];
  search: string;
  percentileFilter: string;
  competitorFilter: string;
  sort: string;
  direction: 'asc' | 'desc';
  onSearch: (value: string) => void;
  onPercentileFilter: (value: string) => void;
  onCompetitorFilter: (value: string) => void;
  onSort: (value: string) => void;
  onDirection: (value: 'asc' | 'desc') => void;
  onRegion: (value: string) => void;
  onCompetitor: (value: string) => void;
  onPage: (value: number | ((current: number) => number)) => void;
  onExport: (fmt: 'csv' | 'xlsx') => void;
}) {
  const cards = [
    ['Всего товаров', summary.totalProducts],
    ['Персентиль рассчитан', summary.productsWithPercentile],
    ['Без данных', summary.productsWithoutPercentile],
    ['С конкурентами', summary.productsWithCompetitors],
    ['Без конкурентов', summary.productsWithoutCompetitors],
    ['Покрытие', `${fmtNumber(summary.coveragePercent)}%`],
  ];
  const regions = Array.from(new Set(groups.map((group) => group.region).filter(Boolean)));
  const competitors = Array.from(
    new Set(groups.filter((group) => !selectedRegion || group.region === selectedRegion).map((group) => group.competitor).filter(Boolean))
  );

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {cards.map(([label, value]) => (
          <div key={String(label)} className="admin-card p-4">
            <div className="text-xs font-medium uppercase tracking-wide text-gray-500">{label}</div>
            <div className="mt-2 text-2xl font-semibold tabular-nums text-gray-900">{value}</div>
          </div>
        ))}
      </div>

      <div className="admin-card p-4">
        <div className="mb-3 grid grid-cols-1 gap-3 md:grid-cols-2">
          <Select value={selectedRegion || '__none__'} onValueChange={(value) => onRegion(value === '__none__' ? '' : value)}>
            <SelectTrigger><SelectValue placeholder="Регион" /></SelectTrigger>
            <SelectContent>
              {regions.length ? regions.map((region) => (
                <SelectItem key={region} value={region}>{region}</SelectItem>
              )) : <SelectItem value="__none__">Нет регионов</SelectItem>}
            </SelectContent>
          </Select>
          <Select value={selectedCompetitor || '__none__'} onValueChange={(value) => onCompetitor(value === '__none__' ? '' : value)}>
            <SelectTrigger><SelectValue placeholder="Конкурент" /></SelectTrigger>
            <SelectContent>
              {competitors.length ? competitors.map((competitor) => (
                <SelectItem key={competitor} value={competitor}>{competitor}</SelectItem>
              )) : <SelectItem value="__none__">Нет конкурентов</SelectItem>}
            </SelectContent>
          </Select>
        </div>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-[minmax(220px,1fr)_180px_180px_180px_140px_auto]">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-gray-400" />
            <Input value={search} onChange={(event) => onSearch(event.target.value)} placeholder="SKU или название" className="pl-9" />
          </div>
          <Select value={percentileFilter} onValueChange={onPercentileFilter}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все percentile</SelectItem>
              <SelectItem value="has_percentile">Has percentile</SelectItem>
              <SelectItem value="no_percentile">No percentile</SelectItem>
            </SelectContent>
          </Select>
          <Select value={competitorFilter} onValueChange={onCompetitorFilter}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все конкуренты</SelectItem>
              <SelectItem value="has_competitors">Has competitors</SelectItem>
              <SelectItem value="no_competitors">No competitors</SelectItem>
            </SelectContent>
          </Select>
          <Select value={sort} onValueChange={onSort}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="sku">SKU</SelectItem>
              <SelectItem value="name">Название</SelectItem>
              <SelectItem value="percentile">Percentile</SelectItem>
              <SelectItem value="competitor_count">Competitor count</SelectItem>
              <SelectItem value="status">Status</SelectItem>
            </SelectContent>
          </Select>
          <Select value={direction} onValueChange={(value) => onDirection(value as 'asc' | 'desc')}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="asc">Asc</SelectItem>
              <SelectItem value="desc">Desc</SelectItem>
            </SelectContent>
          </Select>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => onExport('csv')}>
              <FileDown className="mr-2 h-4 w-4" />
              CSV
            </Button>
            <Button variant="outline" onClick={() => onExport('xlsx')}>
              <FileDown className="mr-2 h-4 w-4" />
              XLSX
            </Button>
          </div>
        </div>
      </div>

      <div className="admin-table-card">
        <div className="flex items-center justify-between border-b border-gray-100 px-4 py-3 text-sm text-gray-600">
          <span>Показано {fmtNumber(rows.length)} из {fmtNumber(total)} · {selectedRegion || '—'} / {selectedCompetitor || '—'}</span>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onPage((current) => Math.max(1, current - 1))}>Назад</Button>
            <span className="tabular-nums">{page} / {Math.max(1, pageCount)}</span>
            <Button variant="outline" size="sm" disabled={!pageCount || page >= pageCount} onClick={() => onPage((current) => current + 1)}>Вперед</Button>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="admin-table">
            <thead>
              <tr>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">SKU</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Product name</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Manufacturer</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Global rating</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Local rating</th>
                {priceColumns.map((column) => (
                  <th key={`price-${column.id}`} className="px-4 py-3 text-left text-sm font-medium text-gray-700">{column.label}</th>
                ))}
                {percentileNumbers.map((percentile) => (
                  <th key={`pct-${percentile}`} className="px-4 py-3 text-left text-sm font-medium text-gray-700">P{percentile}</th>
                ))}
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Competitor count</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.length ? rows.map((row) => (
                <tr key={row.productId}>
                  <td className="px-4 py-3 text-sm font-medium text-gray-900 tabular-nums">{row.sku}</td>
                  <td className="px-4 py-3 text-sm text-gray-900">{row.productName}</td>
                  <td className="px-4 py-3 text-sm text-gray-700">{row.manufacturer || '—'}</td>
                  <td className="px-4 py-3 text-sm text-gray-700 tabular-nums">{fmtNumber(row.globalRating)}</td>
                  <td className="px-4 py-3 text-sm text-gray-700 tabular-nums">{fmtNumber(row.localRating)}</td>
                  {priceColumns.map((column) => (
                    <td key={`price-${row.productId}-${column.id}`} className="px-4 py-3 text-sm text-gray-700 tabular-nums">{fmtNumber(row.branchPrices?.[String(column.id)])}</td>
                  ))}
                  {percentileNumbers.map((percentile) => (
                    <td key={`pct-${row.productId}-${percentile}`} className="px-4 py-3 text-sm text-gray-900 tabular-nums">{fmtNumber(row.percentiles?.[String(percentile)])}</td>
                  ))}
                  <td className="px-4 py-3 text-sm text-gray-700 tabular-nums">{fmtNumber(row.competitorCount)}</td>
                  <td className="px-4 py-3 text-sm"><span className={`status-pill ${row.hasPercentile ? 'ok' : 'warn'}`}>{row.status}</span></td>
                </tr>
              )) : (
                <tr>
                  <td colSpan={8 + priceColumns.length + percentileNumbers.length} className="px-4 py-8 text-center text-sm text-gray-500">Нет строк percentile</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export function CompetitorsTab({ formatCode }: Props) {
  const [sources, setSources] = useState<CompetitorSource[]>([]);
  const [sourceSearch, setSourceSearch] = useState('');
  const [sourceFilter, setSourceFilter] = useState('__all__');
  const [opened, setOpened] = useState<{ meta: any; items: PriceListItem[] } | null>(null);
  const [activeListId, setActiveListId] = useState<number | null>(null);
  const [excelFile, setExcelFile] = useState<File | null>(null);
  const [percentiles, setPercentiles] = useState<PercentileSource[]>([]);
  const [percentileRows, setPercentileRows] = useState<PercentileBrowserRow[]>([]);
  const [percentileSummary, setPercentileSummary] = useState<PercentileSummary>({
    totalProducts: 0,
    productsWithPercentile: 0,
    productsWithoutPercentile: 0,
    productsWithCompetitors: 0,
    productsWithoutCompetitors: 0,
    coveragePercent: 0,
  });
  const [percentileTotal, setPercentileTotal] = useState(0);
  const [percentilePage, setPercentilePage] = useState(1);
  const [percentilePageCount, setPercentilePageCount] = useState(0);
  const [percentileGroups, setPercentileGroups] = useState<PercentileGroup[]>([]);
  const [percentileRegion, setPercentileRegion] = useState('');
  const [percentileCompetitor, setPercentileCompetitor] = useState('');
  const [percentilePriceColumns, setPercentilePriceColumns] = useState<PercentilePriceColumn[]>([]);
  const [percentileNumbers, setPercentileNumbers] = useState<number[]>([10, 20, 30, 40, 60]);
  const [percentileSearch, setPercentileSearch] = useState('');
  const [appliedPercentileSearch, setAppliedPercentileSearch] = useState('');
  const [percentileFilter, setPercentileFilter] = useState('all');
  const [percentileCompetitorFilter, setPercentileCompetitorFilter] = useState('all');
  const [percentileSort, setPercentileSort] = useState('sku');
  const [percentileDirection, setPercentileDirection] = useState<'asc' | 'desc'>('asc');
  const [provisorDiagnostics, setProvisorDiagnostics] = useState<ProvisorDiagnostics | null>(null);
  const [provisorAccounts, setProvisorAccounts] = useState<ProvisorAccount[]>([]);
  const [selectedProvisorAccountIds, setSelectedProvisorAccountIds] = useState<number[]>([]);
  const [provisorAccountSearch, setProvisorAccountSearch] = useState('');
  const [provisorAccountSelectorOpen, setProvisorAccountSelectorOpen] = useState(false);

  const [mappingPlatform, setMappingPlatform] = useState<Platform>('provisor');
  const [mappingStatus, setMappingStatus] = useState<MappingStatus>('unmapped');
  const [sourceQuery, setSourceQuery] = useState('');
  const [productQuery, setProductQuery] = useState('');
  const [appliedSourceQuery, setAppliedSourceQuery] = useState('');
  const [appliedProductQuery, setAppliedProductQuery] = useState('');
  const [mappingPage, setMappingPage] = useState(1);
  const [mappingPagination, setMappingPagination] = useState<MappingPagination>({ page: 1, pageSize: 50, total: 0, pageCount: 0 });
  const [codeRows, setCodeRows] = useState<CodeMappingRow[]>([]);
  const [metrics, setMetrics] = useState<CodeMappingMetric[]>([]);
  const [selectedRow, setSelectedRow] = useState<CodeMappingRow | null>(null);
  const [selectedCandidate, setSelectedCandidate] = useState<CodeMappingCandidate | null>(null);
  const [productSearch, setProductSearch] = useState('');
  const [productResults, setProductResults] = useState<ProductSearchRow[]>([]);
  const [selectedProduct, setSelectedProduct] = useState<ProductSearchRow | null>(null);

  const [activeJob, setActiveJob] = useState<JobState | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mappingRequestRef = useRef<AbortController | null>(null);

  const loadSources = async () => {
    const res = await fetch(`/api/competitors/price-lists?format_code=${encodeURIComponent(formatCode)}`);
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить прайс-листы конкурентов');
    setSources(Array.isArray(data) ? data : []);
  };

  const loadPercentiles = async () => {
    const res = await fetch(`/api/competitors/percentiles?format_code=${encodeURIComponent(formatCode)}`);
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить персентили');
    setPercentiles(Array.isArray(data) ? data : []);
  };

  const loadPercentileRows = async () => {
    const params = new URLSearchParams({
      format_code: formatCode,
      region: percentileRegion,
      competitor: percentileCompetitor,
      q: appliedPercentileSearch,
      percentile_filter: percentileFilter,
      competitor_filter: percentileCompetitorFilter,
      sort: percentileSort,
      direction: percentileDirection,
      page: String(percentilePage),
      page_size: '100',
    });
    const res = await fetch(`/api/competitors/percentile-rows?${params.toString()}`);
    const text = await res.text();
    const data = parseJsonOrNull(text) as PercentilePayload | null;
    if (!res.ok) throw new Error((data as any)?.detail || text || 'Не удалось загрузить персентили');
    setPercentileRows(Array.isArray(data?.items) ? data.items : []);
    setPercentileSummary(data?.summary || {
      totalProducts: 0,
      productsWithPercentile: 0,
      productsWithoutPercentile: 0,
      productsWithCompetitors: 0,
      productsWithoutCompetitors: 0,
      coveragePercent: 0,
    });
    setPercentileTotal(Number(data?.total || 0));
    setPercentilePageCount(Number(data?.pageCount || 0));
    setPercentileGroups(Array.isArray(data?.groups) ? data.groups : []);
    setPercentilePriceColumns(Array.isArray(data?.priceColumns) ? data.priceColumns : []);
    setPercentileNumbers(Array.isArray(data?.percentiles) ? data.percentiles.map(Number) : [10, 20, 30, 40, 60]);
    if (data?.selectedRegion && data.selectedRegion !== percentileRegion) setPercentileRegion(data.selectedRegion);
    if (data?.selectedCompetitor && data.selectedCompetitor !== percentileCompetitor) setPercentileCompetitor(data.selectedCompetitor);
    if (data?.page && data.page !== percentilePage) setPercentilePage(data.page);
  };

  const loadProvisorDiagnostics = async () => {
    const res = await fetch(`/api/competitors/provisor-diagnostics?format_code=${encodeURIComponent(formatCode)}`);
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) throw new Error(data?.detail || text || 'Failed to load Provisor diagnostics');
    setProvisorDiagnostics(data);
  };

  const loadProvisorAccounts = async () => {
    const res = await fetch('/api/provisor/accounts');
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) throw new Error(data?.detail || text || 'Failed to load Provisor accounts');
    setProvisorAccounts(Array.isArray(data) ? data : []);
  };

  const loadCodeMappings = async (signal?: AbortSignal) => {
    const controller = signal ? null : new AbortController();
    if (controller) {
      mappingRequestRef.current?.abort();
      mappingRequestRef.current = controller;
    }
    const requestSignal = signal || controller?.signal;
    const params = new URLSearchParams({
      platform: mappingPlatform,
      status: mappingStatus,
      format_code: formatCode,
      page: String(mappingPage),
      limit: '50',
      include_candidates: 'false',
    });
    if (appliedSourceQuery) params.set('source_q', appliedSourceQuery);
    if (appliedProductQuery) params.set('product_q', appliedProductQuery);
    const res = await fetch(`/api/competitors/code-mappings/catalog-view?${params.toString()}`, { signal: requestSignal });
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить таблицу соответствий');
    setCodeRows(Array.isArray(data?.items) ? data.items : []);
    setMetrics(Array.isArray(data?.metrics) ? data.metrics : []);
    setMappingPagination(data?.pagination || { page: mappingPage, pageSize: 50, total: 0, pageCount: 0 });
    if (data?.pagination?.page && data.pagination.page !== mappingPage) setMappingPage(data.pagination.page);
    if (controller && mappingRequestRef.current === controller) mappingRequestRef.current = null;
  };

  const loadAll = async () => {
    setIsLoading(true);
    setError(null);
    try {
      await Promise.all([loadSources(), loadPercentileRows(), loadProvisorDiagnostics(), loadProvisorAccounts()]);
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    setPercentileRegion('');
    setPercentileCompetitor('');
    setPercentilePage(1);
    setPercentileRows([]);
    setPercentilePriceColumns([]);
  }, [formatCode]);

  useEffect(() => {
    void loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [formatCode]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setAppliedPercentileSearch(percentileSearch.trim());
      setPercentilePage(1);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [percentileSearch]);

  useEffect(() => {
    setIsLoading(true);
    void loadPercentileRows()
      .catch((e: any) => setError(e?.message || 'Не удалось загрузить персентили'))
      .finally(() => setIsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [formatCode, percentileRegion, percentileCompetitor, appliedPercentileSearch, percentileFilter, percentileCompetitorFilter, percentileSort, percentileDirection, percentilePage]);

  useEffect(() => {
    mappingRequestRef.current?.abort();
    const controller = new AbortController();
    mappingRequestRef.current = controller;
    const timer = window.setTimeout(() => {
      setIsLoading(true);
      void loadCodeMappings(controller.signal)
        .catch((e: any) => {
          if (e?.name !== 'AbortError') setError(e?.message || 'Ошибка загрузки таблицы соответствий');
        })
        .finally(() => {
          if (mappingRequestRef.current === controller) {
            setIsLoading(false);
            mappingRequestRef.current = null;
          }
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      mappingRequestRef.current?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mappingPlatform, mappingStatus, appliedSourceQuery, appliedProductQuery, mappingPage, formatCode]);

  const submitMappingSearch = () => {
    setAppliedSourceQuery(sourceQuery.trim());
    setAppliedProductQuery(productQuery.trim());
    setMappingPage(1);
  };

  const exportPercentiles = (fmt: 'csv' | 'xlsx') => {
    const params = new URLSearchParams({
      format_code: formatCode,
      region: percentileRegion,
      competitor: percentileCompetitor,
      q: appliedPercentileSearch,
      percentile_filter: percentileFilter,
      competitor_filter: percentileCompetitorFilter,
      sort: percentileSort,
      direction: percentileDirection,
    });
    window.location.href = `/api/competitors/percentile-rows/export.${fmt}?${params.toString()}`;
  };

  const pollJob = async (jobId: string) => {
    while (true) {
      const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось получить статус задачи');
      setActiveJob(data);
      if (data?.status === 'success') return data;
      if (data?.status === 'error' || data?.status === 'cancelled') {
        throw new Error(data?.error || data?.message || 'Задача завершилась с ошибкой');
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
  };

  const refreshSources = async (source: Platform, options?: { accountIds?: number[]; filialIds?: number[] }) => {
    setIsLoading(true);
    setError(null);
    try {
      const accountIds = Array.from(new Set((options?.accountIds || []).map((id) => Number(id)).filter((id) => Number.isFinite(id))));
      const filialIds = Array.from(new Set((options?.filialIds || []).map((id) => Number(id)).filter((id) => Number.isFinite(id))));
      const res = await fetch(`/api/price-formats/${encodeURIComponent(formatCode)}/competitor-price-lists/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source, sourceType: source, forceRefresh: true, accountIds, filialIds }),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось обновить источники');
      if (!data?.job_id) throw new Error('Backend не вернул job_id');
      setActiveJob({ id: data.job_id, status: data.status || 'pending', progress: 0, message: data.message || 'Обновляем источники' });
      await pollJob(data.job_id);
      await Promise.all([loadSources(), loadPercentileRows(), loadCodeMappings(), loadProvisorDiagnostics(), loadProvisorAccounts()]);
      toast.success(`${platformLabel(source)} обновлен`);
    } catch (e: any) {
      setError(e?.message || 'Ошибка обновления');
    } finally {
      setIsLoading(false);
    }
  };

  const uploadManualPriceList = async () => {
    if (!excelFile) {
      setError('Выберите Excel-файл');
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const fd = new FormData();
      fd.append('file', excelFile);
      const res = await fetch(`/api/price-formats/${encodeURIComponent(formatCode)}/competitor-price-lists/upload-excel`, {
        method: 'POST',
        body: fd,
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить Excel');
      await Promise.all([loadSources(), loadProvisorDiagnostics()]);
      toast.success('Прайс-лист конкурента загружен');
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки Excel');
    } finally {
      setIsLoading(false);
    }
  };

  const openPriceList = async (id: number) => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/competitor-price-lists/${id}`);
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось открыть прайс');
      setOpened(data);
      setActiveListId(id);
    } catch (e: any) {
      setError(e?.message || 'Ошибка открытия');
    } finally {
      setIsLoading(false);
    }
  };

  const searchProducts = async (queryOverride?: string) => {
    const query = (queryOverride ?? productSearch).trim();
    if (!query) return;
    const params = new URLSearchParams({ q: query, limit: '30' });
    const res = await fetch(`/api/products/search?${params.toString()}`);
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) throw new Error(data?.detail || text || 'Не удалось найти товары');
    setProductResults(Array.isArray(data) ? data : []);
  };

  const loadCandidatesForRow = async (row: CodeMappingRow) => {
    if (row.candidates?.length || row.mappingStatus === 'mapped') return;
    const params = new URLSearchParams({
      platform: mappingPlatform,
      status: 'unmapped',
      format_code: formatCode,
      product_q: row.ourSku || row.ourName || '',
      page: '1',
      limit: '1',
      include_candidates: 'true',
    });
    const res = await fetch(`/api/competitors/code-mappings/catalog-view?${params.toString()}`);
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить кандидатов');
    const fresh = (Array.isArray(data?.items) ? data.items : []).find(
      (item: CodeMappingRow) => item.ourProductId === row.ourProductId || item.ourSku === row.ourSku,
    );
    if (!fresh) return;
    setSelectedRow((current) => (current?.ourProductId === row.ourProductId ? { ...current, ...fresh } : current));
    setSelectedCandidate(fresh.bestCandidate || fresh.candidates?.[0] || (fresh.itemId ? fresh : null));
    setCodeRows((current) => current.map((item) => (item.ourProductId === row.ourProductId ? { ...item, ...fresh } : item)));
  };

  const selectMappingRow = (row: CodeMappingRow) => {
    setSelectedRow(row);
    setSelectedCandidate(row.bestCandidate || row.candidates?.[0] || (row.itemId ? row : null));
    setSelectedProduct(null);
    const query = candidateQueryForRow(row);
    setProductSearch(query);
    setProductResults([]);
    void loadCandidatesForRow(row).catch((err: any) => setError(err?.message || 'Ошибка загрузки кандидатов'));
  };

  const mapSelected = async () => {
    const productId = selectedRow?.ourProductId || selectedRow?.productId;
    const candidate = selectedCandidate || selectedRow;
    if (!selectedRow || !productId || !candidate?.sourceMatchKey) {
      setError('Выберите наш товар и конкурентский товар-кандидат');
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/competitors/code-mappings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          platform: mappingPlatform,
          status: 'mapped',
          itemId: candidate.itemId,
          sourceExternalKey: candidate.sourceExternalKey,
          sourceMatchKey: candidate.sourceMatchKey,
          sourceName: candidate.sourceName,
          sourceManufacturer: candidate.sourceManufacturer,
          sourceDosageForm: candidate.sourceDosageForm,
          sourceNormalizedName: candidate.sourceNormalizedName,
          ourProductId: productId,
          confidence: candidate.confidence || 100,
        }),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось сохранить сопоставление');
      setSelectedRow(null);
      setSelectedCandidate(null);
      setSelectedProduct(null);
      await loadCodeMappings();
      toast.success('Сопоставление сохранено');
    } catch (e: any) {
      setError(e?.message || 'Ошибка сопоставления');
    } finally {
      setIsLoading(false);
    }
  };

  const rejectRow = async (row: CodeMappingRow) => {
    setIsLoading(true);
    setError(null);
    try {
      const res = row.mappingId
        ? await fetch(`/api/competitors/code-mappings/${row.mappingId}/reject`, { method: 'POST' })
        : await fetch('/api/competitors/code-mappings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              platform: row.platform,
              status: 'rejected',
              itemId: row.itemId,
              sourceExternalKey: row.sourceExternalKey,
              sourceMatchKey: row.sourceMatchKey,
              sourceName: row.sourceName,
              sourceManufacturer: row.sourceManufacturer,
              sourceDosageForm: row.sourceDosageForm,
              sourceNormalizedName: row.sourceNormalizedName,
            }),
          });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось отклонить позицию');
      await loadCodeMappings();
      toast.success('Позиция отклонена');
    } catch (e: any) {
      setError(e?.message || 'Ошибка отклонения');
    } finally {
      setIsLoading(false);
    }
  };

  const unmapRow = async (row: CodeMappingRow) => {
    if (!row.mappingId) return;
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/competitors/code-mappings/${row.mappingId}/unmap`, { method: 'POST' });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось отвязать позицию');
      await loadCodeMappings();
      toast.success('Сопоставление отвязано');
    } catch (e: any) {
      setError(e?.message || 'Ошибка отвязки');
    } finally {
      setIsLoading(false);
    }
  };

  const filteredSources = useMemo(() => {
    const q = sourceSearch.trim().toLowerCase();
    return sources.filter((row) => {
      if (sourceFilter !== '__all__' && row.sourceType !== sourceFilter) return false;
      return !q || [row.sourceName, row.branchName, row.competitorName, row.accountLogin, row.name].some((value) => String(value || '').toLowerCase().includes(q));
    });
  }, [sources, sourceFilter, sourceSearch]);

  const provisorAccountOptions = useMemo(() => {
    const refreshedCounts = new Map<number, number>();
    for (const row of sources) {
      if (row.sourceType !== 'provisor') continue;
      const id = Number(row.accountId);
      if (!Number.isFinite(id)) continue;
      refreshedCounts.set(id, (refreshedCounts.get(id) || 0) + 1);
    }
    return provisorAccounts
      .map((account) => {
        const apiCount = Number(account.price_list_count);
        return {
          id: account.id,
          label: account.login || `Account ${account.id}`,
          count: Number.isFinite(apiCount) ? apiCount : refreshedCounts.get(account.id) || 0,
          status: account.status || 'not_checked',
          lastSuccessAt: account.last_success_at || null,
        };
      })
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [provisorAccounts, sources]);

  const filteredProvisorAccountOptions = useMemo(() => {
    const q = provisorAccountSearch.trim().toLowerCase();
    if (!q) return provisorAccountOptions;
    return provisorAccountOptions.filter((account) => account.label.toLowerCase().includes(q));
  }, [provisorAccountOptions, provisorAccountSearch]);

  const selectedProvisorAccountCount = useMemo(() => {
    const availableIds = new Set(provisorAccountOptions.map((account) => account.id));
    return selectedProvisorAccountIds.filter((id) => availableIds.has(id)).length;
  }, [provisorAccountOptions, selectedProvisorAccountIds]);

  useEffect(() => {
    // Temporary selector diagnostics while Provisor account filtering is being stabilized.
    console.debug('[CompetitorsTab] Provisor account selector', {
      sourceRows: sources.length,
      provisorAccounts: provisorAccounts.length,
      provisorAccountOptions: provisorAccountOptions.length,
      selectedProvisorAccountIds: selectedProvisorAccountIds.length,
      filteredProvisorAccountOptions: filteredProvisorAccountOptions.length,
    });
  }, [filteredProvisorAccountOptions.length, provisorAccountOptions.length, provisorAccounts.length, selectedProvisorAccountIds.length, sources.length]);

  const sourceTypes = useMemo(() => Array.from(new Set(sources.map((row) => row.sourceType).filter(Boolean))).sort(), [sources]);
  const metricByPlatform = useMemo(() => Object.fromEntries(metrics.map((metric) => [metric.platform, metric])) as Record<Platform, CodeMappingMetric | undefined>, [metrics]);
  const selectedMetric = metricByPlatform[mappingPlatform];

  const toggleProvisorAccount = (accountId: number, checked: boolean) => {
    setSelectedProvisorAccountIds((prev) => (
      checked
        ? Array.from(new Set([...prev, accountId]))
        : prev.filter((id) => id !== accountId)
    ));
  };

  const renderProvisorAccountSelector = () => {
    if (!provisorAccountOptions.length) {
      return (
        <div className="mt-3 border-t border-gray-100 pt-3">
          <div className="rounded-md border border-dashed border-gray-200 px-3 py-2 text-sm text-gray-500">
            No Provisor accounts found
            <span className="ml-2 text-xs text-gray-400">configured accounts: {provisorAccounts.length} · source rows: {sources.length}</span>
          </div>
        </div>
      );
    }

    return (
      <div className="mt-3 flex flex-col gap-2 border-t border-gray-100 pt-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="text-sm font-medium text-gray-900">Provisor accounts</div>
          <div className="text-xs text-gray-500">
            configured: {provisorAccounts.length} · options: {provisorAccountOptions.length} · selected ids: {selectedProvisorAccountIds.length} · filtered: {filteredProvisorAccountOptions.length}
          </div>
        </div>
        <Popover open={provisorAccountSelectorOpen} onOpenChange={setProvisorAccountSelectorOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              className="inline-flex h-9 w-full items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 sm:w-[260px]"
            >
              <span className="truncate">Accounts ({selectedProvisorAccountCount} selected)</span>
              <ChevronDown className="h-4 w-4 shrink-0 text-gray-500" />
            </button>
          </PopoverTrigger>
          <PopoverContent align="end" className="z-[100] w-[min(calc(100vw-2rem),380px)] p-0">
            <div className="border-b border-gray-200 p-3">
              <div className="relative">
                <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
                <Input
                  value={provisorAccountSearch}
                  onChange={(e) => setProvisorAccountSearch(e.target.value)}
                  placeholder="Search account login"
                  className="h-9 pl-9"
                />
              </div>
              <div className="mt-3 flex items-center justify-between gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 px-2"
                  onClick={() => setSelectedProvisorAccountIds(provisorAccountOptions.map((account) => account.id))}
                >
                  Select All
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 px-2"
                  onClick={() => setSelectedProvisorAccountIds([])}
                >
                  Clear All
                </Button>
              </div>
            </div>
            <ScrollArea className="h-72">
              <div className="p-2">
                <div className="mb-2 rounded bg-gray-50 px-2 py-1 text-xs text-gray-500">
                  configured: {provisorAccounts.length} · options: {provisorAccountOptions.length} · selected ids: {selectedProvisorAccountIds.length} · filtered: {filteredProvisorAccountOptions.length}
                </div>
                {filteredProvisorAccountOptions.map((account) => {
                  const checked = selectedProvisorAccountIds.includes(account.id);
                  return (
                    <label
                      key={account.id}
                      className="flex cursor-pointer items-center gap-3 rounded-md px-2 py-2 text-sm hover:bg-gray-50"
                    >
                      <Checkbox
                        checked={checked}
                        onCheckedChange={(value) => toggleProvisorAccount(account.id, value === true)}
                        aria-label={`Select ${account.label}`}
                      />
                      <span className="min-w-0 flex-1 truncate text-gray-900">{account.label}</span>
                      <span className="shrink-0 tabular-nums text-gray-500">{account.count} PLK</span>
                    </label>
                  );
                })}
                {!filteredProvisorAccountOptions.length ? (
                  <div className="px-3 py-8 text-center text-sm text-gray-500">No accounts found</div>
                ) : null}
              </div>
            </ScrollArea>
          </PopoverContent>
        </Popover>
      </div>
    );
  };

  const renderMappingsWorkflow = () => (
    <div className="space-y-4">
      <div className="admin-card p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h3 className="text-base font-semibold text-gray-900">Таблица соответствий</h3>
            <p className="mt-1 text-sm text-gray-600">
              Сопоставления применяются глобально для площадки. Текущий контекст ЦФ: <strong>{formatCode}</strong>.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-2 sm:flex">
            {(['provisor', 'vidman'] as Platform[]).map((platform) => (
              <Button
                key={platform}
                type="button"
                variant={mappingPlatform === platform ? 'default' : 'outline'}
                size="sm"
                onClick={() => {
                  setMappingPlatform(platform);
                  setMappingPage(1);
                  setSelectedRow(null);
                  setSelectedProduct(null);
                  setProductResults([]);
                }}
              >
                {platformLabel(platform)}
              </Button>
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-9">
        {[
          ['Всего товаров нашего каталога', selectedMetric?.total],
          ['Manual/catalog mappings', selectedMetric?.mapped],
          ['Candidates needing manual review', selectedMetric?.unmapped],
          ['Отклонено', selectedMetric?.rejected],
          ['No mapping candidates', selectedMetric?.noCandidates],
          ['Manual mapping coverage', `${fmtNumber(selectedMetric?.mappingCoveragePercent ?? selectedMetric?.coveragePercent)}%`],
          ['Generated competitor coverage', `${fmtNumber(selectedMetric?.generatedPricingCoverage?.coveragePercent)}%`],
          ['Rows with competitor', selectedMetric?.generatedPricingCoverage?.withCompetitors],
          ['Rows without competitor', selectedMetric?.generatedPricingCoverage?.withoutCompetitors],
        ].map(([label, value]) => (
          <div key={String(label)} className="admin-card p-4">
            <div className="text-xs font-medium uppercase tracking-wide text-gray-500">{label}</div>
            <div className="mt-2 text-2xl font-semibold text-gray-900">{typeof value === 'string' ? value : fmtNumber(value as number | undefined)}</div>
          </div>
        ))}
      </div>

      <div className="admin-card p-4">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {(['unmapped'] as MappingStatus[]).map((status) => (
              <Button
                key={status}
                type="button"
                variant={mappingStatus === status ? 'default' : 'outline'}
                size="sm"
                onClick={() => {
                  setMappingStatus(status);
                  setMappingPage(1);
                  setSelectedRow(null);
                  setSelectedProduct(null);
                  setProductResults([]);
                }}
              >
                {statusLabel(status)}
              </Button>
            ))}
          </div>
          <div className="grid grid-cols-1 gap-2 lg:grid-cols-[minmax(220px,1fr)_minmax(220px,1fr)_auto]">
            <Input value={sourceQuery} onChange={(e) => setSourceQuery(e.target.value)} placeholder="Товар конкурента или производитель" />
            <Input
              value={productQuery}
              onChange={(e) => setProductQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') submitMappingSearch(); }}
              placeholder="Наш SKU, товар или производитель"
            />
            <Button variant="outline" size="sm" onClick={submitMappingSearch}>
              <Search className="mr-2 h-4 w-4" />
              Найти
            </Button>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(420px,1fr)_minmax(360px,480px)]">
        <div className="admin-table-card">
          <div className="border-b border-gray-200 px-4 py-3">
            <div className="text-sm font-semibold text-gray-900">{statusLabel(mappingStatus)}: {platformLabel(mappingPlatform)}</div>
            <div className="text-xs text-gray-500">Выберите позицию конкурента, затем подтвердите кандидата или найдите товар вручную.</div>
          </div>
          <div className="thin-scrollbar max-h-[680px] overflow-auto">
            <table className="admin-table">
              <thead className="sticky top-0 z-10">
                <tr>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Наш SKU / товар</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Наш производитель</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Товар конкурента</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Источник / дата цены</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Статус</th>
                </tr>
              </thead>
              <tbody>
                {codeRows.map((row) => (
                  <tr
                    key={`${row.platform}-${row.ourProductId}-${row.sourceMatchKey || row.ourSku}`}
                    className={`cursor-pointer ${selectedRow?.ourProductId === row.ourProductId ? 'bg-blue-50' : ''}`}
                    onClick={() => selectMappingRow(row)}
                  >
                    <td className="px-4 py-3 text-sm text-gray-900 min-w-72">
                      <div className="font-medium">{row.ourSku || '—'} · {row.ourName || '—'}</div>
                      <div className="mt-1 text-xs text-gray-500">{fmtNumber(row.candidatesCount)} кандидатов · {row.sourceName || 'нет выбранного конкурента'}</div>
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">{row.ourManufacturer || '—'}</td>
                    <td className="px-4 py-3 text-sm text-gray-700">{row.sourceName || '—'}<div className="text-xs text-gray-500">{row.sourceManufacturer || ''}</div></td>
                    <td className="px-4 py-3 text-sm text-gray-700">{platformLabel(row.platform)} · {row.priceDate || '—'}</td>
                    <td className="px-4 py-3 text-sm">
                      <span className={`status-pill ${catalogStatusClass(row)}`}>{catalogStatusLabel(row)}</span>
                    </td>
                  </tr>
                ))}
                {!codeRows.length ? (
                  <tr>
                    <td colSpan={5} className="px-4 py-8 text-center text-sm text-gray-500">
                      Нет позиций по выбранным фильтрам
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between gap-3 border-t border-gray-200 px-4 py-3 text-sm text-gray-600">
            <span>{fmtNumber(mappingPagination.total)} товаров · страница {fmtNumber(mappingPagination.page)} из {fmtNumber(mappingPagination.pageCount || 1)}</span>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" disabled={mappingPage <= 1} onClick={() => setMappingPage((page) => Math.max(1, page - 1))}>Назад</Button>
              <Button variant="outline" size="sm" disabled={mappingPagination.pageCount === 0 || mappingPage >= mappingPagination.pageCount} onClick={() => setMappingPage((page) => page + 1)}>Вперёд</Button>
            </div>
          </div>
        </div>

        <div className="admin-card p-4">
          {selectedRow ? (
            <div className="space-y-4">
              <div>
                <div className="text-xs font-medium uppercase tracking-wide text-gray-500">Наш товар</div>
                <h3 className="mt-1 text-base font-semibold text-gray-900">{selectedRow.ourSku || '—'} · {selectedRow.ourName || '—'}</h3>
                <div className="mt-1 text-sm text-gray-600">{selectedRow.ourManufacturer || 'Производитель не указан'}</div>
                <div className="mt-2 flex flex-wrap gap-2 text-xs">
                  <span className={`status-pill ${catalogStatusClass(selectedRow)}`}>{catalogStatusLabel(selectedRow)}</span>
                  <span className="status-pill">{platformLabel(selectedRow.platform)}</span>
                  <span className="status-pill">{selectedRow.priceListName || 'Прайс-лист не указан'}</span>
                </div>
              </div>

              <div>
                <div className="mb-2 text-sm font-semibold text-gray-900">Кандидаты конкурента</div>
                <div className="thin-scrollbar max-h-64 overflow-auto rounded-md border border-gray-200">
                  {selectedRow.candidates?.length ? selectedRow.candidates.map((row) => (
                    <button
                      key={`${row.itemId}-${row.sourceMatchKey}`}
                      type="button"
                      onClick={() => setSelectedCandidate(row)}
                      className={`block w-full border-b border-gray-100 px-3 py-2 text-left text-sm hover:bg-gray-50 ${selectedCandidate?.sourceMatchKey === row.sourceMatchKey ? 'bg-blue-50' : ''}`}
                    >
                      <div className="flex items-center justify-between gap-3">
                        <span className="font-semibold text-gray-900">{row.sourceName || '—'}</span>
                        <span className="text-xs text-gray-500">confidence: {fmtNumber(row.confidence)}</span>
                      </div>
                      <div className="mt-1 text-xs text-gray-500">{row.sourceManufacturer || 'Производитель не указан'} · {row.priceListName || 'Источник не указан'} · {row.priceDate || 'дата цены не указана'}</div>
                    </button>
                  )) : (
                    <div className="px-3 py-6 text-center text-sm text-gray-500">Кандидаты конкурента не найдены.</div>
                  )}
                </div>
                <Button className="mt-3 w-full bg-blue-600 hover:bg-blue-700" onClick={mapSelected} disabled={isLoading || !selectedCandidate || selectedRow.status === 'rejected'}>
                  <Link2 className="mr-2 h-4 w-4" />
                  Подтвердить соответствие
                </Button>
              </div>

              <div className="rounded-md border border-gray-200 p-3">
                <div className="mb-2 text-sm font-semibold text-gray-900">Найти наш товар</div>
                <div className="grid grid-cols-1 gap-2">
                  <Input
                    value={productSearch}
                    onChange={(e) => setProductSearch(e.target.value)}
                    placeholder="SKU, наименование или производитель"
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') void searchProducts().catch((err: any) => setError(err?.message || 'Ошибка поиска'));
                    }}
                  />
                  <Button variant="outline" size="sm" onClick={() => searchProducts().catch((err: any) => setError(err?.message || 'Ошибка поиска'))}>
                    <Search className="mr-2 h-4 w-4" />
                    Найти товар
                  </Button>
                </div>
                <Button className="mt-2 w-full" onClick={mapSelected} disabled={isLoading || !selectedCandidate || selectedRow.status === 'rejected'}>
                  Сопоставить вручную
                </Button>
              </div>

              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <Button variant="outline" size="sm" onClick={() => rejectRow(selectedRow)} disabled={isLoading || selectedRow.status === 'rejected'}>
                  <XCircle className="mr-2 h-4 w-4" />
                  Отклонить
                </Button>
                <Button variant="outline" size="sm" className="text-red-600 hover:text-red-700" onClick={() => unmapRow(selectedRow)} disabled={!selectedRow.mappingId || selectedRow.status !== 'mapped' || isLoading}>
                  <Trash2 className="mr-2 h-4 w-4" />
                  Убрать соответствие
                </Button>
              </div>

              <details className="rounded-md border border-gray-200 p-3 text-sm">
                <summary className="cursor-pointer font-medium text-gray-700">Технические детали</summary>
                <dl className="mt-3 grid grid-cols-1 gap-2 text-xs text-gray-600">
                  <div><dt className="font-medium text-gray-900">source_external_key</dt><dd>{selectedRow.sourceExternalKey || '—'}</dd></div>
                  <div><dt className="font-medium text-gray-900">source_match_key</dt><dd className="break-all">{selectedRow.sourceMatchKey || '—'}</dd></div>
                  <div><dt className="font-medium text-gray-900">match_type</dt><dd>{selectedRow.matchType || '—'}</dd></div>
                  <div><dt className="font-medium text-gray-900">item_id / mapping_id</dt><dd>{selectedRow.itemId} / {selectedRow.mappingId || '—'}</dd></div>
                </dl>
              </details>
            </div>
          ) : (
            <div className="flex min-h-[360px] items-center justify-center rounded-md border border-dashed border-gray-300 p-6 text-center">
              <div>
                <div className="text-base font-semibold text-gray-900">Выберите товар конкурента</div>
                <p className="mt-2 text-sm text-gray-600">После выбора здесь появятся кандидаты наших товаров, ручной поиск и действия по сопоставлению.</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );

  return (
    <div className="space-y-4">
      {error ? <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div> : null}
      {activeJob && ['pending', 'running'].includes(activeJob.status) ? (
        <div className="admin-card p-4">
          <div className="mb-2 flex items-center justify-between text-sm">
            <span className="font-medium text-gray-900">{activeJob.message || 'Выполняется задача'}</span>
            <span className="font-semibold text-blue-700">{activeJob.progress || 0}%</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-gray-100">
            <div className="h-full rounded-full bg-blue-600 transition-all" style={{ width: `${Math.max(0, Math.min(100, activeJob.progress || 0))}%` }} />
          </div>
        </div>
      ) : null}
      {activeJob?.result && !['pending', 'running'].includes(activeJob.status) ? (
        <div className="admin-card p-3 text-sm text-gray-700">
          <div className="flex flex-wrap gap-x-5 gap-y-1">
            <span><strong>Requested:</strong> {(activeJob.result.accounts_requested || []).join(', ') || 'all'}</span>
            <span><strong>Processed:</strong> {(activeJob.result.accounts_processed || []).join(', ') || 'none'}</span>
            <span><strong>Skipped:</strong> {(activeJob.result.accounts_skipped || []).join(', ') || 'none'}</span>
            <span><strong>Updated:</strong> {activeJob.result.updated_count || 0}</span>
            <span><strong>Unchanged:</strong> {activeJob.result.skipped_unchanged || 0}</span>
            <span><strong>Timeout:</strong> {activeJob.result.skipped_timeout || 0}</span>
          </div>
        </div>
      ) : null}

      <Tabs defaultValue="price-lists" className="w-full">
        <TabsList className="w-full justify-start border-b border-gray-200 rounded-none h-auto p-0 bg-transparent">
          <TabsTrigger value="price-lists" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">
            Прайс-листы конкурентов
          </TabsTrigger>
          <TabsTrigger value="percentiles" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">
            Персентили
          </TabsTrigger>
          <TabsTrigger value="mappings" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">
            Таблица соответствий
          </TabsTrigger>
        </TabsList>

        <TabsContent value="price-lists" className="m-0 pt-4">
          <div className="space-y-4">
            <div className="admin-card p-4">
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-[180px_minmax(240px,1fr)_auto]">
                <Select value={sourceFilter} onValueChange={setSourceFilter}>
                  <SelectTrigger>
                    <SelectValue placeholder="Тип источника" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__all__">Все типы</SelectItem>
                    {sourceTypes.map((type) => (
                      <SelectItem key={type} value={type}>{type}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
                  <Input value={sourceSearch} onChange={(e) => setSourceSearch(e.target.value)} placeholder="Поиск по региону, конкуренту, клиенту..." className="pl-10" />
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button variant="outline" size="sm" onClick={() => refreshSources('provisor')} disabled={isLoading}>
                    <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
                    Обновить Provisor
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => refreshSources('provisor', { accountIds: selectedProvisorAccountIds })}
                    disabled={isLoading || selectedProvisorAccountIds.length === 0}
                  >
                    <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
                    Refresh selected
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => refreshSources('vidman')} disabled={isLoading}>
                    <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
                    Обновить Vidman
                  </Button>
                </div>
              </div>
              {renderProvisorAccountSelector()}
            </div>

            {provisorDiagnostics ? (
              <div className="admin-card p-4">
                <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                  <div>
                    <h3 className="text-sm font-semibold text-gray-900">Provisor diagnostics</h3>
                    <p className="mt-1 text-sm text-gray-600">
                      Format {provisorDiagnostics.formatCode}, branch {provisorDiagnostics.branch || 'all branches'}
                    </p>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => void loadProvisorDiagnostics()} disabled={isLoading}>
                    <RefreshCw className="mr-2 h-4 w-4" />
                    Reload diagnostics
                  </Button>
                </div>

                <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-7">
                  {[
                    ['Global Provisor PL', provisorDiagnostics.visibility.totalProvisorGlobalPool],
                    ['Visible here', provisorDiagnostics.visibility.visibleForFormatBranch],
                    ['Hidden by branch', provisorDiagnostics.visibility.hiddenByBranch],
                    ['Hidden zero items', provisorDiagnostics.visibility.hiddenZeroItems],
                    ['Hidden by dedupe', provisorDiagnostics.visibility.hiddenByDedupe],
                    ['Selected', provisorDiagnostics.visibility.selectedTotal],
                    ['Active', provisorDiagnostics.visibility.activeTotal],
                  ].map(([label, value]) => (
                    <div key={String(label)} className="rounded border border-gray-200 bg-white px-3 py-2">
                      <div className="text-xs font-medium uppercase text-gray-500">{label}</div>
                      <div className="mt-1 text-lg font-semibold tabular-nums text-gray-900">{fmtNumber(value as number)}</div>
                    </div>
                  ))}
                </div>

                <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
                  <div className="rounded border border-gray-200 bg-white p-3">
                    <div className="text-xs font-medium uppercase text-gray-500">Coverage</div>
                    <div className="mt-2 space-y-1 text-sm text-gray-700">
                      <div className="flex justify-between gap-3"><span>Total products</span><strong>{fmtNumber(provisorDiagnostics.coverage.totalProducts)}</strong></div>
                      <div className="flex justify-between gap-3"><span>With goodsId</span><strong>{fmtNumber(provisorDiagnostics.coverage.productsWithProvisorGoodsId)}</strong></div>
                      <div className="flex justify-between gap-3"><span>Without goodsId</span><strong>{fmtNumber(provisorDiagnostics.coverage.productsWithoutProvisorGoodsId)}</strong></div>
                      <div className="flex justify-between gap-3"><span>Unmatched rows with goodsId</span><strong>{fmtNumber(provisorDiagnostics.coverage.unmatchedRowsWithGoodsId)}</strong></div>
                      <div className="flex justify-between gap-3"><span>Unmatched goodsId exists</span><strong>{fmtNumber(provisorDiagnostics.coverage.unmatchedRowsWhoseGoodsIdExistsInProduct)}</strong></div>
                    </div>
                  </div>

                  <div className="rounded border border-gray-200 bg-white p-3">
                    <div className="text-xs font-medium uppercase text-gray-500">Active Provisor PL</div>
                    <div className="mt-2 max-h-32 space-y-1 overflow-auto text-sm text-gray-700">
                      {provisorDiagnostics.coverage.activeProvisorPriceLists.length ? provisorDiagnostics.coverage.activeProvisorPriceLists.map((row) => (
                        <div key={row.id} className="flex justify-between gap-3">
                          <span className="truncate">{row.branchName || row.sourceKey}</span>
                          <strong className="tabular-nums">{fmtNumber(row.itemsCount)}</strong>
                        </div>
                      )) : <span>No active Provisor PL</span>}
                    </div>
                  </div>

                  <div className="rounded border border-gray-200 bg-white p-3">
                    <div className="text-xs font-medium uppercase text-gray-500">Reference filial {provisorDiagnostics.referenceFilialCoverage.referenceFilialId}</div>
                    <div className="mt-2 space-y-1 text-sm text-gray-700">
                      <div className="flex justify-between gap-3"><span>Rows</span><strong>{fmtNumber(provisorDiagnostics.referenceFilialCoverage.rowsTotal)}</strong></div>
                      <div className="flex justify-between gap-3"><span>Rows with goodsId</span><strong>{fmtNumber(provisorDiagnostics.referenceFilialCoverage.rowsWithGoodsId)}</strong></div>
                      <div className="flex justify-between gap-3"><span>SKU matched</span><strong>{fmtNumber(provisorDiagnostics.referenceFilialCoverage.distributorGoodsIdMatchedProducts)}</strong></div>
                      <div className="flex justify-between gap-3"><span>SKU not found</span><strong>{fmtNumber(provisorDiagnostics.referenceFilialCoverage.skuNotFound)}</strong></div>
                    </div>
                  </div>
                </div>

                <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
                  <div className="rounded border border-gray-200 bg-white p-3">
                    <div className="text-xs font-medium uppercase text-gray-500">Match types</div>
                    <div className="mt-2 max-h-36 space-y-1 overflow-auto text-sm text-gray-700">
                      {provisorDiagnostics.coverage.matchTypeDistribution.map((row) => (
                        <div key={row.matchType} className="flex justify-between gap-3">
                          <span className="truncate">{row.matchType}</span>
                          <strong className="tabular-nums">{fmtNumber(row.rows)}</strong>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div className="rounded border border-gray-200 bg-white p-3">
                    <div className="text-xs font-medium uppercase text-gray-500">Reference SKU not found examples</div>
                    <div className="mt-2 max-h-36 space-y-1 overflow-auto text-sm text-gray-700">
                      {provisorDiagnostics.referenceFilialCoverage.topSkuNotFoundExamples.length ? provisorDiagnostics.referenceFilialCoverage.topSkuNotFoundExamples.map((row, index) => (
                        <div key={`${row.sourceKey}-${row.distributorGoodsId}-${index}`} className="grid grid-cols-[120px_120px_minmax(0,1fr)] gap-2">
                          <span className="tabular-nums">{row.goodsId || 'no goodsId'}</span>
                          <span className="truncate">{row.distributorGoodsId || 'no SKU'}</span>
                          <span className="truncate">{row.name}</span>
                        </div>
                      )) : <span>No examples</span>}
                    </div>
                  </div>
                </div>
              </div>
            ) : null}

            <div className="admin-card p-4">
              <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(240px,420px)_auto]">
                <Input type="file" accept=".xlsx,.csv" onChange={(e) => setExcelFile(e.target.files?.[0] ?? null)} />
                <Button variant="outline" size="sm" onClick={uploadManualPriceList} disabled={isLoading}>
                  <FileUp className="mr-2 h-4 w-4" />
                  Загрузить manual Excel
                </Button>
              </div>
            </div>

            <div className="admin-table-card">
              <div className="thin-scrollbar max-h-[560px] min-h-[360px] overflow-auto">
                <table className="admin-table">
                  <thead className="sticky top-0 z-10">
                    <tr>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Источник</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Регион / филиал</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Конкурент</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Клиент / логин</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Тип</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Филиал ЦФ</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Дата цен</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Позиций</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Статус</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Последнее обновление</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Ошибки / timeout</th>
                      <th className="px-4 py-3 text-right text-sm font-medium text-gray-700">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredSources.map((row) => (
                      <tr key={row.id} className={activeListId === row.id ? 'bg-blue-50' : ''}>
                        <td className="px-4 py-3 text-sm font-medium text-gray-900 min-w-64">{row.sourceName || row.name}</td>
                        <td className="px-4 py-3 text-sm text-gray-700 whitespace-nowrap">{row.branchName || 'Без филиала'}</td>
                        <td className="px-4 py-3 text-sm text-gray-700">{row.competitorName || '—'}</td>
                        <td className="px-4 py-3 text-sm text-gray-700 whitespace-nowrap">{row.accountLogin || row.accountId || '—'}</td>
                        <td className="px-4 py-3 text-sm text-gray-700">{row.sourceType}</td>
                        <td className="px-4 py-3 text-sm text-gray-700">
                          <span
                            className={`status-pill ${row.visibleForFormatBranch === false ? 'warn' : 'ok'}`}
                            title={row.branchMatchReason || row.branchMismatchReason || ''}
                          >
                            {row.visibleForFormatBranch === false ? 'Другой' : 'Совпадает'}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-sm text-gray-700 whitespace-nowrap">{row.priceDate || '—'}</td>
                        <td className="px-4 py-3 text-sm text-gray-700 tabular-nums">{fmtNumber(row.itemsCount)}</td>
                        <td className="px-4 py-3 text-sm text-gray-700">
                          <span className={`status-pill ${refreshStatusClass(row)}`} title={row.refreshMessage || row.status || ''}>
                            {refreshStatusLabel(row)}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-sm text-gray-700 whitespace-nowrap">{row.lastCheckedAt || row.lastSuccessAt || row.lastUpdatedAt || row.updatedAt || row.sourceUpdatedAt || '—'}</td>
                        <td className="px-4 py-3 text-sm text-gray-700">{row.errorSummary || row.refreshMessage || '—'}</td>
                        <td className="px-4 py-3 text-sm">
                          <div className="flex items-center justify-end gap-2">
                            <Button variant="outline" size="sm" className="h-7 px-2" onClick={() => openPriceList(row.id)}>
                              <Eye className="mr-1 h-4 w-4" />
                              Открыть
                            </Button>
                            <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => { window.location.href = `/api/competitor-price-lists/${row.id}/export.csv`; }}>
                              CSV
                            </Button>
                            <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => { window.location.href = `/api/competitor-price-lists/${row.id}/export.xlsx`; }}>
                              XLSX
                            </Button>
                            {row.sourceType === 'provisor' || row.sourceType === 'vidman' ? (
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-7 px-2"
                                onClick={() => refreshSources(row.sourceType as Platform, {
                                  accountIds: row.accountId ? [Number(row.accountId)] : [],
                                  filialIds: row.sourceType === 'provisor' && row.filialId ? [Number(row.filialId)] : [],
                                })}
                                disabled={isLoading}
                              >
                                Пересчитать
                              </Button>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {opened ? (
              <div className="admin-card p-5 space-y-4">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold text-gray-900">Строки прайса: {opened.meta?.name}</h3>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={() => { if (activeListId) window.location.href = `/api/competitor-price-lists/${activeListId}/export.xlsx`; }}>
                      <FileDown className="mr-2 h-4 w-4" />
                      Excel
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => { if (activeListId) window.location.href = `/api/competitor-price-lists/${activeListId}/export.csv`; }}>
                      <FileDown className="mr-2 h-4 w-4" />
                      CSV
                    </Button>
                  </div>
                </div>
                <div className="admin-table-card">
                  <div className="thin-scrollbar max-h-[420px] overflow-auto">
                    <table className="admin-table">
                      <thead className="sticky top-0 z-10">
                        <tr>
                          <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">goodsId</th>
                          <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">distributorGoodsId</th>
                          <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Название</th>
                          <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Производитель</th>
                          <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Цена</th>
                          <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Остаток</th>
                          <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Match type</th>
                        </tr>
                      </thead>
                      <tbody>
                        {opened.items.map((item, index) => (
                          <tr key={`${item.provisor_id}-${index}`}>
                            <td className="px-4 py-3 text-sm text-gray-700">{item.provisor_goods_id || '—'}</td>
                            <td className="px-4 py-3 text-sm text-gray-700">{item.distributor_goods_id || '—'}</td>
                            <td className="px-4 py-3 text-sm text-gray-900 min-w-80">{item.name || item.distributor_goods_name}</td>
                            <td className="px-4 py-3 text-sm text-gray-700">{item.manufacturer || '—'}</td>
                            <td className="px-4 py-3 text-sm text-gray-900 tabular-nums">{fmtNumber(item.distributor_price)}</td>
                            <td className="px-4 py-3 text-sm text-gray-700 tabular-nums">{fmtNumber(item.stock)}</td>
                            <td className="px-4 py-3 text-sm text-gray-700">{item.match_type || 'unmatched'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        </TabsContent>

        <TabsContent value="percentiles" className="m-0 pt-4">
          <PercentileBrowser
            rows={percentileRows}
            summary={percentileSummary}
            total={percentileTotal}
            page={percentilePage}
            pageCount={percentilePageCount}
            groups={percentileGroups}
            selectedRegion={percentileRegion}
            selectedCompetitor={percentileCompetitor}
            priceColumns={percentilePriceColumns}
            percentileNumbers={percentileNumbers}
            search={percentileSearch}
            percentileFilter={percentileFilter}
            competitorFilter={percentileCompetitorFilter}
            sort={percentileSort}
            direction={percentileDirection}
            onSearch={setPercentileSearch}
            onPercentileFilter={(value) => { setPercentileFilter(value); setPercentilePage(1); }}
            onCompetitorFilter={(value) => { setPercentileCompetitorFilter(value); setPercentilePage(1); }}
            onSort={(value) => { setPercentileSort(value); setPercentilePage(1); }}
            onDirection={(value) => { setPercentileDirection(value); setPercentilePage(1); }}
            onRegion={(value) => { setPercentileRegion(value); setPercentileCompetitor(''); setPercentilePage(1); }}
            onCompetitor={(value) => { setPercentileCompetitor(value); setPercentilePage(1); }}
            onPage={setPercentilePage}
            onExport={exportPercentiles}
          />
        </TabsContent>

        <TabsContent value="percentiles-legacy" className="m-0 pt-4">
          <div className="admin-table-card">
            <div className="overflow-x-auto">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Источник</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Регион</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Конкурент</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Percentile</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">SKU</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Source count</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">generated_at</th>
                  </tr>
                </thead>
                <tbody>
                  {percentiles.map((row) => (
                    <tr key={row.id}>
                      <td className="px-4 py-3 text-sm font-medium text-gray-900">{row.name}</td>
                      <td className="px-4 py-3 text-sm text-gray-700">{row.region}</td>
                      <td className="px-4 py-3 text-sm text-gray-700">{row.competitor}</td>
                      <td className="px-4 py-3 text-sm text-gray-900">P{row.percentile}</td>
                      <td className="px-4 py-3 text-sm text-gray-700 tabular-nums">{fmtNumber(row.skuCount)}</td>
                      <td className="px-4 py-3 text-sm text-gray-700 tabular-nums">{fmtNumber(row.sourceCount)}</td>
                      <td className="px-4 py-3 text-sm text-gray-700 whitespace-nowrap">{row.generatedAt || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </TabsContent>

        <TabsContent value="mappings" className="m-0 pt-4">
          {renderMappingsWorkflow()}
          {false && (
          <div className="space-y-4">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {(['provisor', 'vidman'] as Platform[]).map((platform) => {
                const metric = metricByPlatform[platform];
                return (
                  <button
                    key={platform}
                    type="button"
                    onClick={() => setMappingPlatform(platform)}
                    className={`admin-card p-4 text-left transition ${mappingPlatform === platform ? 'ring-2 ring-blue-500' : ''}`}
                  >
                    <div className="flex items-center justify-between">
                      <div className="text-sm font-semibold text-gray-900">{platformLabel(platform)}</div>
                      <div className="text-lg font-semibold text-blue-700">{fmtNumber(metric?.mappingCoveragePercent ?? metric?.coveragePercent)}%</div>
                    </div>
                    <div className="mt-3 grid grid-cols-4 gap-2 text-xs text-gray-600">
                      <span>Всего: <strong className="text-gray-900">{fmtNumber(metric?.total)}</strong></span>
                      <span>ОК: <strong className="text-gray-900">{fmtNumber(metric?.mapped)}</strong></span>
                      <span>Нет: <strong className="text-gray-900">{fmtNumber(metric?.unmapped)}</strong></span>
                      <span>Reject: <strong className="text-gray-900">{fmtNumber(metric?.rejected)}</strong></span>
                    </div>
                  </button>
                );
              })}
            </div>

            <div className="admin-card p-4">
              <div className="grid grid-cols-1 gap-3 xl:grid-cols-[160px_180px_minmax(220px,1fr)_minmax(220px,1fr)_auto]">
                <Select value={mappingPlatform} onValueChange={(value) => setMappingPlatform(value as Platform)}>
                  <SelectTrigger>
                    <SelectValue placeholder="Площадка" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="provisor">Provisor</SelectItem>
                    <SelectItem value="vidman">Vidman</SelectItem>
                  </SelectContent>
                </Select>
                <Select value={mappingStatus} onValueChange={(value) => setMappingStatus(value as MappingStatus)}>
                  <SelectTrigger>
                    <SelectValue placeholder="Статус" />
                  </SelectTrigger>
                  <SelectContent>
                    {(['all', 'mapped', 'unmapped', 'rejected'] as MappingStatus[]).map((item) => (
                      <SelectItem key={item} value={item}>{statusLabel(item)}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Input value={sourceQuery} onChange={(e) => setSourceQuery(e.target.value)} placeholder="Поиск по товару конкурента" />
                <Input value={productQuery} onChange={(e) => setProductQuery(e.target.value)} placeholder="Поиск по нашему товару" />
                <Button variant="outline" size="sm" onClick={loadCodeMappings}>
                  <Search className="mr-2 h-4 w-4" />
                  Найти
                </Button>
              </div>
            </div>

            <div className="admin-card p-4">
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-[minmax(260px,1fr)_auto]">
                <Input
                  value={productSearch}
                  onChange={(e) => setProductSearch(e.target.value)}
                  placeholder="Наш товар для сопоставления: SKU, название, производитель"
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void searchProducts().catch((err: any) => setError(err?.message || 'Ошибка поиска'));
                  }}
                />
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" onClick={() => searchProducts().catch((err: any) => setError(err?.message || 'Ошибка поиска'))}>
                    <Search className="mr-2 h-4 w-4" />
                    Найти товар
                  </Button>
                  <Button onClick={mapSelected} disabled={isLoading || !selectedRow || !selectedProduct} className="bg-blue-600 hover:bg-blue-700">
                    <Link2 className="mr-2 h-4 w-4" />
                    Сопоставить
                  </Button>
                </div>
              </div>
              {selectedRow ? (
                <div className="mt-3 rounded-md bg-blue-50 p-3 text-sm text-blue-900">
                  Выбрана позиция {platformLabel(selectedRow.platform)}: <strong>{selectedRow.sourceName}</strong>
                </div>
              ) : null}
              {selectedProduct ? (
                <div className="mt-2 rounded-md bg-green-50 p-3 text-sm text-green-900">
                  Выбран наш товар: <strong>{selectedProduct.sku}</strong> {selectedProduct.name}
                </div>
              ) : null}
              {productResults.length ? (
                <div className="mt-3 max-h-56 overflow-auto rounded-md border border-gray-200">
                  {productResults.map((row) => (
                    <button
                      key={`${row.productId}-${row.sku}`}
                      type="button"
                      onClick={() => setSelectedProduct(row)}
                      className={`block w-full border-b border-gray-100 px-3 py-2 text-left text-sm hover:bg-gray-50 ${selectedProduct?.productId === row.productId ? 'bg-blue-50' : ''}`}
                    >
                      <span className="font-medium text-gray-900">{row.sku}</span>
                      <span className="ml-2 text-gray-700">{row.name}</span>
                      <span className="ml-2 text-xs text-gray-500">{row.manufacturer}</span>
                    </button>
                  ))}
                </div>
              ) : null}
            </div>

            <div className="admin-table-card">
              <div className="thin-scrollbar max-h-[640px] overflow-auto">
                <table className="admin-table">
                  <thead className="sticky top-0 z-10">
                    <tr>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Статус</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Площадка</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">External key</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Товар конкурента</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Производитель конкурента</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Наш SKU</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Наш товар</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Источник</th>
                      <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Match type</th>
                      <th className="px-4 py-3 text-right text-sm font-medium text-gray-700">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {codeRows.map((row) => (
                      <tr key={`${row.platform}-${row.sourceMatchKey}-${row.itemId}`} className={selectedRow?.itemId === row.itemId ? 'bg-blue-50' : ''}>
                        <td className="px-4 py-3 text-sm text-gray-700">{statusLabel(row.status)}</td>
                        <td className="px-4 py-3 text-sm text-gray-700">{platformLabel(row.platform)}</td>
                        <td className="px-4 py-3 text-sm text-gray-700">{row.sourceExternalKey || '—'}</td>
                        <td className="px-4 py-3 text-sm text-gray-900 min-w-80">{row.sourceName || '—'}</td>
                        <td className="px-4 py-3 text-sm text-gray-700">{row.sourceManufacturer || '—'}</td>
                        <td className="px-4 py-3 text-sm font-medium text-gray-900">{row.ourSku || row.matchedSku || '—'}</td>
                        <td className="px-4 py-3 text-sm text-gray-900 min-w-80">{row.ourName || '—'}</td>
                        <td className="px-4 py-3 text-sm text-gray-700">{row.priceListName || '—'}</td>
                        <td className="px-4 py-3 text-sm text-gray-700">{row.matchType || '—'}</td>
                        <td className="px-4 py-3 text-right text-sm">
                          <div className="flex items-center justify-end gap-2">
                            <Button variant="outline" size="sm" className="h-7 px-2" onClick={() => setSelectedRow(row)} disabled={row.status === 'rejected'}>
                              Выбрать
                            </Button>
                            <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => rejectRow(row)} disabled={isLoading || row.status === 'rejected'}>
                              <XCircle className="mr-1 h-4 w-4" />
                              Отклонить
                            </Button>
                            <Button variant="ghost" size="sm" className="h-7 px-2 text-red-600 hover:text-red-700" onClick={() => unmapRow(row)} disabled={!row.mappingId || row.status !== 'mapped' || isLoading}>
                              <Trash2 className="mr-1 h-4 w-4" />
                              Отвязать
                            </Button>
                          </div>
                        </td>
                      </tr>
                    ))}
                    {!codeRows.length ? (
                      <tr>
                        <td colSpan={10} className="px-4 py-8 text-center text-sm text-gray-500">
                          Нет позиций по выбранным фильтрам
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
