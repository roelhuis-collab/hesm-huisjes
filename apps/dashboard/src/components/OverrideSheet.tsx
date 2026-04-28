/**
 * OverrideSheet — bottom-sheet UI for the five Layer-2 override actions.
 *
 * The user picks one option, the sheet POSTs to /override on the Cloud Run
 * service, and closes. The Cloud Run handler stores the override in
 * Firestore; the next /optimize cycle picks it up. Layer-1 limits are
 * enforced server-side, so we don't validate values here.
 *
 * Auth: we send the Firebase ID token as Authorization: Bearer <token>.
 * PR11b will gate the Cloud Run user endpoints on that token; for now
 * the server accepts any caller (--allow-unauthenticated) but checking
 * the token client-side keeps the dashboard self-consistent.
 */

import { CalendarOff, Droplet, Flame, Power, Users } from 'lucide-react';
import { useState } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { API_BASE_URL } from '../lib/firebase';

interface Props {
  open: boolean;
  onClose: () => void;
}

type OverrideKind =
  | 'holiday'
  | 'guest_mode'
  | 'boost_dhw'
  | 'manual_off'
  | 'boost_heating';

interface Option {
  kind: OverrideKind;
  label: string;
  hint: string;
  icon: typeof Power;
  hours: number;
}

const OPTIONS: Option[] = [
  {
    kind: 'boost_dhw',
    label: 'Boost boiler nu',
    hint: 'Eénmalige opwarming naar 60 °C — 1 uur',
    icon: Droplet,
    hours: 1,
  },
  {
    kind: 'boost_heating',
    label: 'Comfort-boost',
    hint: 'Setpoint +1 °C voor 2 uur',
    icon: Flame,
    hours: 2,
  },
  {
    kind: 'guest_mode',
    label: 'Gasten-modus',
    hint: 'Comfort prio voor 24 uur',
    icon: Users,
    hours: 24,
  },
  {
    kind: 'holiday',
    label: 'Vakantie',
    hint: 'Verlaagde setpoints, DHW alleen op nood — 7 dagen',
    icon: CalendarOff,
    hours: 24 * 7,
  },
  {
    kind: 'manual_off',
    label: 'Alles uit',
    hint: 'Optimizer pauzeren tot je het uitzet',
    icon: Power,
    hours: 0,
  },
];

export function OverrideSheet({ open, onClose }: Props) {
  const { user } = useAuth();
  const [busy, setBusy] = useState<OverrideKind | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  async function send(opt: Option) {
    if (!user) {
      setError('Niet ingelogd.');
      return;
    }
    setBusy(opt.kind);
    setError(null);
    try {
      const token = await user.getIdToken();
      const res = await fetch(`${API_BASE_URL}/override`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          kind: opt.kind,
          duration_hours: opt.hours,
          payload: {},
        }),
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-end justify-center bg-black/50 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl rounded-t-3xl border-t border-slate-800 bg-slate-950 p-6 pb-10 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-light tracking-tight">Tijdelijk overrulen</h2>
          <button
            onClick={onClose}
            className="text-xs uppercase tracking-widest text-slate-500 hover:text-amber-400"
          >
            sluit
          </button>
        </div>

        <ul className="divide-y divide-slate-800">
          {OPTIONS.map((opt) => {
            const Icon = opt.icon;
            const isBusy = busy === opt.kind;
            return (
              <li key={opt.kind}>
                <button
                  disabled={busy !== null}
                  onClick={() => send(opt)}
                  className="
                    flex w-full items-center gap-4 px-2 py-4 text-left
                    transition-colors hover:bg-slate-900
                    disabled:opacity-50
                  "
                >
                  <Icon size={20} className="text-amber-400" />
                  <div className="flex-1">
                    <div className="text-sm">{opt.label}</div>
                    <div className="text-xs text-slate-500">{opt.hint}</div>
                  </div>
                  {isBusy && (
                    <span className="text-[10px] uppercase tracking-widest text-slate-400">
                      versturen…
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>

        {error && (
          <p className="mt-4 rounded-md bg-rose-950/50 px-3 py-2 text-xs text-rose-300">
            Er ging iets mis: {error}
          </p>
        )}
      </div>
    </div>
  );
}
