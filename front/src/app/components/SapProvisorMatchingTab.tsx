import { useCallback, useEffect, useMemo, useState } from 'react';
import { ChevronLeft, ChevronRight, Link2, Search } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';

type SapRow = {
  productId: number;
  sapSku: string;
  sapName: string;
  sapManufacturer: string;
  dosage: string;
  form: string;
  packageCount?: number | null;
};

type Candidate = {
  sourceExternalKey?: string;
  sourceMatchKey: string;
  sourceName?: string;
  sourceManufacturer?: string;
  sourceDosageForm?: string;
  goodsId?: number;
  fullName?: string;
  dose?: string;
  dosageForm?: string;
  packageCount?: number | null;
  producer?: string;
  country?: string;
  confidence?: number | null;
  mappingConflict?: boolean;
  selectable?: boolean;
  mappedProductSku?: string;
  mappedProductName?: string;
};

type QueueResponse = {
  items: SapRow[];
  pagination: { page: number; pageSize: number; total: number; pageCount: number };
  sourceHealth: {
    complete: boolean;
    availablePlants: string[];
    missingPlants: string[];
    stalePlants: string[];
    conflictingPlants: string[];
    uniqueSkuCount?: number;
    missingIdentityCount?: number;
    ambiguousIdentityCount?: number;
  };
};

const parseJson = (text: string) => {
  try { return text ? JSON.parse(text) : null; } catch { return null; }
};

const unknown = (value?: string | number | null) => value === undefined || value === null || value === '' ? 'Не указано' : String(value);

const candidateGoodsId = (candidate: Candidate | null) => {
  const value = candidate?.goodsId ?? candidate?.sourceExternalKey;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
};

