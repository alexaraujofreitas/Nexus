/** Format a number as USD currency. Omits cents for values >= $1,000. */
export function formatUSD(value: number): string {
  const abs = Math.abs(value);
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: abs >= 1000 ? 0 : 2,
    maximumFractionDigits: abs >= 1000 ? 0 : 2,
  }).format(value);
}

/** Format a percentage */
export function formatPct(value: number, decimals = 2): string {
  return `${value >= 0 ? '+' : ''}${value.toFixed(decimals)}%`;
}

/** Format a date relative to now */
export function timeAgo(date: string | Date | number | null | undefined): string {
  if (date == null) return '—';
  const now = new Date();
  const d = typeof date === 'string' ? new Date(date)
          : typeof date === 'number' ? new Date(date < 1e10 ? date * 1000 : date)
          : date;
  if (isNaN(d.getTime())) return '—';
  const seconds = Math.floor((now.getTime() - d.getTime()) / 1000);
  if (seconds < 0) return 'just now';

  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/** Classname merge helper */
export function cn(...classes: (string | undefined | false)[]): string {
  return classes.filter(Boolean).join(' ');
}
