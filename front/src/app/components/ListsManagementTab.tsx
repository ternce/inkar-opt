import { useEffect, useMemo, useState } from 'react';
import { Archive, Copy, Download, FileUp, Pencil, Plus, Search, Trash2 } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from './ui/dialog';
import { listTypeLabel, listTypeOptions } from './listTypeLabels';

type PriceFormat = {
  code: string;
  name: string;
  branch: string;
};

type ListRow = {
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

type ListCard = ListRow & {
  items: Array<{
    sku: string;
    name: string;
    manufacturer: string;
    value: number | null;
    comment: string;
  }>;
};

type ImportSummary = {
  total_rows?: number;
  processed?: number;
  not_found?: number;
  duplicates?: number;
  errors?: number;
  empty_rows?: number;
  invalid_rows?: number;
};

type ImportIssue = {
  row?: number;
  code?: string;
  message?: string;
  identifier?: string;
  field?: string;
};

type ImportResult = {
  list_id?: number;
  list_type: string;
  filename: string;
  item_count: number;
  summary?: ImportSummary;
  errors?: ImportIssue[];
};

type Props = {
  priceFormats?: PriceFormat[];
  selectedFormatCode?: string;
};

const parseJson = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const formatScope = (row: ListRow) => {
  if (row.scope === 'global') return 'Глобально';
  if (!row.priceFormats.length) return 'Не привязан';
  return row.priceFormats.map((format) => format.code).join(', ');
};

const formatFileSize = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
};

const isExcelFile = (file: File) => /\.(xlsx|xls)$/i.test(file.name);

