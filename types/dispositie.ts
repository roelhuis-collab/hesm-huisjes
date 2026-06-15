/**
 * HESM by Huisjes — types voor de dispositie-engine (spot-gedreven).
 *
 * De engine kiest per kwartier een bestemming voor het PV-overschot
 * (zelf verbruiken / opslaan / terugleveren / curtailen) en optimaliseert
 * tegen de live spot-prijs + de Zonneplan-tariefcomponenten in TARIFF_CONFIG.
 *
 * De Python policy-laag (apps/optimizer/src/optimizer/dispositie.py) spiegelt
 * deze structuren met dataclasses voor runtime-gebruik in Cloud Run.
 *
 * config/tariff.energiedirect.ts bevat de oude staffel en blijft als historische
 * referentie staan — de engine raakt 'm niet meer aan.
 */

export type Disposition = 'self_consume' | 'store' | 'export' | 'curtail';

export type TariffRegime = 'saldering' | 'no_saldering';

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
  spotPriceEurPerKwh: number;
  forecastSurplusKwh: number;
  cumYtdTerugleveringKwh: number;
  allocations: DispositionAllocation[];
  expectedSavingEur: number;
  rationale: string;
}
