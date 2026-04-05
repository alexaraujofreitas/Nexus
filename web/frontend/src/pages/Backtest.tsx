import { useState, useEffect, useRef } from 'react';
import { FlaskConical, Play, Loader2 } from 'lucide-react';
import { startBacktest, getBacktestStatus, getBacktestResults } from '../api/backtest';
import type { BacktestMetrics } from '../api/backtest';
import { cn, formatPct } from '../lib/utils';

const SYMBOLS = [
  'BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'XRP/USDT', 'SOL/USDT',
  'TRX/USDT', 'DOGE/USDT', 'ADA/USDT', 'BCH/USDT', 'HYPE/USDT',
  'LINK/USDT', 'XLM/USDT', 'AVAX/USDT', 'HBAR/USDT', 'SUI/USDT',
  'NEAR/USDT', 'ICP/USDT', 'ONDO/USDT', 'ALGO/USDT', 'RENDER/USDT',
];
const TIMEFRAMES = ['15m', '30m', '1h', '4h'];

function MetricBadge({ label, value, threshold, format = 'num' }: {
  label: string; value: number; threshold?: number; format?: 'num' | 'pct';
}) {
  const fmt = format === 'pct' ? formatPct(value) : value.toFixed(2);
  const color = threshold !== undefined ? (value >= threshold ? 'text-green-600' : 'text-red-600') : 'text-gray-900';
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-3">
      <p className="text-xs text-gray-400 mb-0.5">{label}</p>
      <p className={cn('text-lg font-mono font-semibold', color)}>{fmt}</p>
    </div>
  );
}

export default function Backtest() {
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']);
  const [startDate, setStartDate] = useState('2024-01-01');
  const [endDate, setEndDate] = useState('2026-03-01');
  const [timeframe, setTimeframe] = useState('30m');
  const [feePct, setFeePct] = useState(0.04);

  const [jobId, setJobId] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [results, setResults] = useState<BacktestMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const toggleSymbol = (sym: string) => {
    setSelectedSymbols((prev) =>
      prev.includes(sym) ? prev.filter((s) => s !== sym) : [...prev, sym],
    );
  };

  const handleStart = async () => {
    setRunning(true);
    setProgress(0);
    setElapsed(0);
    setResults(null);
    setError(null);
    try {
      const resp = await startBacktest({
        symbols: selectedSymbols,
        start_date: startDate,
        end_date: endDate,
        timeframe,
        fee_pct: feePct,
      });
      setJobId(resp.job_id);
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Failed to start backtest');
      setRunning(false);
    }
  };

  // Poll progress
  useEffect(() => {
    if (!jobId || !running) return;
    pollRef.current = setInterval(async () => {
      try {
        const st = await getBacktestStatus(jobId);
        setProgress(st.progress_pct ?? 0);
        setElapsed(st.elapsed_s ?? 0);
        if (st.status === 'complete' || st.status === 'error') {
          clearInterval(pollRef.current!);
          if (st.status === 'complete') {
            const res = await getBacktestResults(jobId);
            setResults(res.metrics);
          } else {
            setError('Backtest failed');
          }
          setRunning(false);
        }
      } catch {
        // Poll silently
      }
    }, 2000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [jobId, running]);

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <FlaskConical className="w-5 h-5 text-gray-400" />
        <h1 className="text-xl font-semibold text-gray-900">Backtesting</h1>
      </div>

      <div className="flex flex-col lg:flex-row gap-4">
        {/* Config panel */}
        <div className="lg:w-72 shrink-0 bg-white rounded-lg border border-gray-200 p-4 space-y-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Configuration</p>

          <div>
            <p className="text-sm text-gray-600 mb-2">Symbols</p>
            <div className="flex flex-wrap gap-1.5">
              {SYMBOLS.map((s) => (
                <button
                  key={s}
                  onClick={() => toggleSymbol(s)}
                  className={cn(
                    'px-3 py-1.5 rounded-lg text-xs font-medium min-h-[36px] transition-colors',
                    selectedSymbols.includes(s)
                      ? 'bg-blue-600 text-white'
                      : 'bg-gray-100 text-gray-500 hover:bg-gray-200',
                  )}
                >
                  {s.replace('/USDT', '')}
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-xs text-gray-500">Start</label>
              <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} className="w-full px-2 py-1.5 border border-gray-300 rounded text-sm min-h-[44px]" />
            </div>
            <div>
              <label className="text-xs text-gray-500">End</label>
              <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} className="w-full px-2 py-1.5 border border-gray-300 rounded text-sm min-h-[44px]" />
            </div>
          </div>

          <div>
            <label className="text-xs text-gray-500">Timeframe</label>
            <div className="flex gap-1.5 mt-1">
              {TIMEFRAMES.map((t) => (
                <button key={t} onClick={() => setTimeframe(t)} className={cn(
                  'px-3 py-1.5 rounded text-xs font-medium min-h-[36px]',
                  timeframe === t ? 'bg-gray-900 text-white' : 'bg-gray-100 text-gray-600',
                )}>{t}</button>
              ))}
            </div>
          </div>

          <div>
            <label className="text-xs text-gray-500">Fee % (per side)</label>
            <input type="number" value={feePct} onChange={(e) => setFeePct(parseFloat(e.target.value) || 0)} step={0.01} min={0} max={1} className="w-full px-2 py-1.5 border border-gray-300 rounded text-sm font-mono min-h-[44px]" />
          </div>

          <button
            onClick={handleStart}
            disabled={running || selectedSymbols.length === 0}
            className={cn(
              'w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium min-h-[44px] transition-colors',
              running ? 'bg-gray-200 text-gray-400 cursor-not-allowed' : 'bg-blue-600 text-white hover:bg-blue-700',
            )}
          >
            {running ? <><Loader2 className="w-4 h-4 animate-spin" /> Running...</> : <><Play className="w-4 h-4" /> Run Backtest</>}
          </button>
        </div>

        {/* Results */}
        <div className="flex-1 space-y-4">
          {/* Progress */}
          {running && (
            <div className="bg-white rounded-lg border border-gray-200 p-4">
              <div className="flex justify-between text-sm mb-2">
                <span className="text-gray-600">Running backtest...</span>
                <span className="text-gray-500">{elapsed.toFixed(0)}s elapsed</span>
              </div>
              <div className="w-full bg-gray-100 rounded-full h-3">
                <div className="bg-blue-600 h-3 rounded-full transition-all" style={{ width: `${progress}%` }} />
              </div>
              <p className="text-xs text-gray-400 mt-1 text-right">{progress.toFixed(0)}%</p>
            </div>
          )}

          {error && (
            <div className="bg-red-50 border border-red-200 text-red-800 text-sm rounded-lg px-4 py-3">{error}</div>
          )}

          {results && (
            <>
              <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Results</p>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
                <MetricBadge label="Profit Factor" value={results.pf} threshold={1.0} />
                <MetricBadge label="Win Rate" value={results.wr} threshold={45} format="pct" />
                <MetricBadge label="Max Drawdown" value={Math.abs(results.max_dd)} format="pct" />
                <MetricBadge label="CAGR" value={results.cagr} format="pct" />
                <MetricBadge label="Sharpe" value={results.sharpe} threshold={0.5} />
                <MetricBadge label="Trades" value={results.n_trades} />
              </div>
            </>
          )}

          {!running && !results && !error && (
            <div className="bg-white rounded-lg border border-gray-200 p-12 text-center">
              <FlaskConical className="w-10 h-10 text-gray-300 mx-auto mb-3" />
              <p className="text-sm text-gray-400">Configure parameters and click Run Backtest</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
