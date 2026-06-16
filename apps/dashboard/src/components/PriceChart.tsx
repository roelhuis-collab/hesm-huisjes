/**
 * PriceChart — vandaag's kwartier-spot van EnergyZero.
 *
 * Public endpoint, geen auth. We tonen de KALE spot (excl. btw, excl.
 * opslagen). De all-in importprijs per kwartier zit in TARIFF_CONFIG en
 * wordt door de dispositie-engine berekend; deze grafiek is puur
 * marktvisualisatie.
 */

import { useEffect, useState } from 'react';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

interface QuarterPoint {
  label: string;
  spot: number;
}

const TODAY = new Date();
const D = String(TODAY.getDate()).padStart(2, '0');
const M = String(TODAY.getMonth() + 1).padStart(2, '0');
const Y = TODAY.getFullYear();

interface EzBaseItem {
  start: string;
  price?: { value: string | number };
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function PriceChart() {
  const [points, setPoints] = useState<QuarterPoint[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(
      `https://public.api.energyzero.nl/public/v1/prices?energyType=ENERGY_TYPE_ELECTRICITY&date=${D}-${M}-${Y}&interval=INTERVAL_QUARTER`,
    )
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: { base?: EzBaseItem[] }) => {
        if (cancelled) return;
        const base = Array.isArray(data.base) ? data.base : [];
        const parsed = base
          .map((b) => ({
            label: fmtTime(b.start),
            spot: Number(b.price?.value ?? NaN),
          }))
          .filter((p) => Number.isFinite(p.spot));
        setPoints(parsed);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
      <header className="mb-4 flex items-center justify-between">
        <h2 className="text-[10px] uppercase tracking-[0.25em] text-slate-500">
          EPEX spot — vandaag (kale prijs)
        </h2>
        <span className="text-[10px] uppercase tracking-widest text-slate-600">
          energyzero
        </span>
      </header>

      {error && (
        <p className="text-xs text-rose-300">Kon spot niet ophalen: {error}</p>
      )}

      {!error && points === null && (
        <p className="text-xs text-slate-500">Laden…</p>
      )}

      {!error && points && points.length === 0 && (
        <p className="text-xs text-slate-500">
          Dag-vooruit-prijzen nog niet gepubliceerd (verschijnen rond 14:00).
        </p>
      )}

      {!error && points && points.length > 0 && (
        <div className="h-48">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={points} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
              <XAxis
                dataKey="label"
                stroke="#64748b"
                fontSize={10}
                tickLine={false}
                interval="preserveStartEnd"
              />
              <YAxis
                stroke="#64748b"
                fontSize={10}
                tickLine={false}
                tickFormatter={(v: number) => `€${v.toFixed(2)}`}
              />
              <Tooltip
                contentStyle={{
                  background: '#0f172a',
                  border: '1px solid #1e293b',
                  fontSize: 12,
                }}
                formatter={(value: number) => [`€${value.toFixed(3)}/kWh`, 'spot']}
              />
              <Area
                type="monotone"
                dataKey="spot"
                stroke="#fbbf24"
                fill="#fbbf24"
                fillOpacity={0.15}
                strokeWidth={1.5}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}
