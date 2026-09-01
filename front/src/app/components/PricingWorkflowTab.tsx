import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { toast } from 'sonner';
import { Download, FileText, Play, RefreshCw } from 'lucide-react';
import { Button } from './ui/button';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog';
import { Input } from './ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { StickyTableToolbar } from './StickyTableToolbar';
import { formatDateTimeKz } from '../timezone';
import { shouldPollPercentilePreparation, type PercentilePreparation } from '../percentilePreparationStatus';
import { SUPPORTED_CITIES, isSupportedCity } from '../supportedCities';

type PriceFormat = {
  id?: string | number;
  code: string;
  name: string;
  branch: string;
};

type BranchFormatRow = {
  id: number;
  code: string;
  name: string;
  branch: string;
  pricingRule: string;
  pricingRuleId: number | null;
  assignedPlkCount: number;
  lastGeneratedAt: string;
  lastActivationDate: string;
  lastRunStatus: string;
  lastPriceListNumber: string;
  lastSkuCount: number;
  sapCategory?: string;
};

type ReadinessItem = {
  kind: string;
  label: string;
  status: 'ok' | 'warning' | 'error';
  message: string;
};

type FormatReadiness = {
  formatCode: string;
  formatName: string;
  status: 'ok' | 'warning' | 'error';
  canGenerate: boolean;
  items: ReadinessItem[];
  errors: ReadinessItem[];
  warnings: ReadinessItem[];
  percentilePreparation?: PercentilePreparation;
};

type GeneratedPriceList = {
  id: number;
  number: string;
  format: string;
  formatName: string;
  branch: string;
  date: string;
  createdAt: string;
  activationDate: string;
  user: string;
  status: string;
  skuCount: number;
};

type BatchItem = {
  formatCode: string;
  formatName?: string;
  status: 'queued' | 'running' | 'success' | 'warning' | 'failed' | 'cancelled';
  progress: number;
  message: string;
  error?: string;
  workflowRunId?: number;
  priceListNumber?: string;
};

type SapExportMode = 'auto' | 'manual';

type SapVersion = {
  workflow_run_id: number;
  price_list_id: number;
  price_list_number: string;
  started_at?: string | null;
  finished_at?: string | null;
  is_latest: boolean;
};

type SapFormatVersions = {
  price_format_id: number;
  code: string;
  name: string;
  sap_category: string | null;
  versions: SapVersion[];
};

type Props = {
  selectedFormatCode?: string;
  branch: string;
  priceFormats: PriceFormat[];
  onFormatChange: (format: PriceFormat) => void;
  onNavigate: (section: any) => void;
  onOpenPriceList?: (priceListNumber: string) => void;
  onOpenAnalytics?: (priceListNumber: string) => void;
  isReadOnly?: boolean;
};

type PriceRow = {
  format: BranchFormatRow;
  readiness?: FormatReadiness;
  priceList?: GeneratedPriceList;
  versionCount: number;
  canGenerate: boolean;
  canExport: boolean;
};

const parseJsonOrNull = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const apiErrorMessage = (data: any, text: string, fallback: string) => {
  const detail = data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail?.message) return String(detail.message);
  return text || fallback;
};

const branchKey = (value: any) => String(value || '').trim().toLocaleLowerCase('ru-RU');
const isSameBranch = (left: any, right: any) => branchKey(left) === branchKey(right);

const toInputDate = (date: Date) => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
};

const tomorrowInputDate = () => {
  const date = new Date();
  date.setDate(date.getDate() + 1);
  return toInputDate(date);
};

const inputDateToDisplayDate = (value: string) => {
  if (!value) return '';
  const [year, month, day] = value.split('-');
  if (!year || !month || !day) return value;
  return `${day}.${month}.${year}`;
};

const fmtDate = (value: any) => {
  if (!value) return '—';
  return formatDateTimeKz(value);
};

const readinessText = (row?: FormatReadiness) => {
  if (!row) return 'Проверяется';
  if (row.status === 'ok') return 'Готово';
  if (row.status === 'warning') return 'Есть предупреждения';
  return 'Не готово';
};

