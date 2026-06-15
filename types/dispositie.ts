/**
 * HESM by Huisjes — types voor de dispositie-engine.
 *
 * De engine kiest per kwartier een bestemming voor het PV-overschot
 * (zelf verbruiken / opslaan / terugleveren / curtailen) en optimaliseert
 * tegen de terugleverstaffel (2026, saldering) en het per-kWh-regime (2027).
 *
 * Deze types vormen het publieke datamodel. De Python policy-laag
 * (apps/optimizer/src/optimizer/dispositie.py) spiegelt deze structuren
 * met dataclasses voor runtime-gebruik in Cloud Run.
 */

export type Disposition = 'self_consume' | 'store' | 'export' | 'curtail';

export type TariffRegime = 'saldering' | 'no_saldering';

export interface StaffelBand {
  min: number;
  max: number;
  costPerYear: number;
}

export interface BatteryConfig {
  usableKwh: number;
  maxChargeKw: number;
  roundTripEfficiency: number;
}

export interface EvChargerConfig {
  maxKw: number;
  homeDaytimeProbability: number;
}

export interface HeatPumpConfig {
  model: string;
  annualElectricKwh: number;
  dhwShiftableKwhPerDay: number;
  controllable: boolean;
}

export interface SiteConfig {
  pvKwp: number;
  annualPvYieldKwh: number;
  inverter: string;
  meter: string;
  heatPump: HeatPumpConfig;
  bufferOverheatKwhPerDay: number;
  battery: BatteryConfig | null;
  evCharger: EvChargerConfig | null;
  exportLimitKw: number | null;
}

export interface DeferrableLoad {
  id: string;
  label: string;
  availableKwh: number;
  controllable: boolean;
}

export interface DispositionAllocation {
  disposition: Disposition;
  loadId?: string;
  kwh: number;
  marginalGainEurPerKwh: number;
}

export interface DispositionDecision {
  intervalStart: string;
  regime: TariffRegime;
  forecastSurplusKwh: number;
  cumYtdTerugleveringKwh: number;
  allocations: DispositionAllocation[];
  expectedSavingEur: number;
  rationale: string;
}
