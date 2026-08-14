import { useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Copy, Plus, Save, Trash2 } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Label } from './ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';
import { PricingSettingsTab } from './PricingSettingsTab';
import {
  NO_COPY_SOURCE,
  applyPricingRuleCreateSuccess,
  buildPricingRuleCreatePayload,
  canSubmitPricingRuleCreate,
  draftFromCopySource,
  pricingRuleCreateErrorMessage,
} from '../pricingRuleCreateFlow';
import {
  CREATE_NEW_TEMPLATE,
  CURRENT_FORMAT_SETTINGS,
  appliedTemplateIdForKind,
  resolveInitialRoundingSelection,
  resolveInitialTemplateSelection,
} from '../pricingTemplateSelection';

type RangeRow = {
  id?: number;
  costFrom: string;
  costTo: string;
  markupPercent?: string;
  bendPercent?: string;
  sortOrder?: number;
};

type Template = {
  id: number;
  code: string;
  name: string;
  description: string;
  isActive: boolean;
  rows: RangeRow[];
};

type RoundingRule = {
  id: number;
  code: string;
  name: string;
  mode: string;
  precision: number;
  step: number | null;
  isActive: boolean;
};

type PricingRule = {
  id: number;
  code: string;
  name: string;
  description: string;
  regionScope: string;
  branchScope: string;
  markupTemplateId: number | null;
  bendTemplateId: number | null;
  noCompetitorTemplateId: number | null;
  roundingRuleId: number | null;
  isActive: boolean;
};

type AppliedRuleStatus = {
  ruleId: number | null;
  ruleName?: string;
  appliedAt?: string;
  status?: string;
  isManualChanged?: boolean;
  tablesUpdated?: string[];
  tablesChanged?: string[];
  roundingRuleName?: string;
};

type PriceFormatSettings = {
  appliedMarkupTemplateId?: number | null;
  appliedBendTemplateId?: number | null;
  appliedNoCompetitorTemplateId?: number | null;
  appliedRoundingRuleId?: number | null;
  recommendedMarkups?: Array<{ lowerBound?: number | string; upperBound?: number | string | null; markupPercent?: number | string }>;
  bendRanges?: Array<{ priceFrom?: number | string; bendPercent?: number | string }>;
  noCompetitorMarkups?: Array<{ lowerBound?: number | string; upperBound?: number | string | null; markupPercent?: number | string }>;
};

type Props = {
  formatCode: string;
  onNavigate?: (section: 'pricing-workflow' | 'analytics' | 'pricelists' | 'competitors' | 'pricing' | 'universal-lists') => void;
};

const parseJsonOrNull = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const roundingModeLabel = (mode: string) => {
  if (mode === 'math') return 'Математическое';
  if (mode === 'up') return 'Вверх';
  if (mode === 'down') return 'Вниз';
  return mode;
};

const emptyTemplate = (kind: 'markup' | 'bend' | 'noCompetitor'): Template => ({
  id: 0,
  code: '',
  name: '',
  description: '',
  isActive: true,
  rows: [
    kind === 'bend'
      ? { costFrom: '0', costTo: '', bendPercent: '0.3', sortOrder: 0 }
      : { costFrom: '0', costTo: '', markupPercent: '10', sortOrder: 0 },
  ],
});

const toPayloadRows = (rows: RangeRow[], valueKey: 'markupPercent' | 'bendPercent') =>
  rows.map((row, index) => ({
    costFrom: Number(row.costFrom),
    costTo: row.costTo === '' ? null : Number(row.costTo),
    [valueKey]: Number(row[valueKey] || 0),
    sortOrder: index,
  }));

const rowsFromFormatSettings = (
  settings: PriceFormatSettings | null,
  kind: 'markup' | 'bend' | 'noCompetitor'
): RangeRow[] => {
  if (!settings) return [];
  if (kind === 'bend') {
    return (settings.bendRanges || []).map((row, index) => ({
      costFrom: String(row.priceFrom ?? '0'),
      costTo: '',
      bendPercent: String(row.bendPercent ?? ''),
      sortOrder: index,
    }));
  }
  const source = kind === 'noCompetitor' ? settings.noCompetitorMarkups : settings.recommendedMarkups;
  return (source || []).map((row, index) => ({
    costFrom: String(row.lowerBound ?? '0'),
    costTo: row.upperBound == null ? '' : String(row.upperBound),
    markupPercent: String(row.markupPercent ?? ''),
    sortOrder: index,
  }));
};

