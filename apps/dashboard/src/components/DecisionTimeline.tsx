/**
 * Last-24h decisions timeline.
 *
 * Subscribes to ``decisions`` collection (descending timestamp, limit 30)
 * and renders each as a row with the tag, time, and Dutch reason. Empty
 * state when the optimizer hasn't run yet.
 */

import { collection, limit, onSnapshot, orderBy, query } from 'firebase/firestore';
import { useEffect, useState } from 'react';
import { db } from '../lib/firebase';

interface Decision {
  id: string;
  timestamp: string;
  tag: string;
  reason: string;
  rationale: string;
  estimated_savings_eur?: number | null;
}

const TAG_COLORS: Record<string, string> = {
  BOOST: 'bg-amber-500/20 text-amber-300',
  'PV-DUMP': 'bg-emerald-500/20 text-emerald-300',
  COAST: 'bg-rose-500/20 text-rose-300',
  NORMAL: 'bg-slate-700/40 text-slate-300',
  'NEG-PRICE': 'bg-emerald-500/30 text-emerald-200',
  OVERRIDE: 'bg-fuchsia-500/20 text-fuchsia-300',
};

export function DecisionTimeline() {
  const [rows, setRows] = useState<Decision[]>([]);

  useEffect(() => {
    const q = query(
      collection(db, 'decisions'),
      orderBy('timestamp', 'desc'),
      limit(30),
    );
    return onSnapshot(q, (snap) => {
      setRows(
        snap.docs.map((d) => ({
          id: d.id,
          ...(d.data() as Omit<Decision, 'id'>),
        })),
      );
    });
  }, []);

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
      <header className="mb-4 flex items-center justify-between">
        <h2 className="text-[10px] uppercase tracking-[0.25em] text-slate-500">
          Beslissingen — laatste 24 uur
        </h2>
        <span className="text-[10px] uppercase tracking-widest text-slate-600">
          {rows.length}
        </span>
      </header>

      {rows.length === 0 ? (
        <p className="py-6 text-center text-sm text-slate-500">
          Nog geen beslissingen — de optimizer draait pas zodra connectors live staan.
        </p>
      ) : (
        <ol className="space-y-2 max-h-96 overflow-y-auto pr-1">
          {rows.map((d) => {
            const color = TAG_COLORS[d.tag] ?? TAG_COLORS.NORMAL;
            const ts = new Date(d.timestamp);
            const hh = String(ts.getHours()).padStart(2, '0');
            const mm = String(ts.getMinutes()).padStart(2, '0');
            return (
              <li
                key={d.id}
                className="flex items-start gap-3 rounded-md border border-slate-800/60 bg-slate-900/40 px-3 py-2"
              >
                <span className="font-mono tabular-nums text-xs text-slate-500 pt-1">
                  {hh}:{mm}
                </span>
                <span className={`shrink-0 rounded px-2 py-0.5 text-[10px] uppercase tracking-widest ${color}`}>
                  {d.tag}
                </span>
                <span className="flex-1 text-sm text-slate-300">{d.reason}</span>
                {typeof d.estimated_savings_eur === 'number' && d.estimated_savings_eur > 0 && (
                  <span className="font-mono tabular-nums text-xs text-emerald-400">
                    +€{d.estimated_savings_eur.toFixed(2)}
                  </span>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
