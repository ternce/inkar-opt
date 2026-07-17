import { Button } from './ui/button';
import { Input } from './ui/input';
import { Label } from './ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';
import { BarChart3, FileText, ListChecks, Save, Settings, Trash2, PlugZap, RefreshCw, Percent, SlidersHorizontal, TrendingDown } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { listTypeLabel } from './listTypeLabels';
import {
  competitorFreshnessClassName,
  competitorFreshnessLabel,
  competitorLastDataReplacement,
  competitorLastSuccessfulCheck,
  formatLocalDate,
  formatLocalDateTime,
} from '../competitorTimestamps';

type MarkupRow = {
  id: number;
  lowerBound: string;
  upperBound: string;
  markupPercent: string;
};

type BendRow = {
  id: number;
  priceFrom: string;
  bendPercent: string;
};

type PricingSettingsTabProps = {
  formatCode: string;
  onNavigate?: (section: 'pricing-workflow' | 'analytics' | 'pricelists' | 'competitors' | 'pricing' | 'universal-lists') => void;
};

type SourceAccount = {
  id: number;
  sourceType: string;
  login: string;
  status: string;
  statusMessage: string;
  lastSuccessAt: string;
  priceListsCount: number;
  isActive: boolean;
  config: Record<string, any>;
};

type PricingRuleOption = {
  id: number;
  name: string;
  description?: string;
};

