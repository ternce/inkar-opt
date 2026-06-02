import { useEffect, useMemo, useState } from 'react';
import { Toaster } from 'sonner';
import {
  BarChart3,
  BookOpen,
  BriefcaseBusiness,
  Calculator,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  Database,
  FileText,
  Home,
  ListChecks,
  Plus,
  RefreshCw,
  Settings,
  SlidersHorizontal,
  Users,
} from 'lucide-react';
import { Button } from './components/ui/button';
import { PriceListsTab } from './components/PriceListsTab';
import { CompetitorAssignmentTab } from './components/CompetitorAssignmentTab';
import { CompetitorsTab } from './components/CompetitorsTab';
import { ReferencesTab } from './components/ReferencesTab';
import { PricingWorkflowTab } from './components/PricingWorkflowTab';
import { ListsManagementTab } from './components/ListsManagementTab';
import { UniversalListsOverviewTab } from './components/UniversalListsOverviewTab';
import { ContractorsTab } from './components/ContractorsTab';
import { PricingRulesTab } from './components/PricingRulesTab';
import { AnalyticsTab } from './components/AnalyticsTab';

interface PriceFormat {
  id: string;
  name: string;
  code: string;
  branch: string;
}

type CurrentUser = {
  username: string;
  displayName: string;
  role: string;
  isReadOnly: boolean;
  canSeeAllBranches: boolean;
  canWrite: boolean;
  branches: Array<{ branchId: string; branchName: string }>;
};

type NavigationKey =
  | 'home'
  | 'pricing-workflow'
  | 'pricelists'
  | 'competitors'
  | 'lists'
  | 'universal-lists'
  | 'contractors'
  | 'settings'
  | 'pricing'
  | 'references'
  | 'competitor-domain'
  | 'analytics';

type NavigationItem = {
  key: NavigationKey;
  label: string;
  description: string;
  icon: typeof Home;
};

type PricingContextState = {
  branch: string;
  region: string;
  priceFormatCode: string;
};

type FormatDashboardRow = {
  code: string;
  name: string;
  branch: string;
  pricingRule: string;
  status: string;
  lastGeneratedAt: string;
  lastActivationDate: string;
  user: string;
  dataStatus: string;
};

const navigationItems: NavigationItem[] = [
  { key: 'home', label: 'Начальная страница', description: 'Рабочий экран менеджера по выбранному филиалу', icon: Home },
  { key: 'pricing-workflow', label: 'Формирование прайс-листа', description: 'Пошаговый workflow формирования нового прайса', icon: Calculator },
  { key: 'pricelists', label: 'Сформированные прайс-листы', description: 'Просмотр, анализ и экспорт рассчитанных прайс-листов', icon: FileText },
  { key: 'competitors', label: 'Назначение ПЛК', description: 'Выбор источников конкурентов для расчёта', icon: ClipboardList },
  { key: 'lists', label: 'Работа со списками', description: 'Ограничения, фиксированные цены и списочные правила', icon: ListChecks },
  { key: 'universal-lists', label: 'Универсальные списки', description: 'Обзор активных списков, привязок к ЦФ и влияния на формирование', icon: BookOpen },
  { key: 'contractors', label: 'Контрагенты', description: 'Контрагенты, холдинги и связанные данные', icon: BriefcaseBusiness },
  { key: 'settings', label: 'Настройки', description: 'Общие настройки модуля и служебные параметры', icon: Settings },
  { key: 'pricing', label: 'Ценообразование', description: 'Правила ЦО, шаблоны наценок, прогибы и округления', icon: SlidersHorizontal },
  { key: 'references', label: 'Справочники', description: 'Загрузка, статусы и история обновления справочников', icon: Database },
  { key: 'competitor-domain', label: 'Конкуренты', description: 'Прайс-листы конкурентов, персентили и соответствия кодов', icon: Users },
  { key: 'analytics', label: 'Итоги ЦО', description: 'Итоги переоценки, изменения цен и причины расчёта', icon: BarChart3 },
];

const defaultBranches = [
  'Алматы',
  'Астана',
  'Шымкент',
  'Актау',
  'Актобе',
  'Атырау',
  'Караганда',
  'Костанай',
  'Кызылорда',
  'Павлодар',
  'Петропавловск',
  'Талдыкорган',
  'Уральск',
  'Усть-Каменогорск',
];

