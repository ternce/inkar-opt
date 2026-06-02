import { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { ExternalLink, Percent, PlusCircle, RefreshCw, Search, Trash2, Users, X } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';

type PriceFormat = {
  id?: string;
  code: string;
  name: string;
  branch: string;
};

type SourceRow = {
  id: string;
  sourceId: string | number;
  sourceType: string;
  sourceKey?: string;
  sourceName: string;
  name?: string;
  region: string;
  branchName: string;
  competitorName: string;
  accountId?: string;
  accountLogin: string;
  priceDate: string;
  generatedAt?: string;
  itemsCount: number;
  skuCount?: number;
  status?: string;
  coefficient?: number;
  active?: boolean;
  isSelected?: boolean;
  isPercentile?: boolean;
  percentile?: number;
};

type AssignmentRow = SourceRow & {
  coefficient: number;
  active: boolean;
};

type FormatSummary = {
  pricingRule: string;
  assignmentsCount: number;
  lastGeneratedAt: string;
};

type Props = {
  formatCode: string;
  branch: string;
  priceFormats: PriceFormat[];
  onFormatChange: (format: PriceFormat) => void;
  onNavigate: (section: any) => void;
};

const parseJsonOrNull = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const branchKey = (value: any) => String(value || '').trim().toLocaleLowerCase('ru-RU');
const isSameBranch = (left: any, right: any) => branchKey(left) === branchKey(right);

const fmtDate = (value: any) => {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleDateString('ru-RU');
};

const freshness = (value: any) => {
  if (!value) return 'нет данных';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return 'актуально';
  const today = new Date();
  const startToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const startValue = new Date(parsed.getFullYear(), parsed.getMonth(), parsed.getDate()).getTime();
  const ageDays = (startToday - startValue) / 86400000;
  if (ageDays <= 0) return 'актуально';
  if (ageDays > 1) return 'устарело';
  return 'устарело';
};

const freshnessClassName = (value: any) => {
  const label = freshness(value);
  if (label === 'актуально') return 'ok';
  if (label === 'нет данных') return '';
  if (label === 'ошибка') return 'bad';
  return 'warn';
};

const normalizeSource = (row: any): SourceRow => ({
  id: String(row.id ?? row.sourceId ?? ''),
  sourceId: row.sourceId ?? row.id ?? '',
  sourceType: String(row.sourceType || 'manual'),
  sourceKey: String(row.sourceKey || row.id || ''),
  sourceName: String(row.sourceName || row.name || row.displayName || ''),
  name: String(row.name || row.sourceName || ''),
  region: String(row.region || row.branchName || ''),
  branchName: String(row.branchName || row.region || ''),
  competitorName: String(row.competitorName || row.competitor || row.supplier || ''),
  accountId: String(row.accountId || ''),
  accountLogin: String(row.accountLogin || row.accountId || ''),
  priceDate: String(row.priceDate || row.generatedAt || ''),
  generatedAt: String(row.generatedAt || ''),
  itemsCount: Number(row.itemsCount ?? row.skuCount ?? 0),
  skuCount: Number(row.skuCount ?? row.itemsCount ?? 0),
  status: String(row.status || ''),
  coefficient: Number(row.coefficient ?? 1),
  active: Boolean(row.active ?? row.isSelected ?? true),
  isSelected: Boolean(row.isSelected),
  isPercentile: row.sourceType === 'percentile' || Boolean(row.isPercentile),
  percentile: row.percentile != null ? Number(row.percentile) : undefined,
});

const percentileToSource = (row: any): SourceRow =>
  normalizeSource({
    id: row.id,
    sourceId: row.id,
    sourceType: 'percentile',
    sourceName: row.name || `${row.region || 'Регион'} - Эмити - Персентиль ${row.percentile}`,
    region: row.region,
    branchName: row.region,
    competitorName: row.competitor || 'Эмити',
    accountLogin: `Персентиль ${row.percentile}`,
    priceDate: row.generatedAt,
    generatedAt: row.generatedAt,
    itemsCount: row.skuCount,
    skuCount: row.skuCount,
    percentile: row.percentile,
    isPercentile: true,
  });

export function CompetitorAssignmentTab({ formatCode, branch, priceFormats, onFormatChange, onNavigate }: Props) {
  const [selectedFormatCode, setSelectedFormatCode] = useState(formatCode);
  const [availableSources, setAvailableSources] = useState<SourceRow[]>([]);
  const [assignments, setAssignments] = useState<AssignmentRow[]>([]);
  const [summaries, setSummaries] = useState<Record<string, FormatSummary>>({});
  const [competitorFilter, setCompetitorFilter] = useState('__all__');
  const [sourceTypeFilter, setSourceTypeFilter] = useState('__all__');
  const [regionFilter, setRegionFilter] = useState('__branch__');
  const [searchTerm, setSearchTerm] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setSelectedFormatCode(formatCode);
  }, [formatCode]);

  const branchFormats = useMemo(
    () => priceFormats.filter((format) => !branch || isSameBranch(format.branch, branch)),
    [branch, priceFormats]
  );

  const selectedFormat = useMemo(
    () => priceFormats.find((format) => format.code === selectedFormatCode) || branchFormats[0] || priceFormats[0],
    [branchFormats, priceFormats, selectedFormatCode]
  );

  const loadAssignments = async (code = selectedFormat?.code || selectedFormatCode) => {
    if (!code) return [];
    const res = await fetch(`/api/price-formats/${encodeURIComponent(code)}/competitor-assignments`);
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить назначенные ПЛК');
    const rows = (Array.isArray(data) ? data : []).map(normalizeSource) as AssignmentRow[];
    if (code === selectedFormatCode || code === selectedFormat?.code) setAssignments(rows);
    return rows;
  };

  const loadSources = async () => {
    if (!selectedFormat?.code) return;
    setIsLoading(true);
    setError(null);
    try {
      const [sourcesRes, percentileRes, assignmentRows] = await Promise.all([
        fetch(`/api/competitors/price-lists?format_code=${encodeURIComponent(selectedFormat.code)}`),
        fetch(`/api/competitors/percentiles?format_code=${encodeURIComponent(selectedFormat.code)}`),
        loadAssignments(selectedFormat.code),
      ]);
      const sourcesText = await sourcesRes.text();
      const percentileText = await percentileRes.text();
      const sourcesData = parseJsonOrNull(sourcesText);
      const percentileData = parseJsonOrNull(percentileText);
      if (!sourcesRes.ok) throw new Error(sourcesData?.detail || sourcesText || 'Не удалось загрузить источники цен');
      if (!percentileRes.ok) throw new Error(percentileData?.detail || percentileText || 'Не удалось загрузить percentile sources');
      const rows = [
        ...(Array.isArray(sourcesData) ? sourcesData.map(normalizeSource) : []),
        ...(Array.isArray(percentileData) ? percentileData.map(percentileToSource) : []),
      ];
      const assignedKeys = new Set(assignmentRows.map((row) => `${row.sourceType}:${row.sourceId}`));
      setAvailableSources(rows.map((row) => ({ ...row, isSelected: assignedKeys.has(`${row.sourceType}:${row.sourceId}`) })));
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки раздела');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void loadSources();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedFormat?.code]);

  useEffect(() => {
    let cancelled = false;
    const loadSummaries = async () => {
      const next: Record<string, FormatSummary> = {};
      await Promise.all(
        branchFormats.map(async (format) => {
          try {
            const [settingsRes, latestRes, assignmentsRes] = await Promise.all([
              fetch(`/api/price-formats/${encodeURIComponent(format.code)}/settings`),
              fetch(`/api/price-lists/latest?format_code=${encodeURIComponent(format.code)}`),
              fetch(`/api/price-formats/${encodeURIComponent(format.code)}/competitor-assignments`),
            ]);
            const settings = parseJsonOrNull(await settingsRes.text());
            const latest = parseJsonOrNull(await latestRes.text());
            const assigned = parseJsonOrNull(await assignmentsRes.text());
            next[format.code] = {
              pricingRule: settings?.pricingRule || '—',
              assignmentsCount: Array.isArray(assigned) ? assigned.length : 0,
              lastGeneratedAt: latest?.date || '',
            };
          } catch {
            next[format.code] = { pricingRule: '—', assignmentsCount: 0, lastGeneratedAt: '' };
          }
        })
      );
      if (!cancelled) setSummaries(next);
    };
    void loadSummaries();
    return () => {
      cancelled = true;
    };
  }, [branchFormats]);

  const competitorOptions = useMemo(
    () => Array.from(new Set(availableSources.map((row) => row.competitorName).filter(Boolean))).sort((a, b) => a.localeCompare(b, 'ru')),
    [availableSources]
  );

  const sourceTypeOptions = useMemo(
    () => Array.from(new Set(availableSources.map((row) => row.sourceType).filter(Boolean))).sort(),
    [availableSources]
  );

  const filteredSources = useMemo(() => {
    const q = searchTerm.trim().toLowerCase();
    return availableSources.filter((row) => {
      if (competitorFilter !== '__all__' && row.competitorName !== competitorFilter) return false;
      if (sourceTypeFilter !== '__all__' && row.sourceType !== sourceTypeFilter) return false;
      if (regionFilter === '__branch__' && branch && !isSameBranch(row.branchName || row.region, branch)) return false;
      if (regionFilter !== '__all__' && regionFilter !== '__branch__' && !isSameBranch(row.branchName || row.region, regionFilter)) return false;
      return !q || [row.sourceName, row.region, row.branchName, row.competitorName, row.accountLogin, row.sourceType].some((value) =>
        String(value || '').toLowerCase().includes(q)
      );
    });
  }, [availableSources, branch, competitorFilter, regionFilter, searchTerm, sourceTypeFilter]);

  const regionOptions = useMemo(
    () => Array.from(new Set(availableSources.map((row) => row.branchName || row.region).filter(Boolean))).sort((a, b) => a.localeCompare(b, 'ru')),
    [availableSources]
  );
  const branchScopeLabel = selectedFormat?.branch || branch || 'текущего филиала';

  const selectFormat = (format: PriceFormat) => {
    setSelectedFormatCode(format.code);
    onFormatChange(format);
  };

  const addSource = async (source: SourceRow) => {
    if (!selectedFormat?.code) return;
    if (assignments.some((row) => `${row.sourceType}:${row.sourceId}` === `${source.sourceType}:${source.sourceId}`)) {
      toast.info('Источник уже назначен');
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/price-formats/${encodeURIComponent(selectedFormat.code)}/competitor-assignments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sourceId: source.sourceId,
          sourceType: source.sourceType,
          sourceName: source.sourceName || source.name,
          coefficient: 1.0,
          active: true,
        }),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось назначить источник');
      toast.success('Источник назначен');
      await loadSources();
    } catch (e: any) {
      const message = e?.message || 'Ошибка назначения источника';
      if (message.includes('уже назначен')) toast.info('Источник уже назначен');
      else setError(message);
    } finally {
      setIsLoading(false);
    }
  };

  const saveAssignment = async (assignment: AssignmentRow, patch: Partial<AssignmentRow>) => {
    if (!selectedFormat?.code) return;
    const next = { ...assignment, ...patch };
    setAssignments((prev) => prev.map((row) => (row.id === assignment.id ? next : row)));
    try {
      const res = await fetch(`/api/price-formats/${encodeURIComponent(selectedFormat.code)}/competitor-assignments/${encodeURIComponent(assignment.id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ coefficient: Number(next.coefficient || 1), active: Boolean(next.active) }),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось сохранить назначение');
      await loadSources();
    } catch (e: any) {
      setError(e?.message || 'Ошибка сохранения назначения');
      await loadSources();
    }
  };

  const deleteAssignment = async (assignment: AssignmentRow) => {
    if (!selectedFormat?.code) return;
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/price-formats/${encodeURIComponent(selectedFormat.code)}/competitor-assignments/${encodeURIComponent(assignment.id)}`, {
        method: 'DELETE',
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось удалить назначение');
      toast.success('Источник удалён из назначений');
      await loadSources();
    } catch (e: any) {
      setError(e?.message || 'Ошибка удаления назначения');
    } finally {
      setIsLoading(false);
    }
  };

  const resetFilters = () => {
    setCompetitorFilter('__all__');
    setSourceTypeFilter('__all__');
    setRegionFilter('__branch__');
    setSearchTerm('');
  };

  if (!selectedFormat) {
    return (
      <div className="empty-state">
        <Users className="h-8 w-8 text-blue-600" />
        <h3>Нет ценовых форматов</h3>
        <p>Для выбранного филиала пока нет ценовых форматов для назначения ПЛК.</p>
      </div>
    );
  }

  return (
    <div className="assignment-workspace">
      <section className="assignment-toolbar">
        <div>
          <div className="eyebrow">Назначение ПЛК</div>
          <h3>{branch || 'Без филиала'} · {selectedFormat.code}</h3>
        </div>
        <Select value={competitorFilter} onValueChange={setCompetitorFilter}>
          <SelectTrigger>
            <SelectValue placeholder="Конкурент" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все конкуренты</SelectItem>
            {competitorOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={sourceTypeFilter} onValueChange={setSourceTypeFilter}>
          <SelectTrigger>
            <SelectValue placeholder="Тип источника" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все типы</SelectItem>
            {sourceTypeOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={regionFilter} onValueChange={setRegionFilter}>
          <SelectTrigger>
            <SelectValue placeholder="Регион" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__branch__">Текущий филиал</SelectItem>
            <SelectItem value="__all__">Все регионы</SelectItem>
            {regionOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
          </SelectContent>
        </Select>
        <div className="assignment-search">
          <Search className="h-4 w-4" />
          <Input value={searchTerm} onChange={(event) => setSearchTerm(event.target.value)} placeholder="Поиск источника" />
        </div>
        <Button variant="outline" onClick={resetFilters} disabled={isLoading}>
          <X className="mr-2 h-4 w-4" />
          Сброс
        </Button>
      </section>

      {error ? <div className="dashboard-alert">{error}</div> : null}

      <section className="assignment-layout">
        <section className="assignment-panel assignment-formats">
          <div className="card-title-row">
            <h3>Ценовые форматы</h3>
            <span className="muted-count">{branchFormats.length} шт.</span>
          </div>
          <div className="assignment-format-list">
            {branchFormats.length ? branchFormats.map((format) => {
              const summary = summaries[format.code] || { pricingRule: '—', assignmentsCount: 0, lastGeneratedAt: '' };
              return (
                <button
                  key={format.code}
                  type="button"
                  className={`assignment-format-card ${format.code === selectedFormat.code ? 'active' : ''}`}
                  onClick={() => selectFormat(format)}
                >
                  <div className="assignment-format-head">
                    <strong>{format.code}</strong>
                    <span>{summary.assignmentsCount} ПЛК</span>
                  </div>
                  <div className="assignment-format-name">{format.name || format.code}</div>
                  <dl>
                    <div><dt>Филиал</dt><dd>{format.branch || 'Без филиала'}</dd></div>
                    <div><dt>Правило ЦО</dt><dd>{summary.pricingRule || '—'}</dd></div>
                    <div><dt>Последнее формирование</dt><dd>{summary.lastGeneratedAt || '—'}</dd></div>
                  </dl>
                </button>
              );
            }) : (
              <div className="compact-empty">Для выбранного филиала пока нет ценовых форматов</div>
            )}
          </div>
        </section>

        <section className="assignment-main">
          <section className="assignment-panel">
            <div className="card-title-row">
              <div>
                <h3>Доступные источники цен</h3>
                <p>Refresh, импорт и сопоставления находятся в разделе “Конкуренты”.</p>
                <p>Показаны доступные ПЛК региона {branchScopeLabel}.</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" size="sm" onClick={() => onNavigate('competitor-domain')}>
                  <Users className="mr-2 h-4 w-4" />
                  Управлять конкурентами
                </Button>
                <Button variant="outline" size="sm" onClick={() => onNavigate('competitor-domain')}>
                  <Percent className="mr-2 h-4 w-4" />
                  Пересчитать персентили
                </Button>
                <Button variant="outline" size="sm" onClick={() => loadSources()} disabled={isLoading}>
                  <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
                  Обновить список
                </Button>
              </div>
            </div>
            <CompactTable
              empty="Для выбранного филиала нет доступных источников цен"
              columns={['Источник', 'Регион', 'Конкурент', 'Клиент / логин', 'Тип', 'Дата цен', 'Позиций', 'Актуальность', '']}
              rows={filteredSources.map((row) => [
                row.sourceName || row.name || '—',
                row.branchName || row.region || '—',
                row.competitorName || '—',
                row.accountLogin || row.accountId || '—',
                <span key={`${row.id}-type`} className="status-pill">{row.sourceType}</span>,
                fmtDate(row.priceDate),
                Number(row.itemsCount || 0).toLocaleString('ru-RU'),
                <span key={`${row.id}-fresh`} className={`status-pill ${freshnessClassName(row.priceDate)}`}>{freshness(row.priceDate)}</span>,
                <Button key={`${row.id}-add`} variant="ghost" size="sm" onClick={() => addSource(row)} disabled={isLoading || row.isSelected}>
                  <PlusCircle className="mr-1 h-4 w-4" />
                  {row.isSelected ? 'Назначен' : 'Добавить'}
                </Button>,
              ])}
            />
          </section>

          <section className="assignment-panel">
            <div className="card-title-row">
              <h3>Назначенные ПЛК для выбранного ЦФ</h3>
              <span className="muted-count">{assignments.length} источников</span>
            </div>
            <CompactTable
              empty="Нет назначенных ПЛК"
              columns={['Источник', 'Конкурент', 'Регион', 'Клиент / логин', 'Коэффициент', 'Дата цен', 'Активен', 'Действия']}
              rows={assignments.map((row) => [
                row.sourceName || row.name || '—',
                row.competitorName || '—',
                row.branchName || row.region || '—',
                row.accountLogin || row.accountId || '—',
                <Input
                  key={`${row.id}-coef`}
                  className="numeric-input assignment-coef-input"
                  defaultValue={String(row.coefficient ?? 1)}
                  onBlur={(event) => saveAssignment(row, { coefficient: Number(event.target.value || 1) })}
                />,
                fmtDate(row.priceDate),
                <input
                  key={`${row.id}-active`}
                  type="checkbox"
                  checked={Boolean(row.active)}
                  onChange={(event) => saveAssignment(row, { active: event.target.checked })}
                />,
                <div key={`${row.id}-actions`} className="assignment-actions">
                  <Button variant="ghost" size="sm" onClick={() => onNavigate('competitor-domain')}>
                    <ExternalLink className="mr-1 h-4 w-4" />
                    Открыть
                  </Button>
                  <Button variant="ghost" size="sm" className="text-red-600 hover:text-red-700" onClick={() => deleteAssignment(row)}>
                    <Trash2 className="mr-1 h-4 w-4" />
                    Удалить
                  </Button>
                </div>,
              ])}
            />
          </section>
        </section>
      </section>
    </div>
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
