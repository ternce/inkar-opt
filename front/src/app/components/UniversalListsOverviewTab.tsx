import { useEffect, useMemo, useState } from 'react';
import { CheckCircle2, ListChecks, RefreshCw } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { listTypeImpact, listTypeLabel, listTypeOptions } from './listTypeLabels';

type PriceFormat = {
  code: string;
  name: string;
  branch: string;
};

type UniversalListRow = {
  id: number;
  name: string;
  code: string;
  type: string;
  typeLabel: string;
  active: boolean;
  status: string;
  itemsCount: number;
  priceFormats: PriceFormat[];
  scope: 'global' | 'formats';
  startDate: string;
  endDate: string;
  updatedAt: string;
  comment: string;
};

const parseJson = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const parseRuDate = (value: string) => {
  if (!value) return null;
  const [day, month, year] = value.split('.').map(Number);
  if (!day || !month || !year) return null;
  return new Date(year, month - 1, day);
};

const isInPeriod = (row: UniversalListRow) => {
  const today = new Date();
  const start = parseRuDate(row.startDate);
  const end = parseRuDate(row.endDate);
  if (start && start > today) return false;
  if (end && end < new Date(today.getFullYear(), today.getMonth(), today.getDate())) return false;
  return true;
};

const formatScope = (row: UniversalListRow) => {
  if (row.scope === 'global') return 'Все ЦФ';
  if (!row.priceFormats.length) return 'Не привязан';
  const preview = row.priceFormats.slice(0, 3).map((format) => format.code).join(', ');
  return row.priceFormats.length > 3 ? `${preview} +${row.priceFormats.length - 3}` : preview;
};

const applicationState = (row: UniversalListRow) => {
  if (!row.active) return { applies: false, label: 'Не будет применен', reason: 'список выключен' };
  if (!isInPeriod(row)) return { applies: false, label: 'Не будет применен', reason: 'вне периода действия' };
  if (row.itemsCount <= 0) return { applies: false, label: 'Не будет применен', reason: 'нет товаров' };
  if (row.scope !== 'global' && row.priceFormats.length === 0) {
    return { applies: false, label: 'Не будет применен', reason: 'нет привязанных ЦФ' };
  }
  return {
    applies: true,
    label: 'Будет применен',
    reason: row.scope === 'global' ? 'для всех ЦФ' : `для ${row.priceFormats.length} ЦФ`,
  };
};

export function UniversalListsOverviewTab() {
  const [rows, setRows] = useState<UniversalListRow[]>([]);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('__all__');
  const [statusFilter, setStatusFilter] = useState('__all__');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');

  const loadRows = async () => {
    setIsLoading(true);
    setError('');
    try {
      const params = new URLSearchParams();
      if (search.trim()) params.set('search', search.trim());
      if (typeFilter !== '__all__') params.set('type', typeFilter);
      if (statusFilter !== '__all__') params.set('status', statusFilter);
      const res = await fetch(`/api/lists-management?${params.toString()}`);
      const text = await res.text();
      const data = parseJson(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить универсальные списки');
      setRows(Array.isArray(data) ? data : []);
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки универсальных списков');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void loadRows();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typeFilter, statusFilter]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadRows(), 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const stats = useMemo(() => {
    const active = rows.filter((row) => row.active).length;
    const applied = rows.filter((row) => applicationState(row).applies).length;
    const linked = rows.filter((row) => row.scope === 'global' || row.priceFormats.length > 0).length;
    const items = rows.reduce((sum, row) => sum + Number(row.itemsCount || 0), 0);
    return { active, applied, linked, items };
  }, [rows]);

  return (
    <div className="business-workspace">
      <div className="business-toolbar">
        <div>
          <h2>Универсальные списки</h2>
          <p>Универсальные списки влияют на расчет цен для выбранных ценовых форматов.</p>
        </div>
        <div className="business-actions">
          <Button variant="outline" onClick={() => void loadRows()} disabled={isLoading}>
            <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
            Обновить
          </Button>
        </div>
      </div>

      <section className="status-cards">
        <Metric label="Всего списков" value={rows.length} tone="blue" />
        <Metric label="Активные" value={stats.active} tone={stats.active ? 'green' : 'slate'} />
        <Metric label="Участвуют в generate" value={stats.applied} tone={stats.applied ? 'green' : 'amber'} />
        <Metric label="Товаров в списках" value={stats.items.toLocaleString('ru-RU')} tone="slate" />
      </section>

      <div className="business-filters">
        <div className="business-search">
          <ListChecks className="h-4 w-4" />
          <Input placeholder="Поиск по названию или коду" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <Select value={typeFilter} onValueChange={setTypeFilter}>
          <SelectTrigger><SelectValue placeholder="Тип ограничения" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все типы</SelectItem>
            {listTypeOptions.map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger><SelectValue placeholder="Статус" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все статусы</SelectItem>
            <SelectItem value="актив">Активные</SelectItem>
            <SelectItem value="неактив">Неактивные</SelectItem>
            <SelectItem value="архив">Архивные</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {error ? <div className="business-alert bad">{error}</div> : null}

      <section className="business-panel">
        <div className="panel-head">
          <h3>Контроль влияния на формирование</h3>
          <span>{rows.length} шт.</span>
        </div>
        <div className="table-scroll">
          <table className="business-table">
            <thead>
              <tr>
                <th>Список</th>
                <th>Тип ограничения</th>
                <th>Статус</th>
                <th>Период действия</th>
                <th>Товаров</th>
                <th>Привязанных ЦФ</th>
                <th>ЦФ</th>
                <th>Следующее формирование</th>
                <th>Влияние</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const state = applicationState(row);
                return (
                  <tr key={row.id}>
                    <td>
                      <strong>{row.name}</strong>
                      <div className="muted">{row.code}</div>
                    </td>
                    <td>{listTypeLabel(row.type, row.typeLabel)}</td>
                    <td><span className={`status-pill ${row.active ? 'ok' : 'muted'}`}>{row.status}</span></td>
                    <td>{row.startDate || '—'} — {row.endDate || '—'}</td>
                    <td>{Number(row.itemsCount || 0).toLocaleString('ru-RU')}</td>
                    <td>{row.scope === 'global' ? 'Все' : row.priceFormats.length}</td>
                    <td>{formatScope(row)}</td>
                    <td>
                      <span className={`status-pill ${state.applies ? 'ok' : 'warn'}`}>
                        {state.applies ? <CheckCircle2 className="h-3.5 w-3.5 mr-1" /> : null}
                        {state.label}
                      </span>
                      <div className="muted">{state.reason}</div>
                    </td>
                    <td>{listTypeImpact(row.type)}</td>
                  </tr>
                );
              })}
              {!rows.length ? (
                <tr><td colSpan={9} className="empty-cell">Универсальные списки пока не созданы</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: any; tone: 'blue' | 'green' | 'amber' | 'slate' }) {
  return (
    <div className={`status-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