const parseJsonOrNull = (text: string) => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

const fmtDate = (value: any) => {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('ru-RU');
};

const branchKey = (value: any) => String(value || '').trim().toLocaleLowerCase('ru-RU');

const isSameBranch = (left: any, right: any) => branchKey(left) === branchKey(right);

const uniqueByBranch = (branches: string[]) =>
  Array.from(new Map(branches.map((branch) => [branchKey(branch), branch])).values());

const statusLabel = (value: any) => {
  const text = String(value || '').toLowerCase();
  if (['fresh', 'success', 'ok', 'актуально'].includes(text)) return 'актуально';
  if (['error', 'ошибка'].includes(text)) return 'ошибка';
  if (['stale', 'missing', 'устарело'].includes(text)) return 'устарело';
  if (['running'].includes(text)) return 'обновляется';
  return value || 'устарело';
};

const freshnessClassName = (value: any) => {
  const label = statusLabel(value);
  if (label === 'актуально') return 'ok';
  if (label === 'ошибка') return 'bad';
  if (label === 'обновляется') return 'warn';
  return '';
};

const priceDateFreshness = (value: any) => {
  if (!value) return 'устарело';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return 'актуально';
  const ageDays = (Date.now() - parsed.getTime()) / 86400000;
  return ageDays <= 2 ? 'актуально' : 'устарело';
};

