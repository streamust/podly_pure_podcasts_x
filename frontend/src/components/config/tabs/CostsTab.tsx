import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'react-hot-toast';
import ModalShell from '../../ModalShell';
import { costsApi } from '../../../services/api';
import type { CostSummary, CallLog, EstimatedCostBackfillResult, TokenBackfillResult } from '../../../types';
import { formatBackendDateTime } from '../../../utils/datetime';

function MonthSelector({
  year,
  month,
  onChange,
}: {
  year: number;
  month: number;
  onChange: (year: number, month: number) => void;
}) {
  const months = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
  ];
  return (
    <div className="flex items-center gap-2">
      <button
        onClick={() => {
          const d = new Date(year, month - 2);
          onChange(d.getFullYear(), d.getMonth() + 1);
        }}
        className="px-2 py-1 text-sm border border-gray-300 rounded hover:bg-gray-100"
      >
        ‹
      </button>
      <span className="text-sm font-medium w-24 text-center">
        {months[month - 1]} {year}
      </span>
      <button
        onClick={() => {
          const d = new Date(year, month);
          onChange(d.getFullYear(), d.getMonth() + 1);
        }}
        className="px-2 py-1 text-sm border border-gray-300 rounded hover:bg-gray-100"
      >
        ›
      </button>
    </div>
  );
}

function costTokenTotal(call: CallLog['calls'][number]) {
  const hasCostTokens =
    call.prompt_tokens != null ||
    call.cached_prompt_tokens != null ||
    call.completion_tokens != null;
  if (!hasCostTokens) return call.total_tokens ?? null;
  return (
    (call.prompt_tokens ?? 0) +
    (call.cached_prompt_tokens ?? 0) +
    (call.completion_tokens ?? 0)
  );
}

function EstimatedCostBackfillSummary({ result }: { result: EstimatedCostBackfillResult }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-sm">
      <div className="border border-gray-200 rounded p-2">
        <div className="text-xs text-gray-500">Scanned</div>
        <div className="font-mono text-gray-900">{result.scanned.toLocaleString()}</div>
      </div>
      <div className="border border-gray-200 rounded p-2">
        <div className="text-xs text-gray-500">Eligible (billable LLM)</div>
        <div className="font-mono text-gray-900">{result.eligible.toLocaleString()}</div>
      </div>
      <div className="border border-gray-200 rounded p-2">
        <div className="text-xs text-gray-500">Non-billable</div>
        <div className="font-mono text-gray-900">{result.skipped_non_billable.toLocaleString()}</div>
      </div>
      <div className="border border-gray-200 rounded p-2">
        <div className="text-xs text-gray-500">Updated</div>
        <div className="font-mono text-gray-900">{result.updated.toLocaleString()}</div>
      </div>
    </div>
  );
}

