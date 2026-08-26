import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, Search, ShieldAlert, X } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from './ui/badge';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from './ui/table';

type ReviewRow = {
  canonicalProductId: number;
  canonicalName: string;
  canonicalManufacturer: string;
  canonicalSignature: string;
  tier: string;
  reviewReason: string;
  topCandidateProductId: number | null;
  topCandidateName: string;
  topCandidateScore: number | null;
  scoreGap: number | null;
  candidateCount: number;
  matchStatus: string;
};

type Candidate = {
  rank: number;
  productId: number;
  code: string;
  name: string;
  manufacturer: string;
  score: number;
  nameScore: number;
  manufacturerScore: number;
  structuralScore: number;
  variantScore: number;
  sharedFields: string[];
  missingFields: { vidman?: string[]; internal?: string[] };
  conflicts: string[];
  reason: string;
  normalized: Record<string, any>;
};

type ReviewDetail = {
  canonicalProductId: number;
  tier: string;
  reviewReason: string;
  match: { status: string; productId: number | null };
  vidman: {
    canonicalName: string;
    canonicalManufacturer: string;
    canonicalSignature: string;
    identity: Record<string, any>;
  };
  sourceContext: {
    rawExamples: Array<{ rawName: string; rawManufacturer: string; account: string; priceList: string }>;
  };
  candidates: Candidate[];
  rejectedCandidates: Array<{ productId: number; reason: string; createdAt: string }>;
  audit: Array<{ action: string; newStatus: string; reason: string; actor: string; createdAt: string }>;
};

type ProductSearchRow = {
  productId: number;
  code: string;
  name: string;
  manufacturer: string;
  normalized: Record<string, any>;
};

type Counters = {
  tiers: Record<string, { total: number; reviewed: number; remaining: number }>;
  manuallyApproved: number;
  manuallyUnmatched: number;
};

const tiers = [
  { value: '', label: 'All Review' },
  { value: 'TIER_A_STRONG', label: 'Tier A' },
  { value: 'TIER_B_GOOD', label: 'Tier B' },
  { value: 'TIER_C_AMBIGUOUS', label: 'Tier C' },
  { value: 'TIER_D_WEAK', label: 'Tier D' },
];

const fieldLabels: Record<string, string> = {
  baseName: 'Name',
  manufacturer: 'Manufacturer',
  dosage: 'Dosage',
  strengths: 'Strength',
  concentration: 'Concentration',
  volume: 'Volume',
  packageVolume: 'Package volume',
  weight: 'Weight',
  packageWeight: 'Package weight',
  pack: 'Pack',
  form: 'Form',
  variant: 'Variant',
};

const fmt = (value: number | null | undefined) => (value == null ? '-' : value.toFixed(value % 1 === 0 ? 0 : 1));
const norm = (value: any) => (value == null || value === '' ? '-' : String(value));

function tierTone(tier: string) {
  if (tier.includes('A')) return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  if (tier.includes('B')) return 'border-sky-200 bg-sky-50 text-sky-700';
  if (tier.includes('C')) return 'border-amber-200 bg-amber-50 text-amber-700';
  return 'border-gray-200 bg-gray-50 text-gray-700';
}

function conflictTone(conflicts: string[]) {
  return conflicts.length ? 'border-red-200 bg-red-50 text-red-700' : 'border-emerald-200 bg-emerald-50 text-emerald-700';
}