export default function App() {
  const [priceFormats, setPriceFormats] = useState<PriceFormat[]>([]);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [selectedBranch, setSelectedBranch] = useState(() => localStorage.getItem('selectedBranch') || '');
  const [selectedFormat, setSelectedFormat] = useState<PriceFormat | null>(null);
  const [selectedPricingContext, setSelectedPricingContext] = useState<PricingContextState | null>(null);
  const [activeSection, setActiveSection] = useState<NavigationKey>('home');
  const [focusedPriceListNumber, setFocusedPriceListNumber] = useState('');
  const [sidebarWidth, setSidebarWidth] = useState(() => Number(localStorage.getItem('sidebarWidth') || 320));
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem('sidebarCollapsed') === '1');
  const [isResizingSidebar, setIsResizingSidebar] = useState(false);

  const branchOptions = useMemo(
    () => uniqueByBranch([...priceFormats.map((format) => format.branch), ...defaultBranches]),
    [priceFormats]
  );

  const branchFormats = useMemo(
    () => priceFormats.filter((format) => !selectedBranch || isSameBranch(format.branch, selectedBranch)),
    [priceFormats, selectedBranch]
  );

  const activeItem = useMemo(
    () => navigationItems.find((item) => item.key === activeSection) || navigationItems[0],
    [activeSection]
  );

  const normalizeFormats = (data: any): PriceFormat[] => (
    Array.isArray(data)
      ? data.map((x: any, idx: number) => ({
          id: String(x.id ?? idx + 1),
          name: String(x.name ?? ''),
          code: String(x.code ?? ''),
          branch: String(x.branch ?? ''),
        }))
      : []
  );

  const loadPriceFormats = async () => {
    const formatsRes = await fetch('/api/price-formats');
    const formatsText = await formatsRes.text();
    return normalizeFormats(parseJsonOrNull(formatsText));
  };

  useEffect(() => {
    const load = async () => {
      const [userRes, items] = await Promise.all([
        fetch('/api/current-user'),
        loadPriceFormats(),
      ]);
      const userText = await userRes.text();
      const userData = parseJsonOrNull(userText);
      if (userRes.ok && userData) setCurrentUser(userData);

      const storedBranchHasFormats = selectedBranch && items.some((format) => isSameBranch(format.branch, selectedBranch));
      const firstBranch = storedBranchHasFormats ? selectedBranch : (items[0]?.branch || '');
      const firstFormat = items.find((format) => isSameBranch(format.branch, firstBranch)) || items[0] || null;
      setPriceFormats(items);
      setSelectedBranch(firstBranch);
      setSelectedFormat((prev) => {
        if (prev && items.some((format) => format.code === prev.code && isSameBranch(format.branch, firstBranch))) return prev;
        return firstFormat;
      });
    };
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selectedBranch || !priceFormats.length) return;
    const currentStillValid = selectedFormat && isSameBranch(selectedFormat.branch, selectedBranch);
    if (!currentStillValid && branchFormats.length > 0) {
      setSelectedFormat(branchFormats[0] || null);
    }
    localStorage.setItem('selectedBranch', selectedBranch);
  }, [branchFormats, priceFormats.length, selectedBranch, selectedFormat]);

  useEffect(() => {
    setSelectedPricingContext(
      selectedFormat
        ? {
            branch: selectedBranch || selectedFormat.branch,
            region: selectedBranch || selectedFormat.branch,
            priceFormatCode: selectedFormat.code,
          }
        : null
    );
  }, [selectedBranch, selectedFormat]);

  useEffect(() => {
    if (!isResizingSidebar) return;
    const onMove = (event: MouseEvent) => {
      const next = Math.min(520, Math.max(270, event.clientX));
      setSidebarWidth(next);
      localStorage.setItem('sidebarWidth', String(next));
    };
    const onUp = () => setIsResizingSidebar(false);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isResizingSidebar]);

  const toggleSidebar = () => {
    setSidebarCollapsed((prev) => {
      localStorage.setItem('sidebarCollapsed', prev ? '0' : '1');
      return !prev;
    });
  };

  const openSection = (section: NavigationKey) => {
    setActiveSection(section);
  };

  const openGeneratedPriceList = (priceListNumber: string) => {
    setFocusedPriceListNumber(priceListNumber);
    setActiveSection('pricelists');
  };

  const openGeneratedAnalytics = (priceListNumber: string) => {
    setFocusedPriceListNumber(priceListNumber);
    setActiveSection('analytics');
  };

  const selectBranch = (branch: string) => {
    setSelectedBranch(branch);
    const firstFormat = priceFormats.find((format) => isSameBranch(format.branch, branch));
    if (firstFormat) setSelectedFormat(firstFormat);
  };

  const handleFormatCreated = async (created: PriceFormat) => {
    const items = await loadPriceFormats();
    const next = items.find((item) => item.code === created.code) || created;
    setPriceFormats(items.length ? items : (prev) => [...prev, next]);
    setSelectedBranch(next.branch || '');
    setSelectedFormat(next);
    setActiveSection('home');
  };

  if (!selectedFormat) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center text-sm text-gray-700">
        Нет ценовых форматов. Загрузите данные и обновите страницу.
      </div>
    );
  }

  const renderSection = () => {
    switch (activeSection) {
      case 'home':
        return (
          <HomeDashboard
            branch={selectedBranch}
            branchFormats={branchFormats}
            format={selectedFormat}
            onBranchChange={selectBranch}
            onFormatChange={setSelectedFormat}
            onFormatCreated={handleFormatCreated}
            onNavigate={openSection}
          />
        );
      case 'pricing-workflow':
        return (
          <PricingWorkflowTab
            selectedFormatCode={selectedFormat.code}
            branch={selectedBranch}
            priceFormats={priceFormats}
            onFormatChange={setSelectedFormat}
            onNavigate={openSection}
            onOpenPriceList={openGeneratedPriceList}
            onOpenAnalytics={openGeneratedAnalytics}
            isReadOnly={Boolean(currentUser?.isReadOnly)}
          />
        );
      case 'pricelists':
        return <PriceListsTab formatCode={selectedFormat.code} initialPriceListNumber={focusedPriceListNumber} />;
      case 'competitors':
        return (
          <CompetitorAssignmentTab
            formatCode={selectedFormat.code}
            branch={selectedBranch}
            priceFormats={priceFormats}
            onFormatChange={setSelectedFormat}
            onNavigate={openSection}
          />
        );
      case 'lists':
        return <ListsManagementTab priceFormats={priceFormats} selectedFormatCode={selectedFormat.code} />;
      case 'universal-lists':
        return <UniversalListsOverviewTab />;
      case 'contractors':
        return (
          <ContractorsTab
            branch={selectedBranch}
            selectedFormatCode={selectedFormat.code}
            priceFormats={priceFormats}
            onNavigate={openSection}
          />
        );
      case 'settings':
        return <SettingsOverview onNavigate={openSection} />;
      case 'pricing':
        return <PricingRulesTab formatCode={selectedFormat.code} onNavigate={openSection} />;
      case 'references':
        return <ReferencesTab isReadOnly={Boolean(currentUser?.isReadOnly)} />;
      case 'competitor-domain':
        return <CompetitorsTab formatCode={selectedFormat.code} />;
      case 'analytics':
        return (
          <AnalyticsTab
            branch={selectedBranch}
            selectedFormatCode={selectedFormat.code}
            initialPriceListNumber={focusedPriceListNumber}
            onNavigate={openSection}
          />
        );
      default:
        return (
          <HomeDashboard
            branch={selectedBranch}
            branchFormats={branchFormats}
            format={selectedFormat}
            onBranchChange={selectBranch}
            onFormatChange={setSelectedFormat}
            onFormatCreated={handleFormatCreated}
            onNavigate={openSection}
          />
        );
    }
  };

  return (
    <div className="app-shell">
      <Toaster position="top-right" richColors />
      <header className="app-header">
        <div>
          <h1>Модуль ценообразования</h1>
          <p>Рабочее пространство менеджера ЦО: филиал, ценовые форматы, прайсы и готовность данных</p>
        </div>
      </header>

      <div className="app-main">
        <aside
          className={`app-sidebar ${sidebarCollapsed ? 'collapsed' : ''}`}
          style={{ width: sidebarCollapsed ? 0 : sidebarWidth }}
        >
          <div className="app-sidebar-scroll">
            <button type="button" onClick={toggleSidebar} className="sidebar-collapse" title="Свернуть панель">
              <ChevronLeft className="h-4 w-4" />
            </button>

            <div className="sidebar-section-label">Филиал / регион</div>
            <select className="branch-select" value={selectedBranch} onChange={(event) => selectBranch(event.target.value)}>
              {branchOptions.map((branch) => (
                <option key={branch} value={branch}>
                  {branch || 'Без филиала'}
                </option>
              ))}
            </select>

            <div className="sidebar-section-label">Ценовые форматы</div>
            <div className="format-list">
              {branchFormats.length ? (
                branchFormats.map((format) => (
                  <button
                    key={format.id}
                    type="button"
                    onClick={() => setSelectedFormat(format)}
                    className={`format-item ${selectedFormat.code === format.code ? 'active' : ''}`}
                  >
                    <span className="format-name">{format.name || format.code}</span>
                    <span className="format-meta">{format.code} · {format.branch || 'Без филиала'}</span>
                  </button>
                ))
              ) : (
                <div className="sidebar-empty">Для выбранного филиала нет ценовых форматов</div>
              )}
            </div>

            <div className="sidebar-section-label">Разделы</div>
            <nav className="sidebar-nav">
              {navigationItems.map((item) => {
                const Icon = item.icon;
                return (
                  <button
                    key={item.key}
                    onClick={() => openSection(item.key)}
                    className={`sidebar-nav-item ${activeSection === item.key ? 'active' : ''}`}
                    type="button"
                  >
                    <Icon className="h-4 w-4" />
                    <span>{item.label}</span>
                  </button>
                );
              })}
            </nav>
          </div>
          <div
            role="separator"
            aria-orientation="vertical"
            onMouseDown={() => setIsResizingSidebar(true)}
            className="sidebar-resizer"
          />
        </aside>

        {sidebarCollapsed ? (
          <button type="button" onClick={toggleSidebar} className="sidebar-open" title="Открыть панель">
            <ChevronRight className="h-4 w-4" />
          </button>
        ) : null}

        <main className="app-content">
          <section className="page-heading">
            <div>
              <h2>{activeItem.label}</h2>
              <p>{activeItem.description}</p>
            </div>
            <div className="page-context-pill">
              <span>{currentUser?.role || 'admin'}</span>
              <span>{selectedPricingContext?.branch || 'Без филиала'}</span>
              <strong>{selectedPricingContext?.priceFormatCode || selectedFormat.code}</strong>
            </div>
          </section>
          {renderSection()}
        </main>
      </div>
    </div>
  );
}