function BackfillSummary({ result }: { result: TokenBackfillResult }) {
  const modelEntries = Object.entries(result.models).slice(0, 5);
  return (
    <div className="space-y-3 text-sm">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <div className="border border-gray-200 rounded p-2">
          <div className="text-xs text-gray-500">Scanned</div>
          <div className="font-mono text-gray-900">{result.scanned.toLocaleString()}</div>
        </div>
        <div className="border border-gray-200 rounded p-2">
          <div className="text-xs text-gray-500">Eligible</div>
          <div className="font-mono text-gray-900">{result.eligible.toLocaleString()}</div>
        </div>
        <div className="border border-gray-200 rounded p-2">
          <div className="text-xs text-gray-500">Would update</div>
          <div className="font-mono text-gray-900">{result.would_update.toLocaleString()}</div>
        </div>
        <div className="border border-gray-200 rounded p-2">
          <div className="text-xs text-gray-500">Updated</div>
          <div className="font-mono text-gray-900">{result.updated.toLocaleString()}</div>
        </div>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs text-gray-600">
        <div>Existing token data: <span className="font-mono">{result.skipped_existing.toLocaleString()}</span></div>
        <div>Non-LLM calls: <span className="font-mono">{result.skipped_non_llm.toLocaleString()}</span></div>
        <div>Missing text: <span className="font-mono">{result.skipped_missing_text.toLocaleString()}</span></div>
      </div>
      {modelEntries.length > 0 && (
        <div className="border border-gray-200 rounded">
          <table className="min-w-full text-xs">
            <thead>
              <tr className="border-b border-gray-200">
                <th className="text-left py-2 px-2 font-medium text-gray-600">Model</th>
                <th className="text-right py-2 px-2 font-medium text-gray-600">Would update</th>
                <th className="text-right py-2 px-2 font-medium text-gray-600">Updated</th>
              </tr>
            </thead>
            <tbody>
              {modelEntries.map(([modelName, stats]) => (
                <tr key={modelName} className="border-b border-gray-100 last:border-b-0">
                  <td className="py-2 px-2 font-mono text-gray-700 truncate max-w-56">{modelName}</td>
                  <td className="py-2 px-2 text-right font-mono">{stats.would_update}</td>
                  <td className="py-2 px-2 text-right font-mono">{stats.updated}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {result.errors.length > 0 && (
        <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded p-2">
          {result.errors.length} tokenizer/update error(s); first: {result.errors[0].error}
        </div>
      )}
    </div>
  );
}

export default function CostsTab() {
  const now = new Date();
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth() + 1);
  const [callPage, setCallPage] = useState(1);
  const [showBackfillWizard, setShowBackfillWizard] = useState(false);
  const [backfillLimit, setBackfillLimit] = useState('');
  const [backfillResult, setBackfillResult] = useState<TokenBackfillResult | null>(null);
  const [backfillNotice, setBackfillNotice] = useState<{
    type: 'success' | 'error';
    message: string;
  } | null>(null);
  const [showCostBackfillWizard, setShowCostBackfillWizard] = useState(false);
  const [costBackfillLimit, setCostBackfillLimit] = useState('');
  const [costBackfillResult, setCostBackfillResult] =
    useState<EstimatedCostBackfillResult | null>(null);
  const [costBackfillNotice, setCostBackfillNotice] = useState<{
    type: 'success' | 'error';
    message: string;
  } | null>(null);
  const queryClient = useQueryClient();

  const { data: costs, isLoading: costsLoading } = useQuery<CostSummary>({
    queryKey: ['admin-costs', year, month],
    queryFn: () => costsApi.getCosts(year, month),
  });

  const { data: calls, isLoading: callsLoading } = useQuery<CallLog>({
    queryKey: ['admin-calls', callPage],
    queryFn: () => costsApi.getCalls(callPage, 50),
  });

  const cleanupCancelledMutation = useMutation({
    mutationFn: () => costsApi.cleanupCancelledFeeds(),
    onSuccess: (data) => {
      toast.success(`Removed ${data.removed} cancelled-subscriber feed(s)`);
      queryClient.invalidateQueries({ queryKey: ['admin-costs'] });
    },
    onError: () => toast.error('Cleanup failed'),
  });

  const cleanupOrphanMutation = useMutation({
    mutationFn: () => costsApi.cleanupOrphanFeeds(),
    onSuccess: (data) => {
      toast.success(`Removed ${data.removed} orphan feed(s)`);
      queryClient.invalidateQueries({ queryKey: ['admin-costs'] });
    },
    onError: () => toast.error('Cleanup failed'),
  });

  const costBackfillMutation = useMutation({
    mutationFn: (apply: boolean) => costsApi.backfillEstimatedCost({
      apply,
      limit: costBackfillLimit.trim() ? Number(costBackfillLimit) : null,
    }),
    onMutate: () => {
      setCostBackfillNotice(null);
    },
    onSuccess: (data) => {
      setCostBackfillResult(data);
      if (data.applied) {
        setCostBackfillNotice({
          type: 'success',
          message: `Backfill complete. Updated ${data.updated} model call(s).`,
        });
        toast.success(`Backfilled ${data.updated} model call(s)`);
        queryClient.invalidateQueries({ queryKey: ['admin-costs'] });
        queryClient.invalidateQueries({ queryKey: ['admin-calls'] });
      } else {
        setCostBackfillNotice({
          type: 'success',
          message: `Dry run complete. ${data.eligible} model call(s) would be updated.`,
        });
        toast.success(`Dry run found ${data.eligible} model call(s) to price`);
      }
    },
    onError: () => {
      setCostBackfillNotice({
        type: 'error',
        message: 'Estimated-cost backfill failed. No model calls were updated.',
      });
      toast.error('Estimated-cost backfill failed');
    },
  });

  const backfillMutation = useMutation({
    mutationFn: (apply: boolean) => costsApi.backfillTokenUsage({
      apply,
      limit: backfillLimit.trim() ? Number(backfillLimit) : null,
    }),
    onMutate: () => {
      setBackfillNotice(null);
    },
    onSuccess: (data) => {
      setBackfillResult(data);
      if (data.apply) {
        setBackfillNotice({
          type: data.failed_updates > 0 ? 'error' : 'success',
          message: data.failed_updates > 0
            ? `Backfill finished with ${data.updated} update(s) and ${data.failed_updates} failure(s).`
            : `Backfill complete. Updated ${data.updated} model call(s).`,
        });
        toast.success(`Backfilled ${data.updated} model call(s)`);
        queryClient.invalidateQueries({ queryKey: ['admin-costs'] });
        queryClient.invalidateQueries({ queryKey: ['admin-calls'] });
      } else {
        setBackfillNotice({
          type: 'success',
          message: `Dry run complete. ${data.would_update} model call(s) would be updated.`,
        });
        toast.success(`Dry run found ${data.would_update} model call(s) to update`);
      }
    },
    onError: () => {
      setBackfillNotice({
        type: 'error',
        message: 'Token backfill failed. No token usage was updated.',
      });
      toast.error('Token backfill failed');
    },
  });

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-gray-900">Platform Costs</h3>
          <p className="text-sm text-gray-500 mt-1">
            <b>Platform logic:</b> LiteLLM token pricing + ${costs?.whisper_cost_rate_per_hour ?? 0.04}/hr Whisper + ${costs?.ina_cost_rate_per_hour ?? 0}/hr INA | <b>User attribution:</b> Divided by subscribers
          </p>
        </div>
        <MonthSelector
          year={year}
          month={month}
          onChange={(y, m) => {
            setYear(y);
            setMonth(m);
          }}
        />
      </div>

      {/* Summary */}
      {costsLoading ? (
        <div className="text-sm text-gray-500">Loading cost data…</div>
      ) : costs ? (
        <>
          <div className="bg-indigo-50 border border-indigo-200 rounded-lg p-4">
            <div className="text-2xl font-bold text-indigo-700">
              ${costs.total_cost.toFixed(2)}
            </div>
            <div className="text-sm text-gray-600 mt-1">
              Total processing cost this month
              {' · '}
              <span className="text-gray-700">{(costs.total_audio_hours ?? 0).toFixed(2)} hours of audio</span>
            </div>
            <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2 text-sm text-gray-700">
              <div>LLM: <span className="font-mono">${costs.total_llm_cost.toFixed(2)}</span></div>
              <div>Whisper: <span className="font-mono">${costs.total_whisper_cost.toFixed(2)}</span></div>
              <div>INA: <span className="font-mono">${costs.total_ina_cost.toFixed(2)}</span></div>
            </div>
          </div>

          {/* Per-feed breakdown */}
          <section>
            <h4 className="text-sm font-semibold text-gray-700 mb-3">Cost by Feed</h4>
            {costs.feeds.length === 0 ? (
              <p className="text-sm text-gray-500">No processed episodes this month.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead>
                    <tr className="border-b border-gray-200">
                      <th className="text-left py-2 pr-4 font-medium text-gray-600">Feed</th>
                      <th className="text-right py-2 pr-4 font-medium text-gray-600">Subs</th>
                      <th className="text-right py-2 pr-4 font-medium text-gray-600">Episodes</th>
                      <th className="text-right py-2 pr-4 font-medium text-gray-600">LLM</th>
                      <th className="text-right py-2 pr-4 font-medium text-gray-600">Whisper</th>
                      <th className="text-right py-2 pr-4 font-medium text-gray-600">INA</th>
                      <th className="text-right py-2 font-medium text-gray-600">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {costs.feeds.map((feed) => (
                      <tr key={feed.id} className="border-b border-gray-100 hover:bg-gray-50">
                        <td className="py-2 pr-4 text-gray-800 max-w-xs truncate">{feed.title}</td>
                        <td className="py-2 pr-4 text-right text-gray-600">{feed.subscriber_count}</td>
                        <td className="py-2 pr-4 text-right text-gray-600">{feed.episodes_this_month}</td>
                        <td className="py-2 pr-4 text-right font-mono text-gray-600">${feed.llm_cost.toFixed(2)}</td>
                        <td className="py-2 pr-4 text-right font-mono text-gray-600">${feed.whisper_cost.toFixed(2)}</td>
                        <td className="py-2 pr-4 text-right font-mono text-gray-600">${feed.ina_cost.toFixed(2)}</td>
                        <td className="py-2 text-right font-mono text-gray-800">
                          ${feed.monthly_cost.toFixed(2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Per-user breakdown */}
          <section>
            <h4 className="text-sm font-semibold text-gray-700 mb-3">Cost per User</h4>
            {costs.users.length === 0 ? (
              <p className="text-sm text-gray-500">No users found.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead>
                    <tr className="border-b border-gray-200">
                      <th className="text-left py-2 pr-4 font-medium text-gray-600">User</th>
                      <th className="text-left py-2 pr-4 font-medium text-gray-600">Status</th>
                      <th className="text-right py-2 pr-4 font-medium text-gray-600">Feeds</th>
                      <th className="text-right py-2 pr-4 font-medium text-gray-600">Sub $/mo</th>
                      <th className="text-right py-2 font-medium text-gray-600">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {costs.users.map((user) => (
                      <tr key={user.id} className="border-b border-gray-100 hover:bg-gray-50">
                        <td className="py-2 pr-4 text-gray-800">{user.username}</td>
                        <td className="py-2 pr-4">
                          <span
                            className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${user.subscription_status === 'active'
                              ? 'bg-green-100 text-green-700'
                              : 'bg-gray-100 text-gray-500'
                              }`}
                          >
                            {user.subscription_status}
                          </span>
                        </td>
                        <td className="py-2 pr-4 text-right text-gray-600">{user.feed_count}</td>
                        <td className="py-2 pr-4 text-right font-mono text-gray-600">
                          {user.subscription_amount_cents != null
                            ? `$${(user.subscription_amount_cents / 100).toFixed(2)}`
                            : '—'}
                        </td>
                        <td className="py-2 text-right font-mono text-gray-800">
                          ${user.monthly_cost.toFixed(2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      ) : null}

      {/* API Call Log */}
      <section>
        <h4 className="text-sm font-semibold text-gray-700 mb-3">API Call Log</h4>
        {callsLoading ? (
          <div className="text-sm text-gray-500">Loading call log…</div>
        ) : calls ? (
          <>
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-200">
                    <th className="text-left py-2 pr-4 font-medium text-gray-600">Model</th>
                    <th className="text-left py-2 pr-4 font-medium text-gray-600">Status</th>
                    <th className="text-right py-2 pr-4 font-medium text-gray-600">Tokens</th>
                    <th className="text-right py-2 pr-4 font-medium text-gray-600">Cost</th>
                    <th className="text-right py-2 pr-4 font-medium text-gray-600">Retries</th>
                    <th className="text-right py-2 font-medium text-gray-600">Timestamp</th>
                  </tr>
                </thead>
                <tbody>
                  {calls.calls.map((c) => (
                    <tr key={c.id} className="border-b border-gray-100 hover:bg-gray-50">
                      <td className="py-2 pr-4 text-gray-800 font-mono text-xs">{c.model_name}</td>
                      <td className="py-2 pr-4">
                        <span
                          className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${c.status === 'completed'
                            ? 'bg-green-100 text-green-700'
                            : c.status === 'failed'
                              ? 'bg-red-100 text-red-700'
                              : 'bg-gray-100 text-gray-500'
                            }`}
                        >
                          {c.status}
                        </span>
                      </td>
                      <td
                        className="py-2 pr-4 text-right text-gray-600 font-mono text-xs"
                        title={`prompt ${c.prompt_tokens ?? '—'} + cached ${c.cached_prompt_tokens ?? '—'} + completion ${c.completion_tokens ?? '—'}`}
                      >
                        {costTokenTotal(c) != null ? costTokenTotal(c)?.toLocaleString() : '—'}
                      </td>
                      <td className="py-2 pr-4 text-right text-gray-600 font-mono">
                        {c.estimated_cost != null ? `$${c.estimated_cost.toFixed(4)}` : '—'}
                      </td>
                      <td className="py-2 pr-4 text-right text-gray-600">{c.retry_attempts}</td>
                      <td className="py-2 text-right text-gray-500 text-xs">
                        {formatBackendDateTime(c.timestamp)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {/* Pagination */}
            {calls.pages > 1 && (
              <div className="flex items-center justify-between mt-3">
                <span className="text-xs text-gray-500">
                  Page {calls.page} of {calls.pages} ({calls.total} calls)
                </span>
                <div className="flex gap-2">
                  <button
                    disabled={calls.page <= 1}
                    onClick={() => setCallPage((p) => p - 1)}
                    className="px-3 py-1 text-xs border border-gray-300 rounded disabled:opacity-40 hover:bg-gray-100"
                  >
                    Prev
                  </button>
                  <button
                    disabled={calls.page >= calls.pages}
                    onClick={() => setCallPage((p) => p + 1)}
                    className="px-3 py-1 text-xs border border-gray-300 rounded disabled:opacity-40 hover:bg-gray-100"
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        ) : null}
      </section>

      {/* Cleanup utilities */}
      <section>
        <h4 className="text-sm font-semibold text-gray-700 mb-3">Cleanup Utilities</h4>
        <div className="flex flex-wrap gap-3">
          <button
            onClick={() => cleanupCancelledMutation.mutate()}
            disabled={cleanupCancelledMutation.isPending}
            className="px-4 py-2 text-sm bg-yellow-50 border border-yellow-300 text-yellow-800 rounded hover:bg-yellow-100 disabled:opacity-50"
          >
            {cleanupCancelledMutation.isPending ? 'Removing…' : 'Remove cancelled-subscriber feeds'}
          </button>
          <button
            onClick={() => cleanupOrphanMutation.mutate()}
            disabled={cleanupOrphanMutation.isPending}
            className="px-4 py-2 text-sm bg-red-50 border border-red-300 text-red-800 rounded hover:bg-red-100 disabled:opacity-50"
          >
            {cleanupOrphanMutation.isPending ? 'Removing…' : 'Remove orphan feeds (no subscribers)'}
          </button>
          <button
            onClick={() => {
              setShowBackfillWizard(true);
              setBackfillResult(null);
              setBackfillNotice(null);
            }}
            className="px-4 py-2 text-sm bg-blue-50 border border-blue-300 text-blue-800 rounded hover:bg-blue-100"
          >
            Backfill token usage
          </button>
          <button
            onClick={() => {
              setShowCostBackfillWizard(true);
              setCostBackfillResult(null);
              setCostBackfillNotice(null);
            }}
            className="px-4 py-2 text-sm bg-blue-50 border border-blue-300 text-blue-800 rounded hover:bg-blue-100"
            title="Populate ModelCall.estimated_cost_usd for legacy rows so the stats page can render cost without importing LiteLLM into the web process."
          >
            Backfill estimated cost
          </button>
        </div>
      </section>

      <ModalShell
        isOpen={showBackfillWizard}
        onClose={() => setShowBackfillWizard(false)}
        panelClassName="w-full max-w-2xl bg-white rounded-lg shadow-xl p-5"
      >
        <div className="space-y-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h4 className="text-base font-semibold text-gray-900">Backfill Token Usage</h4>
              <p className="text-sm text-gray-500 mt-1">
                Estimate missing prompt and completion tokens for legacy successful LLM calls. Cached prompt tokens stay blank.
              </p>
            </div>
            <button
              onClick={() => setShowBackfillWizard(false)}
              className="text-gray-400 hover:text-gray-700"
              aria-label="Close"
            >
              ×
            </button>
          </div>

          <label className="block">
            <span className="text-sm font-medium text-gray-700">Scan limit</span>
            <input
              type="number"
              min="1"
              value={backfillLimit}
              onChange={(event) => {
                setBackfillLimit(event.target.value);
                setBackfillResult(null);
                setBackfillNotice(null);
              }}
              placeholder="All model calls"
              className="mt-1 w-full border border-gray-300 rounded px-3 py-2 text-sm"
            />
          </label>

          {backfillResult && <BackfillSummary result={backfillResult} />}

          {backfillNotice && (
            <div
              className={`text-sm rounded border px-3 py-2 ${backfillNotice.type === 'success'
                ? 'bg-green-50 border-green-200 text-green-800'
                : 'bg-red-50 border-red-200 text-red-800'
                }`}
              role="status"
              aria-live="polite"
            >
              {backfillNotice.message}
            </div>
          )}

          <div className="flex flex-wrap justify-end gap-3">
            <button
              onClick={() => backfillMutation.mutate(false)}
              disabled={backfillMutation.isPending}
              className="px-4 py-2 text-sm border border-gray-300 rounded text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {backfillMutation.isPending ? 'Running…' : 'Run dry run'}
            </button>
            <button
              onClick={() => backfillMutation.mutate(true)}
              disabled={
                backfillMutation.isPending ||
                !backfillResult ||
                backfillResult.apply ||
                backfillResult.would_update <= 0
              }
              className="px-4 py-2 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              Apply backfill
            </button>
          </div>
        </div>
      </ModalShell>

      <ModalShell
        isOpen={showCostBackfillWizard}
        onClose={() => setShowCostBackfillWizard(false)}
        panelClassName="w-full max-w-2xl bg-white rounded-lg shadow-xl p-5"
      >
        <div className="space-y-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h4 className="text-base font-semibold text-gray-900">Backfill Estimated Cost</h4>
              <p className="text-sm text-gray-500 mt-1">
                Populate <code>ModelCall.estimated_cost_usd</code> for legacy
                rows so the stats page can render per-call cost without
                importing LiteLLM into the web process. This one call loads
                LiteLLM into the current web process (~160 MiB); restart the
                web service afterwards to release it. New calls are priced
                automatically by the worker.
              </p>
            </div>
            <button
              onClick={() => setShowCostBackfillWizard(false)}
              className="text-gray-400 hover:text-gray-700"
              aria-label="Close"
            >
              ×
            </button>
          </div>

          <label className="block">
            <span className="text-sm font-medium text-gray-700">Scan limit</span>
            <input
              type="number"
              min="1"
              value={costBackfillLimit}
              onChange={(event) => {
                setCostBackfillLimit(event.target.value);
                setCostBackfillResult(null);
                setCostBackfillNotice(null);
              }}
              placeholder="All unpriced successful calls"
              className="mt-1 w-full border border-gray-300 rounded px-3 py-2 text-sm"
            />
          </label>

          {costBackfillResult && <EstimatedCostBackfillSummary result={costBackfillResult} />}

          {costBackfillNotice && (
            <div
              className={`text-sm rounded border px-3 py-2 ${costBackfillNotice.type === 'success'
                ? 'bg-green-50 border-green-200 text-green-800'
                : 'bg-red-50 border-red-200 text-red-800'
                }`}
              role="status"
              aria-live="polite"
            >
              {costBackfillNotice.message}
            </div>
          )}

          <div className="flex flex-wrap justify-end gap-3">
            <button
              onClick={() => costBackfillMutation.mutate(false)}
              disabled={costBackfillMutation.isPending}
              className="px-4 py-2 text-sm border border-gray-300 rounded text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {costBackfillMutation.isPending ? 'Running…' : 'Run dry run'}
            </button>
            <button
              onClick={() => costBackfillMutation.mutate(true)}
              disabled={
                costBackfillMutation.isPending ||
                !costBackfillResult ||
                costBackfillResult.applied ||
                costBackfillResult.eligible <= 0
              }
              className="px-4 py-2 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              Apply backfill
            </button>
          </div>
        </div>
      </ModalShell>
    </div>
  );
}