type RoundingRuleOption = {
  id: number;
  name: string;
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

type FormatPassport = {
  id?: number;
  code: string;
  name: string;
  branch: string;
  lastGeneratedAt?: string;
  lastActivationDate?: string;
  lastRunStatus?: string;
  lastPriceListNumber?: string;
  lastSkuCount?: number;
};

type AssignmentRow = {
  id: string;
  sourceType: string;
  sourceName: string;
  region: string;
  competitorName: string;
  coefficient: number;
  priceDate: string;
  updatedAt?: string;
  sourceUpdatedAt?: string;
  lastCheckedAt?: string;
  lastSuccessAt?: string;
  lastUpdatedAt?: string;
  itemsCount: number;
  active: boolean;
};

type UniversalListRow = {
  id: number;
  name: string;
  type: string;
  typeLabel?: string;
  active: boolean;
  status: string;
  itemsCount: number;
  priceFormats: Array<{ code: string; name: string; branch: string }>;
  scope?: string;
  startDate?: string;
  endDate?: string;
};

type GeneratedPriceListRow = {
  number: string;
  createdAt?: string;
  activationDate?: string;
  status?: string;
  skuCount?: number;
};

type ReadinessItem = {
  kind: string;
  label: string;
  status: 'ok' | 'warning' | 'error' | string;
  message: string;
};

type FormatReadiness = {
  status: 'ok' | 'warning' | 'error' | string;
  canGenerate: boolean;
  items: ReadinessItem[];
};

const DEFAULT_MARKUPS: MarkupRow[] = [
  { id: 1, lowerBound: '0', upperBound: '499.99', markupPercent: '20' },
  { id: 2, lowerBound: '500', upperBound: '999.99', markupPercent: '5' },
  { id: 3, lowerBound: '1000', upperBound: '1999.99', markupPercent: '4' },
  { id: 4, lowerBound: '2000', upperBound: '4999.99', markupPercent: '3' },
  { id: 5, lowerBound: '5000', upperBound: '9999.99', markupPercent: '2.5' },
  { id: 6, lowerBound: '10000', upperBound: '99999999', markupPercent: '2' },
];

const DEFAULT_BENDS: BendRow[] = [
  { id: 1, priceFrom: '0', bendPercent: '0.5' },
  { id: 2, priceFrom: '500', bendPercent: '0.3' },
  { id: 3, priceFrom: '1000', bendPercent: '0.25' },
  { id: 4, priceFrom: '2000', bendPercent: '0.2' },
  { id: 5, priceFrom: '5000', bendPercent: '0.15' },
  { id: 6, priceFrom: '10000', bendPercent: '0.1' },
];

const fmtDateTime = (value?: string) => (value ? new Date(value).toLocaleString('ru-RU') : '—');
const fmtDate = (value?: string) => (value ? new Date(value).toLocaleDateString('ru-RU') : '—');
const fmtNumber = (value?: number | null) => (value === null || value === undefined ? '—' : Number(value).toLocaleString('ru-RU'));

const appliedRuleStatusText = (rule: AppliedRuleStatus | null) => {
  if (!rule || !rule.ruleId) return 'Не применено';
  return rule.isManualChanged ? 'Изменено вручную' : 'Синхронизировано';
};

const readinessText = (status: string) => {
  if (status === 'ok') return 'Готово';
  if (status === 'error') return 'Блокирует запуск';
  return 'Требует внимания';
};

const readinessClassName = (status: string) => {
  if (status === 'ok') return 'ok';
  if (status === 'error') return 'bad';
  return 'warn';
};

const tableLabel = (key: string) => {
  const labels: Record<string, string> = {
    markup: 'Рекомендованные наценки',
    bend: 'Прогибы',
    noCompetitor: 'Наценки без конкурентов',
    no_competitor: 'Наценки без конкурентов',
    rounding: 'Округление',
  };
  return labels[key] || key;
};

const listWillApply = (row: UniversalListRow) => {
  if (!row.active) return false;
  const today = new Date().toISOString().slice(0, 10);
  if (row.startDate && row.startDate > today) return false;
  if (row.endDate && row.endDate < today) return false;
  return row.itemsCount > 0;
};

export function PricingSettingsTab({ formatCode, onNavigate }: PricingSettingsTabProps) {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [name, setName] = useState(formatCode);
  const [branch, setBranch] = useState('');
  const [pricingRule, setPricingRule] = useState('');
  const [pricingRuleId, setPricingRuleId] = useState('none');
  const [pricingRules, setPricingRules] = useState<PricingRuleOption[]>([]);
  const [roundingRuleId, setRoundingRuleId] = useState('none');
  const [roundingRules, setRoundingRules] = useState<RoundingRuleOption[]>([]);
  const [appliedRule, setAppliedRule] = useState<AppliedRuleStatus | null>(null);
  const [competitorPriceMode, setCompetitorPriceMode] = useState('regular');
  const [percentileNumber, setPercentileNumber] = useState('10');
  const [deflectionPercent, setDeflectionPercent] = useState('0');
  const [recommendedMarkups, setRecommendedMarkups] = useState<MarkupRow[]>(DEFAULT_MARKUPS);
  const [noCompetitorMarkups, setNoCompetitorMarkups] = useState<MarkupRow[]>(DEFAULT_MARKUPS);
  const [bendRanges, setBendRanges] = useState<BendRow[]>(DEFAULT_BENDS);
  const [accounts, setAccounts] = useState<SourceAccount[]>([]);
  const [passport, setPassport] = useState<FormatPassport | null>(null);
  const [assignments, setAssignments] = useState<AssignmentRow[]>([]);
  const [universalLists, setUniversalLists] = useState<UniversalListRow[]>([]);
  const [generatedLists, setGeneratedLists] = useState<GeneratedPriceListRow[]>([]);
  const [readiness, setReadiness] = useState<FormatReadiness | null>(null);
  const [accountSource, setAccountSource] = useState('provisor');
  const [accountLogin, setAccountLogin] = useState('');
  const [accountPassword, setAccountPassword] = useState('');
  const [accountConfig, setAccountConfig] = useState('{"filialIds": []}');
  const [accountBusyId, setAccountBusyId] = useState<number | null>(null);

  const parseJsonOrNull = (text: string) => {
    try {
      return text ? JSON.parse(text) : null;
    } catch {
      return null;
    }
  };

  const loadAccounts = async () => {
    const res = await fetch('/api/price-source-accounts');
    const text = await res.text();
    const data = parseJsonOrNull(text);
    if (!res.ok) throw new Error((data && data.detail) || text || 'Не удалось загрузить аккаунты источников');
    setAccounts(Array.isArray(data) ? data.filter((x: SourceAccount) => x.isActive) : []);
  };

  const loadPassportData = async (currentBranch: string) => {
    const [
      formatsRes,
      assignmentsRes,
      listsRes,
      generatedRes,
      readinessRes,
    ] = await Promise.all([
      fetch('/api/pricing-workflow/branch-formats'),
      fetch(`/api/price-formats/${encodeURIComponent(formatCode)}/competitor-assignments`),
      fetch('/api/lists-management'),
      fetch(`/api/generated-price-lists?format_code=${encodeURIComponent(formatCode)}`),
      fetch(`/api/pricing-workflow/readiness?branch_id=${encodeURIComponent(currentBranch)}&format_codes=${encodeURIComponent(formatCode)}`),
    ]);
    const [formatsText, assignmentsText, listsText, generatedText, readinessTextRaw] = await Promise.all([
      formatsRes.text(),
      assignmentsRes.text(),
      listsRes.text(),
      generatedRes.text(),
      readinessRes.text(),
    ]);
    const formatsData = parseJsonOrNull(formatsText);
    const assignmentsData = parseJsonOrNull(assignmentsText);
    const listsData = parseJsonOrNull(listsText);
    const generatedData = parseJsonOrNull(generatedText);
    const readinessData = parseJsonOrNull(readinessTextRaw);

    if (formatsRes.ok && Array.isArray(formatsData)) {
      const row = formatsData.find((item: FormatPassport) => item.code === formatCode);
      setPassport(row || null);
    }
    if (assignmentsRes.ok) setAssignments(Array.isArray(assignmentsData) ? assignmentsData : []);
    if (listsRes.ok) {
      const rows = Array.isArray(listsData) ? listsData : [];
      setUniversalLists(rows.filter((row: UniversalListRow) =>
        row.scope === 'global' || row.priceFormats?.some((pf) => pf.code === formatCode)
      ));
    }
    if (generatedRes.ok) setGeneratedLists(Array.isArray(generatedData) ? generatedData : []);
    if (readinessRes.ok) setReadiness(Array.isArray(readinessData?.items) ? readinessData.items[0] || null : null);
  };

  useEffect(() => {
    setName(formatCode);
    const load = async () => {
      setIsLoading(true);
      setError(null);
      try {
        const [settingsRes, rulesRes, roundingsRes] = await Promise.all([
          fetch(`/api/price-formats/${encodeURIComponent(formatCode)}/settings`),
          fetch('/api/pricing-rules'),
          fetch('/api/pricing-rules/rounding-rules'),
        ]);
        const [text, rulesText, roundingsText] = await Promise.all([
          settingsRes.text(),
          rulesRes.text(),
          roundingsRes.text(),
        ]);
        const data = text ? JSON.parse(text) : null;
        const rulesData = parseJsonOrNull(rulesText);
        const roundingsData = parseJsonOrNull(roundingsText);
        if (!settingsRes.ok) throw new Error((data && data.detail) || 'Не удалось загрузить настройки');

        setPricingRules(Array.isArray(rulesData) ? rulesData : []);
        setRoundingRules(Array.isArray(roundingsData) ? roundingsData : []);
        setBranch(String(data?.branch ?? ''));
        setPricingRule(String(data?.pricingRule ?? ''));
        setPricingRuleId(data?.pricingRuleId ? String(data.pricingRuleId) : 'none');
        setRoundingRuleId(data?.roundingRuleId ? String(data.roundingRuleId) : 'none');
        setAppliedRule(data?.appliedRule || null);
        setCompetitorPriceMode(String(data?.competitorPriceMode ?? 'regular'));
        setPercentileNumber(String(data?.percentileNumber ?? '10'));
        setDeflectionPercent(String(data?.deflectionPercent ?? '0'));

        const rec = Array.isArray(data?.recommendedMarkups) ? data.recommendedMarkups : [];
        if (rec.length) {
          setRecommendedMarkups(
            rec.map((r: any, idx: number) => ({
              id: Number(r?.id ?? idx + 1),
              lowerBound: String(r?.lowerBound ?? ''),
              upperBound: String(r?.upperBound ?? ''),
              markupPercent: String(r?.markupPercent ?? ''),
            }))
          );
        } else {
          setRecommendedMarkups(DEFAULT_MARKUPS);
        }

        const noComp = Array.isArray(data?.noCompetitorMarkups) ? data.noCompetitorMarkups : [];
        if (noComp.length) {
          setNoCompetitorMarkups(
            noComp.map((r: any, idx: number) => ({
              id: Number(r?.id ?? idx + 1),
              lowerBound: String(r?.lowerBound ?? ''),
              upperBound: String(r?.upperBound ?? ''),
              markupPercent: String(r?.markupPercent ?? ''),
            }))
          );
        } else {
          setNoCompetitorMarkups(DEFAULT_MARKUPS);
        }

        const bends = Array.isArray(data?.bendRanges) ? data.bendRanges : [];
        if (bends.length) {
          setBendRanges(
            bends.map((r: any, idx: number) => ({
              id: Number(r?.id ?? idx + 1),
              priceFrom: String(r?.priceFrom ?? ''),
              bendPercent: String(r?.bendPercent ?? ''),
            }))
          );
        } else {
          setBendRanges(DEFAULT_BENDS);
        }
        await loadPassportData(String(data?.branch ?? ''));
      } catch (e: any) {
        setError(e?.message || 'Ошибка загрузки');
      } finally {
        setIsLoading(false);
      }
    };

    void load();
    void loadAccounts().catch((e: any) => setError(e?.message || 'Ошибка загрузки аккаунтов'));
  }, [formatCode]);

  const saveAccount = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const config = accountConfig.trim() ? JSON.parse(accountConfig) : {};
      const res = await fetch('/api/price-source-accounts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sourceType: accountSource,
          login: accountLogin,
          password: accountPassword,
          config,
        }),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error((data && data.detail) || text || 'Не удалось сохранить аккаунт');
      setAccountLogin('');
      setAccountPassword('');
      await loadAccounts();
      toast.success('Аккаунт источника сохранён');
    } catch (e: any) {
      setError(e?.message || 'Ошибка сохранения аккаунта');
      toast.error(e?.message || 'Ошибка сохранения аккаунта');
    } finally {
      setIsLoading(false);
    }
  };

  const testAccount = async (id: number) => {
    setAccountBusyId(id);
    setError(null);
    const url = `/api/price-source-accounts/${id}/test?format_code=${encodeURIComponent(formatCode)}`;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), 30000);
    try {
      console.log('TEST ACCOUNT START', url);
      const res = await fetch(url, { method: 'POST', signal: controller.signal });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      const looksLikeProxyFailure = /proxy|econnrefused|etimedout|socket|fetch failed|network error/i.test(text);
      if (!res.ok && looksLikeProxyFailure) {
        throw new Error('Запрос проверки аккаунта не дошел до сервера. Проверьте прокси или адрес запроса.');
      }
      if (!res.ok) throw new Error((data && data.detail) || text || 'Не удалось проверить подключение');
      await loadAccounts();
      if (data?.status === 'connected') toast.success(data?.statusMessage || 'Подключение проверено');
      else toast.error(data?.statusMessage || 'Подключение не установлено');
    } catch (e: any) {
      const message = e?.name === 'AbortError'
        ? 'Запрос проверки аккаунта не дошел до сервера. Проверьте прокси или адрес запроса.'
        : e?.message || 'Ошибка проверки подключения';
      setError(message);
      toast.error(message);
    } finally {
      window.clearTimeout(timeoutId);
      setAccountBusyId(null);
    }
  };

  const deleteAccount = async (id: number) => {
    setAccountBusyId(id);
    setError(null);
    try {
      const res = await fetch(`/api/price-source-accounts/${id}`, { method: 'DELETE' });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error((data && data.detail) || text || 'Не удалось удалить аккаунт');
      await loadAccounts();
      toast.success('Аккаунт удалён');
    } catch (e: any) {
      setError(e?.message || 'Ошибка удаления аккаунта');
      toast.error(e?.message || 'Ошибка удаления аккаунта');
    } finally {
      setAccountBusyId(null);
    }
  };

  const statusLabel = (status: string) => {
    if (status === 'connected') return 'Подключено';
    if (status === 'invalid_credentials') return 'Неверный логин или пароль';
    if (status === 'session_expired') return 'Сессия истекла';
    if (status === 'source_unavailable') return 'Источник недоступен';
    if (status === 'auth_error') return 'Ошибка авторизации';
    return 'Не проверено';
  };

  const canSave = useMemo(() => !isLoading, [isLoading]);

  const applyRuleToFormat = async () => {
    if (pricingRuleId === 'none') return;
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/price-formats/${encodeURIComponent(formatCode)}/pricing-rule`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pricingRuleId: Number(pricingRuleId) }),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error((data && data.detail) || text || 'Не удалось применить правило ЦО');
      setAppliedRule(data?.appliedRule || null);
      const settingsRes = await fetch(`/api/price-formats/${encodeURIComponent(formatCode)}/settings`);
      const settingsText = await settingsRes.text();
      const settings = parseJsonOrNull(settingsText);
      if (settingsRes.ok && settings) {
        setPricingRule(String(settings?.pricingRule ?? ''));
        setPricingRuleId(settings?.pricingRuleId ? String(settings.pricingRuleId) : 'none');
        setRoundingRuleId(settings?.roundingRuleId ? String(settings.roundingRuleId) : 'none');
        setAppliedRule(settings?.appliedRule || data?.appliedRule || null);
        setRecommendedMarkups((settings.recommendedMarkups || []).map((r: any, idx: number) => ({ id: Number(r?.id ?? idx + 1), lowerBound: String(r?.lowerBound ?? ''), upperBound: String(r?.upperBound ?? ''), markupPercent: String(r?.markupPercent ?? '') })));
        setNoCompetitorMarkups((settings.noCompetitorMarkups || []).map((r: any, idx: number) => ({ id: Number(r?.id ?? idx + 1), lowerBound: String(r?.lowerBound ?? ''), upperBound: String(r?.upperBound ?? ''), markupPercent: String(r?.markupPercent ?? '') })));
        setBendRanges((settings.bendRanges || []).map((r: any, idx: number) => ({ id: Number(r?.id ?? idx + 1), priceFrom: String(r?.priceFrom ?? ''), bendPercent: String(r?.bendPercent ?? '') })));
      }
      toast.success('Правило ЦО применено');
    } catch (e: any) {
      setError(e?.message || 'Ошибка применения правила ЦО');
      toast.error(e?.message || 'Ошибка применения правила ЦО');
    } finally {
      setIsLoading(false);
    }
  };

  const save = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const payload = {
        name,
        branch,
        pricingRule,
        roundingRuleId: roundingRuleId === 'none' ? null : Number(roundingRuleId),
        competitorPriceMode,
        percentileNumber: Number(percentileNumber),
        deflectionPercent: Number(deflectionPercent),
        recommendedMarkups: recommendedMarkups.map((r) => ({
          id: r.id,
          lowerBound: Number(r.lowerBound),
          upperBound: Number(r.upperBound),
          markupPercent: Number(r.markupPercent),
        })),
        noCompetitorMarkups: noCompetitorMarkups.map((r) => ({
          id: r.id,
          lowerBound: Number(r.lowerBound),
          upperBound: Number(r.upperBound),
          markupPercent: Number(r.markupPercent),
        })),
        bendRanges: bendRanges.map((r) => ({
          id: r.id,
          priceFrom: Number(r.priceFrom),
          bendPercent: Number(r.bendPercent),
        })),
      };

      const res = await fetch(`/api/price-formats/${encodeURIComponent(formatCode)}/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const text = await res.text();
      const data = text ? JSON.parse(text) : null;
      if (!res.ok) throw new Error((data && data.detail) || 'Не удалось сохранить настройки');

      // refresh from server response
      setBranch(String(data?.branch ?? ''));
      setPricingRule(String(data?.pricingRule ?? ''));
      setPricingRuleId(data?.pricingRuleId ? String(data.pricingRuleId) : 'none');
      setRoundingRuleId(data?.roundingRuleId ? String(data.roundingRuleId) : 'none');
      setAppliedRule(data?.appliedRule || null);
      setCompetitorPriceMode(String(data?.competitorPriceMode ?? 'regular'));
      setPercentileNumber(String(data?.percentileNumber ?? '10'));
      setDeflectionPercent(String(data?.deflectionPercent ?? '0'));
      if (Array.isArray(data?.recommendedMarkups)) {
        setRecommendedMarkups(
          data.recommendedMarkups.map((r: any, idx: number) => ({
            id: Number(r?.id ?? idx + 1),
            lowerBound: String(r?.lowerBound ?? ''),
            upperBound: String(r?.upperBound ?? ''),
            markupPercent: String(r?.markupPercent ?? ''),
          }))
        );
      }
      if (Array.isArray(data?.noCompetitorMarkups)) {
        setNoCompetitorMarkups(
          data.noCompetitorMarkups.map((r: any, idx: number) => ({
            id: Number(r?.id ?? idx + 1),
            lowerBound: String(r?.lowerBound ?? ''),
            upperBound: String(r?.upperBound ?? ''),
            markupPercent: String(r?.markupPercent ?? ''),
          }))
        );
      }
      if (Array.isArray(data?.bendRanges)) {
        setBendRanges(
          data.bendRanges.map((r: any, idx: number) => ({
            id: Number(r?.id ?? idx + 1),
            priceFrom: String(r?.priceFrom ?? ''),
            bendPercent: String(r?.bendPercent ?? ''),
          }))
        );
      }
      await loadPassportData(String(data?.branch ?? branch ?? ''));
    } catch (e: any) {
      setError(e?.message || 'Ошибка сохранения');
    } finally {
      setIsLoading(false);
    }
  };

  const latestGenerated = generatedLists[0];
  const displayPassport = passport || {
    code: formatCode,
    name: name || formatCode,
    branch,
    lastGeneratedAt: latestGenerated?.createdAt,
    lastActivationDate: latestGenerated?.activationDate,
    lastRunStatus: latestGenerated?.status,
    lastPriceListNumber: latestGenerated?.number,
    lastSkuCount: latestGenerated?.skuCount,
  };
  const activeAssignments = assignments.filter((row) => row.active !== false);
  const activeLists = universalLists.filter((row) => row.active);
  const ruleTables = appliedRule?.tablesUpdated?.length ? appliedRule.tablesUpdated : ['markup', 'bend', 'no_competitor', 'rounding'];
  const readinessGroups = [
    { title: 'Справочники', items: readiness?.items?.filter((item) => ['products', 'rating_global', 'rating_local'].includes(item.kind)) || [] },
    { title: 'ПЛК', items: readiness?.items?.filter((item) => ['competitors', 'competitor_freshness'].includes(item.kind)) || [] },
    { title: 'Правило ЦО', items: readiness?.items?.filter((item) => ['pricing_rule', 'markup', 'bend', 'no_competitor'].includes(item.kind)) || [] },
    { title: 'Списки', items: [{ kind: 'lists', label: 'Универсальные списки', status: activeLists.length ? 'ok' : 'warning', message: `Активных списков: ${activeLists.length}` }] },
    { title: 'Себестоимость/остатки', items: readiness?.items?.filter((item) => ['cost', 'stock'].includes(item.kind)) || [] },
  ];

  return (
    <div className="space-y-6">
      <section className="admin-card p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="text-xs font-medium uppercase tracking-wide text-gray-500">Паспорт ЦФ</div>
            <h2 className="mt-1 text-2xl font-semibold text-gray-900">{displayPassport.name || displayPassport.code}</h2>
            <div className="mt-2 flex flex-wrap gap-2 text-sm">
              <span className="status-pill">{displayPassport.code}</span>
              <span className="status-pill">{displayPassport.branch || 'Филиал не указан'}</span>
              <span className={`status-pill ${readinessClassName(readiness?.status || 'warning')}`}>{readinessText(readiness?.status || 'warning')}</span>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3 text-sm lg:min-w-[420px]">
            <div><div className="text-gray-500">Последний прайс</div><strong className="text-gray-900">{displayPassport.lastPriceListNumber || latestGenerated?.number || '—'}</strong></div>
            <div><div className="text-gray-500">Дата формирования</div><strong className="text-gray-900">{fmtDateTime(displayPassport.lastGeneratedAt || latestGenerated?.createdAt)}</strong></div>
            <div><div className="text-gray-500">Дата начала действия</div><strong className="text-gray-900">{fmtDate(displayPassport.lastActivationDate || latestGenerated?.activationDate)}</strong></div>
            <div><div className="text-gray-500">SKU в прайсе</div><strong className="text-gray-900">{fmtNumber(displayPassport.lastSkuCount || latestGenerated?.skuCount)}</strong></div>
          </div>
        </div>
      </section>

      <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <div className="admin-card p-5 space-y-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="text-base font-semibold text-gray-900">Правило ценообразования</h3>
              <p className="mt-1 text-sm text-gray-600">Как сейчас считается этот ценовой формат.</p>
            </div>
            <span className={`status-pill ${appliedRule?.isManualChanged ? 'warn' : appliedRule?.ruleId ? 'ok' : 'bad'}`}>{appliedRuleStatusText(appliedRule)}</span>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div><div className="text-sm text-gray-500">Примененное правило</div><strong className="text-gray-900">{appliedRule?.ruleName || pricingRule || 'Не выбрано'}</strong></div>
            <div><div className="text-sm text-gray-500">Дата применения</div><strong className="text-gray-900">{fmtDateTime(appliedRule?.appliedAt)}</strong></div>
          </div>
          {appliedRule?.isManualChanged ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
              Часть параметров изменена вручную. При следующем применении правила ЦО ручные изменения могут быть перезаписаны шаблоном.
            </div>
          ) : null}
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {ruleTables.map((item) => (
              <div key={item} className="rounded-md border border-gray-200 px-3 py-2 text-sm">
                <span className="status-pill ok">Обновлено</span>
                <div className="mt-2 font-medium text-gray-900">{tableLabel(item)}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="admin-card p-5 space-y-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="text-base font-semibold text-gray-900">Быстрые действия</h3>
              <p className="mt-1 text-sm text-gray-600">Переходы к рабочим разделам по этому ЦФ.</p>
            </div>
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <Button variant="outline" onClick={() => onNavigate?.('pricing-workflow')}><FileText className="mr-2 h-4 w-4" />Формирование прайс-листа</Button>
            <Button variant="outline" onClick={() => onNavigate?.('analytics')}><BarChart3 className="mr-2 h-4 w-4" />Открыть аналитику</Button>
            <Button variant="outline" onClick={() => onNavigate?.('pricelists')}><FileText className="mr-2 h-4 w-4" />Сформированные прайсы</Button>
            <Button variant="outline" onClick={() => onNavigate?.('competitors')}><PlugZap className="mr-2 h-4 w-4" />Настроить ПЛК</Button>
            <Button variant="outline" onClick={() => onNavigate?.('pricing')}><Settings className="mr-2 h-4 w-4" />Изменить правило ЦО</Button>
            <Button variant="outline" onClick={() => onNavigate?.('universal-lists')}><ListChecks className="mr-2 h-4 w-4" />Универсальные списки</Button>
          </div>
        </div>
      </section>

      <section className="admin-card p-5 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold text-gray-900">Источники конкурентов</h3>
            <p className="mt-1 text-sm text-gray-600">Назначенные ПЛК, которые участвуют в расчете.</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => onNavigate?.('competitors')}>Перейти к назначению ПЛК</Button>
        </div>
        <div className="admin-table-card">
          <table className="admin-table">
            <thead><tr><th>Источник / конкурент</th><th>Регион</th><th>Дата цен</th><th>Последняя успешная проверка</th><th>Последняя замена данных</th><th>Коэффициент</th><th>Актуальность</th></tr></thead>
            <tbody>
              {activeAssignments.map((row) => (
                <tr key={row.id}>
                  <td><div className="font-medium text-gray-900">{row.competitorName || row.sourceName}</div><div className="text-xs text-gray-500">{row.sourceName}</div></td>
                  <td>{row.region || '—'}</td>
                  <td>{formatLocalDate(row.priceDate)}</td>
                  <td>{formatLocalDateTime(competitorLastSuccessfulCheck(row))}</td>
                  <td>{formatLocalDateTime(competitorLastDataReplacement(row))}</td>
                  <td>{Number(row.coefficient || 1).toLocaleString('ru-RU')}</td>
                  <td><span className={`status-pill ${competitorFreshnessClassName(row)}`}>{competitorFreshnessLabel(row)}</span></td>
                </tr>
              ))}
              {!activeAssignments.length ? <tr><td colSpan={7} className="empty-cell">ПЛК не назначены</td></tr> : null}
            </tbody>
          </table>
        </div>
      </section>

      <section className="admin-card p-5 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold text-gray-900">Универсальные списки</h3>
            <p className="mt-1 text-sm text-gray-600">Активные списки, которые связаны с этим ЦФ или действуют глобально.</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => onNavigate?.('universal-lists')}>Открыть универсальные списки</Button>
        </div>
        <div className="admin-table-card">
          <table className="admin-table">
            <thead><tr><th>Список</th><th>Тип</th><th>Товаров</th><th>Период действия</th><th>Следующее формирование</th></tr></thead>
            <tbody>
              {activeLists.map((row) => (
                <tr key={row.id}>
                  <td><div className="font-medium text-gray-900">{row.name}</div><div className="text-xs text-gray-500">{row.scope === 'global' ? 'Для всех ЦФ' : 'Привязан к ЦФ'}</div></td>
                  <td>{listTypeLabel(row.type, row.typeLabel)}</td>
                  <td>{fmtNumber(row.itemsCount)}</td>
                  <td>{row.startDate || '—'} — {row.endDate || '—'}</td>
                  <td><span className={`status-pill ${listWillApply(row) ? 'ok' : 'warn'}`}>{listWillApply(row) ? 'Будет применяться' : 'Не будет применяться'}</span></td>
                </tr>
              ))}
              {!activeLists.length ? <tr><td colSpan={5} className="empty-cell">Активных списков для ЦФ нет</td></tr> : null}
            </tbody>
          </table>
        </div>
      </section>

      <section className="admin-card p-5 space-y-4">
        <h3 className="text-base font-semibold text-gray-900">Готовность к формированию</h3>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-5">
          {readinessGroups.map((group) => {
            const statuses = group.items.map((item) => item.status);
            const status = statuses.includes('error') ? 'error' : statuses.includes('warning') ? 'warning' : 'ok';
            return (
              <div key={group.title} className="rounded-md border border-gray-200 p-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="font-medium text-gray-900">{group.title}</div>
                  <span className={`status-pill ${readinessClassName(status)}`}>{readinessText(status)}</span>
                </div>
                <div className="mt-3 space-y-2 text-xs text-gray-600">
                  {group.items.map((item) => <div key={item.kind}>{item.label}: {item.message}</div>)}
                </div>
              </div>
            );
          })}
        </div>
      </section>

      <details className="admin-card p-5">
        <summary className="cursor-pointer text-base font-semibold text-gray-900">Технические детали</summary>
        <div className="mt-5 space-y-6">
      {/* Settings Form */}
      <div className="admin-card p-6 space-y-5">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900 mb-4">
          <SlidersHorizontal className="h-4 w-4 text-blue-600" />
          Основные параметры ценообразования
        </h3>
        
        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label htmlFor="name">Наименование</Label>
            <Input id="name" value={name} onChange={(e) => setName(e.target.value)} disabled={isLoading} />
          </div>
          
          <div className="space-y-2">
            <Label htmlFor="branch">Филиал</Label>
            <Select value={branch || 'none'} onValueChange={(v) => setBranch(v === 'none' ? '' : v)} disabled={isLoading}>
              <SelectTrigger id="branch">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">—</SelectItem>
                <SelectItem value="Астана">Астана</SelectItem>
                <SelectItem value="Алматы">Алматы</SelectItem>
                <SelectItem value="Шымкент">Шымкент</SelectItem>
              </SelectContent>
            </Select>
          </div>
          
          <div className="space-y-2 col-span-2">
            <Label htmlFor="rule">Правило ЦО</Label>
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_auto]">
              <Select value={pricingRuleId} onValueChange={setPricingRuleId} disabled={isLoading}>
                <SelectTrigger id="rule">
                  <SelectValue placeholder="Выберите правило ЦО" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">Не выбрано</SelectItem>
                  {pricingRules.map((rule) => (
                    <SelectItem key={rule.id} value={String(rule.id)}>{rule.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button type="button" onClick={applyRuleToFormat} disabled={isLoading || pricingRuleId === 'none'} className="bg-blue-600 hover:bg-blue-700">
                Применить к ЦФ
              </Button>
            </div>
            {appliedRule ? <AppliedRuleStatusCard appliedRule={appliedRule} /> : null}
          </div>

          <div className="space-y-2">
            <Label htmlFor="competitor-mode">Источник цены конкурента</Label>
            <Select value={competitorPriceMode || 'regular'} onValueChange={setCompetitorPriceMode} disabled={isLoading}>
              <SelectTrigger id="competitor-mode">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="regular">Обычные цены</SelectItem>
                <SelectItem value="percentile">Персентиль</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="percentile-number">Персентиль</Label>
            <Select value={percentileNumber || '10'} onValueChange={setPercentileNumber} disabled={isLoading || competitorPriceMode !== 'percentile'}>
              <SelectTrigger id="percentile-number">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {[10, 20, 30, 40, 50, 60, 70, 80, 90].map((pct) => (
                  <SelectItem key={pct} value={String(pct)}>{pct}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2 col-span-2">
            <Label htmlFor="rounding-rule">Округление</Label>
            <Select value={roundingRuleId} onValueChange={setRoundingRuleId} disabled={isLoading}>
              <SelectTrigger id="rounding-rule">
                <SelectValue placeholder="Правило округления" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">Не выбрано</SelectItem>
                {roundingRules.map((rule) => (
                  <SelectItem key={rule.id} value={String(rule.id)}>{rule.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2 col-span-2">
            <Label htmlFor="deflection">Прогиб по умолчанию (%)</Label>
            <Input
              id="deflection"
              value={deflectionPercent}
              onChange={(e) => setDeflectionPercent(e.target.value)}
              disabled={isLoading}
            />
            <div className="text-xs text-gray-500">
              Используется как запасной вариант, если таблица прогиба ниже пустая.
            </div>
          </div>
        </div>
        
        <div className="flex justify-end">
          <Button size="sm" className="bg-blue-600 hover:bg-blue-700" onClick={save} disabled={!canSave}>
            <Save className="h-4 w-4 mr-2" />
            Сохранить настройки
          </Button>
        </div>
      </div>

      {error ? <div className="text-sm text-red-600">{error}</div> : null}

      <div className="admin-card p-6 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-gray-900">Аккаунты источников прайс-листов</h3>
          <Button variant="outline" size="sm" onClick={() => loadAccounts()} disabled={isLoading}>
            <RefreshCw className="h-4 w-4 mr-2" />
            Обновить
          </Button>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-[180px_1fr_1fr_1.5fr_auto] gap-3 items-end">
          <div className="space-y-2">
            <Label>Источник</Label>
            <Select value={accountSource} onValueChange={(v) => {
              setAccountSource(v);
              setAccountConfig(v === 'provisor' ? '{"filialIds": []}' : '{"priceListIds": []}');
            }}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="provisor">Провизор</SelectItem>
                <SelectItem value="vidman">Видман</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>Логин</Label>
            <Input value={accountLogin} onChange={(e) => setAccountLogin(e.target.value)} disabled={isLoading} />
          </div>
          <div className="space-y-2">
            <Label>Пароль</Label>
            <Input type="password" value={accountPassword} onChange={(e) => setAccountPassword(e.target.value)} disabled={isLoading} />
          </div>
          <div className="space-y-2">
            <Label>Настройки JSON</Label>
            <Input value={accountConfig} onChange={(e) => setAccountConfig(e.target.value)} disabled={isLoading} />
          </div>
          <Button size="sm" className="bg-blue-600 hover:bg-blue-700" onClick={saveAccount} disabled={isLoading}>
            Сохранить
          </Button>
        </div>

        <div className="border border-gray-200 rounded-lg overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Источник</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Логин</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Статус</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Последняя синхронизация</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Прайсов</th>
                  <th className="px-4 py-3 text-right text-sm font-medium text-gray-700">Действия</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account) => (
                  <tr key={account.id} className="border-b border-gray-200 hover:bg-gray-50">
                    <td className="px-4 py-3 text-sm text-gray-900">{account.sourceType === 'provisor' ? 'Провизор' : 'Видман'}</td>
                    <td className="px-4 py-3 text-sm text-gray-700">{account.login}</td>
                    <td className="px-4 py-3 text-sm">
                      <div className={account.status === 'connected' ? 'text-green-700' : 'text-amber-700'}>
                        {statusLabel(account.status)}
                      </div>
                      {account.statusMessage ? <div className="text-xs text-gray-500 max-w-md truncate">{account.statusMessage}</div> : null}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700 whitespace-nowrap">
                      {account.lastSuccessAt ? new Date(account.lastSuccessAt).toLocaleString('ru-RU') : '—'}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      {account.sourceType === 'vidman' && account.status === 'connected' && Number(account.priceListsCount || 0) === 0
                        ? <span className="text-xs text-gray-500">После обновления</span>
                        : account.priceListsCount}
                    </td>
                    <td className="px-4 py-3 text-sm">
                      <div className="flex items-center justify-end gap-2">
                        <Button variant="outline" size="sm" onClick={() => testAccount(account.id)} disabled={accountBusyId === account.id}>
                          <PlugZap className="h-4 w-4 mr-2" />
                          Проверить
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => deleteAccount(account.id)} disabled={accountBusyId === account.id}>
                          <Trash2 className="h-4 w-4 mr-2" />
                          Удалить
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
                {!accounts.length ? (
                  <tr>
                    <td className="px-4 py-6 text-sm text-gray-500 text-center" colSpan={6}>
                      Аккаунты источников ещё не добавлены.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Markup Rules Table */}
      <div className="admin-card p-5 space-y-4">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900">
          <Percent className="h-4 w-4 text-blue-600" />
          Таблица рекомендуемых наценок
        </h3>
        <div className="admin-table-card">
          <div className="overflow-x-auto">
            <table className="admin-table">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-200">
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700 w-16">
                    №
                  </th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">
                    Нижняя граница
                  </th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">
                    Верхняя граница
                  </th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">
                    Наценка
                  </th>
                </tr>
              </thead>
              <tbody>
                {recommendedMarkups.map((rule) => (
                  <tr
                    key={rule.id}
                    className="border-b border-gray-200 hover:bg-gray-50"
                  >
                    <td className="px-4 py-3 text-sm text-gray-900">
                      {rule.id}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      <Input
                        className="numeric-input"
                        value={rule.lowerBound}
                        disabled={isLoading}
                        onChange={(e) =>
                          setRecommendedMarkups((prev) =>
                            prev.map((x) => (x.id === rule.id ? { ...x, lowerBound: e.target.value } : x))
                          )
                        }
                      />
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      <Input
                        className="numeric-input"
                        value={rule.upperBound}
                        disabled={isLoading}
                        onChange={(e) =>
                          setRecommendedMarkups((prev) =>
                            prev.map((x) => (x.id === rule.id ? { ...x, upperBound: e.target.value } : x))
                          )
                        }
                      />
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      <div className="percent-field">
                        <Input
                          className="numeric-input"
                          value={rule.markupPercent}
                          disabled={isLoading}
                          onChange={(e) =>
                            setRecommendedMarkups((prev) =>
                              prev.map((x) => (x.id === rule.id ? { ...x, markupPercent: e.target.value } : x))
                            )
                          }
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="admin-card p-5 space-y-4">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900">
          <Percent className="h-4 w-4 text-blue-600" />
          Шкала наценок для товаров без цен конкурентов
        </h3>
        <div className="admin-table-card">
          <div className="overflow-x-auto">
            <table className="admin-table">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-200">
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700 w-16">№</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Нижняя граница</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Верхняя граница</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Наценка</th>
                </tr>
              </thead>
              <tbody>
                {noCompetitorMarkups.map((rule) => (
                  <tr key={rule.id} className="border-b border-gray-200 hover:bg-gray-50">
                    <td className="px-4 py-3 text-sm text-gray-900">{rule.id}</td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      <Input
                        className="numeric-input"
                        value={rule.lowerBound}
                        disabled={isLoading}
                        onChange={(e) =>
                          setNoCompetitorMarkups((prev) =>
                            prev.map((x) => (x.id === rule.id ? { ...x, lowerBound: e.target.value } : x))
                          )
                        }
                      />
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      <Input
                        className="numeric-input"
                        value={rule.upperBound}
                        disabled={isLoading}
                        onChange={(e) =>
                          setNoCompetitorMarkups((prev) =>
                            prev.map((x) => (x.id === rule.id ? { ...x, upperBound: e.target.value } : x))
                          )
                        }
                      />
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      <div className="percent-field">
                        <Input
                          className="numeric-input"
                          value={rule.markupPercent}
                          disabled={isLoading}
                          onChange={(e) =>
                            setNoCompetitorMarkups((prev) =>
                              prev.map((x) => (x.id === rule.id ? { ...x, markupPercent: e.target.value } : x))
                            )
                          }
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="admin-card p-5 space-y-4">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900">
          <TrendingDown className="h-4 w-4 text-blue-600" />
          Таблица прогиба по цене конкурента
        </h3>
        <div className="admin-table-card">
          <div className="overflow-x-auto">
            <table className="admin-table">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-200">
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700 w-16">№</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Цена от</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">Прогиб (%)</th>
                </tr>
              </thead>
              <tbody>
                {bendRanges.map((rule) => (
                  <tr key={rule.id} className="border-b border-gray-200 hover:bg-gray-50">
                    <td className="px-4 py-3 text-sm text-gray-900">{rule.id}</td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      <Input
                        className="numeric-input"
                        value={rule.priceFrom}
                        disabled={isLoading}
                        onChange={(e) =>
                          setBendRanges((prev) =>
                            prev.map((x) => (x.id === rule.id ? { ...x, priceFrom: e.target.value } : x))
                          )
                        }
                      />
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700">
                      <div className="percent-field">
                        <Input
                          className="numeric-input"
                          value={rule.bendPercent}
                          disabled={isLoading}
                          onChange={(e) =>
                            setBendRanges((prev) =>
                              prev.map((x) => (x.id === rule.id ? { ...x, bendPercent: e.target.value } : x))
                            )
                          }
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
        </div>
      </details>
    </div>
  );
}

function AppliedRuleStatusCard({ appliedRule }: { appliedRule: AppliedRuleStatus }) {
  const updated = appliedRule.tablesUpdated?.length ? appliedRule.tablesUpdated.join(', ') : 'нет данных';
  const changed = appliedRule.tablesChanged?.length ? appliedRule.tablesChanged.join(', ') : '';
  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${appliedRule.isManualChanged ? 'border-amber-200 bg-amber-50 text-amber-800' : 'border-green-200 bg-green-50 text-green-800'}`}>
      <div className="font-medium">
        {appliedRule.ruleName || 'Правило ЦО'} · {appliedRule.appliedAt ? new Date(appliedRule.appliedAt).toLocaleString('ru-RU') : 'не применялось'}
      </div>
      <div className="mt-1">
        {appliedRule.isManualChanged ? 'изменено вручную' : 'синхронизировано'} · обновлено: {updated}
        {changed ? ` · отличается: ${changed}` : ''}
      </div>
    </div>
  );
}
