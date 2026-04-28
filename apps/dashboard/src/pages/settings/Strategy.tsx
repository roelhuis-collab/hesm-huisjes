/**
 * Settings/Strategy — pick a preset OR set custom Layer-2 weights.
 *
 * Presets reflect the four named strategies on the server. Custom shows
 * four sliders that we normalize to sum-to-1 on save (the server also
 * normalizes, so we don't have to be exact).
 */

import { useEffect, useState } from 'react';
import { useAuth } from '../../contexts/AuthContext';
import {
  type Policy,
  type Strategy as StrategyId,
  type StrategyWeights,
  getPolicy,
  updatePolicy,
} from '../../lib/api';
import { SettingsLayout } from './Layout';

const PRESET_LABELS: { id: StrategyId; label: string; tagline: string }[] = [
  { id: 'max_saving', label: 'Maximaal besparen', tagline: 'Comfort maakt plaats voor lage kosten.' },
  { id: 'comfort_first', label: 'Comfort eerst', tagline: 'Klimaat strak, kosten secundair.' },
  { id: 'max_self_consumption', label: 'Max eigenverbruik', tagline: 'PV zoveel mogelijk zelf verstoken.' },
  { id: 'eco_green_hours', label: 'Groene uren', tagline: 'Plan rond windrijke / zonnige momenten.' },
  { id: 'custom', label: 'Custom', tagline: 'Stel de gewichten zelf in.' },
];

const DEFAULT_CUSTOM: StrategyWeights = {
  cost: 0.55,
  comfort: 0.25,
  self_consumption: 0.15,
  renewable_share: 0.05,
};

export default function Strategy() {
  const { user } = useAuth();
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [strategy, setStrategy] = useState<StrategyId>('max_saving');
  const [weights, setWeights] = useState<StrategyWeights>(DEFAULT_CUSTOM);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  useEffect(() => {
    if (!user) return;
    getPolicy(user)
      .then((p) => {
        setPolicy(p);
        setStrategy(p.strategy);
        setWeights(p.custom_weights ?? DEFAULT_CUSTOM);
      })
      .finally(() => setLoading(false));
  }, [user]);

  async function save() {
    if (!user || !policy) return;
    setSaving(true);
    try {
      const update =
        strategy === 'custom'
          ? { strategy, custom_weights: weights }
          : { strategy };
      const result = await updatePolicy(user, update);
      setPolicy(result.policy);
      setSavedAt(new Date().toLocaleTimeString('nl-NL'));
    } finally {
      setSaving(false);
    }
  }

  if (loading || !policy) {
    return (
      <SettingsLayout title="Strategie">
        <p className="text-xs uppercase tracking-widest text-slate-600">laden…</p>
      </SettingsLayout>
    );
  }

  const dirty =
    strategy !== policy.strategy ||
    (strategy === 'custom' &&
      JSON.stringify(weights) !== JSON.stringify(policy.custom_weights));

  return (
    <SettingsLayout title="Strategie (Laag 2)">
      <p className="mb-8 max-w-prose text-sm text-slate-400">
        Bepaalt wat de optimizer voorrang geeft binnen de Laag 1-grenzen.
        Een preset is meestal genoeg; gebruik <em>Custom</em> alleen als je
        een specifieke balans wil.
      </p>

      <div className="mb-10 space-y-2">
        {PRESET_LABELS.map(({ id, label, tagline }) => (
          <label
            key={id}
            className={`
              flex cursor-pointer items-start gap-4 rounded-xl border p-4 transition-all
              ${strategy === id
                ? 'border-amber-400/60 bg-slate-900'
                : 'border-slate-800 bg-slate-900/50 hover:bg-slate-900'}
            `}
          >
            <input
              type="radio"
              name="strategy"
              value={id}
              checked={strategy === id}
              onChange={() => setStrategy(id)}
              className="mt-1 accent-amber-400"
            />
            <div>
              <div className="text-base">{label}</div>
              <div className="text-sm text-slate-500">{tagline}</div>
            </div>
          </label>
        ))}
      </div>

      {strategy === 'custom' && (
        <div className="mb-10 space-y-5 rounded-xl border border-slate-800 bg-slate-900/50 p-6">
          <h3 className="text-[10px] uppercase tracking-[0.25em] text-slate-500">
            Gewichten (worden genormaliseerd op de server)
          </h3>
          <Slider label="Kosten" value={weights.cost} onChange={(v) => setWeights({ ...weights, cost: v })} />
          <Slider label="Comfort" value={weights.comfort} onChange={(v) => setWeights({ ...weights, comfort: v })} />
          <Slider
            label="Eigenverbruik"
            value={weights.self_consumption}
            onChange={(v) => setWeights({ ...weights, self_consumption: v })}
          />
          <Slider
            label="Groen aandeel"
            value={weights.renewable_share}
            onChange={(v) => setWeights({ ...weights, renewable_share: v })}
          />
        </div>
      )}

      <div className="sticky bottom-4 mt-12 flex items-center justify-between gap-4 rounded-xl border border-slate-800 bg-slate-950/95 px-4 py-3 backdrop-blur">
        <span className="text-xs text-slate-500">
          {savedAt ? <>opgeslagen om {savedAt}</> : dirty ? 'onopgeslagen' : 'geen wijzigingen'}
        </span>
        <button
          onClick={save}
          disabled={!dirty || saving}
          className="
            rounded-lg bg-amber-400 px-5 py-2 text-sm font-medium text-slate-950
            disabled:cursor-not-allowed disabled:bg-slate-800 disabled:text-slate-500
          "
        >
          {saving ? 'Opslaan…' : 'Opslaan'}
        </button>
      </div>
    </SettingsLayout>
  );
}

function Slider({ label, value, onChange }: { label: string; value: number; onChange: (v: number) => void }) {
  return (
    <div>
      <div className="mb-1 flex justify-between text-sm">
        <span className="text-slate-300">{label}</span>
        <span className="font-mono tabular-nums text-amber-400">
          {(value * 100).toFixed(0)}%
        </span>
      </div>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={value}
        onChange={(e) => onChange(Number.parseFloat(e.target.value))}
        className="w-full accent-amber-400"
      />
    </div>
  );
}
