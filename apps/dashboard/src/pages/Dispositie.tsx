/**
 * Dispositie — kwartier-besparingsadvies (Zonneplan dynamisch).
 *
 * Toont voor vandaag:
 *  - som van expected_saving_eur over alle kwartieren
 *  - cum YTD teruglevering + Zonnebonus-koppositie (kWh tot 7.500-cap)
 *  - laatste 24 uur aan allocaties als timeline
 *
 * Subscribt op ``disposition_decisions`` in Firestore. Live updates via
 * onSnapshot zodat het iPad-scherm meegroeit met de cycle.
 */

import { collection, limit, onSnapshot, orderBy, query } from 'firebase/firestore';
import { ChevronLeft } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import {
  type DispositionDecision,
  TARIFF_CONFIG,
  dispositionLabel,
  zonnebonusRemainingKwh,
} from '../lib/dispositie';
import { db } from '../lib/firebase';

const TODAY_ISO_DATE = () => new Date().toISOString().slice(0, 10);

const DISPOSITION_COLORS: Record<string, string> = {
  self_consume: 'bg-emerald-500/20 text-emerald-300',
  store: 'bg-sky-500/20 text-sky-300',
  export: 'bg-slate-700/40 text-slate-300',
  curtail: 'bg-rose-500/20 text-rose-300',
};

function useDispositionDecisions(): DispositionDecision[] {
  const [rows, setRows] = useState<DispositionDecision[]>([]);
  useEffect(() => {
    const q = query(
      collection(db, 'disposition_decisions'),
      orderBy('interval_start', 'desc'),
      limit(96),
    );
    return onSnapshot(q, (snap) => {
      setRows(
        snap.docs.map((d) => ({
          id: d.id,
          ...(d.data() as Omit<DispositionDecision, 'id'>),
        })),
      );
    });
  }, []);
  return rows;
}

export default function Dispositie() {
  const decisions = useDispositionDecisions();

  const today = TODAY_ISO_DATE();
  const todays = useMemo(
    () => decisions.filter((d) => d.interval_start.startsWith(today)),
    [decisions, today],
  );

  const totalSavingToday = todays.reduce((sum, d) => sum + (d.expected_saving_eur ?? 0), 0);
  const latest = decisions[0];
  const latestCumYtd = latest?.cum_ytd_teruglevering_kwh ?? 0;
  const latestRegime = latest?.regime ?? 'saldering';
  const latestSpot = latest?.spot_price_eur_per_kwh ?? 0;
  const zonnebonusLeft = zonnebonusRemainingKwh(latestCumYtd);
  const capReached = zonnebonusLeft <= 0;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-900 px-6 py-4">
        <div className="mx-auto flex max-w-6xl items-center justify-between">
          <Link
            to="/"
            className="flex items-center gap-1 text-xs uppercase tracking-widest text-slate-500 hover:text-amber-400"
          >
            <ChevronLeft size={14} /> simpel
          </Link>
          <h1 className="text-sm font-light tracking-wide">Dispositie-advies</h1>
          <span className="text-[10px] uppercase tracking-widest text-slate-600">
            {latestRegime === 'saldering' ? 'saldering' : 'no saldering'} · {TARIFF_CONFIG.supplier}
          </span>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-6 px-6 py-6">
        <div className="grid gap-6 md:grid-cols-3">
          <Card
            title="Besparing vandaag"
            value={`€${totalSavingToday.toFixed(2)}`}
            footnote={`${todays.length} kwartieren geregistreerd`}
          />
          <Card
            title="Spot nu"
            value={`€${latestSpot.toFixed(3)}/kWh`}
            footnote={`+ inkoopvergoeding €${TARIFF_CONFIG.inkoopvergoedingEurPerKwh.toFixed(3)} + energiebelasting €${TARIFF_CONFIG.energyTaxEurPerKwh.toFixed(3)}`}
          />
          <Card
            title="Zonnebonus-ruimte"
            value={
              capReached
                ? 'cap bereikt'
                : `${Math.round(zonnebonusLeft).toLocaleString('nl-NL')} kWh`
            }
            footnote={
              capReached
                ? `cumulatieve teruglevering ${Math.round(latestCumYtd).toLocaleString('nl-NL')} kWh boven 7.500 kWh`
                : `+${(TARIFF_CONFIG.zonnebonusPercentage * 100).toFixed(0)}% over spot tussen ${TARIFF_CONFIG.zonnebonusStartHour}:00 en ${TARIFF_CONFIG.zonnebonusEndHour}:00`
            }
          />
        </div>

        <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
          <header className="mb-4 flex items-center justify-between">
            <h2 className="text-[10px] uppercase tracking-[0.25em] text-slate-500">
              Allocaties — laatste 24 uur
            </h2>
            <span className="text-[10px] uppercase tracking-widest text-slate-600">
              {decisions.length}
            </span>
          </header>

          {decisions.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">
              Nog geen dispositie-beslissingen. De engine draait pas zodra de
              cycle live PV-data ontvangt.
            </p>
          ) : (
            <ol className="space-y-2 max-h-[28rem] overflow-y-auto pr-1">
              {decisions.map((d) => (
                <DecisionRow key={d.id ?? d.interval_start} decision={d} />
              ))}
            </ol>
          )}
        </section>
      </main>
    </div>
  );
}

function Card({
  title,
  value,
  footnote,
}: {
  title: string;
  value: string;
  footnote?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
      <p className="text-[10px] uppercase tracking-[0.25em] text-slate-500">{title}</p>
      <p className="mt-2 font-mono text-3xl tabular-nums text-amber-300">{value}</p>
      {footnote && (
        <p className="mt-2 text-xs text-slate-500">{footnote}</p>
      )}
    </div>
  );
}

function DecisionRow({ decision }: { decision: DispositionDecision }) {
  const ts = new Date(decision.interval_start);
  const hh = String(ts.getHours()).padStart(2, '0');
  const mm = String(ts.getMinutes()).padStart(2, '0');
  const spot = decision.spot_price_eur_per_kwh ?? 0;

  return (
    <li className="flex items-start gap-3 rounded-md border border-slate-800/60 bg-slate-900/40 px-3 py-2">
      <span className="font-mono tabular-nums text-xs text-slate-500 pt-1">
        {hh}:{mm}
      </span>
      <span className="font-mono tabular-nums text-xs text-slate-400 pt-1 w-16 text-right">
        €{spot.toFixed(3)}
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {decision.allocations.length === 0 ? (
            <span className="text-xs text-slate-500">geen surplus</span>
          ) : (
            decision.allocations.map((a, i) => {
              const color = DISPOSITION_COLORS[a.disposition] ?? DISPOSITION_COLORS.export;
              const label = a.load_id ? `${dispositionLabel(a.disposition)} (${a.load_id})` : dispositionLabel(a.disposition);
              return (
                <span
                  key={`${decision.id ?? decision.interval_start}-${i}`}
                  className={`rounded px-2 py-0.5 text-[10px] uppercase tracking-widest ${color}`}
                >
                  {label} {a.kwh.toFixed(2)} kWh
                </span>
              );
            })
          )}
        </div>
        <p className="mt-1 text-xs text-slate-500 truncate">
          surplus {decision.forecast_surplus_kwh.toFixed(2)} kWh — {decision.rationale}
        </p>
      </div>
      {decision.expected_saving_eur > 0 && (
        <span className="font-mono tabular-nums text-xs text-emerald-400">
          +€{decision.expected_saving_eur.toFixed(2)}
        </span>
      )}
    </li>
  );
}
