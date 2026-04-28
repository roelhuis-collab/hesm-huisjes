/**
 * Shared chrome for every Settings sub-page — back link, title, sub-nav.
 */

import { ChevronLeft } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link, NavLink, useLocation } from 'react-router-dom';

const TABS: { to: string; label: string }[] = [
  { to: '/settings/limits', label: 'Limieten' },
  { to: '/settings/strategy', label: 'Strategie' },
  { to: '/settings/learning', label: 'Lerend' },
  { to: '/settings/connectors', label: 'Verbindingen' },
];

export function SettingsLayout({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  const { pathname } = useLocation();
  const showTabs = TABS.some((t) => pathname.startsWith(t.to));

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-900 px-6 py-4">
        <div className="mx-auto flex max-w-3xl items-center justify-between">
          <Link
            to="/"
            className="flex items-center gap-1 text-xs uppercase tracking-widest text-slate-500 hover:text-amber-400"
          >
            <ChevronLeft size={14} /> terug
          </Link>
          <h1 className="text-sm font-light tracking-wide">{title}</h1>
          <div className="w-12" />
        </div>
        {showTabs && (
          <nav className="mx-auto mt-4 flex max-w-3xl gap-6 overflow-x-auto">
            {TABS.map((tab) => (
              <NavLink
                key={tab.to}
                to={tab.to}
                className={({ isActive }) =>
                  `whitespace-nowrap pb-2 text-xs uppercase tracking-widest ${
                    isActive
                      ? 'border-b border-amber-400 text-amber-400'
                      : 'text-slate-500 hover:text-slate-200'
                  }`
                }
              >
                {tab.label}
              </NavLink>
            ))}
          </nav>
        )}
      </header>
      <main className="mx-auto max-w-3xl px-6 py-8">{children}</main>
    </div>
  );
}
