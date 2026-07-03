import { useEffect, useMemo, useState } from 'react';
import { Archive, FileUp, Pencil, Search } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';

type ContractorRow = {
  id: number;
  holding: string;
  counterparty: string;
  pharmacy: string;
  region: string;
  branch: string;
  formatCode: string;
  formatName: string;
  status: string;
  updatedAt: string;
  selectedSources: number;
  latestPriceList: string;
  latestPriceListDate: string;
  references: Array<{ type: string; status: string; lastUpdatedAt: string; rows: number }>;
};

type ContractorCard = ContractorRow & {
  priceFormats: Array<{ code: string; name: string; branch: string }>;
  assignments: Array<{
    source: string;
    competitor: string;
    region: string;
    login: string;
    coefficient: number;
    active: boolean;
  }>;
  recentPriceLists: Array<{ number: string; format: string; branch: string; date: string; status: string; skuCount: number }>;
};

type PriceFormat = {
  code: string;
  name: string;
  branch: string;
};

type Props = {
  branch?: string;
  selectedFormatCode?: string;
  priceFormats?: PriceFormat[];
  onNavigate?: (section: any) => void;
};

const parseJson = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const statusText = (status: string) => {
  const value = String(status || '').toLowerCase();
  if (value === 'active') return 'Активен';
  if (value === 'inactive') return 'Неактивен';
  if (value === 'success') return 'Успешно';
  if (value === 'partial') return 'Частично';
  if (value === 'error') return 'Ошибка';
  if (value === 'running') return 'Выполняется';
  if (value === 'updated') return 'Обновлено';
  if (value === 'checked') return 'Проверено';
  if (value === 'timeout') return 'Тайм-аут';
  return status || '—';
};

