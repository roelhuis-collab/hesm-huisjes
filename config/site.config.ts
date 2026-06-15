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
 * Status contract: Zonneplan dynamisch (vanaf 08-07-2026). Geen staffel; engine stuurt op spot.
 */

import type { SiteConfig } from '../types/dispositie';

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
  grossTerugleveringKwh: 7000,  // bruto teruglevering — relevant voor de 7.500 kWh Zonnebonus-cap
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
 * Tariefconfiguratie — Zonneplan dynamisch (contract per 08-07-2026).
 *
 * Geen staffel meer, geen vaste terugleverkosten. De engine rekent per kwartier met:
 *
 *   importPrice(t) = spot(t) + inkoopvergoedingEurPerKwh + energyTaxEurPerKwh
 *   exportValue(t) = spot(t) + terugleveropslagEurPerKwh
 *                    + (overdag & (spot+opslag)>0 & ytdExport<cap ? zonnebonus.percentage × spot : 0)
 *                    + (saldering.active ? energyTaxEurPerKwh : 0)
 *
 * Saldering t/m 31-12-2026 → energyTax wordt teruggegeven op je export (binnen saldeerbereik).
 * Per 01-01-2027 vervalt de saldering-term: regime_for() in de engine schakelt automatisch.
 *
 * Alle bedragen incl. 21% btw. config/tariff.energiedirect.ts blijft staan als historische
 * referentie van de Energiedirect-staffel (contract afgelopen 07-07-2026) — niet meer in gebruik.
 */
export const TARIFF_CONFIG = {
  contractType: 'dynamic' as const,
  supplier: 'zonneplan',

  // EPEX-spot komt per kwartier van een SpotPriceProvider (ENTSO-E day-ahead, of stub).
  // Alleen componenten die we BOVENOP de spot tellen leven hier.
  inkoopvergoedingEurPerKwh: 0.025,    // Zonneplan inkoopopslag (incl. btw, maandgemiddelde 2026)
  energyTaxEurPerKwh: 0.1316,          // Energiebelasting 1e schijf 2026 (€0,1088 ex btw × 1,21)
  terugleveropslagEurPerKwh: 0,        // Zonneplan rekent geen vaste terugleverkosten

  // Zonnebonus: 10% extra op de spot bovenop terugleververgoeding, alleen overdag,
  // alleen bij positieve (spot+opslag), en capped op 7.500 kWh teruglevering per jaar.
  zonnebonusCapKwh: 7500,
  zonnebonusPercentage: 0.10,
  zonnebonusStartHour: 10,             // inclusief
  zonnebonusEndHour: 15,               // exclusief

  // Saldering-statusvlag (datum-gestuurd in de engine via regime_for()).
  saldering: {
    active: true,
    untilDate: '2027-01-01',           // saldering vervalt per deze datum
  },
} as const;
