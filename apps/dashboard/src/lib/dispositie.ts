/**
 * Dashboard-side mirror van /types/dispositie.ts + /config/site.config.ts → TARIFF_CONFIG.
 *
 * Spot-gedreven (Zonneplan dynamisch). Bron van waarheid blijft /config en de
 * Python policy-laag — synchroniseer bij contract- of leverancierwissel.
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
  spot_price_eur_per_kwh?: number;
  forecast_surplus_kwh: number;
  cum_ytd_teruglevering_kwh: number;
  allocations: DispositionAllocation[];
  expected_saving_eur: number;
  rationale: string;
}

/** Tarief-componenten (Zonneplan dynamisch, incl. btw). Spiegel van TARIFF_CONFIG. */
export const TARIFF_CONFIG = {
  contractType: 'dynamic',
  supplier: 'zonneplan',
  inkoopvergoedingEurPerKwh: 0.025,
  energyTaxEurPerKwh: 0.1316,
  terugleveropslagEurPerKwh: 0,
  zonnebonusCapKwh: 7500,
  zonnebonusPercentage: 0.1,
  zonnebonusStartHour: 10,
  zonnebonusEndHour: 15,
} as const;

const DISPOSITION_LABELS: Record<Disposition, string> = {
  self_consume: 'zelf verbruiken',
  store: 'opslaan',
  export: 'terugleveren',
  curtail: 'afknijpen',
};

export function dispositionLabel(d: Disposition): string {
  return DISPOSITION_LABELS[d];
}

/** Hoeveel kWh nog onder de Zonnebonus-cap voor dit jaar. */
export function zonnebonusRemainingKwh(cumYtdKwh: number): number {
  return Math.max(0, TARIFF_CONFIG.zonnebonusCapKwh - cumYtdKwh);
}