const readinessClassName = (row?: FormatReadiness) => {
  if (!row) return 'warn';
  if (row.status === 'ok') return 'ok';
  if (row.status === 'error') return 'bad';
  return 'warn';
};

export function PricingWorkflowTab({
  selectedFormatCode,
  branch,
  priceFormats,
  onFormatChange,
  onNavigate,
  onOpenPriceList,
  isReadOnly = false,
}: Props) {
  const [selectedBranch, setSelectedBranch] = useState(branch || '');
  const [activationDate, setActivationDate] = useState(tomorrowInputDate);
  const [formats, setFormats] = useState<BranchFormatRow[]>([]);
  const [readiness, setReadiness] = useState<FormatReadiness[]>([]);
  const [priceLists, setPriceLists] = useState<GeneratedPriceList[]>([]);
  const [generateCodes, setGenerateCodes] = useState<string[]>([]);
  const [exportCodes, setExportCodes] = useState<string[]>([]);
  const [batchItems, setBatchItems] = useState<BatchItem[]>([]);
  const [sapDialogOpen, setSapDialogOpen] = useState(false);
  const [sapMode, setSapMode] = useState<SapExportMode>('auto');
  const [sapVersions, setSapVersions] = useState<SapFormatVersions[]>([]);
  const [manualPriceLists, setManualPriceLists] = useState<Record<number, string>>({});
  const [isSapLoading, setIsSapLoading] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedDisplayDate = useMemo(() => inputDateToDisplayDate(activationDate), [activationDate]);

  const branchOptions = useMemo(() => {
    return [...SUPPORTED_CITIES];
  }, []);

  const priceByFormat = useMemo(() => {
    const map = new Map<string, GeneratedPriceList>();
    for (const priceList of priceLists) {
      if (priceList.activationDate === selectedDisplayDate && !map.has(priceList.format)) {
        map.set(priceList.format, priceList);
      }
    }
    return map;
  }, [priceLists, selectedDisplayDate]);

  const versionCountByFormat = useMemo(() => {
    const map = new Map<string, number>();
    for (const priceList of priceLists) {
      if (priceList.activationDate === selectedDisplayDate) {
        map.set(priceList.format, (map.get(priceList.format) || 0) + 1);
      }
    }
    return map;
  }, [priceLists, selectedDisplayDate]);

  const readinessByFormat = useMemo(
    () => new Map(readiness.map((item) => [item.formatCode, item])),
    [readiness]
  );

  const rows: PriceRow[] = useMemo(
    () => formats.map((format) => {
      const ready = readinessByFormat.get(format.code);
      const priceList = priceByFormat.get(format.code);
      return {
        format,
        readiness: ready,
        priceList,
        versionCount: versionCountByFormat.get(format.code) || 0,
        canGenerate: !isReadOnly && Boolean(ready?.canGenerate),
        canExport: Boolean(priceList),
      };
    }),
    [formats, isReadOnly, priceByFormat, readinessByFormat, versionCountByFormat]
  );

  const generatableRows = rows.filter((row) => row.canGenerate);
  const exportableRows = rows.filter((row) => row.canExport);
  const allGenerateSelected = generatableRows.length > 0 && generatableRows.every((row) => generateCodes.includes(row.format.code));
  const allExportSelected = exportableRows.length > 0 && exportableRows.every((row) => exportCodes.includes(row.format.code));
  const selectedSapRows = useMemo(
    () => rows.filter((row) => exportCodes.includes(row.format.code) && row.canExport),
    [rows, exportCodes]
  );

  const loadReadiness = async (codes: string[], nextBranch = selectedBranch) => {
    if (!codes.length) {
      setReadiness([]);
      return [];
    }
    const params = new URLSearchParams({
      branch_id: nextBranch,
      format_codes: codes.join(','),
    });
    const res = await fetch(`/api/pricing-workflow/readiness?${params.toString()}`);
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) throw new Error(apiErrorMessage(data, text, 'Не удалось проверить готовность данных'));
    const items = Array.isArray(data?.items) ? data.items : [];
    setReadiness(items);
    return items as FormatReadiness[];
  };

  useEffect(() => {
    if (!readiness.some((row) => shouldPollPercentilePreparation(row.percentilePreparation?.status))) return;
    const timer = window.setTimeout(() => {
      void loadReadiness(formats.map((format) => format.code), selectedBranch).catch((e: any) => {
        setError(e?.message || 'Не удалось обновить готовность данных');
      });
    }, 2500);
    return () => window.clearTimeout(timer);
  }, [readiness, formats, selectedBranch]);

  const loadData = async (nextBranch = selectedBranch) => {
    setIsLoading(true);
    setError(null);
    try {
      const branchParams = nextBranch ? `?branch_id=${encodeURIComponent(nextBranch)}` : '';
      const priceParams = new URLSearchParams();
      if (nextBranch) priceParams.set('branch', nextBranch);
      const [formatsRes, priceListsRes] = await Promise.all([
        fetch(`/api/pricing-workflow/branch-formats${branchParams}`),
        fetch(`/api/generated-price-lists?${priceParams.toString()}`),
      ]);
      const [formatsText, priceListsText] = await Promise.all([formatsRes.text(), priceListsRes.text()]);
      const formatsData = parseJsonOrNull(formatsText);
      const priceListsData = parseJsonOrNull(priceListsText);
      if (!formatsRes.ok) throw new Error(apiErrorMessage(formatsData, formatsText, 'Не удалось загрузить ценовые форматы'));
      if (!priceListsRes.ok) throw new Error(apiErrorMessage(priceListsData, priceListsText, 'Не удалось загрузить сформированные прайсы'));

      const nextFormats = Array.isArray(formatsData) ? formatsData : [];
      setFormats(nextFormats);
      setPriceLists(Array.isArray(priceListsData) ? priceListsData : []);
      setGenerateCodes((prev) => prev.filter((code) => nextFormats.some((format: BranchFormatRow) => format.code === code)));
      setExportCodes((prev) => prev.filter((code) => nextFormats.some((format: BranchFormatRow) => format.code === code)));
      await loadReadiness(nextFormats.map((format: BranchFormatRow) => format.code), nextBranch);
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    setSelectedBranch(isSupportedCity(branch) ? branch : SUPPORTED_CITIES[0]);
  }, [branch]);

  useEffect(() => {
    void loadData(selectedBranch);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedBranch]);

  useEffect(() => {
    setGenerateCodes((prev) => prev.filter((code) => rows.some((row) => row.format.code === code && row.canGenerate)));
    setExportCodes((prev) => prev.filter((code) => rows.some((row) => row.format.code === code && row.canExport)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activationDate, formats.length, priceLists.length, readiness.length]);

  const selectBranch = (value: string) => {
    setSelectedBranch(value === '__blank__' ? '' : value);
    setGenerateCodes([]);
    setExportCodes([]);
    setBatchItems([]);
  };

  const toggleGenerate = (row: PriceRow, checked: boolean) => {
    if (!row.canGenerate) return;
    setGenerateCodes((prev) => (checked ? Array.from(new Set([...prev, row.format.code])) : prev.filter((code) => code !== row.format.code)));
    const format = priceFormats.find((item) => item.code === row.format.code);
    if (checked && format) onFormatChange(format);
  };

  const toggleExport = (row: PriceRow, checked: boolean) => {
    if (!row.canExport) return;
    setExportCodes((prev) => (checked ? Array.from(new Set([...prev, row.format.code])) : prev.filter((code) => code !== row.format.code)));
  };

  const toggleAllGenerate = (checked: boolean) => {
    setGenerateCodes(checked ? generatableRows.map((row) => row.format.code) : []);
  };

  const toggleAllExport = (checked: boolean) => {
    setExportCodes(checked ? exportableRows.map((row) => row.format.code) : []);
  };

  const generateSelected = async () => {
    if (!generateCodes.length) return;
    setIsLoading(true);
    setError(null);
    let checked: FormatReadiness[] = [];
    try {
      checked = await loadReadiness(generateCodes, selectedBranch);
    } catch (e: any) {
      setError(e?.message || 'Ошибка проверки готовности');
      setIsLoading(false);
      return;
    }
    setIsLoading(false);

    const blocked = checked.filter((row) => generateCodes.includes(row.formatCode) && !row.canGenerate);
    if (checked.length !== generateCodes.length || blocked.length) {
      toast.error('Есть ошибки готовности данных. Формирование заблокировано.');
      return;
    }

    setIsGenerating(true);
    setBatchItems(
      generateCodes.map((code) => ({
        formatCode: code,
        formatName: formats.find((format) => format.code === code)?.name,
        status: 'queued',
        progress: 0,
        message: 'Ожидает запуска',
      }))
    );
    try {
      const res = await fetch('/api/pricing-workflow/generate-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          branch_id: selectedBranch,
          format_codes: generateCodes,
          activation_date: activationDate,
        }),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(apiErrorMessage(data, text, 'Не удалось запустить формирование'));
      setBatchItems(Array.isArray(data?.items) ? data.items : []);
      setGenerateCodes([]);
      toast.success('Формирование завершено');
      await loadData(selectedBranch);
    } catch (e: any) {
      setError(e?.message || 'Ошибка формирования');
      setBatchItems((prev) => prev.map((item) => (item.status === 'success' ? item : {
        ...item,
        status: 'failed',
        progress: 100,
        message: 'Ошибка',
        error: e?.message || 'Ошибка формирования',
      })));
    } finally {
      setIsGenerating(false);
    }
  };

  const exportSelectedForSap = () => {
    if (!selectedSapRows.length) return;
    setSapMode('auto');
    setSapDialogOpen(true);
    void loadSapVersions();
  };

  const loadSapVersions = async () => {
    const selectedIds = selectedSapRows.map((row) => row.format.id).filter((id): id is number => typeof id === 'number');
    if (!selectedIds.length) return;
    setIsSapLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        branch_id: selectedBranch,
        activation_date: activationDate,
        price_format_ids: selectedIds.join(','),
      });
      const res = await fetch(`/api/sap-export/versions?${params.toString()}`);
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(apiErrorMessage(data, text, 'Не удалось загрузить версии для SAP'));
      const formats = Array.isArray(data?.formats) ? data.formats : [];
      setSapVersions(formats);
      const nextManual: Record<number, string> = {};
      formats.forEach((format: SapFormatVersions) => {
        const latest = format.versions.find((version) => version.is_latest) || format.versions[0];
        if (latest) nextManual[format.price_format_id] = String(latest.price_list_id);
      });
      setManualPriceLists(nextManual);
    } catch (e: any) {
      setError(e?.message || 'Не удалось загрузить версии для SAP');
      toast.error(e?.message || 'Не удалось загрузить версии для SAP');
    } finally {
      setIsSapLoading(false);
    }
  };

  const confirmSapExport = async () => {
    const selectedIds = selectedSapRows.map((row) => row.format.id).filter((id): id is number => typeof id === 'number');
    if (!selectedIds.length) return;
    setIsSapLoading(true);
    setError(null);
    try {
      const body = sapMode === 'auto'
        ? {
            branch_id: selectedBranch,
            activation_date: activationDate,
            mode: 'auto',
            price_format_ids: selectedIds,
          }
        : {
            branch_id: selectedBranch,
            activation_date: activationDate,
            mode: 'manual',
            items: selectedIds.map((priceFormatId) => ({
              price_format_id: priceFormatId,
              price_list_id: Number(manualPriceLists[priceFormatId] || 0),
            })),
          };
      const res = await fetch('/api/sap-export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const blob = await res.blob();
      if (!res.ok) {
        const text = await blob.text();
        const data = parseJsonOrNull(text);
        throw new Error(apiErrorMessage(data, text, 'Не удалось сформировать SAP'));
      }
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename\*=UTF-8''([^;]+)/);
      const filename = match ? decodeURIComponent(match[1]) : `SAP_${selectedBranch}_${activationDate}.xlsx`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setSapDialogOpen(false);
      toast.success('Файл SAP сформирован');
    } catch (e: any) {
      setError(e?.message || 'Не удалось сформировать SAP');
      toast.error(e?.message || 'Не удалось сформировать SAP');
    } finally {
      setIsSapLoading(false);
    }
  };

  const openPriceList = (priceListNumber?: string) => {
    if (!priceListNumber) return;
    if (onOpenPriceList) onOpenPriceList(priceListNumber);
    else onNavigate('pricelists');
  };

  const sapVersionLabel = (version?: SapVersion) => {
    if (!version) return 'Нет успешной версии';
    const value = version.finished_at || version.started_at;
    const time = value ? new Date(value).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : '—';
    return `${time} — wf${version.workflow_run_id}`;
  };

  if (!selectedBranch && !formats.length && !priceFormats.length) {
    return (
      <div className="empty-state">
        <FileText className="h-8 w-8 text-blue-600" />
        <h3>Выберите филиал для формирования прайс-листа</h3>
        <p>После выбора филиала появятся ценовые форматы и прайсы на выбранную дату начала действия.</p>
      </div>
    );
  }

  return (
    <div className="pricing-run-workspace">
      <section className="pricing-run-toolbar">
        <div>
          <div className="eyebrow">Формирование прайс-листа</div>
          <h3>{selectedBranch || 'Без филиала'}</h3>
        </div>
        <label className="toolbar-field">
          <span>Дата начала действия</span>
          <Input type="date" value={activationDate} onChange={(event) => setActivationDate(event.target.value)} />
        </label>
        <Select value={selectedBranch || '__blank__'} onValueChange={selectBranch}>
          <SelectTrigger>
            <SelectValue placeholder="Филиал" />
          </SelectTrigger>
          <SelectContent>
            {branchOptions.map((item) => (
              <SelectItem key={item || '__blank__'} value={item || '__blank__'}>
                {item || 'Без филиала'}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button onClick={generateSelected} disabled={isReadOnly || !generateCodes.length || isGenerating || isLoading} className="bg-blue-600 hover:bg-blue-700">
          <Play className="mr-2 h-4 w-4" />
          Сформировать
        </Button>
        <Button variant="outline" onClick={exportSelectedForSap} disabled={!exportCodes.length || isSapLoading}>
          <Download className="mr-2 h-4 w-4" />
          Сформировать файл SAP
        </Button>
        <Button variant="ghost" onClick={() => loadData(selectedBranch)} disabled={isLoading || isGenerating}>
          <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
          Обновить
        </Button>
      </section>

      {error ? <div className="dashboard-alert">{error}</div> : null}
      {isReadOnly ? <div className="dashboard-alert">Роль viewer: формирование прайс-листов доступно только для просмотра.</div> : null}

      <section className="pricing-run-panel pricing-run-wide">
        <div className="card-title-row">
          <div>
            <h3>Ценовые форматы</h3>
            <p>Прайс-лист ищется по дате начала действия: {selectedDisplayDate || 'не выбрана'}.</p>
          </div>
          <div className="pricing-run-actions">
            <Button variant="outline" size="sm" onClick={() => toggleAllGenerate(!allGenerateSelected)} disabled={!generatableRows.length || isReadOnly}>
              {allGenerateSelected ? 'Снять формирование' : 'Выбрать все для формирования'}
            </Button>
            <Button variant="outline" size="sm" onClick={() => toggleAllExport(!allExportSelected)} disabled={!exportableRows.length}>
              {allExportSelected ? 'Снять выгрузку' : 'Выбрать все для выгрузки'}
            </Button>
          </div>
        </div>
        <StickyTableToolbar summary={`Generate: ${generateCodes.length} / Export: ${exportCodes.length}`}>
          <Button variant="outline" size="sm" onClick={() => toggleAllGenerate(!allGenerateSelected)} disabled={!generatableRows.length || isReadOnly}>
            {allGenerateSelected ? 'Clear generate' : 'Select all generate'}
          </Button>
          <Button variant="outline" size="sm" onClick={() => toggleAllExport(!allExportSelected)} disabled={!exportableRows.length}>
            {allExportSelected ? 'Clear export' : 'Select all export'}
          </Button>
          <Button onClick={generateSelected} disabled={isReadOnly || !generateCodes.length || isGenerating || isLoading} className="bg-blue-600 hover:bg-blue-700" size="sm">
            <Play className="mr-2 h-4 w-4" />
            Generate
          </Button>
          <Button variant="outline" onClick={exportSelectedForSap} disabled={!exportCodes.length || isSapLoading} size="sm">
            <Download className="mr-2 h-4 w-4" />
            SAP
          </Button>
        </StickyTableToolbar>
        <CompactTable
          empty="Для выбранного филиала нет ценовых форматов"
          columns={[
            'Филиал',
            'Ценовой формат',
            'Прайс-лист',
            'Статус готовности',
            <label key="generate-all" className="table-checkbox-head">
              <input
                type="checkbox"
                checked={allGenerateSelected}
                disabled={!generatableRows.length || isReadOnly}
                onChange={(event) => toggleAllGenerate(event.target.checked)}
              />
              Сформировать
            </label>,
            <label key="export-all" className="table-checkbox-head">
              <input
                type="checkbox"
                checked={allExportSelected}
                disabled={!exportableRows.length}
                onChange={(event) => toggleAllExport(event.target.checked)}
              />
              Экспорт в SAP
            </label>,
          ]}
          rows={rows.map((row) => {
            const alreadyGenerated = Boolean(row.priceList);
            return [
              row.format.branch || 'Без филиала',
              <button key={`${row.format.code}-format`} type="button" className="table-link" onClick={() => {
                const format = priceFormats.find((item) => item.code === row.format.code);
                if (format) onFormatChange(format);
              }}>
                {row.format.code}
              </button>,
              row.priceList ? (
                <div key={`${row.format.code}-price-list`}>
                  <button type="button" className="table-link" onClick={() => openPriceList(row.priceList?.number)}>
                    {row.priceList.number} · {row.priceList.activationDate}
                  </button>
                  <div className="text-xs text-amber-700">
                    На эту дату уже есть прайс. Будет создана новая версия.
                    {row.versionCount > 1 ? ` Версий: ${row.versionCount}.` : ''}
                  </div>
                </div>
              ) : (
                <span key={`${row.format.code}-no-price`} className="muted-cell">Не сформирован</span>
              ),
              <span key={`${row.format.code}-ready`} className={`status-pill ${readinessClassName(row.readiness)}`}>
                {readinessText(row.readiness)}
              </span>,
              <label key={`${row.format.code}-generate`} className="row-checkbox-cell">
                <input
                  type="checkbox"
                  checked={generateCodes.includes(row.format.code)}
                  disabled={!row.canGenerate}
                  title={alreadyGenerated ? 'На эту дату уже есть прайс. Будет создана новая версия.' : row.readiness?.canGenerate ? 'Выбрать для формирования' : 'Есть ошибки готовности'}
                  onChange={(event) => toggleGenerate(row, event.target.checked)}
                />
                <span>{alreadyGenerated && row.canGenerate ? 'Новая версия' : row.canGenerate ? 'Можно' : 'Нельзя'}</span>
              </label>,
              <label key={`${row.format.code}-export`} className="row-checkbox-cell">
                <input
                  type="checkbox"
                  checked={exportCodes.includes(row.format.code)}
                  disabled={!row.canExport}
                  onChange={(event) => toggleExport(row, event.target.checked)}
                />
                <span>{row.canExport ? 'Файл для SAP' : 'Нет прайса'}</span>
              </label>,
            ];
          })}
        />
      </section>

      {batchItems.length ? (
        <section className="pricing-run-panel pricing-run-wide">
          <div className="card-title-row">
            <h3>Статусы выполнения</h3>
            {isGenerating ? <span className="muted-count">выполняется...</span> : null}
          </div>
          <CompactTable
            empty="Запуски пока не выполнялись"
            columns={['ЦФ', 'Статус', 'Прогресс', 'Сообщение', 'Результат']}
            rows={batchItems.map((item) => [
              item.formatCode,
              <span key={`${item.formatCode}-status`} className={`status-pill ${item.status === 'success' ? 'ok' : item.status === 'failed' ? 'bad' : 'warn'}`}>{item.status}</span>,
              `${item.progress || 0}%`,
              item.error || item.message || '—',
              item.priceListNumber ? (
                <Button key={`${item.formatCode}-open`} variant="ghost" size="sm" onClick={() => openPriceList(item.priceListNumber)}>
                  Открыть прайс
                </Button>
              ) : '—',
            ])}
          />
        </section>
      ) : null}

      <Dialog open={sapDialogOpen} onOpenChange={setSapDialogOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>Сформировать файл SAP</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="grid gap-2 text-sm text-gray-700 sm:grid-cols-2">
              <div><span className="font-medium">Филиал:</span> {selectedBranch || '—'}</div>
              <div><span className="font-medium">Дата начала действия:</span> {inputDateToDisplayDate(activationDate)}</div>
            </div>
            <div className="flex flex-wrap gap-3 text-sm">
              <label className="inline-flex items-center gap-2">
                <input type="radio" checked={sapMode === 'auto'} onChange={() => setSapMode('auto')} />
                Последние сформированные
              </label>
              <label className="inline-flex items-center gap-2">
                <input type="radio" checked={sapMode === 'manual'} onChange={() => setSapMode('manual')} />
                Выбрать версии вручную
              </label>
            </div>
            <div className="compact-table-wrap">
              <table className="compact-table">
                <thead>
                  <tr>
                    <th>Категория</th>
                    <th>Ценовой формат</th>
                    <th>Версия</th>
                  </tr>
                </thead>
                <tbody>
                  {sapVersions.length ? sapVersions.map((format) => {
                    const latest = format.versions.find((version) => version.is_latest) || format.versions[0];
                    return (
                      <tr key={format.price_format_id}>
                        <td>{format.sap_category || 'Не настроено'}</td>
                        <td>{format.code} · {format.name}</td>
                        <td>
                          {sapMode === 'manual' ? (
                            <Select
                              value={manualPriceLists[format.price_format_id] || ''}
                              onValueChange={(value) => setManualPriceLists((prev) => ({ ...prev, [format.price_format_id]: value }))}
                              disabled={isSapLoading || !format.versions.length}
                            >
                              <SelectTrigger>
                                <SelectValue placeholder="Версия" />
                              </SelectTrigger>
                              <SelectContent>
                                {format.versions.map((version) => (
                                  <SelectItem key={version.price_list_id} value={String(version.price_list_id)}>
                                    {sapVersionLabel(version)}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          ) : (
                            sapVersionLabel(latest)
                          )}
                        </td>
                      </tr>
                    );
                  }) : (
                    <tr>
                      <td colSpan={3} className="compact-empty">
                        {isSapLoading ? 'Загрузка версий...' : 'Нет данных для SAP'}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSapDialogOpen(false)} disabled={isSapLoading}>Отмена</Button>
            <Button onClick={confirmSapExport} disabled={isSapLoading || !sapVersions.length} className="bg-blue-600 hover:bg-blue-700">
              <Download className="mr-2 h-4 w-4" />
              Сформировать SAP
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <section className="pricing-run-links">
        <Button variant="outline" onClick={() => onNavigate('pricelists')}>Сформированные прайс-листы</Button>
        <Button variant="outline" onClick={() => onNavigate('competitors')}>Назначение ПЛК</Button>
        <Button variant="outline" onClick={() => onNavigate('pricing')}>Правила ЦО</Button>
      </section>
    </div>
  );
}

function CompactTable({ columns, rows, empty }: { columns: ReactNode[]; rows: ReactNode[][]; empty: string }) {
  return (
    <div className="compact-table-wrap">
      <table className="compact-table">
        <thead>
          <tr>{columns.map((column, idx) => <th key={idx}>{column}</th>)}</tr>
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
