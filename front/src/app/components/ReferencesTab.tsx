import { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { FileUp, RefreshCw, Search, UploadCloud } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';

type ReferenceType = {
  code: string;
  name: string;
};

type Branch = {
  id: string;
  name: string;
};

type ReferenceStatus = {
  branchId: string;
  branchName: string;
  dataType: string;
  lastUpdatedAt: string;
  rowsCount: number;
  status: string;
  freshness: 'fresh' | 'stale' | 'error' | 'running' | 'missing';
  error: string;
};

type ReadinessMatrix = {
  columns: ReferenceType[];
  rows: Array<{
    branchId: string;
    branchName: string;
    cells: Record<string, ReferenceStatus>;
  }>;
};

type ImportJob = {
  id: number;
  dataType: string;
  branchIds: string;
  filename: string;
  sourceType: string;
  status: string;
  rowsTotal: number;
  rowsSuccess: number;
  rowsFailed: number;
  error: string;
  log: string;
  createdAt: string;
  startedAt: string;
  finishedAt: string;
  userName: string;
};

type BatchResult = {
  status: string;
  sourceType: string;
  jobs: ImportJob[];
  jobsTotal: number;
  jobsSuccess: number;
  jobsPartial: number;
  jobsError: number;
};

const parseJsonOrNull = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const freshnessClass = (freshness: ReferenceStatus['freshness']) => {
  if (freshness === 'fresh') return 'border-emerald-200 bg-emerald-50 text-emerald-800';
  if (freshness === 'running') return 'border-amber-200 bg-amber-50 text-amber-800';
  if (freshness === 'error') return 'border-red-200 bg-red-50 text-red-800';
  if (freshness === 'stale') return 'border-slate-200 bg-slate-50 text-slate-700';
  return 'border-gray-200 bg-gray-50 text-gray-500';
};

const freshnessLabel = (freshness: ReferenceStatus['freshness']) => {
  if (freshness === 'fresh') return 'актуально';
  if (freshness === 'running') return 'импорт';
  if (freshness === 'error') return 'ошибка';
  if (freshness === 'stale') return 'устарело';
  return 'нет данных';
};

const fmtDate = (value: string) => {
  if (!value) return '-';
  try {
    return new Date(value).toLocaleString('ru-RU');
  } catch {
    return value;
  }
};

const parseBranchIds = (value: string) => {
  try {
    const parsed = JSON.parse(value || '[]');
    return Array.isArray(parsed) ? parsed.map(String).join(', ') : String(value || '');
  } catch {
    return value || '';
  }
};

export function ReferencesTab({ isReadOnly = false }: { isReadOnly?: boolean }) {
  const [types, setTypes] = useState<ReferenceType[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [matrix, setMatrix] = useState<ReadinessMatrix>({ columns: [], rows: [] });
  const [imports, setImports] = useState<ImportJob[]>([]);

  const [selectedType, setSelectedType] = useState('');
  const [selectedBranchIds, setSelectedBranchIds] = useState<string[]>([]);
  const [file, setFile] = useState<File | null>(null);

  const [batchTypeIds, setBatchTypeIds] = useState<string[]>([]);
  const [batchBranchIds, setBatchBranchIds] = useState<string[]>([]);
  const [batchFiles, setBatchFiles] = useState<Record<string, File | null>>({});
  const [activeBatch, setActiveBatch] = useState<BatchResult | null>(null);

  const [historySearch, setHistorySearch] = useState('');
  const [activeJob, setActiveJob] = useState<ImportJob | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const typeName = useMemo(() => Object.fromEntries(types.map((row) => [row.code, row.name])), [types]);
  const branchName = useMemo(() => Object.fromEntries(branches.map((row) => [row.id, row.name])), [branches]);

  const loadAll = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [typesRes, branchesRes, matrixRes, importsRes] = await Promise.all([
        fetch('/api/references/types'),
        fetch('/api/references/branches'),
        fetch('/api/references/readiness-matrix'),
        fetch('/api/references/imports'),
      ]);
      const [typesText, branchesText, matrixText, importsText] = await Promise.all([
        typesRes.text(),
        branchesRes.text(),
        matrixRes.text(),
        importsRes.text(),
      ]);
      const typesData = parseJsonOrNull(typesText);
      const branchesData = parseJsonOrNull(branchesText);
      const matrixData = parseJsonOrNull(matrixText);
      const importsData = parseJsonOrNull(importsText);
      if (!typesRes.ok) throw new Error(typesData?.detail || typesText || 'Не удалось загрузить типы');
      if (!branchesRes.ok) throw new Error(branchesData?.detail || branchesText || 'Не удалось загрузить филиалы');
      if (!matrixRes.ok) throw new Error(matrixData?.detail || matrixText || 'Не удалось загрузить матрицу');
      if (!importsRes.ok) throw new Error(importsData?.detail || importsText || 'Не удалось загрузить историю');

      const nextTypes = Array.isArray(typesData) ? typesData : [];
      setTypes(nextTypes);
      setBranches(Array.isArray(branchesData) ? branchesData : []);
      setMatrix(matrixData && Array.isArray(matrixData.rows) ? matrixData : { columns: nextTypes, rows: [] });
      setImports(Array.isArray(importsData) ? importsData : []);
      setSelectedType((prev) => prev || nextTypes[0]?.code || '');
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки справочников');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void loadAll();
  }, []);

  const toggleValue = (value: string, setter: (updater: (prev: string[]) => string[]) => void) => {
    setter((prev) => (prev.includes(value) ? prev.filter((id) => id !== value) : [...prev, value]));
  };

  const upload = async () => {
    if (isReadOnly) return setError('Роль viewer: импорт справочников доступен только для просмотра');
    if (!selectedType) return setError('Выберите тип данных');
    if (!selectedBranchIds.length) return setError('Выберите один или несколько филиалов');
    if (!file) return setError('Выберите Excel-файл');

    setIsLoading(true);
    setError(null);
    setActiveJob({
      id: 0,
      dataType: selectedType,
      branchIds: JSON.stringify(selectedBranchIds),
      filename: file.name,
      sourceType: 'excel',
      status: 'running',
      rowsTotal: 0,
      rowsSuccess: 0,
      rowsFailed: 0,
      error: '',
      log: '[]',
      createdAt: '',
      startedAt: '',
      finishedAt: '',
      userName: 'UI',
    });
    try {
      const fd = new FormData();
      fd.append('file', file);
      const params = new URLSearchParams({
        data_type: selectedType,
        branch_ids: selectedBranchIds.join(','),
        user_name: 'UI',
      });
      const res = await fetch(`/api/references/import?${params.toString()}`, { method: 'POST', body: fd });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить справочник');
      setActiveJob(data);
      await loadAll();
      if (data?.status === 'error') setError(data.error || 'Импорт завершился с ошибкой');
      else if (data?.status === 'partial') toast.warning('Импорт завершен частично');
      else toast.success('Справочник загружен');
    } catch (e: any) {
      setError(e?.message || 'Ошибка импорта');
    } finally {
      setIsLoading(false);
    }
  };

  const uploadBatch = async () => {
    if (isReadOnly) return setError('Роль viewer: batch import доступен только для просмотра');
    if (!batchTypeIds.length) return setError('Выберите типы данных для batch import');
    if (!batchBranchIds.length) return setError('Выберите филиалы для batch import');
    const missingFiles = batchTypeIds.filter((type) => !batchFiles[type]);
    if (missingFiles.length) return setError(`Прикрепите Excel для: ${missingFiles.map((type) => typeName[type] || type).join(', ')}`);

    setIsLoading(true);
    setError(null);
    setActiveBatch({
      status: 'running',
      sourceType: 'excel',
      jobs: [],
      jobsTotal: batchTypeIds.length,
      jobsSuccess: 0,
      jobsPartial: 0,
      jobsError: 0,
    });
    try {
      const fd = new FormData();
      fd.append('data_types', batchTypeIds.join(','));
      fd.append('branch_ids', batchBranchIds.join(','));
      fd.append('user_name', 'UI');
      fd.append('source_type', 'excel');
      for (const type of batchTypeIds) {
        const nextFile = batchFiles[type];
        if (nextFile) fd.append('files', nextFile);
      }
      const res = await fetch('/api/references/import/batch', { method: 'POST', body: fd });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось выполнить batch import');
      setActiveBatch(data);
      await loadAll();
      if (data?.status === 'error') toast.error('Batch import завершился с ошибками');
      else if (data?.status === 'partial') toast.warning('Batch import завершен частично');
      else toast.success('Batch import завершен');
    } catch (e: any) {
      setError(e?.message || 'Ошибка batch import');
    } finally {
      setIsLoading(false);
    }
  };

  const filteredImports = useMemo(() => {
    const q = historySearch.trim().toLowerCase();
    if (!q) return imports;
    return imports.filter((row) =>
      [row.filename, row.dataType, row.status, row.userName, row.error].some((value) => String(value || '').toLowerCase().includes(q))
    );
  }, [historySearch, imports]);

  return (
    <div className="space-y-4">
      {error ? <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div> : null}
      {isReadOnly ? <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">Роль viewer: справочники доступны только для просмотра.</div> : null}

      {activeBatch ? (
        <div className="admin-card p-4">
          <div className="mb-2 flex items-center justify-between gap-3">
            <div className="text-sm font-medium text-gray-900">Batch import · {activeBatch.sourceType}</div>
            <div className="text-sm font-semibold text-blue-700">{activeBatch.status}</div>
          </div>
          <div className="grid grid-cols-2 gap-2 text-sm text-gray-700 md:grid-cols-4">
            <div>jobs: {activeBatch.jobsTotal}</div>
            <div className="text-green-700">success: {activeBatch.jobsSuccess}</div>
            <div className="text-amber-700">partial: {activeBatch.jobsPartial}</div>
            <div className="text-red-700">error: {activeBatch.jobsError}</div>
          </div>
        </div>
      ) : null}

      {activeJob ? (
        <div className="admin-card p-4">
          <div className="mb-2 flex items-center justify-between gap-3">
            <div className="text-sm font-medium text-gray-900">
              {typeName[activeJob.dataType] || activeJob.dataType} · {activeJob.filename}
            </div>
            <div className="text-sm font-semibold text-blue-700">{activeJob.status}</div>
          </div>
          <div className="grid grid-cols-2 gap-2 text-sm text-gray-700 md:grid-cols-4">
            <div>Всего: {activeJob.rowsTotal}</div>
            <div className="text-green-700">Успешно: {activeJob.rowsSuccess}</div>
            <div className="text-red-700">Ошибок: {activeJob.rowsFailed}</div>
            <div>{fmtDate(activeJob.finishedAt || activeJob.startedAt)}</div>
          </div>
        </div>
      ) : null}

      <Tabs defaultValue="upload" className="w-full">
        <TabsList className="h-auto w-full justify-start rounded-none border-b border-gray-200 bg-transparent p-0">
          <TabsTrigger value="upload" className="rounded-none border-b border-transparent px-4 py-2 data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:text-blue-700 data-[state=active]:shadow-none">
            Загрузка данных
          </TabsTrigger>
          <TabsTrigger value="status" className="rounded-none border-b border-transparent px-4 py-2 data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:text-blue-700 data-[state=active]:shadow-none">
            Матрица готовности
          </TabsTrigger>
          <TabsTrigger value="history" className="rounded-none border-b border-transparent px-4 py-2 data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:text-blue-700 data-[state=active]:shadow-none">
            История загрузок
          </TabsTrigger>
        </TabsList>

        <TabsContent value="upload" className="m-0 pt-4">
          <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(420px,1fr)_minmax(520px,1.15fr)]">
            <div className="admin-card p-4">
              <div className="mb-4 flex items-center justify-between gap-3">
                <div className="text-sm font-semibold text-gray-900">Одиночный импорт</div>
                <FileUp className="h-4 w-4 text-gray-400" />
              </div>
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-[280px_minmax(320px,1fr)] 2xl:grid-cols-1">
                <div className="space-y-4">
                  <div>
                    <div className="mb-2 text-sm font-semibold text-gray-900">Тип данных</div>
                    <Select value={selectedType} onValueChange={setSelectedType}>
                      <SelectTrigger>
                        <SelectValue placeholder="Выберите тип" />
                      </SelectTrigger>
                      <SelectContent>
                        {types.map((type) => (
                          <SelectItem key={type.code} value={type.code}>
                            {type.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <div className="mb-2 text-sm font-semibold text-gray-900">Excel-файл</div>
                    <Input type="file" accept=".xlsx" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
                  </div>
                  <Button onClick={upload} disabled={isLoading || isReadOnly} className="w-full bg-blue-600 hover:bg-blue-700">
                    <FileUp className="mr-2 h-4 w-4" />
                    Загрузить
                  </Button>
                </div>
                <BranchPicker
                  branches={branches}
                  selected={selectedBranchIds}
                  onToggle={(id) => toggleValue(id, setSelectedBranchIds)}
                  onAll={() => setSelectedBranchIds(branches.map((branch) => branch.id))}
                  onReset={() => setSelectedBranchIds([])}
                />
              </div>
            </div>

            <div className="admin-card p-4">
              <div className="mb-4 flex items-center justify-between gap-3">
                <div className="text-sm font-semibold text-gray-900">Batch Excel import</div>
                <UploadCloud className="h-4 w-4 text-gray-400" />
              </div>
              <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(260px,0.85fr)_minmax(320px,1fr)]">
                <div className="space-y-4">
                  <div>
                    <div className="mb-2 text-sm font-semibold text-gray-900">Типы данных</div>
                    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-1">
                      {types.map((type) => {
                        const checked = batchTypeIds.includes(type.code);
                        return (
                          <button
                            type="button"
                            key={type.code}
                            onClick={() => toggleValue(type.code, setBatchTypeIds)}
                            className={`rounded-md border px-3 py-2 text-left text-sm transition-colors ${
                              checked ? 'border-blue-300 bg-blue-50 text-blue-800' : 'border-gray-200 bg-white text-gray-700 hover:bg-gray-50'
                            }`}
                          >
                            {type.name}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                  <div className="space-y-3">
                    <div className="text-sm font-semibold text-gray-900">Excel по выбранным типам</div>
                    {batchTypeIds.length ? (
                      batchTypeIds.map((type) => (
                        <div key={type} className="rounded-md border border-gray-200 p-3">
                          <div className="mb-2 text-xs font-semibold text-gray-600">{typeName[type] || type}</div>
                          <Input
                            type="file"
                            accept=".xlsx"
                            onChange={(event) => setBatchFiles((prev) => ({ ...prev, [type]: event.target.files?.[0] ?? null }))}
                          />
                        </div>
                      ))
                    ) : (
                      <div className="rounded-md border border-gray-200 bg-gray-50 p-3 text-sm text-gray-500">Сначала выберите типы данных</div>
                    )}
                  </div>
                  <Button onClick={uploadBatch} disabled={isLoading || isReadOnly} className="w-full bg-blue-600 hover:bg-blue-700">
                    <UploadCloud className="mr-2 h-4 w-4" />
                    Запустить batch import
                  </Button>
                </div>
                <BranchPicker
                  branches={branches}
                  selected={batchBranchIds}
                  onToggle={(id) => toggleValue(id, setBatchBranchIds)}
                  onAll={() => setBatchBranchIds(branches.map((branch) => branch.id))}
                  onReset={() => setBatchBranchIds([])}
                />
              </div>
            </div>
          </div>
        </TabsContent>

        <TabsContent value="status" className="m-0 pt-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="flex flex-wrap gap-2 text-xs">
              {(['fresh', 'stale', 'missing', 'error', 'running'] as ReferenceStatus['freshness'][]).map((state) => (
                <span key={state} className={`rounded-md border px-2 py-1 ${freshnessClass(state)}`}>{freshnessLabel(state)}</span>
              ))}
            </div>
            <Button variant="outline" size="sm" onClick={loadAll} disabled={isLoading}>
              <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
              Обновить
            </Button>
          </div>
          <div className="admin-table-card">
            <div className="thin-scrollbar max-h-[640px] overflow-auto">
              <table className="admin-table">
                <thead className="sticky top-0 z-10">
                  <tr>
                    <th className="min-w-40 px-4 py-3 text-left text-sm font-medium text-gray-700">Филиал</th>
                    {matrix.columns.map((type) => (
                      <th key={type.code} className="min-w-44 px-4 py-3 text-left text-sm font-medium text-gray-700">
                        {type.name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {matrix.rows.map((row) => (
                    <tr key={row.branchId}>
                      <td className="whitespace-nowrap px-4 py-3 text-sm font-semibold text-gray-900">{row.branchName}</td>
                      {matrix.columns.map((type) => {
                        const cell = row.cells[type.code];
                        return (
                          <td key={type.code} className="px-3 py-2 text-sm">
                            <div className={`rounded-md border p-2 ${freshnessClass(cell?.freshness || 'missing')}`} title={cell?.error || ''}>
                              <div className="font-medium">{freshnessLabel(cell?.freshness || 'missing')}</div>
                              <div className="text-xs">{fmtDate(cell?.lastUpdatedAt || '')}</div>
                              <div className="text-xs">rows: {cell?.rowsCount || 0}</div>
                            </div>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </TabsContent>

        <TabsContent value="history" className="m-0 pt-4">
          <div className="admin-card mb-4 p-4">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
              <Input value={historySearch} onChange={(event) => setHistorySearch(event.target.value)} placeholder="Поиск по истории" className="pl-10" />
            </div>
          </div>
          <div className="admin-table-card">
            <div className="thin-scrollbar max-h-[640px] overflow-auto">
              <table className="admin-table">
                <thead className="sticky top-0 z-10">
                  <tr>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Дата</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Тип данных</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Филиалы</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Файл</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Пользователь</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Статус</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">rows_total</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">rows_success</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">rows_failed</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Ошибка</th>
                    <th className="px-4 py-3 text-right text-sm font-medium text-gray-700">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredImports.map((job) => (
                    <tr key={job.id}>
                      <td className="whitespace-nowrap px-4 py-3 text-sm text-gray-700">{fmtDate(job.createdAt)}</td>
                      <td className="px-4 py-3 text-sm text-gray-900">{typeName[job.dataType] || job.dataType}</td>
                      <td className="px-4 py-3 text-sm text-gray-700">{parseBranchIds(job.branchIds).split(', ').map((id) => branchName[id] || id).join(', ')}</td>
                      <td className="min-w-48 px-4 py-3 text-sm text-gray-700">{job.filename}</td>
                      <td className="px-4 py-3 text-sm text-gray-700">{job.userName || '-'}</td>
                      <td className="px-4 py-3 text-sm text-gray-700">{job.status}</td>
                      <td className="px-4 py-3 text-sm tabular-nums text-gray-700">{job.rowsTotal}</td>
                      <td className="px-4 py-3 text-sm tabular-nums text-green-700">{job.rowsSuccess}</td>
                      <td className="px-4 py-3 text-sm tabular-nums text-red-700">{job.rowsFailed}</td>
                      <td className="min-w-56 px-4 py-3 text-sm text-red-700">{job.error || '-'}</td>
                      <td className="px-4 py-3 text-right text-sm">
                        <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => setActiveJob(job)}>
                          Открыть лог
                        </Button>
                        <Button variant="ghost" size="sm" className="h-7 px-2 text-gray-400" disabled>
                          Скачать ошибки
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function BranchPicker({
  branches,
  selected,
  onToggle,
  onAll,
  onReset,
}: {
  branches: Branch[];
  selected: string[];
  onToggle: (id: string) => void;
  onAll: () => void;
  onReset: () => void;
}) {
  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="text-sm font-semibold text-gray-900">Филиалы / регионы</div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={onAll}>Все</Button>
          <Button variant="outline" size="sm" onClick={onReset}>Сброс</Button>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-2 2xl:grid-cols-3">
        {branches.map((branch) => {
          const checked = selected.includes(branch.id);
          return (
            <button
              type="button"
              key={branch.id}
              onClick={() => onToggle(branch.id)}
              className={`rounded-md border px-3 py-2 text-left text-sm transition-colors ${
                checked ? 'border-blue-300 bg-blue-50 text-blue-800' : 'border-gray-200 bg-white text-gray-700 hover:bg-gray-50'
              }`}
            >
              <span className="font-medium">{branch.name}</span>
              <span className="ml-2 text-xs text-gray-500">{branch.id}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
