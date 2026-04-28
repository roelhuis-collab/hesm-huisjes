/**
 * Settings/Limits — edit Layer-1 hard limits.
 *
 * The server validates on PUT /policy and returns ``{validation_errors:
 * [...]}`` in the 400 detail. We surface those messages inline so the user
 * understands why a change was rejected (e.g. "boiler_legionella_floor_c
 * < 45°C is niet veilig").
 */

import { useEffect, useState } from 'react';
import { useAuth } from '../../contexts/AuthContext';
import {
  ApiError,
  type Policy,
  type SystemLimits,
  type TempBand,
  getPolicy,
  updatePolicy,
} from '../../lib/api';
import { SettingsLayout } from './Layout';

type LimitsState = SystemLimits;

function bandRow(
  zone: 'living_room' | 'bedroom' | 'bathroom',
  label: string,
  band: TempBand,
  onChange: (next: TempBand) => void,
) {
  return (
    <div key={zone} className="grid grid-cols-3 gap-3 items-center">
      <span className="text-sm text-slate-300">{label}</span>
      <NumberInput
        value={band.min_c}
        onChange={(v) => onChange({ ...band, min_c: v })}
        suffix="min °C"
      />
      <NumberInput
        value={band.max_c}
        onChange={(v) => onChange({ ...band, max_c: v })}
        suffix="max °C"
      />
    </div>
  );
}

function NumberInput({
  value,
  onChange,
  suffix,
  step = 0.5,
}: {
  value: number;
  onChange: (v: number) => void;
  suffix?: string;
  step?: number;
}) {
  return (
    <label className="flex items-center gap-2 rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-sm focus-within:border-amber-400/60">
      <input
        type="number"
        step={step}
        value={value}
        onChange={(e) => {
          const next = Number.parseFloat(e.target.value);
          if (!Number.isNaN(next)) onChange(next);
        }}
        className="w-full bg-transparent font-mono tabular-nums text-slate-100 outline-none"
      />
      {suffix && (
        <span className="shrink-0 text-[10px] uppercase tracking-widest text-slate-500">
          {suffix}
        </span>
      )}
    </label>
  );
}