export function ListsManagementTab({ priceFormats = [], selectedFormatCode = '' }: Props) {
  const [rows, setRows] = useState<ListRow[]>([]);
  const [opened, setOpened] = useState<ListCard | null>(null);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('__all__');
  const [statusFilter, setStatusFilter] = useState('__all__');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<ListRow | null>(null);
  const [form, setForm] = useState({
    name: '',
    code: '',
    type: 'fixed_price',
    active: true,
    startDate: '',
    endDate: '',
    formatCodes: [] as string[],
  });
  const [newItem, setNewItem] = useState({ sku: '', value: '' });
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importStatus, setImportStatus] = useState('');
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [importErrors, setImportErrors] = useState<ImportIssue[]>([]);
  const [isImporting, setIsImporting] = useState(false);

  const selectedCodes = useMemo(() => new Set(form.formatCodes), [form.formatCodes]);

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
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить списки');
      setRows(Array.isArray(data) ? data : []);
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки списков');
    } finally {
      setIsLoading(false);
    }
  };

  const openCard = async (id: number) => {
    setIsLoading(true);
    setError('');
    if (opened?.id !== id) {
      setImportFile(null);
      setImportStatus('');
      setImportResult(null);
      setImportErrors([]);
    }
    try {
      const res = await fetch(`/api/lists-management/${id}`);
      const text = await res.text();
      const data = parseJson(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось открыть список');
      setOpened(data);
    } catch (e: any) {
      setError(e?.message || 'Ошибка открытия списка');
    } finally {
      setIsLoading(false);
    }
  };

  const openEditor = (row?: ListRow) => {
    setEditing(row || null);
    setForm({
      name: row?.name || '',
      code: row?.code || '',
      type: row?.type || 'fixed_price',
      active: row?.active ?? true,
      startDate: row?.startDate || '',
      endDate: row?.endDate || '',
      formatCodes: row?.priceFormats.map((format) => format.code) || (selectedFormatCode ? [selectedFormatCode] : []),
    });
    setEditorOpen(true);
  };

  const saveList = async () => {
    const url = editing ? `/api/lists-management/${editing.id}` : '/api/lists-management';
    const res = await fetch(url, {
      method: editing ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(form),
    });
    const text = await res.text();
    const data = parseJson(text);
    if (!res.ok) {
      setError(data?.detail || text || 'Не удалось сохранить список');
      return;
    }
    setEditorOpen(false);
    await loadRows();
    if (editing) await openCard(editing.id);
  };

  const copyList = async (row: ListRow) => {
    await fetch(`/api/lists-management/${row.id}/copy`, { method: 'POST' });
    await loadRows();
  };

  const archiveList = async (row: ListRow) => {
    await fetch(`/api/lists-management/${row.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'Архивный', active: false }),
    });
    await loadRows();
  };

  const deleteList = async (row: ListRow) => {
    await fetch(`/api/universal-lists/${row.id}`, { method: 'DELETE' });
    if (opened?.id === row.id) setOpened(null);
    await loadRows();
  };

  const handleImportFile = (file: File | null) => {
    setImportResult(null);
    setImportErrors([]);
    setImportStatus('');
    if (!file) {
      setImportFile(null);
      return;
    }
    if (!isExcelFile(file)) {
      setImportFile(null);
      setImportStatus('Выберите файл Excel в формате .xlsx или .xls');
      return;
    }
    setImportFile(file);
  };

  const importExcelList = async () => {
    setImportResult(null);
    setImportErrors([]);
    if (!opened) {
      setImportStatus('Откройте список для импорта Excel');
      return;
    }
    if (!importFile) {
      setImportStatus('Выберите Excel-файл');
      return;
    }
    if (!isExcelFile(importFile)) {
      setImportStatus('Поддерживаются только файлы .xlsx и .xls');
      return;
    }
    const fd = new FormData();
    fd.append('file', importFile);
    setIsImporting(true);
    setImportStatus('Загрузка файла...');
    try {
      const listId = opened.id;
      const res = await fetch(`/api/lists-management/${listId}/import-excel`, { method: 'POST', body: fd });
      const text = await res.text();
      const data = parseJson(text);
      if (!res.ok) {
        const detail = data?.detail || text || 'Не удалось импортировать Excel';
        setImportStatus(String(detail));
        setImportErrors(Array.isArray(data?.errors) ? data.errors : []);
        return;
      }
      setImportResult(data);
      setImportErrors(Array.isArray(data?.errors) ? data.errors : []);
      setImportStatus('Импорт завершен');
      await loadRows();
      await openCard(listId);
    } catch (e: any) {
      setImportStatus(e?.message || 'Ошибка импорта Excel');
    } finally {
      setIsImporting(false);
    }
  };

  const addItem = async () => {
    if (!opened || !newItem.sku.trim()) return;
    const res = await fetch(`/api/lists-management/${opened.id}/items`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sku: newItem.sku.trim(), value: Number(newItem.value || 0) }),
    });
    if (res.ok) {
      setNewItem({ sku: '', value: '' });
      await openCard(opened.id);
    } else {
      const text = await res.text();
      setError(parseJson(text)?.detail || text || 'Не удалось добавить товар');
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

  return (
    <div className="business-workspace">
      <div className="business-toolbar">
        <div>
          <h2>Работа со списками</h2>
          <p>Фиксированные цены, ограничения наценок, исключения и специальные правила для ЦФ.</p>
        </div>
        <div className="business-actions">
          <Button variant="outline" onClick={() => void loadRows()} disabled={isLoading}>Обновить</Button>
          <Button onClick={() => openEditor()}>
            <Plus className="h-4 w-4 mr-2" />
            Создать список
          </Button>
        </div>
      </div>

      <div className="business-filters">
        <div className="business-search">
          <Search className="h-4 w-4" />
          <Input placeholder="Поиск по названию или коду" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <Select value={typeFilter} onValueChange={setTypeFilter}>
          <SelectTrigger><SelectValue placeholder="Тип списка" /></SelectTrigger>
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

      {error && <div className="business-alert bad">{error}</div>}

      <div className="business-grid two-columns">
        <section className="business-panel">
          <div className="panel-head">
            <h3>Списки</h3>
            <span>{rows.length} шт.</span>
          </div>
          <div className="table-scroll">
            <table className="business-table">
              <thead>
                <tr>
                  <th>Наименование</th>
                  <th>Тип списка</th>
                  <th>Активен</th>
                  <th>Товаров</th>
                  <th>Привязанные ЦФ</th>
                  <th>Дата начала</th>
                  <th>Дата окончания</th>
                  <th>Последнее изменение</th>
                  <th>Комментарий</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className={opened?.id === row.id ? 'active-row' : ''}>
                    <td>
                      <button className="link-button" onClick={() => void openCard(row.id)}>{row.name}</button>
                      <div className="muted">{row.code}</div>
                    </td>
                    <td>{listTypeLabel(row.type, row.typeLabel)}</td>
                    <td><span className={`status-pill ${row.active ? 'ok' : 'muted'}`}>{row.status}</span></td>
                    <td>{row.itemsCount}</td>
                    <td>{formatScope(row)}</td>
                    <td>{row.startDate || '—'}</td>
                    <td>{row.endDate || '—'}</td>
                    <td>{row.updatedAt || '—'}</td>
                    <td>{row.comment || '—'}</td>
                    <td>
                      <div className="row-actions">
                        <Button variant="ghost" size="sm" onClick={() => void openCard(row.id)}>Открыть</Button>
                        <Button variant="ghost" size="sm" onClick={() => openEditor(row)}><Pencil className="h-4 w-4" /></Button>
                        <Button variant="ghost" size="sm" onClick={() => void copyList(row)}><Copy className="h-4 w-4" /></Button>
                        <Button variant="ghost" size="sm" onClick={() => void archiveList(row)}><Archive className="h-4 w-4" /></Button>
                        <Button variant="ghost" size="sm" onClick={() => void deleteList(row)}><Trash2 className="h-4 w-4" /></Button>
                      </div>
                    </td>
                  </tr>
                ))}
                {!rows.length && (
                  <tr><td colSpan={10} className="empty-cell">Списки пока не созданы</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="business-panel">
          <div className="panel-head">
            <h3>Карточка списка</h3>
            {opened && (
              <div className="business-actions">
                <Button variant="outline" size="sm" onClick={() => { window.location.href = `/api/lists-management/${opened.id}/export.csv`; }}>
                  <Download className="h-4 w-4 mr-2" />CSV
                </Button>
                <Button variant="outline" size="sm" onClick={() => { window.location.href = `/api/lists-management/${opened.id}/export.xlsx`; }}>
                  <Download className="h-4 w-4 mr-2" />XLSX
                </Button>
              </div>
            )}
          </div>
          {!opened ? (
            <div className="empty-state">Откройте список, чтобы увидеть параметры, товары и привязку к ЦФ.</div>
          ) : (
            <>
              <div className="details-grid">
                <div><span>Наименование</span><strong>{opened.name}</strong></div>
                <div><span>Код</span><strong>{opened.code}</strong></div>
                <div><span>Тип</span><strong>{opened.typeLabel || opened.type}</strong></div>
                <div><span>Активность</span><strong>{opened.status}</strong></div>
                <div><span>Период</span><strong>{opened.startDate || '—'} — {opened.endDate || '—'}</strong></div>
                <div><span>Привязка</span><strong>{formatScope(opened)}</strong></div>
              </div>
              <div className="import-row">
                <label className="file-button">
                  <FileUp className="h-4 w-4" />
                  Выбрать Excel
                  <input type="file" accept=".xlsx,.xls" onChange={(e) => handleImportFile(e.target.files?.[0] || null)} />
                </label>
                <Button onClick={() => void importExcelList()} disabled={!importFile || isImporting}>
                  {isImporting ? 'Импорт...' : 'Импортировать'}
                </Button>
                {importFile && <span className="muted">{importFile.name} · {formatFileSize(importFile.size)}</span>}
              </div>
              {importStatus && <div className={`business-alert ${importResult ? 'ok' : importStatus === 'Загрузка файла...' ? '' : 'bad'}`}>{importStatus}</div>}
              {importResult && (
                <div className="details-grid">
                  <div><span>total_rows</span><strong>{importResult.summary?.total_rows ?? 0}</strong></div>
                  <div><span>processed</span><strong>{importResult.summary?.processed ?? 0}</strong></div>
                  <div><span>not_found</span><strong>{importResult.summary?.not_found ?? 0}</strong></div>
                  <div><span>duplicates</span><strong>{importResult.summary?.duplicates ?? 0}</strong></div>
                  <div><span>errors</span><strong>{importResult.summary?.errors ?? 0}</strong></div>
                </div>
              )}
              {!!importErrors.length && (
                <div className="table-scroll compact">
                  <table className="business-table">
                    <thead>
                      <tr>
                        <th>Строка</th>
                        <th>Ошибка</th>
                        <th>Идентификатор</th>
                        <th>Поле</th>
                      </tr>
                    </thead>
                    <tbody>
                      {importErrors.slice(0, 20).map((issue, index) => (
                        <tr key={`${issue.row || 0}-${issue.code || 'error'}-${index}`}>
                          <td>{issue.row ?? '—'}</td>
                          <td>{issue.message || issue.code || 'Ошибка валидации'}</td>
                          <td>{issue.identifier || '—'}</td>
                          <td>{issue.field || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="import-row">
                <Input placeholder="SKU" value={newItem.sku} onChange={(e) => setNewItem((prev) => ({ ...prev, sku: e.target.value }))} />
                <Input placeholder="Значение правила" value={newItem.value} onChange={(e) => setNewItem((prev) => ({ ...prev, value: e.target.value }))} />
                <Button variant="outline" onClick={() => void addItem()}>Добавить товар</Button>
              </div>
              <div className="table-scroll compact">
                <table className="business-table">
                  <thead>
                    <tr>
                      <th>SKU</th>
                      <th>Наименование</th>
                      <th>Производитель</th>
                      <th>Значение правила</th>
                      <th>Комментарий</th>
                    </tr>
                  </thead>
                  <tbody>
                    {opened.items.map((item) => (
                      <tr key={item.sku}>
                        <td>{item.sku}</td>
                        <td>{item.name}</td>
                        <td>{item.manufacturer || '—'}</td>
                        <td>{item.value ?? '—'}</td>
                        <td>{item.comment || '—'}</td>
                      </tr>
                    ))}
                    {!opened.items.length && <tr><td colSpan={5} className="empty-cell">В списке пока нет товаров</td></tr>}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </section>
      </div>

      <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{editing ? 'Редактировать список' : 'Создать список'}</DialogTitle>
          </DialogHeader>
          <div className="editor-grid">
            <label>Наименование<Input value={form.name} onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))} /></label>
            <label>Код<Input value={form.code} onChange={(e) => setForm((prev) => ({ ...prev, code: e.target.value }))} /></label>
            <label>
              Тип
              <Select value={form.type} onValueChange={(value) => setForm((prev) => ({ ...prev, type: value }))}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>{listTypeOptions.map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent>
              </Select>
            </label>
            <label>Дата начала<Input type="date" value={form.startDate} onChange={(e) => setForm((prev) => ({ ...prev, startDate: e.target.value }))} /></label>
            <label>Дата окончания<Input type="date" value={form.endDate} onChange={(e) => setForm((prev) => ({ ...prev, endDate: e.target.value }))} /></label>
            <label className="checkbox-line"><input type="checkbox" checked={form.active} onChange={(e) => setForm((prev) => ({ ...prev, active: e.target.checked }))} />Активен</label>
          </div>
          <div className="format-picker">
            <div className="format-picker-head">
              <strong>Привязанные ЦФ</strong>
              <Button variant="ghost" size="sm" onClick={() => setForm((prev) => ({ ...prev, formatCodes: [] }))}>Глобально</Button>
            </div>
            <div className="format-chip-grid">
              {priceFormats.map((format) => (
                <label key={format.code} className={`format-chip ${selectedCodes.has(format.code) ? 'selected' : ''}`}>
                  <input
                    type="checkbox"
                    checked={selectedCodes.has(format.code)}
                    onChange={(e) => {
                      setForm((prev) => ({
                        ...prev,
                        formatCodes: e.target.checked
                          ? [...prev.formatCodes, format.code]
                          : prev.formatCodes.filter((code) => code !== format.code),
                      }));
                    }}
                  />
                  <span>{format.code}</span>
                  <small>{format.branch}</small>
                </label>
              ))}
            </div>
          </div>
          <div className="dialog-actions">
            <Button variant="outline" onClick={() => setEditorOpen(false)}>Отмена</Button>
            <Button onClick={() => void saveList()}>Сохранить</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}