function HomeDashboard({
  branch,
  branchFormats,
  format,
  onBranchChange,
  onFormatChange,
  onFormatCreated,
  onNavigate,
}: {
  branch: string;
  branchFormats: PriceFormat[];
  format: PriceFormat;
  onBranchChange: (branch: string) => void;
  onFormatChange: (format: PriceFormat) => void;
  onFormatCreated: (format: PriceFormat) => Promise<void>;
  onNavigate: (section: NavigationKey) => void;
}) {
  const [settings, setSettings] = useState<any | null>(null);
  const [priceLists, setPriceLists] = useState<any[]>([]);
  const [formatRows, setFormatRows] = useState<FormatDashboardRow[]>([]);
  const [assignedSources, setAssignedSources] = useState<any[]>([]);
  const [referenceStatuses, setReferenceStatuses] = useState<any[]>([]);
  const [showCreateFormat, setShowCreateFormat] = useState(false);
  const [newFormatCode, setNewFormatCode] = useState('');
  const [newFormatName, setNewFormatName] = useState('');
  const [newFormatBranch, setNewFormatBranch] = useState(branch || format.branch || '');
  const [newFormatRule, setNewFormatRule] = useState('');
  const [pricingRules, setPricingRules] = useState<any[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [settingsRes, listsRes, sourcesRes, refsRes, rulesRes] = await Promise.all([
        fetch(`/api/price-formats/${encodeURIComponent(format.code)}/settings`),
        fetch(`/api/price-lists?format_code=${encodeURIComponent(format.code)}`),
        fetch(`/api/competitors/price-lists?format_code=${encodeURIComponent(format.code)}`),
        fetch('/api/references/status'),
        fetch('/api/pricing-rules'),
      ]);
      const [settingsText, listsText, sourcesText, refsText, rulesText] = await Promise.all([
        settingsRes.text(),
        listsRes.text(),
        sourcesRes.text(),
        refsRes.text(),
        rulesRes.text(),
      ]);
      const settingsData = parseJsonOrNull(settingsText);
      const listsData = parseJsonOrNull(listsText);
      const sourcesData = parseJsonOrNull(sourcesText);
      const refsData = parseJsonOrNull(refsText);
      const rulesData = parseJsonOrNull(rulesText);
      if (!settingsRes.ok) throw new Error(settingsData?.detail || settingsText || 'Не удалось загрузить настройки формата');
      if (!listsRes.ok) throw new Error(listsData?.detail || listsText || 'Не удалось загрузить прайс-листы');
      if (!sourcesRes.ok) throw new Error(sourcesData?.detail || sourcesText || 'Не удалось загрузить ПЛК');
      if (!refsRes.ok) throw new Error(refsData?.detail || refsText || 'Не удалось загрузить статусы справочников');

      const selectedLists = Array.isArray(listsData) ? listsData : [];
      const selectedSources = Array.isArray(sourcesData) ? sourcesData.filter((row: any) => row.isSelected) : [];
      const refs = Array.isArray(refsData) ? refsData : [];
      if (rulesRes.ok) setPricingRules(Array.isArray(rulesData) ? rulesData : []);

      setSettings(settingsData || null);
      setPriceLists(selectedLists.slice(0, 6));
      setAssignedSources(selectedSources.slice(0, 8));
      setReferenceStatuses(refs);

      const rows = await Promise.all(
        branchFormats.map(async (priceFormat) => {
          const [formatSettingsRes, formatListsRes] = await Promise.all([
            fetch(`/api/price-formats/${encodeURIComponent(priceFormat.code)}/settings`),
            fetch(`/api/price-lists?format_code=${encodeURIComponent(priceFormat.code)}`),
          ]);
          const [formatSettingsText, formatListsText] = await Promise.all([
            formatSettingsRes.text(),
            formatListsRes.text(),
          ]);
          const formatSettings = parseJsonOrNull(formatSettingsText) || {};
          const formatLists = parseJsonOrNull(formatListsText);
          const last = Array.isArray(formatLists) ? formatLists[0] : null;
          return {
            code: priceFormat.code,
            name: priceFormat.name || priceFormat.code,
            branch: priceFormat.branch,
            pricingRule: formatSettings.pricingRule || formatSettings.pricingRuleId || '—',
            status: 'Активен',
            lastGeneratedAt: last?.date || '',
            lastActivationDate: last?.activationDate || '',
            user: last?.user || '',
            dataStatus: buildDataStatus(refs, priceFormat.branch),
          };
        })
      );
      setFormatRows(rows);
    } catch (e: any) {
      setError(e?.message || 'Ошибка загрузки начальной страницы');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [format.code, branch, branchFormats.map((item) => item.code).join('|')]);

  const referenceRows = useMemo(() => {
    const wanted = [
      ['stock', 'Остатки'],
      ['cost', 'Себестоимость'],
      ['rating_global', 'Рейтинг общий'],
      ['rating_local', 'Рейтинг локальный'],
    ];
    return wanted.map(([code, label]) => {
      const rows = referenceStatuses.filter(
        (row) => row.dataType === code && (!branch || isSameBranch(row.branchName, branch))
      );
      const latest = rows
        .filter((row) => row.lastUpdatedAt)
        .sort((a, b) => String(b.lastUpdatedAt).localeCompare(String(a.lastUpdatedAt)))[0];
      const status = latest?.freshness || latest?.status || (rows.length ? rows[0]?.status : 'missing');
      return {
        code,
        label,
        status,
        updatedAt: latest?.lastUpdatedAt || '',
        rowsCount: rows.reduce((sum, row) => sum + Number(row.rowsCount || 0), 0),
      };
    });
  }, [branch, referenceStatuses]);

  useEffect(() => {
    setNewFormatBranch(branch || format.branch || '');
  }, [branch, format.branch]);

  const createPriceFormat = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const selectedRule = pricingRules.find((rule) => String(rule.id) === newFormatRule);
      const res = await fetch('/api/price-formats', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          code: newFormatCode.trim(),
          name: newFormatName.trim(),
          branch: newFormatBranch.trim(),
          pricingRuleId: newFormatRule || undefined,
          pricingRule: selectedRule?.name || '',
        }),
      });
      const text = await res.text();
      const data = parseJsonOrNull(text);
      if (!res.ok) throw new Error(data?.detail || text || 'Не удалось создать ценовой формат');
      await onFormatCreated({
        id: String(data?.id ?? data?.code ?? newFormatCode),
        code: String(data?.code ?? newFormatCode).trim(),
        name: String(data?.name ?? newFormatName).trim(),
        branch: String(data?.branch ?? newFormatBranch).trim(),
      });
      setShowCreateFormat(false);
      setNewFormatCode('');
      setNewFormatName('');
      setNewFormatRule('');
      await load();
    } catch (e: any) {
      setError(e?.message || 'Ошибка создания ценового формата');
    } finally {
      setIsLoading(false);
    }
  };

  const latestPriceList = priceLists[0] || null;
  const freshReferences = referenceRows.filter((row) => statusLabel(row.status) === 'актуально').length;
  const staleReferences = referenceRows.length - freshReferences;

  return (
    <div className="home-workspace">
      {error ? <div className="dashboard-alert">{error}</div> : null}

      <section className="context-panel">
        <div>
          <div className="eyebrow">Рабочий контекст</div>
          <h3>{branch || 'Филиал не выбран'}</h3>
          <p>{format.code} · {format.name || format.code}</p>
        </div>
        <div className="context-controls">
          <label>
            <span>Филиал / регион</span>
            <select value={branch} onChange={(event) => onBranchChange(event.target.value)}>
              {uniqueByBranch([...branchFormats.map((item) => item.branch), branch, ...defaultBranches]).map((item) => (
                <option key={item} value={item}>{item || 'Без филиала'}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Ценовой формат</span>
            <select value={format.code} onChange={(event) => {
              const next = branchFormats.find((item) => item.code === event.target.value);
              if (next) onFormatChange(next);
            }}>
              {branchFormats.map((item) => (
                <option key={item.code} value={item.code}>{item.code}</option>
              ))}
            </select>
          </label>
          <Button variant="outline" size="sm" onClick={load} disabled={isLoading}>
            <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
            Обновить
          </Button>
          <Button size="sm" onClick={() => setShowCreateFormat((value) => !value)} className="bg-blue-600 hover:bg-blue-700">
            <Plus className="mr-2 h-4 w-4" />
            Создать ценовой формат
          </Button>
        </div>
      </section>

      {showCreateFormat ? (
        <section className="dashboard-card dashboard-wide">
          <div className="card-title-row">
            <h3>Создать ценовой формат</h3>
            <Button variant="ghost" size="sm" onClick={() => setShowCreateFormat(false)}>Закрыть</Button>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
            <label>
              <span className="text-xs font-medium text-gray-500">Код ЦФ</span>
              <input className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm" value={newFormatCode} onChange={(event) => setNewFormatCode(event.target.value)} />
            </label>
            <label>
              <span className="text-xs font-medium text-gray-500">Название</span>
              <input className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm" value={newFormatName} onChange={(event) => setNewFormatName(event.target.value)} />
            </label>
            <label>
              <span className="text-xs font-medium text-gray-500">Филиал</span>
              <input className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm" value={newFormatBranch} onChange={(event) => setNewFormatBranch(event.target.value)} />
            </label>
            <label>
              <span className="text-xs font-medium text-gray-500">Правило ЦО</span>
              <select className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm" value={newFormatRule} onChange={(event) => setNewFormatRule(event.target.value)}>
                <option value="">Без правила</option>
                {pricingRules.map((rule) => (
                  <option key={rule.id} value={String(rule.id)}>{rule.name || rule.code}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="outline" onClick={() => setShowCreateFormat(false)}>Отмена</Button>
            <Button onClick={createPriceFormat} disabled={isLoading || !newFormatCode.trim() || !newFormatName.trim()} className="bg-blue-600 hover:bg-blue-700">
              Создать ценовой формат
            </Button>
          </div>
        </section>
      ) : null}

      <section className="status-cards">
        <StatusCard label="Ценовых форматов" value={branchFormats.length} tone="blue" />
        <StatusCard label="Последних прайсов" value={priceLists.length} tone="slate" />
        <StatusCard label="Назначенных ПЛК" value={assignedSources.length} tone={assignedSources.length ? 'green' : 'amber'} />
        <StatusCard label="Справочники" value={`${freshReferences}/${referenceRows.length}`} hint={staleReferences ? 'есть устаревшие' : 'актуальны'} tone={staleReferences ? 'amber' : 'green'} />
      </section>

      <section className="dashboard-card dashboard-wide">
        <div className="card-title-row">
          <h3>Ценовые форматы филиала</h3>
          <span className="muted-count">{branchFormats.length} шт.</span>
        </div>
        <CompactTable
          empty="Для выбранного филиала пока нет ценовых форматов"
          columns={['Код', 'Наименование', 'Филиал', 'Правило ЦО', 'Статус', 'Последнее формирование', 'Активация', 'Пользователь', 'Данные']}
          rows={formatRows.map((row) => [
            <button key={row.code} type="button" className="table-link" onClick={() => {
              const next = branchFormats.find((item) => item.code === row.code);
              if (next) onFormatChange(next);
            }}>{row.code}</button>,
            row.name,
            row.branch || '—',
            row.pricingRule,
            <span key={`${row.code}-status`} className="status-pill ok">{row.status}</span>,
            row.lastGeneratedAt || '—',
            row.lastActivationDate || '—',
            row.user || '—',
            <span key={`${row.code}-data`} className={`status-pill ${freshnessClassName(row.dataStatus)}`}>{statusLabel(row.dataStatus)}</span>,
          ])}
        />
      </section>

      <section className="dashboard-grid">
        <section className="dashboard-card">
          <div className="card-title-row">
            <h3>Последние сформированные прайс-листы</h3>
            <Button variant="ghost" size="sm" onClick={() => onNavigate('pricelists')}>Все прайсы</Button>
          </div>
          <CompactTable
            empty="Для выбранного филиала пока нет сформированных прайс-листов"
            columns={['Номер', 'Дата формирования', 'Активация', 'Пользователь', 'Статус', '']}
            rows={priceLists.map((row) => [
              row.number || '—',
              row.date || '—',
              row.activationDate || '—',
              row.user || '—',
              row.status || '—',
              <Button key={row.number} variant="outline" size="sm" onClick={() => onNavigate('pricelists')}>Открыть</Button>,
            ])}
          />
        </section>

        <section className="dashboard-card quick-actions-card">
          <h3>Быстрые действия</h3>
          <div className="quick-actions">
            <Button onClick={() => onNavigate('pricing-workflow')} className="bg-blue-600 hover:bg-blue-700">
              <Calculator className="mr-2 h-4 w-4" />
              Сформировать прайс-лист
            </Button>
            <Button variant="outline" onClick={() => onNavigate('competitors')}>
              <ClipboardList className="mr-2 h-4 w-4" />
              Перейти в назначение ПЛК
            </Button>
            <Button variant="outline" onClick={() => onNavigate('references')}>
              <BookOpen className="mr-2 h-4 w-4" />
              Перейти в справочники
            </Button>
            <Button variant="outline" onClick={() => onNavigate('competitor-domain')}>
              <Users className="mr-2 h-4 w-4" />
              Перейти в конкуренты
            </Button>
            <Button variant="outline" onClick={() => onNavigate('pricelists')} disabled={!latestPriceList}>
              <FileText className="mr-2 h-4 w-4" />
              Открыть последний прайс
            </Button>
          </div>
        </section>
      </section>

      <section className="dashboard-card dashboard-wide">
        <div className="card-title-row">
          <h3>Назначенные ПЛК</h3>
          <Button variant="ghost" size="sm" onClick={() => onNavigate('competitors')}>Настроить</Button>
        </div>
        <CompactTable
          empty="Нет назначенных ПЛК"
          columns={['Источник', 'Регион', 'Конкурент', 'Клиент / логин', 'Коэффициент', 'Дата цен', 'Актуальность']}
          rows={assignedSources.map((row) => {
            const freshness = priceDateFreshness(row.priceDate);
            return [
              row.sourceName || row.name || '—',
              row.branchName || row.region || '—',
              row.competitorName || row.supplier || '—',
              row.accountLogin || row.accountId || '—',
              row.coefficient ?? '—',
              row.priceDate || '—',
              <span key={row.id || row.sourceName} className={`status-pill ${freshnessClassName(freshness)}`}>{freshness}</span>,
            ];
          })}
        />
      </section>

      <section className="dashboard-card dashboard-wide">
        <div className="card-title-row">
          <h3>Справочники</h3>
          <Button variant="ghost" size="sm" onClick={() => onNavigate('references')}>Открыть</Button>
        </div>
        <CompactTable
          empty="Нет данных по справочникам для выбранного филиала"
          columns={['Справочник', 'Последнее обновление', 'Строк', 'Статус']}
          rows={referenceRows.map((row) => [
            row.label,
            row.updatedAt ? fmtDate(row.updatedAt) : '—',
            row.rowsCount.toLocaleString('ru-RU'),
            <span key={row.code} className={`status-pill ${freshnessClassName(row.status)}`}>{statusLabel(row.status)}</span>,
          ])}
        />
      </section>
    </div>
  );
}

function buildDataStatus(referenceStatuses: any[], branch: string) {
  const important = ['stock', 'cost', 'rating_global', 'rating_local'];
  const rows = referenceStatuses.filter((row) => important.includes(row.dataType) && isSameBranch(row.branchName, branch));
  if (!rows.length) return 'missing';
  if (rows.some((row) => statusLabel(row.freshness || row.status) === 'ошибка')) return 'error';
  if (rows.every((row) => statusLabel(row.freshness || row.status) === 'актуально')) return 'fresh';
  return 'stale';
}

function StatusCard({ label, value, hint, tone }: { label: string; value: any; hint?: string; tone: 'blue' | 'green' | 'amber' | 'slate' }) {
  return (
    <div className={`status-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {hint ? <small>{hint}</small> : null}
    </div>
  );
}

function SettingsOverview({ onNavigate }: { onNavigate: (section: NavigationKey) => void }) {
  return (
    <div className="empty-state">
      <Settings className="h-8 w-8 text-blue-600" />
      <h3>Общие настройки</h3>
      <p>Раздел зарезервирован для общих параметров модуля. Настройки правил ЦО доступны в разделе “Ценообразование”.</p>
      <div className="flex flex-wrap justify-center gap-2">
        <Button onClick={() => onNavigate('pricing')} className="bg-blue-600 hover:bg-blue-700">Открыть ценообразование</Button>
        <Button variant="outline" onClick={() => onNavigate('competitor-domain')}>Источники конкурентов</Button>
      </div>
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
