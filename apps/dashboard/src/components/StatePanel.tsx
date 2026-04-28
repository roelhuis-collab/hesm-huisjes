/**
 * StatePanel — current snapshot values, mono numerics, single accent color.
 */

import { useLiveState } from '../hooks/useLiveState';

export function StatePanel() {
  const { state, isLive } = useLiveState();

  if (!state) {
    return (
      <Card title="Live waardes">
        <p className="text-sm text-slate-500">
          Nog geen state — de optimizer schrijft pas snapshots zodra connectors
          live staan.
        </p>
      </Card>
    );
  }

  return (
    <Card title="Live waardes" trailing={<LiveDot isLive={isLive} />}>
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
        <Metric label="binnen" value={state.indoor_temp} unit="°C" />
        <Metric label="buiten" value={state.outdoor_temp} unit="°C" />
        <Metric label="boiler" value={state.boiler_temp} unit="°C" />
        <Metric label="buffer" value={state.buffer_temp} unit="°C" />
        <Metric label="pv" value={state.pv_power} unit="W" decimals={0} />
        <Metric label="huis" value={state.house_load} unit="W" decimals={0} />
        <Metric label="warmtepomp" value={state.hp_power} unit="W" decimals={0} />
        <Metric
          label="dompelaar"
          valueText={state.dompelaar_on ? 'AAN' : 'uit'}
          accent={state.dompelaar_on ? 'amber' : 'slate'}
        />
        {typeof state.cop === 'number' && state.cop !== null && (
          <Metric label="COP" value={state.cop} decimals={2} />
        )}
        {typeof state.grid_import === 'number' && state.grid_import !== null && (
          <Metric label="net" value={state.grid_import} unit="W" decimals={0} />
        )}
        {typeof state.price_eur_kwh === 'number' && state.price_eur_kwh !== null && (
          <Metric
            label="spot prijs"
            value={state.price_eur_kwh}
            unit="€/kWh"
            decimals={3}
          />
        )}
      </div>
    </Card>
  );
}

function Metric({
  label,
  value,
  valueText,
  unit,
  decimals = 1,
  accent = 'amber',
}: {
  label: string;
  value?: number;
  valueText?: string;
  unit?: string;
  decimals?: number;
  accent?: 'amber' | 'slate';
}) {
  const text = valueText ?? (typeof value === 'number' ? value.toFixed(decimals) : '—');
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-slate-500">{label}</div>
      <div
        className={`mt-1 font-mono text-2xl tabular-nums ${
          accent === 'amber' ? 'text-amber-400' : 'text-slate-300'
        }`}
      >
        {text}
        {unit && <span className="ml-1 text-xs text-slate-500">{unit}</span>}
      </div>
    </div>
  );
}

function LiveDot({ isLive }: { isLive: boolean }) {
  return (
    <span className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-slate-500">
      <span className={`h-1.5 w-1.5 rounded-full ${isLive ? 'animate-pulse bg-emerald-400' : 'bg-slate-600'}`} />
      {isLive ? 'live' : 'oud'}
    </span>
  );
}

function Card({
  title,
  trailing,
  children,
}: {
  title: string;
  trailing?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
      <header className="mb-4 flex items-center justify-between">
        <h2 className="text-[10px] uppercase tracking-[0.25em] text-slate-500">
          {title}
        </h2>
        {trailing}
      </header>
      {children}
    </section>
  );
}