export default function Limits() {
  const { user } = useAuth();
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [limits, setLimits] = useState<LimitsState | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    getPolicy(user)
      .then((p) => {
        setPolicy(p);
        setLimits(p.limits);
      })
      .catch((e) => setErrors([String(e)]))
      .finally(() => setLoading(false));
  }, [user]);

  async function save() {
    if (!user || !limits) return;
    setSaving(true);
    setErrors([]);
    try {
      const result = await updatePolicy(user, { limits });
      setPolicy(result.policy);
      setSavedAt(new Date().toLocaleTimeString('nl-NL'));
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {
        const body = e.body as { detail?: { validation_errors?: string[] } };
        const ve = body?.detail?.validation_errors;
        setErrors(ve && ve.length > 0 ? ve : [String(e.body ?? e.message)]);
      } else {
        setErrors([String(e)]);
      }
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <SettingsLayout title="Limieten">
        <p className="text-xs uppercase tracking-widest text-slate-600">laden…</p>
      </SettingsLayout>
    );
  }

  if (!limits || !policy) {
    return (
      <SettingsLayout title="Limieten">
        <p className="text-rose-300">Kon de policy niet laden.</p>
      </SettingsLayout>
    );
  }

  const dirty = JSON.stringify(limits) !== JSON.stringify(policy.limits);

  return (
    <SettingsLayout title="Limieten (Laag 1)">
      <p className="mb-8 max-w-prose text-sm text-slate-400">
        Deze waarden zijn fysieke en veiligheidsgrenzen. De optimizer en de AI
        overschrijden ze nooit. Aanpassen mag, maar de server valideert: een
        boiler-legionella onder 45&nbsp;°C of vloer-aanvoer boven 55&nbsp;°C wordt
        geweigerd.
      </p>

      <Section title="Verwarmingscircuit — max aanvoertemperatuur">
        <Row
          label="Vloer (parket)"
          control={
            <NumberInput
              value={limits.floor_max_flow_c}
              onChange={(v) => setLimits({ ...limits, floor_max_flow_c: v })}
              suffix="°C"
              step={1}
            />
          }
        />
        <Row
          label="Badkamer"
          control={
            <NumberInput
              value={limits.bathroom_max_flow_c}
              onChange={(v) => setLimits({ ...limits, bathroom_max_flow_c: v })}
              suffix="°C"
              step={1}
            />
          }
        />
        <Row
          label="Radiatoren (Jaga LTV)"
          control={
            <NumberInput
              value={limits.radiator_max_flow_c}
              onChange={(v) => setLimits({ ...limits, radiator_max_flow_c: v })}
              suffix="°C"
              step={1}
            />
          }
        />
      </Section>

      <Section title="DHW (boiler 500 L)">
        <Row
          label="Legionella-bodem"
          control={
            <NumberInput
              value={limits.boiler_legionella_floor_c}
              onChange={(v) => setLimits({ ...limits, boiler_legionella_floor_c: v })}
              suffix="°C"
              step={1}
            />
          }
        />
        <Row
          label="Maximum"
          control={
            <NumberInput
              value={limits.boiler_max_c}
              onChange={(v) => setLimits({ ...limits, boiler_max_c: v })}
              suffix="°C"
              step={1}
            />
          }
        />
      </Section>

      <Section title="Comfortbanden binnen">
        {bandRow('living_room', 'Woonkamer', limits.living_room, (b) =>
          setLimits({ ...limits, living_room: b }),
        )}
        {bandRow('bedroom', 'Slaapkamer', limits.bedroom, (b) =>
          setLimits({ ...limits, bedroom: b }),
        )}
        {bandRow('bathroom', 'Badkamer', limits.bathroom, (b) =>
          setLimits({ ...limits, bathroom: b }),
        )}
      </Section>

      <Section title="Dompelaar — veiligheid">
        <Row
          label="Maximumprijs"
          control={
            <NumberInput
              value={limits.dompelaar_max_price_eur_kwh}
              onChange={(v) => setLimits({ ...limits, dompelaar_max_price_eur_kwh: v })}
              suffix="€/kWh"
              step={0.01}
            />
          }
        />
        <Row
          label="Min PV-overschot"
          control={
            <NumberInput
              value={limits.dompelaar_only_with_pv_above_w}
              onChange={(v) => setLimits({ ...limits, dompelaar_only_with_pv_above_w: v })}
              suffix="W"
              step={100}
            />
          }
        />
      </Section>

      <Section title="Warmtepomp">
        <Row
          label="Min looptijd (anti-pendelen)"
          control={
            <NumberInput
              value={limits.hp_min_run_minutes}
              onChange={(v) => setLimits({ ...limits, hp_min_run_minutes: v })}
              suffix="min"
              step={1}
            />
          }
        />
      </Section>

      {errors.length > 0 && (
        <div className="mb-6 rounded-md border border-rose-900/50 bg-rose-950/30 p-4 text-sm text-rose-300">
          <p className="mb-2 font-medium">Server weigerde de wijziging:</p>
          <ul className="list-inside list-disc space-y-1">
            {errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="sticky bottom-4 mt-12 flex items-center justify-between gap-4 rounded-xl border border-slate-800 bg-slate-950/95 px-4 py-3 backdrop-blur">
        <span className="text-xs text-slate-500">
          {savedAt ? (
            <>opgeslagen om {savedAt}</>
          ) : dirty ? (
            <>onopgeslagen wijzigingen</>
          ) : (
            <>geen wijzigingen</>
          )}
        </span>
        <button
          onClick={save}
          disabled={!dirty || saving}
          className="
            rounded-lg bg-amber-400 px-5 py-2 text-sm font-medium text-slate-950
            transition-all
            disabled:cursor-not-allowed disabled:bg-slate-800 disabled:text-slate-500
          "
        >
          {saving ? 'Opslaan…' : 'Opslaan'}
        </button>
      </div>
    </SettingsLayout>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-8">
      <h2 className="mb-4 text-[10px] uppercase tracking-[0.25em] text-slate-500">
        {title}
      </h2>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

function Row({ label, control }: { label: string; control: React.ReactNode }) {
  return (
    <div className="grid grid-cols-2 items-center gap-3">
      <span className="text-sm text-slate-300">{label}</span>
      {control}
    </div>
  );
}
