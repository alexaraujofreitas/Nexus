/**
 * Phase 6D: Reusable Loading Skeleton Primitives
 *
 * Provides shimmer-effect skeleton components for loading states.
 * Used by Dashboard, Trading, and Scanner pages.
 */

interface SkeletonProps {
  className?: string;
}

/** Base skeleton block with shimmer animation */
export function SkeletonBlock({ className = '' }: SkeletonProps) {
  return (
    <div
      className={`animate-pulse bg-gray-200 dark:bg-gray-700 rounded ${className}`}
    />
  );
}

/** Skeleton for a stat card (e.g., Dashboard metric tiles) */
export function SkeletonCard() {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4 space-y-3">
      <SkeletonBlock className="h-4 w-24" />
      <SkeletonBlock className="h-8 w-32" />
      <SkeletonBlock className="h-3 w-20" />
    </div>
  );
}

/** Skeleton for a table row */
export function SkeletonTableRow({ cols = 5 }: { cols?: number }) {
  return (
    <tr>
      {Array.from({ length: cols }).map((_, i) => (
        <td key={i} className="px-4 py-3">
          <SkeletonBlock className="h-4 w-full" />
        </td>
      ))}
    </tr>
  );
}

/** Skeleton for the Dashboard page */
export function DashboardSkeleton() {
  return (
    <div className="space-y-6 p-6" data-testid="dashboard-skeleton">
      {/* Metric cards row */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <SkeletonCard key={i} />
        ))}
      </div>
      {/* Chart placeholder */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <SkeletonBlock className="h-6 w-40 mb-4" />
        <SkeletonBlock className="h-48 w-full" />
      </div>
      {/* Table placeholder */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
        <table className="w-full">
          <tbody>
            {Array.from({ length: 5 }).map((_, i) => (
              <SkeletonTableRow key={i} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Skeleton for the Trading page */
export function TradingSkeleton() {
  return (
    <div className="space-y-6 p-6" data-testid="trading-skeleton">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 bg-white dark:bg-gray-800 rounded-lg shadow p-4">
          <SkeletonBlock className="h-6 w-32 mb-4" />
          <SkeletonBlock className="h-64 w-full" />
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4 space-y-3">
          <SkeletonBlock className="h-6 w-28" />
          {Array.from({ length: 4 }).map((_, i) => (
            <SkeletonBlock key={i} className="h-10 w-full" />
          ))}
        </div>
      </div>
    </div>
  );
}

/** Skeleton for the Scanner page */
export function ScannerSkeleton() {
  return (
    <div className="space-y-6 p-6" data-testid="scanner-skeleton">
      <div className="flex gap-4 mb-4">
        <SkeletonBlock className="h-10 w-32" />
        <SkeletonBlock className="h-10 w-24" />
      </div>
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
        <table className="w-full">
          <tbody>
            {Array.from({ length: 8 }).map((_, i) => (
              <SkeletonTableRow key={i} cols={6} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