function TemplateEditor({
  title,
  endpoint,
  kind,
  valueKey,
  valueLabel,
  settings,
}: {
  title: string;
  endpoint: string;
  kind: 'markup' | 'bend' | 'noCompetitor';
  valueKey: 'markupPercent' | 'bendPercent';
  valueLabel: string;
  settings: PriceFormatSettings | null;
}) {
  const [items, setItems] = useState<Template[]>([]);
  const [selectedId, setSelectedId] = useState<string>(CURRENT_FORMAT_SETTINGS);
  const [draft, setDraft] = useState<Template>(() => emptyTemplate(kind));
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const preserveSelectionRef = useRef<string | null>(null);
  const appliedTemplateId = appliedTemplateIdForKind(settings, kind);
  const currentRows = useMemo(() => rowsFromFormatSettings(settings, kind), [settings, kind]);
  const currentSettingsDraft = useMemo(
    () => ({
      ...emptyTemplate(kind),
      name: appliedTemplateId ? 'Текущие настройки ЦФ (шаблон недоступен)' : 'Текущие настройки ЦФ',
      rows: currentRows.length ? currentRows : emptyTemplate(kind).rows,
    }),
    [appliedTemplateId, currentRows, kind]
  );

  const load = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(endpoint);
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить шаблоны');
      const rows = Array.isArray(data) ? data : [];
      setItems(rows);
      if (preserveSelectionRef.current) {
        const preservedId = preserveSelectionRef.current;
        preserveSelectionRef.current = null;
        const current = rows.find((row: Template) => String(row.id) === preservedId);
        if (current) {
          setSelectedId(preservedId);
          setDraft(normalizeTemplate(current, kind));
          return;
        }
      }
      const resolvedSelection = resolveInitialTemplateSelection(settings, kind, rows);
      if (resolvedSelection.mode === 'applied') {
        const applied = rows.find((row: Template) => String(row.id) === resolvedSelection.selectedId);
        if (applied) {
          setSelectedId(String(applied.id));
          setDraft(normalizeTemplate(applied, kind));
        }
      } else {
        setSelectedId(CURRENT_FORMAT_SETTINGS);
        setDraft(currentSettingsDraft);
      }
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endpoint, appliedTemplateId, settings]);

  const select = (value: string) => {
    setSelectedId(value);
    if (value === CREATE_NEW_TEMPLATE) {
      setDraft(emptyTemplate(kind));
      return;
    }
    if (value === CURRENT_FORMAT_SETTINGS) {
      setDraft(currentSettingsDraft);
      return;
    }
    const row = items.find((item) => String(item.id) === value);
    if (row) setDraft(normalizeTemplate(row, kind));
  };

  const save = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const payload = {
        code: draft.code,
        name: draft.name,
        description: draft.description,
        isActive: draft.isActive,
        rows: toPayloadRows(draft.rows, valueKey),
      };
      const isNew = selectedId === CREATE_NEW_TEMPLATE || !draft.id;
      const res = await fetch(isNew ? endpoint : `${endpoint}/${draft.id}`, {
        method: isNew ? 'POST' : 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось сохранить шаблон');
      setSelectedId(String(data.id));
      setDraft(normalizeTemplate(data, kind));
      preserveSelectionRef.current = String(data.id);
      await load();
      toast.success('Шаблон сохранён');
    } catch (e: any) {
      setError(e?.message || 'Ошибка сохранения');
    } finally {
      setIsLoading(false);
    }
  };

  const copy = async () => {
    if (!draft.id) return;
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`${endpoint}/${draft.id}/copy`, { method: 'POST' });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось копировать шаблон');
      setSelectedId(String(data.id));
      setDraft(normalizeTemplate(data, kind));
      preserveSelectionRef.current = String(data.id);
      await load();
    } catch (e: any) {
      setError(e?.message || 'Ошибка копирования');
    } finally {
      setIsLoading(false);
    }
  };

  const updateRow = (index: number, patch: Partial<RangeRow>) => {
    setDraft((prev) => ({
      ...prev,
      rows: prev.rows.map((row, idx) => (idx === index ? { ...row, ...patch } : row)),
    }));
  };

  const addRow = () => {
    setDraft((prev) => ({
      ...prev,
      rows: [...prev.rows, kind === 'bend' ? { costFrom: '0', costTo: '', bendPercent: '0', sortOrder: prev.rows.length } : { costFrom: '0', costTo: '', markupPercent: '0', sortOrder: prev.rows.length }],
    }));
  };

  const removeRow = (index: number) => {
    setDraft((prev) => ({ ...prev, rows: prev.rows.filter((_, idx) => idx !== index) }));
  };

  return (
    <div className="space-y-4">
      {error ? <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div> : null}
      <div className="admin-card p-4">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-[260px_1fr_1fr_auto]">
          <Select value={selectedId} onValueChange={select}>
            <SelectTrigger><SelectValue placeholder="Шаблон" /></SelectTrigger>
            <SelectContent>
              {!appliedTemplateId ? <SelectItem value={CURRENT_FORMAT_SETTINGS}>Текущие настройки ЦФ</SelectItem> : null}
              {appliedTemplateId && !items.some((item) => Number(item.id) === Number(appliedTemplateId)) ? (
                <SelectItem value={CURRENT_FORMAT_SETTINGS}>Текущие настройки ЦФ (шаблон недоступен)</SelectItem>
              ) : null}
              {items.map((item) => (
                <SelectItem key={item.id} value={String(item.id)}>
                  {item.name}{Number(item.id) === Number(appliedTemplateId) ? ' — применён' : ''}
                </SelectItem>
              ))}
              <SelectItem value={CREATE_NEW_TEMPLATE}>+ Новый шаблон</SelectItem>
            </SelectContent>
          </Select>
          <Input value={draft.name} onChange={(e) => setDraft((prev) => ({ ...prev, name: e.target.value }))} placeholder="Название" disabled={selectedId === CURRENT_FORMAT_SETTINGS} />
          <Input value={draft.code} onChange={(e) => setDraft((prev) => ({ ...prev, code: e.target.value }))} placeholder="Код" disabled={selectedId === CURRENT_FORMAT_SETTINGS} />
          <div className="flex gap-2">
            <Button size="sm" onClick={save} disabled={isLoading || selectedId === CURRENT_FORMAT_SETTINGS} className="bg-blue-600 hover:bg-blue-700">
              <Save className="mr-2 h-4 w-4" />Сохранить
            </Button>
            <Button size="sm" variant="outline" onClick={copy} disabled={isLoading || !draft.id}>
              <Copy className="mr-2 h-4 w-4" />Копировать
            </Button>
          </div>
        </div>
        <div className="mt-3">
          <Input value={draft.description} onChange={(e) => setDraft((prev) => ({ ...prev, description: e.target.value }))} placeholder="Описание" disabled={selectedId === CURRENT_FORMAT_SETTINGS} />
          {selectedId === CURRENT_FORMAT_SETTINGS ? (
            <div className="mt-2 text-xs text-gray-500">
              Показаны сохранённые настройки текущего ЦФ без выбранного шаблона. Выберите существующий шаблон или “+ Новый шаблон”, чтобы редактировать шаблон.
            </div>
          ) : null}
        </div>
      </div>

      <div className="admin-card p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
          <Button variant="outline" size="sm" onClick={addRow} disabled={selectedId === CURRENT_FORMAT_SETTINGS}><Plus className="mr-2 h-4 w-4" />Добавить строку</Button>
        </div>
        <div className="admin-table-card">
          <table className="admin-table">
            <thead>
              <tr>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">От</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">До</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">{valueLabel}</th>
                <th className="sticky-action-col px-4 py-3 text-right text-sm font-medium text-gray-700">Действия</th>
              </tr>
            </thead>
            <tbody>
              {draft.rows.map((row, index) => (
                <tr key={index}>
                  <td className="px-4 py-3"><Input className="numeric-input" value={row.costFrom} onChange={(e) => updateRow(index, { costFrom: e.target.value })} disabled={selectedId === CURRENT_FORMAT_SETTINGS} /></td>
                  <td className="px-4 py-3"><Input className="numeric-input" value={row.costTo} onChange={(e) => updateRow(index, { costTo: e.target.value })} placeholder="∞" disabled={selectedId === CURRENT_FORMAT_SETTINGS} /></td>
                  <td className="px-4 py-3"><Input className="numeric-input" value={String(row[valueKey] || '')} onChange={(e) => updateRow(index, { [valueKey]: e.target.value })} disabled={selectedId === CURRENT_FORMAT_SETTINGS} /></td>
                  <td className="sticky-action-col px-4 py-3 text-right">
                    <Button variant="ghost" size="sm" className="text-red-600 hover:text-red-700" onClick={() => removeRow(index)} disabled={selectedId === CURRENT_FORMAT_SETTINGS}>
                      <Trash2 className="mr-1 h-4 w-4" />Удалить
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function normalizeTemplate(template: Template, kind: 'markup' | 'bend' | 'noCompetitor'): Template {
  return {
    ...template,
    rows: (template.rows || []).map((row) => ({
      ...row,
      costFrom: String(row.costFrom ?? '0'),
      costTo: row.costTo == null ? '' : String(row.costTo),
      markupPercent: kind === 'bend' ? undefined : String(row.markupPercent ?? ''),
      bendPercent: kind === 'bend' ? String(row.bendPercent ?? '') : undefined,
    })),
  };
}

export function PricingRulesTab({ formatCode, onNavigate }: Props) {
  const [rules, setRules] = useState<PricingRule[]>([]);
  const [markups, setMarkups] = useState<Template[]>([]);
  const [bends, setBends] = useState<Template[]>([]);
  const [noCompetitors, setNoCompetitors] = useState<Template[]>([]);
  const [roundings, setRoundings] = useState<RoundingRule[]>([]);
  const [selectedRuleId, setSelectedRuleId] = useState<string>('new');
  const [draft, setDraft] = useState<PricingRule>(() => emptyRule());
  const [copyFromRuleId, setCopyFromRuleId] = useState<string>(NO_COPY_SOURCE);
  const [formatRuleId, setFormatRuleId] = useState<string>('none');
  const [appliedRule, setAppliedRule] = useState<AppliedRuleStatus | null>(null);
  const [formatSettings, setFormatSettings] = useState<PriceFormatSettings | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadRequestRef = useRef(0);

  const load = async () => {
    const requestId = ++loadRequestRef.current;
    setIsLoading(true);
    setError(null);
    try {
      const [rulesRes, markupsRes, bendsRes, noCompRes, roundingsRes, settingsRes] = await Promise.all([
        fetch('/api/pricing-rules'),
        fetch('/api/pricing-rules/markup-templates'),
        fetch('/api/pricing-rules/bend-templates'),
        fetch('/api/pricing-rules/no-competitor-templates'),
        fetch('/api/pricing-rules/rounding-rules'),
        fetch(`/api/price-formats/${encodeURIComponent(formatCode)}/settings`),
      ]);
      const [rulesData, markupsData, bendsData, noCompData, roundingsData, settingsData] = await Promise.all([
        rulesRes.text().then(parseJsonOrNull),
        markupsRes.text().then(parseJsonOrNull),
        bendsRes.text().then(parseJsonOrNull),
        noCompRes.text().then(parseJsonOrNull),
        roundingsRes.text().then(parseJsonOrNull),
        settingsRes.text().then(parseJsonOrNull),
      ]);
      if (requestId !== loadRequestRef.current) return;
      setRules(Array.isArray(rulesData) ? rulesData : []);
      setMarkups(Array.isArray(markupsData) ? markupsData : []);
      setBends(Array.isArray(bendsData) ? bendsData : []);
      setNoCompetitors(Array.isArray(noCompData) ? noCompData : []);
      setRoundings(Array.isArray(roundingsData) ? roundingsData : []);
      setFormatRuleId(settingsData?.pricingRuleId ? String(settingsData.pricingRuleId) : 'none');
      setAppliedRule(settingsData?.appliedRule || null);
      setFormatSettings(settingsData || null);
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки правил');
    } finally {
      if (requestId === loadRequestRef.current) setIsLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [formatCode]);

  const ruleById = useMemo(() => new Map(rules.map((rule) => [String(rule.id), rule])), [rules]);

  const selectRule = async (value: string) => {
    setSelectedRuleId(value);
    if (value === 'new') {
      setDraft(emptyRule());
      setCopyFromRuleId(NO_COPY_SOURCE);
      return;
    }
    setCopyFromRuleId(NO_COPY_SOURCE);
    const res = await fetch(`/api/pricing-rules/${value}`);
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (res.ok && data) setDraft(normalizeRule(data));
  };

  const selectCopySource = async (value: string) => {
    setCopyFromRuleId(value);
    if (value === NO_COPY_SOURCE) {
      setDraft((prev) => ({
        ...emptyRule(),
        code: prev.code,
        name: prev.name,
      }));
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/pricing-rules/${value}`);
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось загрузить правило-источник');
      setDraft((prev) => draftFromCopySource(prev, normalizeRule(data)));
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки правила-источника');
    } finally {
      setIsLoading(false);
    }
  };

  const saveRule = async () => {
    if (!canSubmitPricingRuleCreate(isLoading)) return;
    setIsLoading(true);
    setError(null);
    try {
      const isNew = selectedRuleId === 'new' || !draft.id;
      const res = await fetch(isNew ? '/api/pricing-rules' : `/api/pricing-rules/${draft.id}`, {
        method: isNew ? 'POST' : 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(isNew ? buildPricingRuleCreatePayload(draft, copyFromRuleId) : draft),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(pricingRuleCreateErrorMessage(data, text));
      const normalized = normalizeRule(data);
      const next = isNew ? applyPricingRuleCreateSuccess(normalized) : { selectedRuleId: String(data.id), draft: normalized, copyFromRuleId };
      setSelectedRuleId(next.selectedRuleId);
      setDraft(next.draft);
      setCopyFromRuleId(next.copyFromRuleId);
      await load();
      toast.success('Правило ЦО сохранено');
    } catch (e: any) {
      setError(e?.message || 'Ошибка сохранения');
    } finally {
      setIsLoading(false);
    }
  };

  const copyRule = async () => {
    if (!draft.id) return;
    const source = normalizeRule(draft);
    setSelectedRuleId('new');
    setCopyFromRuleId(String(source.id));
    setDraft(draftFromCopySource(emptyRule(), source));
  };

  const deleteRule = async () => {
    if (!draft.id) return;
    const res = await fetch(`/api/pricing-rules/${draft.id}`, { method: 'DELETE' });
    if (res.ok) {
      setSelectedRuleId('new');
      setDraft(emptyRule());
      await load();
    }
  };

  const applyToFormat = async () => {
    if (formatRuleId === 'none') return;
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/price-formats/${encodeURIComponent(formatCode)}/pricing-rule`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pricingRuleId: Number(formatRuleId) }),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось применить правило');
      await load();
      toast.success('Правило применено к ценовому формату');
    } catch (e: any) {
      setError(e?.message || 'Ошибка применения');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <Tabs defaultValue="rules" className="w-full">
      {error ? <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div> : null}
      <TabsList className="w-full justify-start border-b border-gray-200 rounded-none h-auto p-0 bg-transparent">
        <TabsTrigger value="rules" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">Правила ЦО</TabsTrigger>
        <TabsTrigger value="markups" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">Диапазоны рекомендованных наценок</TabsTrigger>
        <TabsTrigger value="bends" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">Диапазоны прогибов</TabsTrigger>
        <TabsTrigger value="no-comp" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">Наценки без конкурентов</TabsTrigger>
        <TabsTrigger value="rounding" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">Округления</TabsTrigger>
        <TabsTrigger value="format" className="rounded-none border-b border-transparent data-[state=active]:border-blue-600 data-[state=active]:bg-transparent data-[state=active]:shadow-none data-[state=active]:text-blue-700 px-4 py-2">Ценовой формат</TabsTrigger>
      </TabsList>

      <TabsContent value="rules" className="m-0 pt-4">
        <div className="space-y-4">
          <div className="admin-card p-4">
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-[260px_1fr_auto]">
              <Select value={formatRuleId} onValueChange={setFormatRuleId}>
                <SelectTrigger><SelectValue placeholder="Правило для текущего ЦФ" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">Не выбрано</SelectItem>
                  {rules.map((rule) => <SelectItem key={rule.id} value={String(rule.id)}>{rule.name}</SelectItem>)}
                </SelectContent>
              </Select>
              <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-700">
                {formatRuleId !== 'none' ? ruleById.get(formatRuleId)?.description || 'Правило будет синхронизировано в настройки формата' : 'Выберите правило ЦО для текущего ценового формата'}
              </div>
              <Button onClick={applyToFormat} disabled={isLoading || formatRuleId === 'none'} className="bg-blue-600 hover:bg-blue-700">Применить к ЦФ</Button>
            </div>
            {appliedRule ? <AppliedRulePanel appliedRule={appliedRule} /> : null}
          </div>

          <div className="admin-card p-4 space-y-4">
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-[260px_1fr_1fr_auto]">
              <Select value={selectedRuleId} onValueChange={selectRule}>
                <SelectTrigger><SelectValue placeholder="Правило" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="new">Новое правило</SelectItem>
                  {rules.map((rule) => <SelectItem key={rule.id} value={String(rule.id)}>{rule.name}</SelectItem>)}
                </SelectContent>
              </Select>
              <Input value={draft.name} onChange={(e) => setDraft((prev) => ({ ...prev, name: e.target.value }))} placeholder="Название" />
              <Input value={draft.code} onChange={(e) => setDraft((prev) => ({ ...prev, code: e.target.value }))} placeholder="Код" />
              <div className="flex gap-2">
                <Button size="sm" onClick={saveRule} disabled={!canSubmitPricingRuleCreate(isLoading)} className="bg-blue-600 hover:bg-blue-700"><Save className="mr-2 h-4 w-4" />Сохранить</Button>
                <Button size="sm" variant="outline" onClick={copyRule} disabled={isLoading || !draft.id}><Copy className="mr-2 h-4 w-4" />Копировать</Button>
                <Button size="sm" variant="ghost" className="text-red-600 hover:text-red-700" onClick={deleteRule} disabled={isLoading || !draft.id}><Trash2 className="mr-2 h-4 w-4" />Удалить</Button>
              </div>
            </div>
            {selectedRuleId === 'new' ? (
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-[260px_1fr]">
                <div className="space-y-2">
                  <Label>Копировать из существующего правила</Label>
                  <Select value={copyFromRuleId} onValueChange={selectCopySource} disabled={isLoading}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NO_COPY_SOURCE}>Не копировать</SelectItem>
                      {rules.map((rule) => <SelectItem key={rule.id} value={String(rule.id)}>{rule.name}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
                <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-700">
                  {copyFromRuleId !== NO_COPY_SOURCE
                    ? `Копируется из: ${ruleById.get(copyFromRuleId)?.name || 'выбранное правило'}`
                    : 'Новое правило будет создано без копирования связанных шаблонов.'}
                </div>
              </div>
            ) : null}
            <Input value={draft.description} onChange={(e) => setDraft((prev) => ({ ...prev, description: e.target.value }))} placeholder="Описание" />
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <FieldSelect label="Рекомендованные наценки" value={draft.markupTemplateId} items={markups} disabled={selectedRuleId === 'new' && copyFromRuleId !== NO_COPY_SOURCE} onChange={(id) => setDraft((prev) => ({ ...prev, markupTemplateId: id }))} />
              <FieldSelect label="Прогибы" value={draft.bendTemplateId} items={bends} disabled={selectedRuleId === 'new' && copyFromRuleId !== NO_COPY_SOURCE} onChange={(id) => setDraft((prev) => ({ ...prev, bendTemplateId: id }))} />
              <FieldSelect label="Наценки без конкурентов" value={draft.noCompetitorTemplateId} items={noCompetitors} disabled={selectedRuleId === 'new' && copyFromRuleId !== NO_COPY_SOURCE} onChange={(id) => setDraft((prev) => ({ ...prev, noCompetitorTemplateId: id }))} />
              <FieldSelect label="Округление" value={draft.roundingRuleId} items={roundings} disabled={selectedRuleId === 'new' && copyFromRuleId !== NO_COPY_SOURCE} onChange={(id) => setDraft((prev) => ({ ...prev, roundingRuleId: id }))} />
            </div>
          </div>
        </div>
      </TabsContent>

      <TabsContent value="markups" className="m-0 pt-4"><TemplateEditor title="Диапазоны рекомендованных наценок" endpoint="/api/pricing-rules/markup-templates" kind="markup" valueKey="markupPercent" valueLabel="Наценка (%)" settings={formatSettings} /></TabsContent>
      <TabsContent value="bends" className="m-0 pt-4"><TemplateEditor title="Диапазоны прогибов" endpoint="/api/pricing-rules/bend-templates" kind="bend" valueKey="bendPercent" valueLabel="Прогиб (%)" settings={formatSettings} /></TabsContent>
      <TabsContent value="no-comp" className="m-0 pt-4"><TemplateEditor title="Диапазоны наценок без конкурентов" endpoint="/api/pricing-rules/no-competitor-templates" kind="noCompetitor" valueKey="markupPercent" valueLabel="Наценка (%)" settings={formatSettings} /></TabsContent>
      <TabsContent value="rounding" className="m-0 pt-4"><RoundingEditor items={roundings} appliedRoundingRuleId={formatSettings?.appliedRoundingRuleId ?? null} onReload={load} /></TabsContent>
      <TabsContent value="format" className="m-0 pt-4"><PricingSettingsTab formatCode={formatCode} onNavigate={onNavigate} /></TabsContent>
    </Tabs>
  );
}

function AppliedRulePanel({ appliedRule }: { appliedRule: AppliedRuleStatus }) {
  const tables = appliedRule.tablesUpdated?.length ? appliedRule.tablesUpdated : [];
  const changed = appliedRule.tablesChanged?.length ? appliedRule.tablesChanged : [];
  return (
    <div className={`mt-4 rounded-md border px-3 py-2 text-sm ${appliedRule.isManualChanged ? 'border-amber-200 bg-amber-50 text-amber-800' : 'border-green-200 bg-green-50 text-green-800'}`}>
      <div className="font-medium">
        {appliedRule.ruleName || 'Правило ЦО'} · {appliedRule.appliedAt ? new Date(appliedRule.appliedAt).toLocaleString('ru-RU') : 'не применялось'}
      </div>
      <div className="mt-1">
        {appliedRule.isManualChanged ? 'изменено вручную' : 'синхронизировано'}
        {tables.length ? ` · обновлено: ${tables.join(', ')}` : ''}
        {changed.length ? ` · отличается: ${changed.join(', ')}` : ''}
      </div>
    </div>
  );
}

function FieldSelect({ label, value, items, disabled = false, onChange }: { label: string; value: number | null; items: Array<{ id: number; name: string }>; disabled?: boolean; onChange: (id: number | null) => void }) {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      <Select value={value ? String(value) : 'none'} onValueChange={(v) => onChange(v === 'none' ? null : Number(v))} disabled={disabled}>
        <SelectTrigger><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="none">Не выбрано</SelectItem>
          {items.map((item) => <SelectItem key={item.id} value={String(item.id)}>{item.name}</SelectItem>)}
        </SelectContent>
      </Select>
    </div>
  );
}

function emptyRule(): PricingRule {
  return {
    id: 0,
    code: '',
    name: '',
    description: '',
    regionScope: '',
    branchScope: '',
    markupTemplateId: null,
    bendTemplateId: null,
    noCompetitorTemplateId: null,
    roundingRuleId: null,
    isActive: true,
  };
}

function normalizeRule(rule: PricingRule): PricingRule {
  return {
    ...emptyRule(),
    ...rule,
    markupTemplateId: rule.markupTemplateId ?? null,
    bendTemplateId: rule.bendTemplateId ?? null,
    noCompetitorTemplateId: rule.noCompetitorTemplateId ?? null,
    roundingRuleId: rule.roundingRuleId ?? null,
  };
}

function RoundingEditor({ items, appliedRoundingRuleId, onReload }: { items: RoundingRule[]; appliedRoundingRuleId: number | null; onReload: () => Promise<void> }) {
  const [selectedId, setSelectedId] = useState<string>(CURRENT_FORMAT_SETTINGS);
  const [draft, setDraft] = useState<RoundingRule>({ id: 0, code: '', name: '', mode: 'math', precision: 2, step: 0.01, isActive: true });
  const [error, setError] = useState<string | null>(null);
  const preserveSelectionRef = useRef<string | null>(null);

  useEffect(() => {
    if (preserveSelectionRef.current) {
      const preservedId = preserveSelectionRef.current;
      preserveSelectionRef.current = null;
      const row = items.find((item) => String(item.id) === preservedId);
      if (row) {
        setSelectedId(preservedId);
        setDraft(row);
        return;
      }
    }
    const resolvedSelection = resolveInitialRoundingSelection({ appliedRoundingRuleId }, items);
    if (resolvedSelection.mode === 'applied') {
      const row = items.find((item) => String(item.id) === resolvedSelection.selectedId);
      if (row) {
        setSelectedId(String(row.id));
        setDraft(row);
        return;
      }
    }
    setSelectedId(CURRENT_FORMAT_SETTINGS);
    setDraft({ id: 0, code: '', name: appliedRoundingRuleId ? 'Текущее округление ЦФ (правило недоступно)' : 'Без шаблона', mode: 'math', precision: 2, step: 0.01, isActive: true });
  }, [items, appliedRoundingRuleId]);

  const select = (value: string) => {
    setSelectedId(value);
    if (value === CREATE_NEW_TEMPLATE) {
      setDraft({ id: 0, code: '', name: '', mode: 'math', precision: 2, step: 0.01, isActive: true });
      return;
    }
    if (value === CURRENT_FORMAT_SETTINGS) {
      setDraft({ id: 0, code: '', name: appliedRoundingRuleId ? 'Текущее округление ЦФ (правило недоступно)' : 'Без шаблона', mode: 'math', precision: 2, step: 0.01, isActive: true });
      return;
    }
    const row = items.find((item) => String(item.id) === value);
    if (row) setDraft(row);
  };

  const save = async () => {
    setError(null);
    const isNew = selectedId === CREATE_NEW_TEMPLATE || !draft.id;
    const res = await fetch(isNew ? '/api/pricing-rules/rounding-rules' : `/api/pricing-rules/rounding-rules/${draft.id}`, {
      method: isNew ? 'POST' : 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(draft),
    });
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) {
      setError(data?.detail || text || 'Не удалось сохранить округление');
      return;
    }
    setSelectedId(String(data.id));
    setDraft(data);
    preserveSelectionRef.current = String(data.id);
    await onReload();
  };

  return (
    <div className="space-y-4">
      {error ? <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div> : null}
      <div className="admin-card p-4">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-[240px_1fr_1fr_160px_140px_140px_auto]">
          <Select value={selectedId} onValueChange={select}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {!appliedRoundingRuleId ? <SelectItem value={CURRENT_FORMAT_SETTINGS}>Без шаблона</SelectItem> : null}
              {appliedRoundingRuleId && !items.some((item) => Number(item.id) === Number(appliedRoundingRuleId)) ? (
                <SelectItem value={CURRENT_FORMAT_SETTINGS}>Текущее округление ЦФ (правило недоступно)</SelectItem>
              ) : null}
              {items.map((item) => (
                <SelectItem key={item.id} value={String(item.id)}>
                  {item.name}{Number(item.id) === Number(appliedRoundingRuleId) ? ' — применено' : ''}
                </SelectItem>
              ))}
              <SelectItem value={CREATE_NEW_TEMPLATE}>+ Новое округление</SelectItem>
            </SelectContent>
          </Select>
          <Input value={draft.name} onChange={(e) => setDraft((prev) => ({ ...prev, name: e.target.value }))} placeholder="Название" disabled={selectedId === CURRENT_FORMAT_SETTINGS} />
          <Input value={draft.code} onChange={(e) => setDraft((prev) => ({ ...prev, code: e.target.value }))} placeholder="Код" disabled={selectedId === CURRENT_FORMAT_SETTINGS} />
          <Select value={draft.mode} onValueChange={(mode) => setDraft((prev) => ({ ...prev, mode }))} disabled={selectedId === CURRENT_FORMAT_SETTINGS}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="math">Математическое</SelectItem>
              <SelectItem value="up">Вверх</SelectItem>
              <SelectItem value="down">Вниз</SelectItem>
            </SelectContent>
          </Select>
          <Input value={String(draft.precision)} onChange={(e) => setDraft((prev) => ({ ...prev, precision: Number(e.target.value) }))} placeholder="Точность" disabled={selectedId === CURRENT_FORMAT_SETTINGS} />
          <Input value={draft.step == null ? '' : String(draft.step)} onChange={(e) => setDraft((prev) => ({ ...prev, step: e.target.value === '' ? null : Number(e.target.value) }))} placeholder="Шаг" disabled={selectedId === CURRENT_FORMAT_SETTINGS} />
          <Button onClick={save} disabled={selectedId === CURRENT_FORMAT_SETTINGS} className="bg-blue-600 hover:bg-blue-700"><Save className="mr-2 h-4 w-4" />Сохранить</Button>
        </div>
      </div>
      <div className="admin-table-card">
        <table className="admin-table">
          <thead><tr><th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Код</th><th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Название</th><th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Режим</th><th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Точность</th><th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Шаг</th></tr></thead>
          <tbody>{items.map((item) => <tr key={item.id} onClick={() => select(String(item.id))} className="cursor-pointer hover:bg-gray-50"><td className="px-4 py-3 text-sm text-gray-900">{item.code}</td><td className="px-4 py-3 text-sm text-gray-700">{item.name}</td><td className="px-4 py-3 text-sm text-gray-700">{roundingModeLabel(item.mode)}</td><td className="px-4 py-3 text-sm text-gray-700">{item.precision}</td><td className="px-4 py-3 text-sm text-gray-700">{item.step ?? '—'}</td></tr>)}</tbody>
        </table>
      </div>
    </div>
  );
}
