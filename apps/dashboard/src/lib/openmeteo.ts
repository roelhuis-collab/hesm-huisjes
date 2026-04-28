/**
 * Client-side Open-Meteo fetch.
 *
 * Open-Meteo allows browser CORS so we can hit it directly from the
 * dashboard without going through Cloud Run. Default coords are
 * Sittard (50.99°N, 5.87°E) — same as the backend connector default.
 */

export interface HourlyWeather {
  /** ISO timestamp at hour boundary, local time (Europe/Amsterdam). */
  time: string;
  /** °C at 2m. */
  temperature: number;
  /** % cloud cover. */
  cloudCover: number;
}

const SITTARD_LAT = 50.99;
const SITTARD_LON = 5.87;

interface RawResponse {
  hourly?: {
    time: string[];
    temperature_2m: number[];
    cloud_cover: number[];
  };
}

export async function fetchForecast48h(
  lat = SITTARD_LAT,
  lon = SITTARD_LON,
): Promise<HourlyWeather[]> {
  const url = new URL('https://api.open-meteo.com/v1/forecast');
  url.searchParams.set('latitude', String(lat));
  url.searchParams.set('longitude', String(lon));
  url.searchParams.set('hourly', 'temperature_2m,cloud_cover');
  url.searchParams.set('timezone', 'Europe/Amsterdam');
  url.searchParams.set('forecast_days', '2');

  const res = await fetch(url.toString());
  if (!res.ok) {
    throw new Error(`open-meteo ${res.status}`);
  }
  const json = (await res.json()) as RawResponse;
  if (!json.hourly) {
    throw new Error('open-meteo returned no hourly block');
  }

  const { time, temperature_2m, cloud_cover } = json.hourly;
  const out: HourlyWeather[] = [];
  for (let i = 0; i < time.length; i++) {
    out.push({
      time: time[i],
      temperature: temperature_2m[i],
      cloudCover: cloud_cover[i],
    });
  }
  return out;
}
