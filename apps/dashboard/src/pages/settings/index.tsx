/**
 * Settings landing — links to the four sub-pages.
 */

import { Activity, Cable, Sliders, Thermometer } from 'lucide-react';
import { Link } from 'react-router-dom';
import { SettingsLayout } from './Layout';

const ITEMS = [
  {
    to: '/settings/limits',
    icon: Thermometer,
    title: 'Limieten',
    desc: 'Harde grenzen — vloer, badkamer, boiler, comfortbanden. Layer 1.',
  },
  {
    to: '/settings/strategy',
    icon: Sliders,
    title: 'Strategie',
    desc: 'Kosten / comfort / eigenverbruik / groen — preset of custom. Layer 2.',
  },
  {
    to: '/settings/learning',
    icon: Activity,
    title: 'Lerend gedrag',
    desc: 'Layer 3. Standaard uit; je krijgt een melding zodra er genoeg data is.',
  },
  {
    to: '/settings/connectors',
    icon: Cable,
    title: 'Verbindingen',
    desc: 'Welke device-clouds en data-feeds zijn aangesloten?',
  },
];

export default function SettingsIndex() {
  return (
    <SettingsLayout title="Instellingen">
      <ul className="space-y-3">
        {ITEMS.map(({ to, icon: Icon, title, desc }) => (
          <li key={to}>
            <Link
              to={to}
              className="
                flex items-start gap-4 rounded-xl border border-slate-800
                bg-slate-900/50 p-5 transition-all
                hover:border-amber-400/40 hover:bg-slate-900
              "
            >
              <Icon size={22} className="mt-1 shrink-0 text-amber-400" />
              <div>
                <div className="text-base">{title}</div>
                <div className="mt-1 text-sm text-slate-500">{desc}</div>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </SettingsLayout>
  );
}
