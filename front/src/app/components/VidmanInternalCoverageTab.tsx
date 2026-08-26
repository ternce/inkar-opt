import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, Search, ShieldAlert, X } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from './ui/badge';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from './ui/table';

type CoverageRow = {
  productId: number;
  productCode: string;
  productName: string;
  manufacturer: string;
  coverageStatus: string;
  topCanonicalProductId: number | null;
  topCanonicalName: string;
  topScore: number | null;
  scoreGap: number | null;
  candidateCount: number;
  reviewReason: string;
  tier: string;
  alreadyCovered: boolean;
};

type Candidate = {
  canonicalProductId: number;
  canonicalName: string;
  canonicalManufacturer: string;
  canonicalSignature: string;
  rank: number;
  score: number | null;
  nameScore: number | null;
  manufacturerScore: number | null;
  structuralScore: number | null;
  variantScore: number | null;
  sharedStructuralFields: string[];
  missingOnInternal: string[];
  missingOnVidman: string[];
  hardConflicts: string[];
  reason: string;
  identity: Record<string, any>;
  rawExamples: Array<{ rawName: string; rawManufacturer: string; mainId: number; price: number | null; account: string; priceList: string }>;
  mappingContext: { matchStatus: string; mappedProductId: number | null; mappedProductName: string };
};

type CoverageDetail = {
  productId: number;
  productCode: string;
  productName: string;
  manufacturer: string;
  coverageStatus: string;
  coverage: { tier: string; reviewReason: string; candidateCount: number; topScore: number | null; scoreGap: number | null };
  normalized: Record<string, any>;
  candidates: Candidate[];
  rejectedCandidates: Array<{ canonicalProductId: number; reason: string; createdAt: string }>;
};

type Counters = {
  tiers: Record<string, { total: number; reviewed: number; remaining: number }>;
  totalInternalProducts: number;
  coveredInternalProducts: number;
  uncoveredInternalProducts: number;
  manuallyApprovedFromReverse: number;
  manualNoVidmanMatch: number;
  potentialCoverageStrong: number;
  potentialCoverageStrongGood: number;
};

const tiers = [
  { value: 'COVERAGE_A_STRONG', label: 'Strong' },
  { value: 'COVERAGE_B_GOOD', label: 'Good' },
  { value: 'COVERAGE_C_AMBIGUOUS', label: 'Ambiguous' },
  { value: 'COVERAGE_D_WEAK', label: 'Weak' },
  { value: 'COVERAGE_NO_CANDIDATE', label: 'No candidate' },
  { value: '', label: 'All' },
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
  if (tier.includes('NO')) return 'border-gray-300 bg-gray-100 text-gray-700';
  return 'border-slate-200 bg-slate-50 text-slate-700';
}

function statusTone(status: string) {
  if (status === 'MANUALLY_APPROVED' || status === 'AUTO_MATCHED') return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  if (status === 'MANUAL_NO_VIDMAN_MATCH') return 'border-amber-200 bg-amber-50 text-amber-700';
  if (status === 'REVIEW_REQUIRED') return 'border-blue-200 bg-blue-50 text-blue-700';
  if (status === 'UNMATCHED' || status === 'UNASSIGNED') return 'border-gray-200 bg-gray-50 text-gray-700';
  return 'border-slate-200 bg-slate-50 text-slate-700';
}

