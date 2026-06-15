/**
 * Dashboard-side mirror van /types/dispositie.ts + /config/tariff.energiedirect.ts.
 *
 * Dit zijn copy/paste-constanten om Vite-build zonder cross-package imports te
 * houden. Bron van waarheid blijft /config/*.ts en /types/dispositie.ts —
 * synchroniseer bij contract- of leverancierwissel.
 */

export type Disposition = 'self_consume' | 'store' | 'export' | 'curtail';
export type TariffRegime = 'saldering' | 'no_saldering';

export interface DispositionAllocation {
  disposition: Disposition;
  load_id?: string | null;
  kwh: number;
  marginal_gain_eur_per_kwh: number;
}

export interface DispositionDecision {
  id?: string;
  interval_start: string;
  regime: TariffRegime;
  forecast_surplus_kwh: number;
  cum_ytd_teruglevering_kwh: number;
  allocations: DispositionAllocation[];
  expected_saving_eur: number;
  rationale: string;
}

interface StaffelBand {
  min: number;
  max: number;
  costPerYear: number;
}

/** Energiedirect-staffel per 25 maart 2026, incl. btw. Spiegelt /config/tariff.energiedirect.ts. */
export const ENERGIEDIRECT_STAFFEL: StaffelBand[] = [
  { min: 0, max: 250, costPerYear: 0.0 },
  { min: 251, max: 500, costPerYear: 48.84 },
  { min: 501, max: 750, costPerYear: 81.36 },
  { min: 751, max: 1000, costPerYear: 113.64 },
  { min: 1001, max: 1250, costPerYear: 146.28 },
  { min: 1251, max: 1500, costPerYear: 178.8 },
  { min: 1501, max: 1750, costPerYear: 211.32 },
  { min: 1751, max: 2000, costPerYear: 243.84 },
  { min: 2001, max: 2250, costPerYear: 276.36 },
  { min: 2251, max: 2500, costPerYear: 308.76 },
  { min: 2501, max: 2750, costPerYear: 341.28 },
  { min: 2751, max: 3000, costPerYear: 373.8 },
  { min: 3001, max: 3250, costPerYear: 406.32 },
  { min: 3251, max: 3500, costPerYear: 438.84 },
  { min: 3501, max: 3750, costPerYear: 471.36 },
  { min: 3751, max: 4000, costPerYear: 503.76 },
  { min: 4001, max: 4250, costPerYear: 536.28 },
  { min: 4251, max: 4500, costPerYear: 568.8 },
  { min: 4501, max: 4750, costPerYear: 601.32 },
  { min: 4751, max: 5000, costPerYear: 633.84 },
  { min: 5001, max: 5250, costPerYear: 666.24 },
  { min: 5251, max: 5500, costPerYear: 698.76 },
  { min: 5501, max: 5750, costPerYear: 731.28 },
  { min: 5751, max: 6000, costPerYear: 763.8 },
  { min: 6001, max: 6250, costPerYear: 796.32 },
  { min: 6251, max: 6500, costPerYear: 828.84 },
  { min: 6501, max: 6750, costPerYear: 861.24 },
  { min: 6751, max: 7000, costPerYear: 893.76 },
  { min: 7001, max: 7250, costPerYear: 926.28 },
  { min: 7251, max: 7500, costPerYear: 958.8 },
  { min: 7501, max: 7750, costPerYear: 991.32 },
  { min: 7751, max: 8000, costPerYear: 1023.84 },
  { min: 8001, max: 8250, costPerYear: 1056.24 },
  { min: 8251, max: 8500, costPerYear: 1088.76 },
  { min: 8501, max: 8750, costPerYear: 1121.28 },
  { min: 8751, max: 9000, costPerYear: 1153.8 },
  { min: 9001, max: 9250, costPerYear: 1186.32 },
  { min: 9251, max: 9500, costPerYear: 1218.84 },
  { min: 9501, max: 9750, costPerYear: 1251.24 },
  { min: 9751, max: 10000, costPerYear: 1283.76 },
  { min: 10001, max: 9_999_999, costPerYear: 1332.48 },
];

export interface StaffelPosition {
  currentBand: StaffelBand;
  nextBorderKwh: number | null;
  kwhUntilNextBorder: number | null;
  marginalEurPerKwh: number;
}

/** Bepaal staffelpositie voor een cumulatieve teruglevering. */
export function staffelPositionFor(cumKwh: number): StaffelPosition {
  const band =
    ENERGIEDIRECT_STAFFEL.find((b) => cumKwh >= b.min && cumKwh <= b.max) ??
    ENERGIEDIRECT_STAFFEL[ENERGIEDIRECT_STAFFEL.length - 1];
  const next = ENERGIEDIRECT_STAFFEL.find((b) => b.min > band.max) ?? null;

  const width = band.max - band.min + 1;
  const step = next ? next.costPerYear - band.costPerYear : 0;
  const marginal = step > 0 && Number.isFinite(width) ? step / width : 0;

  return {
    currentBand: band,
    nextBorderKwh: next ? next.min : null,
    kwhUntilNextBorder: next ? Math.max(0, next.min - cumKwh) : null,
    marginalEurPerKwh: marginal,
  };
}

const DISPOSITION_LABELS: Record<Disposition, string> = {
  self_consume: 'zelf verbruiken',
  store: 'opslaan',
  export: 'terugleveren',
  curtail: 'afknijpen',
};

export function dispositionLabel(d: Disposition): string {
  return DISPOSITION_LABELS[d];
}
