/**
 * Settings/Learning — Layer-3 activation status + opt-in.
 *
 * Reads the activation_status doc directly from Firestore so the UI is
 * realtime: when the daily learning_check job flips ``push_sent_at``,
 * the page updates without a refresh. Activate / dismiss go through the
 * Cloud Run /learning/respond endpoint (which mutates Firestore + writes
 * the audit trail).
 */

import { doc, onSnapshot } from 'firebase/firestore';
import { useEffect, useState } from 'react';
import { useAuth } from '../../contexts/AuthContext';
import { db } from '../../lib/firebase';
import { respondToLearning } from '../../lib/api';
import { SettingsLayout } from './Layout';

interface ActivationStatus {
  is_active?: boolean;
  activated_at?: string | null;
  push_sent_at?: string | null;
  push_dismissed_count?: number;
  data_start?: string | null;
}

const MIN_DATA_DAYS = 42;

export default function Learning() {
  const { user } = useAuth();
  const [status, setStatus] = useState<ActivationStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    return onSnapshot(doc(db, 'activation', 'status'), (snap) => {
      setStatus((snap.data() as ActivationStatus | undefined) ?? {});
      setLoading(false);
    });
  }, []);

  if (loading) {
    return (
      <SettingsLayout title="Lerend gedrag">
        <p className="text-xs uppercase tracking-widest text-slate-600">laden…</p>
      </SettingsLayout>
    );
  }

  const isActive = !!status?.is_active;
  const dataStart = status?.data_start ? new Date(status.data_start) : null;
  const daysCollected = dataStart
    ? Math.floor((Date.now() - dataStart.getTime()) / (24 * 60 * 60 * 1000))
    : 0;
  const ready = !isActive && daysCollected >= MIN_DATA_DAYS;

  async function activate(accepted: boolean) {
    if (!user) return;
    setBusy(true);
    setError(null);
    try {
      await respondToLearning(user, accepted);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SettingsLayout title="Lerend gedrag (Laag 3)">
      <p className="mb-8 max-w-prose text-sm text-slate-400">
        De optimizer leert na 42 dagen jouw patronen — wakker- en thuiskomsttijden,
        thermische massa van het huis, hoe vaak Solcast te optimistisch is. Dit
        staat <strong>standaard uit</strong>; je activeert het pas zelf, na een
        push van het systeem.
      </p>

      {/* Status block */}
      <div className="mb-10 rounded-xl border border-slate-800 bg-slate-900/50 p-6">
        <div className="grid grid-cols-2 gap-4 text-sm">
          <Stat label="Status" value={isActive ? 'Actief' : ready ? 'Klaar — vraagt om activatie' : 'Verzamelt data'} accent={isActive ? 'amber' : ready ? 'amber' : 'slate'} />
          <Stat
            label="Dagen verzameld"
            value={`${daysCollected} / ${MIN_DATA_DAYS}`}
          />
          {dataStart && (
            <Stat label="Start" value={dataStart.toLocaleDateString('nl-NL')} />
          )}
          {isActive && status?.activated_at && (
            <Stat
              label="Geactiveerd op"
              value={new Date(status.activated_at).toLocaleDateString('nl-NL')}
            />
          )}
          {!isActive && (status?.push_dismissed_count ?? 0) > 0 && (
            <Stat
              label="Eerder genegeerd"
              value={`${status?.push_dismissed_count ?? 0}×`}
            />
          )}
        </div>

        {/* Progress bar */}
        {!isActive && (
          <div className="mt-6">
            <div className="h-1 overflow-hidden rounded-full bg-slate-800">
              <div
                className="h-full bg-amber-400"
                style={{ width: `${Math.min(100, (daysCollected / MIN_DATA_DAYS) * 100)}%` }}
              />
            </div>
            <p className="mt-2 text-[10px] uppercase tracking-widest text-slate-600">
              {ready ? 'data-drempel bereikt' : `nog ${MIN_DATA_DAYS - daysCollected} dagen`}
            </p>
          </div>
        )}
      </div>

      {/* Action area */}
      {ready && (
        <div className="mb-10 rounded-xl border border-amber-400/40 bg-amber-950/20 p-6">
          <h3 className="mb-2 text-base">Klaar om patronen te leren?</h3>
          <p className="mb-5 text-sm text-slate-300">
            Het systeem heeft genoeg data verzameld om je dagprofiel,
            douche-routine en de thermische massa van het huis te modelleren.
            Activeren is een zachte input — Layer 1-grenzen blijven onaantastbaar
            en je kan dit altijd weer uitzetten.
          </p>
          <div className="flex gap-3">
            <button
              onClick={() => activate(true)}
              disabled={busy}
              className="rounded-lg bg-amber-400 px-5 py-2 text-sm font-medium text-slate-950 disabled:opacity-50"
            >
              {busy ? 'Bezig…' : 'Activeren'}
            </button>
            <button
              onClick={() => activate(false)}
              disabled={busy}
              className="rounded-lg border border-slate-800 px-5 py-2 text-sm text-slate-300 hover:bg-slate-900 disabled:opacity-50"
            >
              Later
            </button>
          </div>
        </div>
      )}

      {isActive && (
        <div className="mb-10 rounded-xl border border-slate-800 bg-slate-900/50 p-6">
          <h3 className="mb-3 text-sm">Geleerd profiel</h3>
          <p className="text-sm text-slate-400">
            Het profiel wordt nachts ververst. De volledige weergave (ritme,
            thermische signatuur, forecast-bias) komt in PR11c.
          </p>
        </div>
      )}

      {error && (
        <p className="rounded-md bg-rose-950/40 px-3 py-2 text-sm text-rose-300">
          {error}
        </p>
      )}
    </SettingsLayout>
  );
}

function Stat({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: 'amber' | 'slate';
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-slate-500">{label}</div>
      <div
        className={`mt-1 font-mono text-base tabular-nums ${
          accent === 'amber' ? 'text-amber-400' : 'text-slate-100'
        }`}
      >
        {value}
      </div>
    </div>
  );
}