export function SapProvisorMatchingTab() {
  const [rows, setRows] = useState<SapRow[]>([]);
  const [pagination, setPagination] = useState({ page: 1, pageSize: 25, total: 0, pageCount: 0 });
  const [health, setHealth] = useState<QueueResponse['sourceHealth'] | null>(null);
  const [query, setQuery] = useState('');
  const [appliedQuery, setAppliedQuery] = useState('');
  const [selected, setSelected] = useState<SapRow | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null);
  const [manualQuery, setManualQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [loadingCandidates, setLoadingCandidates] = useState(false);
  const [error, setError] = useState('');

  const loadQueue = useCallback(async (preferredProductId?: number) => {
    setLoading(true);
    setError('');
    try {
      const params = new URLSearchParams({
        q: appliedQuery,
        page: String(pagination.page),
        pageSize: String(pagination.pageSize),
      });
      const response = await fetch(`/api/admin/sap-provisor/unmatched?${params}`);
      const text = await response.text();
      const data = parseJson(text);
      if (!response.ok) throw new Error(data?.detail || text || 'Не удалось загрузить очередь SAP');
      const nextRows = Array.isArray(data?.items) ? data.items : [];
      setRows(nextRows);
      setPagination(data.pagination);
      setHealth(data.sourceHealth);
      setSelected((current) => {
        const wanted = preferredProductId ?? current?.productId;
        return nextRows.find((row: SapRow) => row.productId === wanted) || nextRows[0] || null;
      });
    } catch (e: any) {
      setRows([]);
      setSelected(null);
      setError(e?.message || 'Не удалось загрузить очередь SAP');
    } finally {
      setLoading(false);
    }
  }, [appliedQuery, pagination.page, pagination.pageSize]);

  useEffect(() => { void loadQueue(); }, [loadQueue]);

  useEffect(() => {
    if (!selected) {
      setCandidates([]);
      setSelectedCandidate(null);
      return;
    }
    const controller = new AbortController();
    setLoadingCandidates(true);
    setCandidates([]);
    setSelectedCandidate(null);
    setManualQuery(selected.sapName);
    void fetch(`/api/admin/sap-provisor/products/${selected.productId}/candidates?limit=10`, { signal: controller.signal })
      .then(async (response) => {
        const text = await response.text();
        const data = parseJson(text);
        if (!response.ok) throw new Error(data?.detail || 'Не удалось получить кандидатов Provisor');
        const next = Array.isArray(data) ? data : [];
        setCandidates(next);
        setSelectedCandidate(next.find((row: Candidate) => !row.mappingConflict && row.selectable !== false) || null);
      })
      .catch((e) => { if (e?.name !== 'AbortError') setError(e?.message || 'Не удалось получить кандидатов Provisor'); })
      .finally(() => setLoadingCandidates(false));
    return () => controller.abort();
  }, [selected]);

  const searchOfficial = async () => {
    const normalized = manualQuery.trim();
    if (!normalized) return;
    setLoadingCandidates(true);
    setError('');
    try {
      const response = await fetch(`/api/admin/sap-provisor/search?limit=30&q=${encodeURIComponent(normalized)}`);
      const text = await response.text();
      const data = parseJson(text);
      if (!response.ok) throw new Error(data?.detail || 'Поиск Provisor завершился ошибкой');
      const next = Array.isArray(data) ? data : [];
      setCandidates(next);
      setSelectedCandidate(next.find((row: Candidate) => !row.mappingConflict && row.selectable !== false) || null);
    } catch (e: any) {
      setError(e?.message || 'Поиск Provisor завершился ошибкой');
    } finally {
      setLoadingCandidates(false);
    }
  };

  const confirm = async () => {
    const goodsId = candidateGoodsId(selectedCandidate);
    if (!selected || !goodsId) return;
    setLoading(true);
    setError('');
    try {
      const response = await fetch('/api/admin/sap-provisor/mappings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ productId: selected.productId, goodsId }),
      });
      const text = await response.text();
      const data = parseJson(text);
      if (!response.ok) throw new Error(data?.detail || text || 'Не удалось сохранить сопоставление');
      toast.success(`SAP ${selected.sapSku} сопоставлен с Provisor Goods ID ${goodsId}`);
      const currentIndex = rows.findIndex((row) => row.productId === selected.productId);
      const nextProductId = rows[currentIndex + 1]?.productId || rows[currentIndex - 1]?.productId;
      setSelectedCandidate(null);
      await loadQueue(nextProductId);
    } catch (e: any) {
      setError(e?.message || 'Не удалось сохранить сопоставление');
    } finally {
      setLoading(false);
    }
  };

  const selectedGoodsId = candidateGoodsId(selectedCandidate);
  const selectedDetails = useMemo(() => selectedCandidate ? [
    selectedCandidate.dosageForm || selectedCandidate.sourceDosageForm,
    selectedCandidate.dose,
    selectedCandidate.packageCount ? `№${selectedCandidate.packageCount}` : '',
  ].filter(Boolean).join(' · ') : '', [selectedCandidate]);

  return (
    <div className="space-y-4">
      <div className="admin-card p-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-gray-900">Очередь активных товаров SAP без сопоставления Provisor</div>
            <div className="mt-1 text-xs text-gray-500">Фильтр: <strong>Без сопоставления</strong> · уникальные SKU по всем разрешённым SAP plant, склад 1000</div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Input value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => {
              if (event.key === 'Enter') { setPagination((value) => ({ ...value, page: 1 })); setAppliedQuery(query.trim()); }
            }} placeholder="SAP SKU или наименование" className="w-72" />
            <Button variant="outline" onClick={() => { setPagination((value) => ({ ...value, page: 1 })); setAppliedQuery(query.trim()); }}>
              <Search className="mr-2 h-4 w-4" />Найти
            </Button>
            <Select value={String(pagination.pageSize)} onValueChange={(value) => setPagination((current) => ({ ...current, page: 1, pageSize: Number(value) }))}>
              <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
              <SelectContent>{[25, 50, 100].map((size) => <SelectItem key={size} value={String(size)}>{size} строк</SelectItem>)}</SelectContent>
            </Select>
          </div>
        </div>
        {health && !health.complete ? (
          <div className="mt-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            Источник SAP для сопоставления требует внимания.
            {health.missingIdentityCount ? ` Не найдены внутренние товары: ${health.missingIdentityCount}.` : ''}
            {health.ambiguousIdentityCount ? ` Неоднозначные товары: ${health.ambiguousIdentityCount}.` : ''}
          </div>
        ) : null}
        {error ? <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{error}</div> : null}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.65fr)_minmax(360px,0.75fr)]">
        <div className="admin-card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1050px] text-left text-xs">
              <thead className="border-b border-gray-200 bg-gray-50 text-gray-600">
                <tr>
                  <th className="px-3 py-2">SAP SKU</th><th className="px-3 py-2">Наименование SAP</th>
                  <th className="px-3 py-2">Производитель SAP</th><th className="px-3 py-2">Товар Provisor</th>
                  <th className="px-3 py-2">Производитель Provisor</th><th className="px-3 py-2">Страна Provisor</th>
                  <th className="px-3 py-2">Дозировка / Фасовка</th><th className="px-3 py-2">Действие</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const active = selected?.productId === row.productId;
                  const candidate = active ? selectedCandidate : null;
                  return (
                    <tr key={row.productId} className={`border-b border-gray-100 ${active ? 'bg-blue-50' : 'hover:bg-gray-50'}`}>
                      <td className="whitespace-nowrap px-3 py-2 font-mono font-semibold">{row.sapSku}</td>
                      <td className="max-w-xs px-3 py-2 font-medium text-gray-900">{row.sapName}</td>
                      <td className="px-3 py-2">{unknown(row.sapManufacturer)}</td>
                      <td className="max-w-xs px-3 py-2">{candidate?.sourceName || candidate?.fullName || 'Не выбран'}</td>
                      <td className="px-3 py-2">{unknown(candidate?.sourceManufacturer || candidate?.producer)}</td>
                      <td className="px-3 py-2">{unknown(candidate?.country)}</td>
                      <td className="px-3 py-2">{[row.form, row.dosage, row.packageCount ? `№${row.packageCount}` : ''].filter(Boolean).join(' · ') || 'Не указано'}</td>
                      <td className="px-3 py-2"><Button size="sm" variant={active ? 'default' : 'outline'} onClick={() => setSelected(row)}>Выбрать</Button></td>
                    </tr>
                  );
                })}
                {!loading && !rows.length ? <tr><td colSpan={8} className="px-4 py-10 text-center text-sm text-gray-500">Очередь пуста</td></tr> : null}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between border-t border-gray-200 px-4 py-3 text-sm text-gray-600">
            <span>{pagination.total} товаров · страница {pagination.page} из {pagination.pageCount || 1}</span>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" disabled={loading || pagination.page <= 1} onClick={() => setPagination((value) => ({ ...value, page: value.page - 1 }))}><ChevronLeft className="h-4 w-4" />Назад</Button>
              <Button size="sm" variant="outline" disabled={loading || !pagination.pageCount || pagination.page >= pagination.pageCount} onClick={() => setPagination((value) => ({ ...value, page: value.page + 1 }))}>Вперёд<ChevronRight className="h-4 w-4" /></Button>
            </div>
          </div>
        </div>

        <div className="admin-card p-4">
          {selected ? <div className="space-y-4">
            <div className="rounded-md border border-blue-200 bg-blue-50 p-3">
              <div className="text-xs font-semibold uppercase text-blue-800">Товар SAP</div>
              <div className="mt-2 font-mono text-sm font-semibold">{selected.sapSku}</div>
              <div className="mt-1 font-semibold text-gray-900">{selected.sapName}</div>
              <div className="mt-1 text-xs text-gray-600">Производитель: {unknown(selected.sapManufacturer)}</div>
              <div className="text-xs text-gray-600">{[selected.form, selected.dosage, selected.packageCount ? `№${selected.packageCount}` : ''].filter(Boolean).join(' · ') || 'Характеристики не указаны'}</div>
            </div>

            <div>
              <div className="mb-2 text-sm font-semibold">Поиск в официальном каталоге Provisor</div>
              <div className="flex gap-2">
                <Input value={manualQuery} onChange={(event) => setManualQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void searchOfficial(); }} placeholder="Название, дозировка, производитель или Goods ID" />
                <Button variant="outline" disabled={loadingCandidates} onClick={() => void searchOfficial()}><Search className="h-4 w-4" /></Button>
              </div>
            </div>

            <div className="max-h-[430px] overflow-auto rounded-md border border-gray-200">
              {loadingCandidates ? <div className="p-5 text-center text-sm text-gray-500">Поиск кандидатов...</div> : candidates.length ? candidates.map((candidate) => (
                <button key={candidate.sourceMatchKey} type="button" disabled={candidate.mappingConflict || candidate.selectable === false} onClick={() => setSelectedCandidate(candidate)}
                  className={`block w-full border-b border-gray-100 p-3 text-left text-sm hover:bg-gray-50 disabled:bg-red-50 ${selectedCandidate?.sourceMatchKey === candidate.sourceMatchKey ? 'bg-blue-50' : ''}`}>
                  <div className="flex items-start justify-between gap-2"><strong>{candidate.sourceName || candidate.fullName || 'Без наименования'}</strong><span className="whitespace-nowrap text-xs text-gray-500">ID {candidate.sourceExternalKey}</span></div>
                  <div className="mt-1 text-xs text-gray-600">{[candidate.dosageForm || candidate.sourceDosageForm, candidate.dose, candidate.packageCount ? `№${candidate.packageCount}` : ''].filter(Boolean).join(' · ') || 'Характеристики не указаны'}</div>
                  <div className="mt-1 text-xs text-gray-600">Производитель: {unknown(candidate.sourceManufacturer || candidate.producer)} · Страна: {unknown(candidate.country)}</div>
                  {candidate.mappingConflict ? <div className="mt-2 text-xs font-semibold text-red-800">Уже сопоставлен: {candidate.mappedProductSku} {candidate.mappedProductName}</div> : null}
                </button>
              )) : <div className="p-5 text-center text-sm text-gray-500">Кандидаты не найдены. Измените поисковый запрос.</div>}
            </div>

            {selectedCandidate ? <div className="rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-900">
              <div className="font-semibold">Выбран Provisor Goods ID {selectedGoodsId}</div>
              <div className="mt-1">{selectedCandidate.sourceName || selectedCandidate.fullName}</div>
              <div className="text-xs">{selectedDetails || 'Характеристики не указаны'} · {unknown(selectedCandidate.sourceManufacturer || selectedCandidate.producer)} · {unknown(selectedCandidate.country)}</div>
            </div> : null}
            <Button className="w-full" disabled={loading || loadingCandidates || !selectedGoodsId || selectedCandidate?.mappingConflict} onClick={() => void confirm()}>
              <Link2 className="mr-2 h-4 w-4" />Сопоставить
            </Button>
            <p className="text-xs text-gray-500">Сопоставление сохраняется только после явного подтверждения. Goods ID повторно проверяется по официальному каталогу.</p>
          </div> : <div className="flex min-h-[360px] items-center justify-center text-center text-sm text-gray-500">Выберите товар SAP в таблице</div>}
        </div>
      </div>
    </div>
  );
}