export function ContractorsTab({ branch = '', selectedFormatCode = '', priceFormats = [], onNavigate }: Props) {
  const [rows, setRows] = useState<ContractorRow[]>([]);
  const [opened, setOpened] = useState<ContractorCard | null>(null);
  const [search, setSearch] = useState('');
  const [branchFilter, setBranchFilter] = useState(branch || '__all__');
  const [formatFilter, setFormatFilter] = useState(selectedFormatCode || '__all__');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');

  const branchOptions = useMemo(
    () => Array.from(new Set([...(priceFormats || []).map((format) => format.branch), ...rows.map((row) => row.branch)].filter(Boolean))).sort((a, b) => a.localeCompare(b, 'ru')),
    [priceFormats, rows]
  );
  const formatOptions = useMemo(
    () => Array.from(new Set([...(priceFormats || []).map((format) => format.code), ...rows.map((row) => row.formatCode)].filter(Boolean))).sort(),
    [priceFormats, rows]
  );

  const loadRows = async () => {
    setIsLoading(true);
    setError('');
    try {
      const params = new URLSearchParams();
      if (search.trim()) params.set('search', search.trim());
      if (branchFilter !== '__all__') params.set('branch', branchFilter);
      if (formatFilter !== '__all__') params.set('format_code', formatFilter);
      const res = await fetch(`/api/contractors?${params.toString()}`);
      const text = await res.text();
      const data = parseJson(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить контрагентов');
      setRows(Array.isArray(data) ? data : []);
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки контрагентов');
    } finally {
      setIsLoading(false);
    }
  };

  const openCard = async (id: number) => {
    setIsLoading(true);
    setError('');
    try {
      const res = await fetch(`/api/contractors/${id}`);
      const text = await res.text();
      const data = parseJson(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось открыть контрагента');
      setOpened(data);
    } catch (e: any) {
      setError(e?.message || 'Ошибка открытия контрагента');
    } finally {
      setIsLoading(false);
    }
  };

  const importContractors = async (file: File | null) => {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch('/api/contractors/import', { method: 'POST', body: fd });
    if (res.ok) await loadRows();
    else setError('Не удалось импортировать Excel');
  };

  useEffect(() => {
    void loadRows();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [branchFilter, formatFilter]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadRows(), 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  return (
    <div className="business-workspace">
      <div className="business-toolbar">
        <div>
          <h2>Контрагенты</h2>
          <p>Упрощенная ERP-структура: холдинг → контрагент → аптека/точка → филиал → ЦФ.</p>
        </div>
        <div className="business-actions">
          <label className="file-button">
            <FileUp className="h-4 w-4" />
            Импорт Excel
            <input type="file" accept=".xlsx,.xls" onChange={(e) => void importContractors(e.target.files?.[0] || null)} />
          </label>
          <Button variant="outline" onClick={() => void loadRows()} disabled={isLoading}>Обновить</Button>
        </div>
      </div>

      <div className="business-filters">
        <div className="business-search">
          <Search className="h-4 w-4" />
          <Input placeholder="Поиск по холдингу, контрагенту, аптеке или ЦФ" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <Select value={branchFilter} onValueChange={setBranchFilter}>
          <SelectTrigger><SelectValue placeholder="Филиал" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все филиалы</SelectItem>
            {branchOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={formatFilter} onValueChange={setFormatFilter}>
          <SelectTrigger><SelectValue placeholder="ЦФ" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">Все ЦФ</SelectItem>
            {formatOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>

      {error && <div className="business-alert bad">{error}</div>}

      <div className="business-grid two-columns">
        <section className="business-panel">
          <div className="panel-head">
            <h3>Структура контрагентов</h3>
            <span>{rows.length} строк</span>
          </div>
          <div className="table-scroll">
            <table className="business-table">
              <thead>
                <tr>
                  <th>Холдинг</th>
                  <th>Контрагент</th>
                  <th>Аптека/точка</th>
                  <th>Регион</th>
                  <th>Филиал</th>
                  <th>ЦФ</th>
                  <th>Статус</th>
                  <th>Последнее обновление</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className={opened?.id === row.id ? 'active-row' : ''}>
                    <td>{row.holding || '—'}</td>
                    <td>{row.counterparty || '—'}</td>
                    <td>{row.pharmacy || '—'}</td>
                    <td>{row.region || '—'}</td>
                    <td>{row.branch || '—'}</td>
                    <td>
                      <button className="link-button" onClick={() => void openCard(row.id)}>{row.formatCode}</button>
                      <div className="muted">{row.formatName}</div>
                    </td>
                    <td><span className={`status-pill ${row.status === 'active' ? 'ok' : 'muted'}`}>{statusText(row.status)}</span></td>
                    <td>{row.updatedAt || '—'}</td>
                    <td>
                      <div className="row-actions">
                        <Button variant="ghost" size="sm" onClick={() => void openCard(row.id)}>Открыть</Button>
                        <Button variant="ghost" size="sm" disabled><Pencil className="h-4 w-4" /></Button>
                        <Button variant="ghost" size="sm" disabled><Archive className="h-4 w-4" /></Button>
                      </div>
                    </td>
                  </tr>
                ))}
                {!rows.length && <tr><td colSpan={9} className="empty-cell">Контрагенты пока не загружены</td></tr>}
              </tbody>
            </table>
          </div>
        </section>

        <section className="business-panel">
          <div className="panel-head">
            <h3>Карточка контрагента</h3>
          </div>
          {!opened ? (
            <div className="empty-state">Откройте строку, чтобы увидеть ЦФ, ПЛК, последние прайсы и статус справочников.</div>
          ) : (
            <>
              <div className="details-grid">
                <div><span>Холдинг</span><strong>{opened.holding || '—'}</strong></div>
                <div><span>Контрагент</span><strong>{opened.counterparty || '—'}</strong></div>
                <div><span>Аптека / точка</span><strong>{opened.pharmacy || '—'}</strong></div>
                <div><span>Филиал</span><strong>{opened.branch || '—'}</strong></div>
                <div><span>Привязанные ЦФ</span><strong>{opened.priceFormats.map((format) => format.code).join(', ') || '—'}</strong></div>
                <div><span>Назначенные ПЛК</span><strong>{opened.assignments?.length || opened.selectedSources || 0}</strong></div>
              </div>

              <div className="mini-section">
                <div className="panel-head compact"><h4>Назначенные ПЛК</h4><Button variant="ghost" size="sm" onClick={() => onNavigate?.('competitors')}>Настроить ПЛК</Button></div>
                <div className="table-scroll compact">
                  <table className="business-table">
                    <thead><tr><th>Источник</th><th>Конкурент</th><th>Регион</th><th>Логин</th><th>Коэффициент</th><th>Активен</th></tr></thead>
                    <tbody>
                      {(opened.assignments || []).map((row, idx) => (
                        <tr key={`${row.source}-${idx}`}>
                          <td>{row.source}</td>
                          <td>{row.competitor || '—'}</td>
                          <td>{row.region || '—'}</td>
                          <td>{row.login || '—'}</td>
                          <td>{row.coefficient}</td>
                          <td>{row.active ? 'Да' : 'Нет'}</td>
                        </tr>
                      ))}
                      {!opened.assignments?.length && <tr><td colSpan={6} className="empty-cell">Нет назначенных ПЛК</td></tr>}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="mini-section">
                <div className="panel-head compact"><h4>Последние прайсы</h4><Button variant="ghost" size="sm" onClick={() => onNavigate?.('pricelists')}>Открыть раздел</Button></div>
                <div className="table-scroll compact">
                  <table className="business-table">
                    <thead><tr><th>Номер</th><th>Дата</th><th>Статус</th><th>SKU</th></tr></thead>
                    <tbody>
                      {(opened.recentPriceLists || []).map((row) => (
                        <tr key={row.number}>
                          <td>{row.number}</td>
                          <td>{row.date}</td>
                          <td>{statusText(row.status)}</td>
                          <td>{row.skuCount}</td>
                        </tr>
                      ))}
                      {!opened.recentPriceLists?.length && <tr><td colSpan={4} className="empty-cell">Для ЦФ пока нет сформированных прайсов</td></tr>}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="mini-section">
                <div className="panel-head compact"><h4>Справочники</h4><Button variant="ghost" size="sm" onClick={() => onNavigate?.('references')}>Обновить</Button></div>
                <div className="status-list">
                  {(opened.references || []).map((row) => (
                    <div key={row.type}>
                      <span>{row.type}</span>
                      <strong>{statusText(row.status)}</strong>
                      <small>{row.lastUpdatedAt || '—'} · {row.rows} строк</small>
                    </div>
                  ))}
                  {!opened.references?.length && <div className="empty-state slim">Нет статуса справочников для выбранного филиала</div>}
                </div>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
