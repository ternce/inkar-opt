import { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { Copy, Plus, Save, Trash2 } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Label } from './ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';
import { PricingSettingsTab } from './PricingSettingsTab';

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

function TemplateEditor({
  title,
  endpoint,
  kind,
  valueKey,
  valueLabel,
}: {
  title: string;
  endpoint: string;
  kind: 'markup' | 'bend' | 'noCompetitor';
  valueKey: 'markupPercent' | 'bendPercent';
  valueLabel: string;
}) {
  const [items, setItems] = useState<Template[]>([]);
  const [selectedId, setSelectedId] = useState<string>('new');
  const [draft, setDraft] = useState<Template>(() => emptyTemplate(kind));
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
      if (selectedId !== 'new') {
        const current = rows.find((row: Template) => String(row.id) === selectedId);
        if (current) setDraft(normalizeTemplate(current, kind));
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
  }, [endpoint]);

  const select = (value: string) => {
    setSelectedId(value);
    if (value === 'new') {
      setDraft(emptyTemplate(kind));
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
      const isNew = selectedId === 'new' || !draft.id;
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
              <SelectItem value="new">Новый шаблон</SelectItem>
              {items.map((item) => <SelectItem key={item.id} value={String(item.id)}>{item.name}</SelectItem>)}
            </SelectContent>
          </Select>
          <Input value={draft.name} onChange={(e) => setDraft((prev) => ({ ...prev, name: e.target.value }))} placeholder="Название" />
          <Input value={draft.code} onChange={(e) => setDraft((prev) => ({ ...prev, code: e.target.value }))} placeholder="Код" />
          <div className="flex gap-2">
            <Button size="sm" onClick={save} disabled={isLoading} className="bg-blue-600 hover:bg-blue-700">
              <Save className="mr-2 h-4 w-4" />Сохранить
            </Button>
            <Button size="sm" variant="outline" onClick={copy} disabled={isLoading || !draft.id}>
              <Copy className="mr-2 h-4 w-4" />Копировать
            </Button>
          </div>
        </div>
        <div className="mt-3">
          <Input value={draft.description} onChange={(e) => setDraft((prev) => ({ ...prev, description: e.target.value }))} placeholder="Описание" />
        </div>
      </div>

      <div className="admin-card p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
          <Button variant="outline" size="sm" onClick={addRow}><Plus className="mr-2 h-4 w-4" />Добавить строку</Button>
        </div>
        <div className="admin-table-card">
          <table className="admin-table">
            <thead>
              <tr>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">От</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">До</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">{valueLabel}</th>
                <th className="px-4 py-3 text-right text-sm font-medium text-gray-700">Действия</th>
              </tr>
            </thead>
            <tbody>
              {draft.rows.map((row, index) => (
                <tr key={index}>
                  <td className="px-4 py-3"><Input className="numeric-input" value={row.costFrom} onChange={(e) => updateRow(index, { costFrom: e.target.value })} /></td>
                  <td className="px-4 py-3"><Input className="numeric-input" value={row.costTo} onChange={(e) => updateRow(index, { costTo: e.target.value })} placeholder="∞" /></td>
                  <td className="px-4 py-3"><Input className="numeric-input" value={String(row[valueKey] || '')} onChange={(e) => updateRow(index, { [valueKey]: e.target.value })} /></td>
                  <td className="px-4 py-3 text-right">
                    <Button variant="ghost" size="sm" className="text-red-600 hover:text-red-700" onClick={() => removeRow(index)}>
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
  const [formatRuleId, setFormatRuleId] = useState<string>('none');
  const [appliedRule, setAppliedRule] = useState<AppliedRuleStatus | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
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
      setRules(Array.isArray(rulesData) ? rulesData : []);
      setMarkups(Array.isArray(markupsData) ? markupsData : []);
      setBends(Array.isArray(bendsData) ? bendsData : []);
      setNoCompetitors(Array.isArray(noCompData) ? noCompData : []);
      setRoundings(Array.isArray(roundingsData) ? roundingsData : []);
      setFormatRuleId(settingsData?.pricingRuleId ? String(settingsData.pricingRuleId) : 'none');
      setAppliedRule(settingsData?.appliedRule || null);
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки правил');
    } finally {
      setIsLoading(false);
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
      return;
    }
    const res = await fetch(`/api/pricing-rules/${value}`);
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (res.ok && data) setDraft(normalizeRule(data));
  };

  const saveRule = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const isNew = selectedRuleId === 'new' || !draft.id;
      const res = await fetch(isNew ? '/api/pricing-rules' : `/api/pricing-rules/${draft.id}`, {
        method: isNew ? 'POST' : 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(draft),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось сохранить правило');
      setSelectedRuleId(String(data.id));
      setDraft(normalizeRule(data));
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
    const res = await fetch(`/api/pricing-rules/${draft.id}/copy`, { method: 'POST' });
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (res.ok && data) {
      setSelectedRuleId(String(data.id));
      setDraft(normalizeRule(data));
      await load();
    }
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
                <Button size="sm" onClick={saveRule} disabled={isLoading} className="bg-blue-600 hover:bg-blue-700"><Save className="mr-2 h-4 w-4" />Сохранить</Button>
                <Button size="sm" variant="outline" onClick={copyRule} disabled={!draft.id}><Copy className="mr-2 h-4 w-4" />Копировать</Button>
                <Button size="sm" variant="ghost" className="text-red-600 hover:text-red-700" onClick={deleteRule} disabled={!draft.id}><Trash2 className="mr-2 h-4 w-4" />Удалить</Button>
              </div>
            </div>
            <Input value={draft.description} onChange={(e) => setDraft((prev) => ({ ...prev, description: e.target.value }))} placeholder="Описание" />
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <FieldSelect label="Рекомендованные наценки" value={draft.markupTemplateId} items={markups} onChange={(id) => setDraft((prev) => ({ ...prev, markupTemplateId: id }))} />
              <FieldSelect label="Прогибы" value={draft.bendTemplateId} items={bends} onChange={(id) => setDraft((prev) => ({ ...prev, bendTemplateId: id }))} />
              <FieldSelect label="Наценки без конкурентов" value={draft.noCompetitorTemplateId} items={noCompetitors} onChange={(id) => setDraft((prev) => ({ ...prev, noCompetitorTemplateId: id }))} />
              <FieldSelect label="Округление" value={draft.roundingRuleId} items={roundings} onChange={(id) => setDraft((prev) => ({ ...prev, roundingRuleId: id }))} />
            </div>
          </div>
        </div>
      </TabsContent>

      <TabsContent value="markups" className="m-0 pt-4"><TemplateEditor title="Диапазоны рекомендованных наценок" endpoint="/api/pricing-rules/markup-templates" kind="markup" valueKey="markupPercent" valueLabel="Наценка (%)" /></TabsContent>
      <TabsContent value="bends" className="m-0 pt-4"><TemplateEditor title="Диапазоны прогибов" endpoint="/api/pricing-rules/bend-templates" kind="bend" valueKey="bendPercent" valueLabel="Прогиб (%)" /></TabsContent>
      <TabsContent value="no-comp" className="m-0 pt-4"><TemplateEditor title="Диапазоны наценок без конкурентов" endpoint="/api/pricing-rules/no-competitor-templates" kind="noCompetitor" valueKey="markupPercent" valueLabel="Наценка (%)" /></TabsContent>
      <TabsContent value="rounding" className="m-0 pt-4"><RoundingEditor items={roundings} onReload={load} /></TabsContent>
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

function FieldSelect({ label, value, items, onChange }: { label: string; value: number | null; items: Array<{ id: number; name: string }>; onChange: (id: number | null) => void }) {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      <Select value={value ? String(value) : 'none'} onValueChange={(v) => onChange(v === 'none' ? null : Number(v))}>
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

function RoundingEditor({ items, onReload }: { items: RoundingRule[]; onReload: () => Promise<void> }) {
  const [selectedId, setSelectedId] = useState('new');
  const [draft, setDraft] = useState<RoundingRule>({ id: 0, code: '', name: '', mode: 'math', precision: 2, step: 0.01, isActive: true });
  const [error, setError] = useState<string | null>(null);

  const select = (value: string) => {
    setSelectedId(value);
    if (value === 'new') {
      setDraft({ id: 0, code: '', name: '', mode: 'math', precision: 2, step: 0.01, isActive: true });
      return;
    }
    const row = items.find((item) => String(item.id) === value);
    if (row) setDraft(row);
  };

  const save = async () => {
    setError(null);
    const isNew = selectedId === 'new' || !draft.id;
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
              <SelectItem value="new">Новое округление</SelectItem>
              {items.map((item) => <SelectItem key={item.id} value={String(item.id)}>{item.name}</SelectItem>)}
            </SelectContent>
          </Select>
          <Input value={draft.name} onChange={(e) => setDraft((prev) => ({ ...prev, name: e.target.value }))} placeholder="Название" />
          <Input value={draft.code} onChange={(e) => setDraft((prev) => ({ ...prev, code: e.target.value }))} placeholder="Код" />
          <Select value={draft.mode} onValueChange={(mode) => setDraft((prev) => ({ ...prev, mode }))}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="math">Математическое</SelectItem>
              <SelectItem value="up">Вверх</SelectItem>
              <SelectItem value="down">Вниз</SelectItem>
            </SelectContent>
          </Select>
          <Input value={String(draft.precision)} onChange={(e) => setDraft((prev) => ({ ...prev, precision: Number(e.target.value) }))} placeholder="Точность" />
          <Input value={draft.step == null ? '' : String(draft.step)} onChange={(e) => setDraft((prev) => ({ ...prev, step: e.target.value === '' ? null : Number(e.target.value) }))} placeholder="Шаг" />
          <Button onClick={save} className="bg-blue-600 hover:bg-blue-700"><Save className="mr-2 h-4 w-4" />Сохранить</Button>
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