export function VidmanInternalCoverageTab({ isReadOnly = false }: { isReadOnly?: boolean }) {
  const [rows, setRows] = useState<CoverageRow[]>([]);
  const [detail, setDetail] = useState<CoverageDetail | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [tier, setTier] = useState('COVERAGE_A_STRONG');
  const [reviewStatus, setReviewStatus] = useState('unreviewed');
  const [search, setSearch] = useState('');
  const [manufacturer, setManufacturer] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [counters, setCounters] = useState<Counters | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const limit = 50;

  const selectedIndex = useMemo(() => rows.findIndex((row) => row.productId === selectedId), [rows, selectedId]);

  const loadCounters = useCallback(async () => {
    const res = await fetch('/api/vidman/internal-coverage/counters');
    if (res.ok) setCounters(await res.json());
  }, []);

  const loadRows = useCallback(async (selectFirst = false) => {
    setLoading(true);
    const params = new URLSearchParams({ review_status: reviewStatus, page: String(page), limit: String(limit) });
    if (tier) params.set('tier', tier);
    if (search.trim()) params.set('search', search.trim());
    if (manufacturer.trim()) params.set('manufacturer', manufacturer.trim());
    try {
      const res = await fetch(`/api/vidman/internal-coverage?${params.toString()}`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const items = Array.isArray(data.items) ? data.items : [];
      setRows(items);
      setTotal(Number(data.total || 0));
      if (selectFirst || !selectedId || !items.some((row: CoverageRow) => row.productId === selectedId)) {
        setSelectedId(items[0]?.productId ?? null);
      }
    } catch (err: any) {
      toast.error(err?.message || 'Failed to load internal coverage rows');
    } finally {
      setLoading(false);
    }
  }, [manufacturer, page, reviewStatus, search, selectedId, tier]);

  const loadDetail = useCallback(async (id: number | null) => {
    if (!id) {
      setDetail(null);
      return;
    }
    setDetailLoading(true);
    try {
      const res = await fetch(`/api/vidman/internal-coverage/${encodeURIComponent(String(id))}`);
      if (!res.ok) throw new Error(await res.text());
      setDetail(await res.json());
    } catch (err: any) {
      toast.error(err?.message || 'Failed to load internal coverage detail');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRows(true);
    void loadCounters();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tier, reviewStatus, page]);

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
    setSelectedId(rows[next]?.productId ?? null);
  }, [rows, selectedIndex]);

  const afterAction = async () => {
    await loadCounters();
    await loadRows(true);
  };

  const action = async (kind: 'approve' | 'reject' | 'no-match', canonicalId?: number) => {
    if (!detail || isReadOnly) return;
    const top = detail.candidates[0];
    const targetCanonicalId = canonicalId ?? top?.canonicalProductId;
    if (kind !== 'no-match' && !targetCanonicalId) return;
    const endpoint =
      kind === 'approve'
        ? `/api/vidman/internal-coverage/${detail.productId}/approve`
        : kind === 'reject'
          ? `/api/vidman/internal-coverage/${detail.productId}/reject-candidate`
          : `/api/vidman/internal-coverage/${detail.productId}/mark-no-match`;
    const body = kind === 'no-match'
      ? { reason: 'manual reverse review' }
      : { canonical_product_id: targetCanonicalId, reason: 'manual reverse review' };
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      toast.error(await res.text());
      return;
    }
    toast.success(kind === 'approve' ? 'Coverage approved' : kind === 'reject' ? 'Candidate rejected' : 'Marked no Vidman match');
    await afterAction();
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || isReadOnly) return;
      const top = detail?.candidates?.[0];
      if (event.key === 'j' || event.key === 'ArrowDown') {
        event.preventDefault();
        moveSelection(1);
      } else if (event.key === 'k' || event.key === 'ArrowUp') {
        event.preventDefault();
        moveSelection(-1);
      } else if (event.key.toLowerCase() === 'a' && top && !top.hardConflicts.length) {
        event.preventDefault();
        void action('approve', top.canonicalProductId);
      } else if (event.key.toLowerCase() === 'r' && top) {
        event.preventDefault();
        void action('reject', top.canonicalProductId);
      } else if (event.key.toLowerCase() === 'n') {
        event.preventDefault();
        void action('no-match');
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  });

  const covered = counters?.coveredInternalProducts ?? 0;
  const totalInternal = counters?.totalInternalProducts ?? 0;

  return (
    <div className="space-y-4">
      <div className="grid gap-3 border-b border-gray-200 pb-4 xl:grid-cols-[1fr_auto]">
        <div className="grid gap-2 md:grid-cols-[170px_150px_1fr_220px]">
          <select className="h-9 rounded-md border border-gray-300 bg-white px-3 text-sm" value={tier} onChange={(e) => { setTier(e.target.value); setPage(1); }}>
            {tiers.map((item) => <option key={item.value || 'all'} value={item.value}>{item.label}</option>)}
          </select>
          <select className="h-9 rounded-md border border-gray-300 bg-white px-3 text-sm" value={reviewStatus} onChange={(e) => { setReviewStatus(e.target.value); setPage(1); }}>
            <option value="unreviewed">Unreviewed</option>
            <option value="reviewed">Reviewed</option>
            <option value="all">All</option>
          </select>
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search code or product name" />
          <Input value={manufacturer} onChange={(e) => setManufacturer(e.target.value)} placeholder="Manufacturer" />
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-5">
          <CounterBox label="Covered" value={`${covered}/${totalInternal}`} />
          <CounterBox label="Uncovered" value={String(counters?.uncoveredInternalProducts ?? 0)} />
          <CounterBox label="Strong left" value={String(counters?.tiers?.COVERAGE_A_STRONG?.remaining ?? 0)} />
          <CounterBox label="Good left" value={String(counters?.tiers?.COVERAGE_B_GOOD?.remaining ?? 0)} />
          <CounterBox label="Strong+Good" value={String(counters?.potentialCoverageStrongGood ?? 0)} />
        </div>
      </div>

      <div className="grid min-h-[680px] gap-4 xl:grid-cols-[minmax(540px,0.95fr)_minmax(540px,1.05fr)]">
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
                <TableHead>Code</TableHead>
                <TableHead>Our product</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Best Vidman</TableHead>
                <TableHead className="text-right">Score</TableHead>
                <TableHead className="text-right">Gap</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow
                  key={row.productId}
                  data-state={row.productId === selectedId ? 'selected' : undefined}
                  className="cursor-pointer"
                  onClick={() => setSelectedId(row.productId)}
                >
                  <TableCell className="font-mono text-xs">{row.productCode || '-'}</TableCell>
                  <TableCell className="max-w-[230px] whitespace-normal">
                    <div className="font-medium text-gray-900">{row.productName}</div>
                    <div className="text-xs text-gray-500">{row.manufacturer || '-'}</div>
                  </TableCell>
                  <TableCell><Badge variant="outline" className={statusTone(row.coverageStatus)}>{row.coverageStatus}</Badge></TableCell>
                  <TableCell className="max-w-[210px] whitespace-normal text-xs">{row.topCanonicalName || '-'}</TableCell>
                  <TableCell className="text-right">{fmt(row.topScore)}</TableCell>
                  <TableCell className="text-right">{fmt(row.scoreGap)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>

        <div className="min-w-0 space-y-4">
          {!detail || detailLoading ? (
            <div className="rounded-md border border-gray-200 p-6 text-sm text-gray-600">{detailLoading ? 'Loading detail...' : 'Select an internal product'}</div>
          ) : (
            <>
              <section className="space-y-3 border-b border-gray-200 pb-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="text-lg font-semibold text-gray-950">{detail.productName}</h3>
                    <p className="text-sm text-gray-600">{detail.productCode || '-'} | {detail.manufacturer || '-'}</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Badge variant="outline" className={tierTone(detail.coverage.tier)}>{detail.coverage.tier}</Badge>
                    <Badge variant="outline" className={statusTone(detail.coverageStatus)}>{detail.coverageStatus}</Badge>
                  </div>
                </div>
                <IdentityGrid identity={detail.normalized} />
                <div className="text-xs text-gray-500 break-all">{detail.normalized?.signature || '-'}</div>
              </section>

              <section className="space-y-3 border-b border-gray-200 pb-4">
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-semibold text-gray-900">Vidman candidates</h4>
                  <Button variant="outline" size="sm" onClick={() => action('no-match')} disabled={isReadOnly || detail.coverageStatus === 'AUTO_MATCHED' || detail.coverageStatus === 'MANUALLY_APPROVED'}>
                    <ShieldAlert className="h-4 w-4" />
                    No Vidman match
                  </Button>
                </div>
                {detail.candidates.map((candidate) => (
                  <CandidateCard
                    key={candidate.canonicalProductId}
                    candidate={candidate}
                    ours={detail.normalized}
                    isReadOnly={isReadOnly}
                    onApprove={() => action('approve', candidate.canonicalProductId)}
                    onReject={() => action('reject', candidate.canonicalProductId)}
                  />
                ))}
                {!detail.candidates.length ? <div className="rounded-md border border-gray-200 p-3 text-sm text-gray-600">No active Vidman candidates remain.</div> : null}
              </section>

              {detail.rejectedCandidates.length ? (
                <section className="space-y-2">
                  <h4 className="text-sm font-semibold text-gray-900">Rejected candidates</h4>
                  {detail.rejectedCandidates.map((item) => (
                    <div key={item.canonicalProductId} className="rounded-md border border-gray-200 px-3 py-2 text-xs text-gray-600">
                      Canonical {item.canonicalProductId}: {item.reason || 'rejected'}
                    </div>
                  ))}
                </section>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function CounterBox({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-gray-200 px-3 py-2">
      <div className="font-semibold text-gray-800">{label}</div>
      <div className="text-gray-500">{value}</div>
    </div>
  );
}

function IdentityGrid({ identity }: { identity: Record<string, any> }) {
  return (
    <div className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-3">
      {Object.entries(fieldLabels).map(([key, label]) => (
        <div key={key} className="rounded-md border border-gray-200 px-3 py-2">
          <div className="text-gray-500">{label}</div>
          <div className="font-medium text-gray-900">{norm(identity?.[key])}</div>
        </div>
      ))}
    </div>
  );
}

function CandidateCard({
  candidate,
  ours,
  isReadOnly,
  onApprove,
  onReject,
}: {
  candidate: Candidate;
  ours: Record<string, any>;
  isReadOnly: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  const hasConflicts = candidate.hardConflicts.length > 0;
  return (
    <div className={`rounded-md border p-3 ${hasConflicts ? 'border-red-200 bg-red-50' : 'border-emerald-200 bg-emerald-50'}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-gray-950">#{candidate.rank} {candidate.canonicalName}</div>
          <div className="text-xs text-gray-600">{candidate.canonicalManufacturer || '-'} | {candidate.canonicalProductId}</div>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className={statusTone(candidate.mappingContext.matchStatus)}>{candidate.mappingContext.matchStatus}</Badge>
          <div className="text-right text-sm font-semibold">{fmt(candidate.score)}</div>
        </div>
      </div>
      {candidate.mappingContext.mappedProductId ? (
        <div className="mt-2 rounded-md border border-white/70 bg-white/70 px-2 py-1 text-xs text-gray-700">
          Mapped to {candidate.mappingContext.mappedProductId}: {candidate.mappingContext.mappedProductName || '-'}
        </div>
      ) : null}
      <EvidenceGrid ours={ours} vidman={candidate.identity} candidate={candidate} />
      {candidate.rawExamples.length ? (
        <div className="mt-3 space-y-1">
          {candidate.rawExamples.slice(0, 3).map((item, idx) => (
            <div key={`${item.rawName}-${idx}`} className="rounded-md border border-white/70 bg-white/70 px-2 py-1 text-xs text-gray-600">
              <div className="font-medium text-gray-900">{item.rawName}</div>
              <div>{item.rawManufacturer || '-'} | {item.account || '-'} | {item.priceList || '-'} | main {item.mainId || '-'}</div>
            </div>
          ))}
        </div>
      ) : null}
      <div className="mt-3 flex flex-wrap gap-2">
        <Button size="sm" onClick={onApprove} disabled={isReadOnly || hasConflicts || candidate.mappingContext.matchStatus === 'AUTO_MATCHED' || candidate.mappingContext.matchStatus === 'MANUALLY_APPROVED'}>
          <Check className="h-4 w-4" />
          Approve
        </Button>
        <Button variant="outline" size="sm" onClick={onReject} disabled={isReadOnly}>
          <X className="h-4 w-4" />
          Reject
        </Button>
        {hasConflicts ? <span className="self-center text-xs font-medium text-red-700">Approval disabled: hard conflict</span> : null}
      </div>
    </div>
  );
}

function EvidenceGrid({ ours, vidman, candidate }: { ours: Record<string, any>; vidman: Record<string, any>; candidate: Candidate }) {
  const missingInternal = new Set(candidate.missingOnInternal || []);
  const missingVidman = new Set(candidate.missingOnVidman || []);
  const conflicts = new Set(candidate.hardConflicts || []);
  const shared = new Set(candidate.sharedStructuralFields || []);
  return (
    <div className="mt-3 grid gap-2 text-xs md:grid-cols-2">
      {Object.entries(fieldLabels).map(([key, label]) => {
        const status = conflicts.has(key)
          ? 'CONFLICT'
          : missingInternal.has(key)
            ? 'MISSING INTERNAL'
            : missingVidman.has(key)
              ? 'MISSING VIDMAN'
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
              <div><span className="text-gray-400">Ours</span><br />{norm(ours?.[key])}</div>
              <div><span className="text-gray-400">Vidman</span><br />{norm(vidman?.[key])}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
