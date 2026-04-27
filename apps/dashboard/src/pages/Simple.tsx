import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { ChevronRight, Sparkles, Settings as SettingsIcon } from 'lucide-react';
import { useLiveState } from '../hooks/useLiveState';
import { OverrideSheet } from '../components/OverrideSheet';

/**
 * Default landing page — designed for iPad in portrait or landscape.
 * Three things, big and obvious:
 *   1. What is the system doing right now (one sentence)
 *   2. How much it has saved today / this month
 *   3. One big override button
 *
 * The advanced dashboard sits behind /advanced for power users.
 */
export default function Simple() {
  const { state, today, currentDecision, isLive } = useLiveState();
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [now, setNow] = useState(new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col">
      {/* Top bar — minimal */}
      <header className="flex items-center justify-between px-8 py-6">
        <div className="flex items-center gap-3">
          <div className={`w-1.5 h-1.5 rounded-full ${isLive ? 'bg-emerald-400 animate-pulse' : 'bg-slate-600'}`} />
          <span className="text-[10px] uppercase tracking-[0.25em] text-slate-500 font-mono">
            {isLive ? 'live' : 'offline'}
          </span>
        </div>
        <div className="flex items-center gap-6">
          <Link
            to="/advanced"
            className="text-xs uppercase tracking-widest text-slate-500 hover:text-amber-400 transition-colors"
          >
            details
          </Link>
          <Link to="/settings" className="text-slate-500 hover:text-slate-200 transition-colors">
            <SettingsIcon size={18} />
          </Link>
        </div>
      </header>

      {/* Hero — what's happening NOW */}
      <main className="flex-1 flex flex-col items-center justify-center px-8 py-12">
        <div className="max-w-2xl w-full text-center">

          {/* The one sentence — biggest text on the screen */}
          <p className="text-3xl md:text-5xl font-extralight leading-tight tracking-tight mb-12">
            <CurrentActionSentence
              decision={currentDecision}
              indoorTemp={state?.indoor_temp}
            />
          </p>

          {/* Savings — today + month */}
          <div className="grid grid-cols-2 gap-6 mb-16 max-w-xl mx-auto">
            <div className="border-r border-slate-800 pr-6 text-right">
              <div className="text-[10px] uppercase tracking-[0.25em] text-slate-500 mb-2">vandaag bespaard</div>
              <div className="font-mono text-4xl font-light text-emerald-400 tabular-nums">
                €{(today?.saved ?? 0).toFixed(2)}
              </div>
            </div>
            <div className="pl-6 text-left">
              <div className="text-[10px] uppercase tracking-[0.25em] text-slate-500 mb-2">deze maand</div>
              <div className="font-mono text-4xl font-light text-emerald-400 tabular-nums">
                €{(today?.month_saved ?? 0).toFixed(0)}
              </div>
            </div>
          </div>

          {/* The one big button */}
          <button
            onClick={() => setOverrideOpen(true)}
            className="
              w-full max-w-md mx-auto
              flex items-center justify-between
              px-8 py-6
              bg-slate-900 hover:bg-slate-800
              border border-slate-800 hover:border-amber-400/40
              rounded-2xl
              transition-all
              active:scale-[0.98]
            "
          >
            <span className="text-base font-light">Tijdelijk overrulen</span>
            <ChevronRight className="text-slate-500" size={20} />
          </button>

          {/* Subtle why-link */}
          {currentDecision?.reason && (
            <Link
              to="/advanced"
              className="
                inline-flex items-center gap-2 mt-8
                text-xs text-slate-500 hover:text-amber-400
                transition-colors
              "
            >
              <Sparkles size={12} />
              <span className="italic">{currentDecision.reason}</span>
            </Link>
          )}
        </div>
      </main>

      {/* Footer — minimal, just timestamp */}
      <footer className="px-8 py-6 text-center">
        <span className="text-[10px] uppercase tracking-widest text-slate-700 font-mono">
          {now.toLocaleTimeString('nl-NL', { hour: '2-digit', minute: '2-digit' })}
          {' · sittard'}
        </span>
      </footer>

      <OverrideSheet open={overrideOpen} onClose={() => setOverrideOpen(false)} />
    </div>
  );
}

/**
 * Renders a natural Dutch sentence describing what the system is doing.
 * Falls back to a calm default if data is loading.
 */
function CurrentActionSentence({ decision, indoorTemp }: {
  decision?: { tag: string; action: string; reason: string };
  indoorTemp?: number;
}) {
  if (!decision) {
    return <span className="text-slate-400">Een moment, ik kijk wat er gebeurt…</span>;
  }

  const tempPart = typeof indoorTemp === 'number'
    ? <>woonkamer <span className="text-amber-400 font-mono tabular-nums">{indoorTemp.toFixed(1)}°C</span></>
    : null;

  const actionMap: Record<string, JSX.Element> = {
    'BOOST': <>De boiler laadt op met <span className="text-amber-400">goedkope zonnestroom</span>.</>,
    'PV-DUMP': <>Zonneoverschot gaat naar de <span className="text-amber-400">boiler en dompelaar</span>.</>,
    'COAST': <>Het systeem coast door dit <span className="text-rose-400">dure piekuur</span>.</>,
    'NORMAL': <>Alles draait op het <span className="text-slate-300">normale dagprofiel</span>.</>,
    'NEG-PRICE': <>Negatieve prijs — dompelaar verbrandt geld <span className="text-emerald-400">voor je</span>.</>,
  };

  return (
    <>
      {actionMap[decision.tag] ?? <>{decision.action}.</>}
      {tempPart && <> &nbsp;·&nbsp; {tempPart}</>}
    </>
  );
}