export function VidmanMatchingReviewTab({ isReadOnly = false }: { isReadOnly?: boolean }) {
  const [rows, setRows] = useState<ReviewRow[]>([]);
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [tier, setTier] = useState('');
  const [status, setStatus] = useState('unreviewed');
  const [search, setSearch] = useState('');
  const [manufacturer, setManufacturer] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [counters, setCounters] = useState<Counters | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [manualQuery, setManualQuery] = useState('');
  const [manualResults, setManualResults] = useState<ProductSearchRow[]>([]);
  const limit = 50;

  const selectedIndex = useMemo(() => rows.findIndex((row) => row.canonicalProductId === selectedId), [rows, selectedId]);

  const loadCounters = useCallback(async () => {
    const res = await fetch('/api/vidman/review/counters');
    if (res.ok) setCounters(await res.json());
  }, []);

  const loadRows = useCallback(async (selectFirst = false) => {
    setLoading(true);
    const params = new URLSearchParams({ status, page: String(page), limit: String(limit) });
    if (tier) params.set('tier', tier);
    if (search.trim()) params.set('search', search.trim());
    if (manufacturer.trim()) params.set('manufacturer', manufacturer.trim());
    try {
      const res = await fetch(`/api/vidman/review?${params.toString()}`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const items = Array.isArray(data.items) ? data.items : [];
      setRows(items);
      setTotal(Number(data.total || 0));
      if (selectFirst || !selectedId || !items.some((row: ReviewRow) => row.canonicalProductId === selectedId)) {
        setSelectedId(items[0]?.canonicalProductId ?? null);
      }
    } catch (err: any) {
      toast.error(err?.message || 'Failed to load Vidman review rows');
    } finally {
      setLoading(false);
    }
  }, [manufacturer, page, search, selectedId, status, tier]);

  const loadDetail = useCallback(async (id: number | null) => {
    if (!id) {
      setDetail(null);
      return;
    }
    setDetailLoading(true);
    try {
      const res = await fetch(`/api/vidman/review/${encodeURIComponent(String(id))}`);
      if (!res.ok) throw new Error(await res.text());
      setDetail(await res.json());
    } catch (err: any) {
      toast.error(err?.message || 'Failed to load review detail');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRows(true);
    void loadCounters();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tier, status, page]);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      setPage(1);
      void loadRows(true);
    }, 250);
    return () => window.clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, manufacturer]);

  useEffect(() => {
    void loadDetail(selectedId);
  }, [loadDetail, selectedId]);

  const moveSelection = useCallback((delta: number) => {
    if (!rows.length) return;
    const current = selectedIndex < 0 ? 0 : selectedIndex;
    const next = Math.max(0, Math.min(rows.length - 1, current + delta));
    setSelectedId(rows[next]?.canonicalProductId ?? null);
  }, [rows, selectedIndex]);

  const afterAction = async () => {
    await loadCounters();
    await loadRows(true);
  };

  const action = async (kind: 'approve' | 'reject' | 'unmatched', productId?: number) => {
    if (!detail || isReadOnly) return;
    const targetProductId = productId ?? detail.candidates[0]?.productId;
    if (kind !== 'unmatched' && !targetProductId) return;
    const endpoint =
      kind === 'approve'
        ? `/api/vidman/review/${detail.canonicalProductId}/approve`
        : kind === 'reject'
          ? `/api/vidman/review/${detail.canonicalProductId}/reject-candidate`
          : `/api/vidman/review/${detail.canonicalProductId}/mark-unmatched`;
    const body = kind === 'unmatched' ? { reason: 'manual review' } : { product_id: targetProductId, reason: 'manual review' };
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      toast.error(await res.text());
      return;
    }
    toast.success(kind === 'approve' ? 'Match approved' : kind === 'reject' ? 'Candidate rejected' : 'Marked unmatched');
    await afterAction();
  };

  const runManualSearch = async () => {
    if (manualQuery.trim().length < 2) {
      setManualResults([]);
      return;
    }
    const params = new URLSearchParams({ q: manualQuery.trim(), limit: '20' });
    const res = await fetch(`/api/vidman/review/internal-products/search?${params.toString()}`);
    if (!res.ok) {
      toast.error(await res.text());
      return;
    }
    setManualResults(await res.json());
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || isReadOnly) return;
      if (event.key === 'j' || event.key === 'ArrowDown') {
        event.preventDefault();
        moveSelection(1);
      } else if (event.key === 'k' || event.key === 'ArrowUp') {
        event.preventDefault();
        moveSelection(-1);
      } else if (event.key.toLowerCase() === 'a') {
        event.preventDefault();
        void action('approve');
      } else if (event.key.toLowerCase() === 'r') {
        event.preventDefault();
        void action('reject');
      } else if (event.key.toLowerCase() === 'u') {
        event.preventDefault();
        void action('unmatched');
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  });

  return (
    <div className="space-y-4">
      <div className="grid gap-3 border-b border-gray-200 pb-4 xl:grid-cols-[1fr_auto]">
        <div className="grid gap-2 md:grid-cols-[160px_160px_1fr_220px]">
          <select className="h-9 rounded-md border border-gray-300 bg-white px-3 text-sm" value={tier} onChange={(e) => { setTier(e.target.value); setPage(1); }}>
            {tiers.map((item) => <option key={item.value || 'all'} value={item.value}>{item.label}</option>)}
          </select>
          <select className="h-9 rounded-md border border-gray-300 bg-white px-3 text-sm" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
            <option value="unreviewed">Unreviewed</option>
            <option value="reviewed">Reviewed</option>
            <option value="all">All statuses</option>
          </select>
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search canonical name or signature" />
          <Input value={manufacturer} onChange={(e) => setManufacturer(e.target.value)} placeholder="Manufacturer" />
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-6">
          {tiers.slice(1).map((item) => {
            const counter = counters?.tiers?.[item.value];
            return (
              <div key={item.value} className="rounded-md border border-gray-200 px-3 py-2">
                <div className="font-semibold text-gray-800">{item.label}</div>
                <div className="text-gray-500">{counter?.reviewed ?? 0}/{counter?.total ?? 0}</div>
              </div>
            );
          })}
          <div className="rounded-md border border-gray-200 px-3 py-2">
            <div className="font-semibold text-gray-800">Approved</div>
            <div className="text-gray-500">{counters?.manuallyApproved ?? 0}</div>
          </div>
          <div className="rounded-md border border-gray-200 px-3 py-2">
            <div className="font-semibold text-gray-800">Unmatched</div>
            <div className="text-gray-500">{counters?.manuallyUnmatched ?? 0}</div>
          </div>
        </div>
      </div>

      <div className="grid min-h-[680px] gap-4 xl:grid-cols-[minmax(520px,0.95fr)_minmax(520px,1.05fr)]">
        <div className="min-w-0 border-r border-gray-200 pr-4">
          <div className="mb-2 flex items-center justify-between text-sm text-gray-600">
            <span>{loading ? 'Loading...' : `${total} rows`}</span>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => setPage((value) => Math.max(1, value - 1))} disabled={page <= 1}>
                <ChevronLeft className="h-4 w-4" />
              </Button>
              <span>Page {page}</span>
              <Button variant="outline" size="sm" onClick={() => setPage((value) => value + 1)} disabled={page * limit >= total}>
                <ChevronRight className="h-4 w-4" />
              </Button>
            </div>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Canonical</TableHead>
                <TableHead>Tier</TableHead>
                <TableHead>Top candidate</TableHead>
                <TableHead className="text-right">Score</TableHead>
                <TableHead className="text-right">Gap</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow
                  key={row.canonicalProductId}
                  data-state={row.canonicalProductId === selectedId ? 'selected' : undefined}
                  className="cursor-pointer"
                  onClick={() => setSelectedId(row.canonicalProductId)}
                >
                  <TableCell className="max-w-[260px] whitespace-normal">
                    <div className="font-medium text-gray-900">{row.canonicalName}</div>
                    <div className="text-xs text-gray-500">{row.canonicalManufacturer || '-'}</div>
                  </TableCell>
                  <TableCell><Badge variant="outline" className={tierTone(row.tier)}>{row.tier.replace('TIER_', '').replace('_', ' ')}</Badge></TableCell>
                  <TableCell className="max-w-[220px] whitespace-normal text-xs">{row.topCandidateName || '-'}</TableCell>
                  <TableCell className="text-right">{fmt(row.topCandidateScore)}</TableCell>
                  <TableCell className="text-right">{fmt(row.scoreGap)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>

        <div className="min-w-0 space-y-4">
          {!detail || detailLoading ? (
            <div className="rounded-md border border-gray-200 p-6 text-sm text-gray-600">{detailLoading ? 'Loading detail...' : 'Select a Vidman row'}</div>
          ) : (
            <>
              <section className="space-y-3 border-b border-gray-200 pb-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="text-lg font-semibold text-gray-950">{detail.vidman.canonicalName}</h3>
                    <p className="text-sm text-gray-600">{detail.vidman.canonicalManufacturer || '-'}</p>
                  </div>
                  <Badge variant="outline" className={tierTone(detail.tier)}>{detail.tier}</Badge>
                </div>
                <IdentityGrid left={detail.vidman.identity} />
                <div className="text-xs text-gray-500 break-all">{detail.vidman.canonicalSignature}</div>
              </section>

              <section className="space-y-2 border-b border-gray-200 pb-4">
                <h4 className="text-sm font-semibold text-gray-900">Raw examples</h4>
                {detail.sourceContext.rawExamples.length ? detail.sourceContext.rawExamples.map((item, idx) => (
                  <div key={`${item.rawName}-${idx}`} className="rounded-md border border-gray-200 px-3 py-2 text-xs">
                    <div className="font-medium text-gray-900">{item.rawName}</div>
                    <div className="text-gray-500">{item.rawManufacturer || '-'} | {item.account || '-'} | {item.priceList || '-'}</div>
                  </div>
                )) : <div className="text-sm text-gray-500">No raw examples linked.</div>}
              </section>

              <section className="space-y-3 border-b border-gray-200 pb-4">
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-semibold text-gray-900">Top candidates</h4>
                  <Button variant="outline" size="sm" onClick={() => action('unmatched')} disabled={isReadOnly}>
                    <ShieldAlert className="h-4 w-4" />
                    Unmatched
                  </Button>
                </div>
                {detail.candidates.map((candidate) => (
                  <div key={candidate.productId} className={`rounded-md border p-3 ${conflictTone(candidate.conflicts)}`}>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div className="text-sm font-semibold text-gray-950">#{candidate.rank} {candidate.name}</div>
                        <div className="text-xs text-gray-600">{candidate.code || '-'} | {candidate.manufacturer || '-'}</div>
                      </div>
                      <div className="text-right text-sm font-semibold">{fmt(candidate.score)}</div>
                    </div>
                    <EvidenceGrid vidman={detail.vidman.identity} internal={candidate.normalized} candidate={candidate} />
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Button size="sm" onClick={() => action('approve', candidate.productId)} disabled={isReadOnly}>
                        <Check className="h-4 w-4" />
                        Approve
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => action('reject', candidate.productId)} disabled={isReadOnly}>
                        <X className="h-4 w-4" />
                        Reject
                      </Button>
                    </div>
                  </div>
                ))}
                {!detail.candidates.length ? <div className="rounded-md border border-gray-200 p-3 text-sm text-gray-600">No active candidates remain.</div> : null}
              </section>

              <section className="space-y-3">
                <h4 className="text-sm font-semibold text-gray-900">Manual internal product search</h4>
                <div className="flex gap-2">
                  <Input value={manualQuery} onChange={(e) => setManualQuery(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') void runManualSearch(); }} placeholder="Search product name, code, manufacturer" />
                  <Button variant="outline" onClick={() => void runManualSearch()}>
                    <Search className="h-4 w-4" />
                  </Button>
                </div>
                <div className="space-y-2">
                  {manualResults.map((item) => (
                    <div key={item.productId} className="flex items-start justify-between gap-3 rounded-md border border-gray-200 px-3 py-2">
                      <div className="min-w-0">
                        <div className="text-sm font-medium text-gray-900">{item.name}</div>
                        <div className="text-xs text-gray-500">{item.code || '-'} | {item.manufacturer || '-'}</div>
                      </div>
                      <Button size="sm" onClick={() => action('approve', item.productId)} disabled={isReadOnly}>
                        <Check className="h-4 w-4" />
                        Approve
                      </Button>
                    </div>
                  ))}
                </div>
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function IdentityGrid({ left }: { left: Record<string, any> }) {
  return (
    <div className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-3">
      {Object.entries(fieldLabels).map(([key, label]) => (
        <div key={key} className="rounded-md border border-gray-200 px-3 py-2">
          <div className="text-gray-500">{label}</div>
          <div className="font-medium text-gray-900">{norm(left[key])}</div>
        </div>
      ))}
    </div>
  );
}

function EvidenceGrid({ vidman, internal, candidate }: { vidman: Record<string, any>; internal: Record<string, any>; candidate: Candidate }) {
  const missingVidman = new Set(candidate.missingFields?.vidman || []);
  const missingInternal = new Set(candidate.missingFields?.internal || []);
  const conflicts = new Set(candidate.conflicts || []);
  const shared = new Set(candidate.sharedFields || []);
  return (
    <div className="mt-3 grid gap-2 text-xs md:grid-cols-2">
      {Object.entries(fieldLabels).map(([key, label]) => {
        const status = conflicts.has(key)
          ? 'CONFLICT'
          : missingVidman.has(key)
            ? 'MISSING ON VIDMAN'
            : missingInternal.has(key)
              ? 'MISSING INTERNAL'
              : shared.has(key)
                ? 'MATCH'
                : 'NO SIGNAL';
        const statusClass = status === 'CONFLICT'
          ? 'bg-red-100 text-red-700'
          : status === 'MATCH'
            ? 'bg-emerald-100 text-emerald-700'
            : status.startsWith('MISSING')
              ? 'bg-amber-100 text-amber-700'
              : 'bg-gray-100 text-gray-600';
        return (
          <div key={key} className="rounded-md border border-white/70 bg-white/70 p-2">
            <div className="mb-1 flex items-center justify-between gap-2">
              <span className="font-medium text-gray-900">{label}</span>
              <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${statusClass}`}>{status}</span>
            </div>
            <div className="grid grid-cols-2 gap-2 text-gray-600">
              <div><span className="text-gray-400">Vidman</span><br />{norm(vidman[key])}</div>
              <div><span className="text-gray-400">Internal</span><br />{norm(internal[key])}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
