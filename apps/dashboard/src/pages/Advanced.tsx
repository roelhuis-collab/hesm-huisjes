/**
 * Advanced — placeholder until PR11c.
 *
 * The full advanced view (price chart, weather panel, decisions
 * timeline, AI chat panel) lives in PR11c. For now this page just
 * acknowledges the link and points back.
 */

import { ChevronLeft, Sparkles } from 'lucide-react';
import { Link } from 'react-router-dom';

export default function Advanced() {
  return (
    <div className="flex min-h-screen flex-col bg-slate-950 text-slate-100">
      <header className="border-b border-slate-900 px-6 py-4">
        <div className="mx-auto flex max-w-3xl items-center justify-between">
          <Link
            to="/"
            className="flex items-center gap-1 text-xs uppercase tracking-widest text-slate-500 hover:text-amber-400"
          >
            <ChevronLeft size={14} /> terug
          </Link>
          <h1 className="text-sm font-light tracking-wide">Advanced</h1>
          <div className="w-12" />
        </div>
      </header>

      <main className="mx-auto flex max-w-3xl flex-1 flex-col items-center justify-center px-6 py-16 text-center">
        <Sparkles size={28} className="mb-6 text-amber-400" />
        <h2 className="mb-3 text-2xl font-extralight tracking-tight">
          Komt eraan in PR11c
        </h2>
        <p className="max-w-md text-sm text-slate-400">
          De gedetailleerde dashboard-weergave (prijscurve, weersvoorspelling,
          beslissingen-timeline en de AI-chat) komt in de volgende PR. Voor
          nu kan je de instellingen aanpassen en handmatig overrulen vanuit
          de Simple-pagina.
        </p>
        <Link
          to="/settings"
          className="mt-8 rounded-lg border border-slate-800 px-5 py-2 text-sm hover:border-amber-400/40 hover:bg-slate-900"
        >
          Naar instellingen
        </Link>
      </main>
    </div>
  );
}
