import { useEffect, useState, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { FileText, Filter, Trash2 } from 'lucide-react';
import { getRecentLogs } from '../api/logs';
import type { LogEntry } from '../api/logs';
import { useWSStore } from '../stores/wsStore';
import { cn } from '../lib/utils';

const LEVELS = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const;
const COMPONENTS = ['ALL', 'engine', 'scanner', 'signals', 'risk', 'executor', 'exchange'] as const;

const LEVEL_COLORS: Record<string, string> = {
  DEBUG: 'text-gray-400 bg-gray-50',
  INFO: 'text-blue-600 bg-blue-50',
  WARNING: 'text-amber-600 bg-amber-50',
  ERROR: 'text-red-600 bg-red-50',
  CRITICAL: 'text-red-700 bg-red-100 font-bold',
};

export default function Logs() {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [level, setLevel] = useState('ALL');
  const [component, setComponent] = useState('ALL');
  const [search, setSearch] = useState('');
  const [autoScroll, setAutoScroll] = useState(true);
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const { subscribe, lastMessage, status } = useWSStore();

  useEffect(() => { if (status === 'connected') subscribe('logs'); }, [status, subscribe]);

  // WS log entries
  useEffect(() => {
    const wsLog = lastMessage['logs'];
    if (wsLog && wsLog.timestamp) {
      setEntries((prev) => [wsLog, ...prev].slice(0, 2000));
    }
  }, [lastMessage]);

  // Initial load
  const { data: initialLogs } = useQuery({
    queryKey: ['logs-recent', level, component, search],
    queryFn: () => getRecentLogs({
      limit: 200,
      level: level !== 'ALL' ? level : undefined,
      component: component !== 'ALL' ? component : undefined,
      search: search || undefined,
    }),
    refetchInterval: 15000,
  });

  useEffect(() => {
    if (initialLogs?.entries) {
      setEntries(initialLogs.entries);
    }
  }, [initialLogs]);

  // Auto-scroll
  useEffect(() => {
    if (autoScroll && listRef.current) {
      listRef.current.scrollTop = 0;
    }
  }, [entries, autoScroll]);

  // Filter entries client-side
  const filtered = entries.filter((e) => {
    if (level !== 'ALL' && e.level !== level) return false;
    if (component !== 'ALL' && e.component !== component) return false;
    if (search && !e.message.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  const formatTime = (ts: string) => {
    try { return new Date(ts).toLocaleTimeString(); } catch { return ts; }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <FileText className="w-5 h-5 text-gray-400" />
        <h1 className="text-xl font-semibold text-gray-900">System Logs</h1>
        <span className="text-xs text-gray-400">{filtered.length} entries</span>
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap gap-2 items-center">
        <div className="flex items-center gap-1.5">
          <Filter className="w-3.5 h-3.5 text-gray-400" />
          <select
            value={level}
            onChange={(e) => setLevel(e.target.value)}
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm min-h-[44px] bg-white"
          >
            {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </div>

        <select
          value={component}
          onChange={(e) => setComponent(e.target.value)}
          className="px-3 py-2 border border-gray-300 rounded-lg text-sm min-h-[44px] bg-white"
        >
          {COMPONENTS.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>

        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search messages..."
          className="px-3 py-2 border border-gray-300 rounded-lg text-sm min-h-[44px] flex-1 min-w-[150px] focus:outline-none focus:ring-2 focus:ring-blue-500"
        />

        <label className="flex items-center gap-2 text-sm text-gray-600 cursor-pointer min-h-[44px] px-2">
          <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} className="rounded" />
          Auto-scroll
        </label>

        <button onClick={() => setEntries([])} className="p-2 text-gray-400 hover:text-red-600 min-h-[44px] min-w-[44px] flex items-center justify-center" title="Clear">
          <Trash2 className="w-4 h-4" />
        </button>
      </div>

      {/* Log stream */}
      <div ref={listRef} className="bg-white rounded-lg border border-gray-200 overflow-y-auto max-h-[calc(100vh-240px)] font-mono text-xs">
        {filtered.length === 0 ? (
          <div className="p-8 text-center text-gray-400 text-sm">No log entries</div>
        ) : (
          filtered.map((e, i) => (
            <div
              key={i}
              className={cn(
                'px-3 py-1.5 border-b border-gray-50 cursor-pointer hover:bg-gray-50 transition-colors',
                (e.level === 'ERROR' || e.level === 'CRITICAL') && 'bg-red-50/50',
              )}
              onClick={() => setExpandedIdx(expandedIdx === i ? null : i)}
            >
              <div className="flex items-start gap-2">
                <span className="text-gray-400 shrink-0 w-16">{formatTime(e.timestamp)}</span>
                <span className={cn('px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0 w-16 text-center', LEVEL_COLORS[e.level] || 'bg-gray-100 text-gray-600')}>
                  {e.level}
                </span>
                <span className="text-gray-500 shrink-0 w-16 truncate">{e.component}</span>
                <span className="text-gray-800 break-all">{e.message}</span>
              </div>
              {expandedIdx === i && e.extra && (
                <pre className="mt-1 ml-[13rem] text-gray-500 text-[10px] whitespace-pre-wrap">
                  {JSON.stringify(e.extra, null, 2)}
                </pre>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
