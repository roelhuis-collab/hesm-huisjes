/**
 * PriceChart — placeholder until ENTSO-E API token lands.
 *
 * The backend connector exists (PR3); only the credential is missing.
 * Once the token is in Secret Manager and the optimizer writes hourly
 * prices to Firestore, this card renders the actual 24h curve. For now
 * it shows what we're waiting on so Roel knows it's deliberate.
 */

import { Clock4 } from 'lucide-react';

export function PriceChart() {
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
      <header className="mb-4 flex items-center justify-between">
        <h2 className="text-[10px] uppercase tracking-[0.25em] text-slate-500">
          EPEX day-ahead — komende 24 uur
        </h2>
        <span className="text-[10px] uppercase tracking-widest text-slate-600">
          entso-e
        </span>
      </header>

      <div className="flex flex-col items-center justify-center py-10 text-center">
        <Clock4 size={22} className="mb-3 text-slate-600" />
        <p className="text-sm text-slate-400">
          Wacht op API-toegang.
        </p>
        <p className="mt-1 max-w-xs text-xs text-slate-600">
          De ENTSO-E-connector staat klaar; zodra de token van
          transparency@entsoe.eu binnenkomt en in Secret Manager landt,
          rendert hier de uurlijkse prijscurve incl. 21% BTW.
        </p>
      </div>
    </section>
  );
}
