/**
 * useLiveState — Firestore realtime subscription powering the Simple page.
 *
 * Subscribes to:
 *   * the most recent ``state_snapshots`` document (by descending timestamp)
 *   * the most recent ``decisions`` document
 *
 * Returns the shape ``Simple.tsx`` already expects:
 *
 *   { state, today, currentDecision, isLive }
 *
 * - ``state``           : latest SystemState — null while loading or empty
 * - ``today``           : { saved, month_saved } — savings panel; both 0
 *                         until the optimizer wires real cost tracking
 * - ``currentDecision`` : { tag, action, reason } — the most recent decision
 * - ``isLive``          : true when both subscriptions have delivered at least
 *                         one snapshot AND the latest update is < 30 min old
 */

import {
  type DocumentData,
  collection,
  limit,
  onSnapshot,
  orderBy,
  query,
} from 'firebase/firestore';
import { useEffect, useState } from 'react';
import { db } from '../lib/firebase';

export interface SystemState {
  timestamp: string;
  pv_power: number;
  house_load: number;
  hp_power: number;
  dompelaar_on: boolean;
  boiler_temp: number;
  buffer_temp: number;
  indoor_temp: number;
  outdoor_temp: number;
  cop?: number | null;
  grid_import?: number | null;
  price_eur_kwh?: number | null;
}

export interface CurrentDecision {
  tag: string;
  action: string;
  reason: string;
}

export interface TodayTotals {
  saved: number;
  month_saved: number;
}

export interface LiveState {
  state?: SystemState;
  today?: TodayTotals;
  currentDecision?: CurrentDecision;
  isLive: boolean;
}

const FRESHNESS_WINDOW_MS = 30 * 60 * 1000;

export function useLiveState(): LiveState {
  const [state, setState] = useState<SystemState | undefined>();
  const [decision, setDecision] = useState<CurrentDecision | undefined>();
  const [latestUpdate, setLatestUpdate] = useState<number>(0);

  useEffect(() => {
    const q = query(
      collection(db, 'state_snapshots'),
      orderBy('timestamp', 'desc'),
      limit(1),
    );
    return onSnapshot(q, (snap) => {
      const doc = snap.docs[0]?.data() as DocumentData | undefined;
      if (doc) {
        setState(doc as SystemState);
        setLatestUpdate(Date.now());
      }
    });
  }, []);

  useEffect(() => {
    const q = query(
      collection(db, 'decisions'),
      orderBy('timestamp', 'desc'),
      limit(1),
    );
    return onSnapshot(q, (snap) => {
      const doc = snap.docs[0]?.data() as DocumentData | undefined;
      if (doc) {
        setDecision({
          tag: String(doc.tag),
          action: String(doc.action ?? ''),
          reason: String(doc.reason ?? ''),
        });
      }
    });
  }, []);

  // ``today`` is a stub until the optimizer persists per-day savings;
  // PR after PR9 (when real cost numbers exist) will replace this.
  const today: TodayTotals = { saved: 0, month_saved: 0 };

  const isLive = latestUpdate > 0 && Date.now() - latestUpdate < FRESHNESS_WINDOW_MS;

  return { state, today, currentDecision: decision, isLive };
}
