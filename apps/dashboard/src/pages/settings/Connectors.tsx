/**
 * Settings/Connectors — render the GET /health wiring map.
 *
 * Each connector listed with a green/red indicator + a one-line
 * explanation of what it does and which PR wires it.
 */

import { CheckCircle2, Circle } from 'lucide-react';
import { useEffect, useState } from 'react';
import { type HealthResponse, getHealth } from '../../lib/api';
import { SettingsLayout } from './Layout';

interface Row {
  key: keyof HealthResponse['wiring'];
  label: string;
  desc: string;
}

const ROWS: Row[] = [
  { key: 'firestore', label: 'Firestore', desc: 'Centrale state-store voor policy, snapshots en beslissingen.' },
  { key: 'homewizard_connector', label: 'HomeWizard P1', desc: 'Slimme meter — netinvoer/teruglevering, fasestromen.' },
  { key: 'entsoe_connector', label: 'ENTSO-E', desc: 'EPEX day-ahead spotprijzen voor NL.' },
  { key: 'openmeteo_connector', label: 'Open-Meteo', desc: 'Weersvoorspelling + grove PV-schatting.' },
  { key: 'weheat_connector', label: 'WeHeat', desc: 'Warmtepomp — temperaturen, COP, setpoint. Wacht op API-toegang.' },
  { key: 'resideo_connector', label: 'Resideo Lyric', desc: 'Thermostaat — kamertemperatuur en setpoint. Wacht op OAuth-app.' },
  { key: 'shelly_connector', label: 'Shelly Cloud', desc: 'Dompelaar-relais (Shelly Pro 2PM). Wacht op auth-key.' },
  { key: 'growatt_connector', label: 'Growatt', desc: 'PV-inverter — productie, fase-output. Wacht op ShinePhone-poll.' },
  { key: 'ai_chat', label: 'AI chat (Claude)', desc: 'Conversational layer — uitleg en advies in het Nederlands.' },
  { key: 'optimizer_v0', label: 'Optimizer v0', desc: 'De rule-based beslisser. Levend zodra alle device-connectors er zijn.' },
];

export default function Connectors() {
  const [data, setData] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getHealth().then(setData).catch((e) => setError(String(e)));
  }, []);

  return (
    <SettingsLayout title="Verbindingen">
      <p className="mb-8 max-w-prose text-sm text-slate-400">
        Live status van elke koppeling. Cloud Run rapporteert dit op{' '}
        <code className="rounded bg-slate-900 px-1 py-0.5 font-mono text-xs">/health</code>.
      </p>

      {error && (
        <p className="mb-6 rounded-md bg-rose-950/40 px-3 py-2 text-sm text-rose-300">
          Kon /health niet ophalen: {error}
        </p>
      )}

      <ul className="divide-y divide-slate-800 rounded-xl border border-slate-800 bg-slate-900/50">
        {ROWS.map(({ key, label, desc }) => {
          const ok = data ? data.wiring[key] : false;
          const Icon = ok ? CheckCircle2 : Circle;
          return (
            <li key={key} className="flex items-start gap-4 px-5 py-4">
              <Icon
                size={18}
                className={`mt-1 shrink-0 ${ok ? 'text-emerald-400' : 'text-slate-700'}`}
              />
              <div className="flex-1">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm">{label}</span>
                  <span
                    className={`text-[10px] uppercase tracking-widest ${
                      ok ? 'text-emerald-400' : 'text-slate-500'
                    }`}
                  >
                    {ok ? 'verbonden' : 'inactief'}
                  </span>
                </div>
                <div className="mt-1 text-xs text-slate-500">{desc}</div>
              </div>
            </li>
          );
        })}
      </ul>

      {data && (
        <p className="mt-6 text-[10px] uppercase tracking-widest text-slate-600">
          {data.service} — {data.status}
        </p>
      )}
    </SettingsLayout>
  );
}
