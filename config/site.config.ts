/**
 * HESM by Huisjes — site configuratie (Kempenstraat 3, 6137 KL Sittard)
 *
 * Echte meetwaarden, niet langer geschat. Bronnen staan per regel.
 * Voedt de dispositie-engine (kwartier-besparingsmodule) en de SurplusForecastProvider.
 *
 * Provenance:
 *  - PV-installatie & opbrengst: omvormer-app (26 × 405 Wp; jaarbars 2023–2026)
 *  - Verbruik & teruglevering:   Greenchoice jaarnota (2024) + eindnota (jul 2025)
 *  - Warmtepomp:                 WeHeat Blackbird P80 datasheet (R290, SCOP 4.7 @ A7/W35)
 *  - Full-electric steady state: na gasafsluiting 15-06-2026, WeHeat 8 kW + 3 kW doorstromer
 *
 * Status regime: SALDERING actief t/m 31-12-2026. Schakelt naar 'no_saldering' per 01-01-2027.
 * Status contract: Energiedirect loopt af 07-07-2026 — leverancier nog te kiezen.
 *   De staffel/tarieven hieronder zijn PLACEHOLDER (Energiedirect, slechtste geval).
 *   Vervang door de gekozen leverancier zodra het contract rond is.
 */

import type { SiteConfig } from '../types/dispositie';
import { ENERGIEDIRECT_STAFFEL, TARIFF } from './tariff.energiedirect';

export const SITE_CONFIG: SiteConfig = {
  // --- PV-installatie (gemeten) ---
  pvKwp: 10.53,                 // 26 panelen × 405 Wp
  annualPvYieldKwh: 10500,      // gemiddelde 2023–2025 (10653 / ~9500 / ~11000); 2026 YTD ~4960 t/m half juni

  inverter: 'growatt',          // verifieer fabrikant/model in repo-adapter
  meter: 'ziv-esmr5',

  // --- Warmtepomp (full-electric vanaf 15-06-2026) ---
  heatPump: {
    model: 'weheat-blackbird-p80',
    // Datasheet SCOP 4.7 geldt bij A7/W35 (mild weer + lage aanvoer). Effectieve seizoens-SCOP
    // ligt bij radiatoren/koude winterdagen realistisch ~4,0. ~15.800 kWh warmtevraag / 4,0 ≈ 3.800 kWh.
    // LET OP: de 3 kW doorstromer is resistief (COP 1) — tapwaterpiek daarlangs profiteert NIET van de SCOP.
    annualElectricKwh: 3800,    // was 4700; aangepast n.a.v. SCOP 4.7 datasheet
    dhwShiftableKwhPerDay: 4.0, // SCHATTING — verschuifbaar tapwater naar zonuren
    controllable: false,        // ZET OP true zodra de WeHeat write-adapter bevestigd werkt
  },

  bufferOverheatKwhPerDay: 3.0, // SCHATTING — extra bufferlading op zonnige dagen; 0 als geen buffervat

  // --- Nog niet aanwezig ---
  battery: null,                // geen thuisaccu (2027-businesscase, hoger op de radar dan EV)
  evCharger: null,              // geen laadpunt

  exportLimitKw: null,          // geen export-limiet op de omvormer ingesteld
};

/**
 * Vaste basislijn van de site (full-electric steady state) voor de forecast-provider en tests.
 * Alle waarden kWh/jaar tenzij anders vermeld.
 */
export const SITE_BASELINE = {
  baseHouseholdKwh: 3800,       // jouw vaste basisverbruik (door jou bevestigd)
  heatPumpKwh: 3800,            // zie heatPump.annualElectricKwh (SCOP 4.7 datasheet, effectief ~4,0)
  totalConsumptionKwh: 7600,    // base + warmtepomp

  pvYieldKwh: 10500,            // = SITE_CONFIG.annualPvYieldKwh
  selfConsumptionKwh: 3500,     // ~33% van PV; warmtepomp draait vooral 's winters, weinig PV-overlap
  grossTerugleveringKwh: 7000,  // STAFFEL-BASIS — pijl op sturen met load shifting
  gridImportKwh: 4100,          // afname van net (= verbruik − zelf-verbruik)
  netPositionKwh: -2900,        // NETTO-EXPORTEUR (import − export)

  /**
   * Maandelijkse PV-verdeling (genormaliseerd, som = 1.0) uit de omvormer-app (2023–2025 gemiddeld).
   * Gebruikt door GrowattForecastProvider om de jaaropbrengst over kwartieren te schalen.
   * jan..dec — let op de sterke zomerpiek (mei/jun) vs. winterdal (nov/dec/jan).
   */
  pvMonthlyShare: [0.018, 0.043, 0.097, 0.124, 0.150, 0.158, 0.137, 0.130, 0.090, 0.044, 0.028, 0.021],
} as const;

/**
 * Tariefregime. Datum-gestuurde switch: saldering → no_saldering op 01-01-2027.
 * PLACEHOLDER-tarieven (Energiedirect). Vervang bij contractkeuze.
 */
export const TARIFF_CONFIG = {
  regime: 'saldering' as const,        // wordt 'no_saldering' per 01-01-2027 (zie engine date-hook)
  supplier: 'energiedirect-PLACEHOLDER',
  staffel: ENERGIEDIRECT_STAFFEL,      // VERVANG door gekozen leverancier (overweeg dynamisch = geen staffel)
  importPriceEurPerKwh: TARIFF.importPriceEurPerKwh,        // 0.23209 all-in
  feedInTariffSalderingEurPerKwh: TARIFF.feedInTariffSalderingEurPerKwh,
  feedInCost2027EurPerKwh: 0.078,      // Energiedirect per-kWh terugleverkosten vanaf 2027
  feedInTariff2027EurPerKwh: 0.06,     // wettelijke bodem ≈ 50% kaal leveringstarief; verfijn bij publicatie
} as const;
