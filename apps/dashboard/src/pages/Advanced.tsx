/**
 * Advanced — the dig-in dashboard. Layered grid:
 *
 *   row 1: live state values + AI chat
 *   row 2: weather chart + price chart (placeholder)
 *   row 3: full-width decisions timeline
 *
 * iPad fits two columns from md breakpoint up; phone falls back to one.
 */

import { ChevronLeft, Settings as SettingsIcon } from 'lucide-react';
import { Link } from 'react-router-dom';
import { ChatPanel } from '../components/ChatPanel';
import { DecisionTimeline } from '../components/DecisionTimeline';
import { PriceChart } from '../components/PriceChart';
import { StatePanel } from '../components/StatePanel';
import { WeatherChart } from '../components/WeatherChart';

export default function Advanced() {
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
          <h1 className="text-sm font-light tracking-wide">Advanced</h1>
          <Link
            to="/settings"
            className="text-slate-500 hover:text-slate-200"
            aria-label="Instellingen"
          >
            <SettingsIcon size={18} />
          </Link>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-6 px-6 py-6">
        <div className="grid gap-6 md:grid-cols-2">
          <StatePanel />
          <ChatPanel />
        </div>
        <div className="grid gap-6 md:grid-cols-2">
          <WeatherChart />
          <PriceChart />
        </div>
        <DecisionTimeline />
      </main>
    </div>
  );
}
