/**
 * 48-hour weather forecast for Sittard — temperature line + cloud cover area.
 *
 * Pulls from Open-Meteo directly client-side (their CORS allows it).
 */

import { useEffect, useState } from 'react';
import {
  Area,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { type HourlyWeather, fetchForecast48h } from '../lib/openmeteo';

interface ChartRow {
  hour: string;
  temp: number;
  cloud: number;
}

function toRows(forecast: HourlyWeather[]): ChartRow[] {
  return forecast.map((h) => {
    const d = new Date(h.time);
    const hh = String(d.getHours()).padStart(2, '0');
    return {
      hour: `${hh}:00`,
      temp: h.temperature,
      cloud: h.cloudCover,
    };
  });
}

export function WeatherChart() {
  const [rows, setRows] = useState<ChartRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchForecast48h()
      .then((forecast) => setRows(toRows(forecast)))
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
      <header className="mb-4 flex items-center justify-between">
        <h2 className="text-[10px] uppercase tracking-[0.25em] text-slate-500">
          Weersvoorspelling — Sittard, komende 48 uur
        </h2>
        <span className="text-[10px] uppercase tracking-widest text-slate-600">
          open-meteo
        </span>
      </header>

      {error && (
        <p className="text-sm text-rose-300">Kon Open-Meteo niet laden: {error}</p>
      )}

      {rows.length > 0 && (
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart
              data={rows}
              margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
            >
              <XAxis
                dataKey="hour"
                interval={5}
                stroke="#475569"
                tick={{ fontSize: 10, fill: '#64748b' }}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                yAxisId="temp"
                stroke="#475569"
                tick={{ fontSize: 10, fill: '#64748b' }}
                tickLine={false}
                axisLine={false}
                domain={['auto', 'auto']}
                width={32}
              />
              <YAxis
                yAxisId="cloud"
                orientation="right"
                stroke="#475569"
                tick={{ fontSize: 10, fill: '#64748b' }}
                tickLine={false}
                axisLine={false}
                domain={[0, 100]}
                width={32}
              />
              <Tooltip
                contentStyle={{
                  background: '#0f172a',
                  border: '1px solid #1e293b',
                  borderRadius: 8,
                  fontSize: 12,
                }}
                labelStyle={{ color: '#94a3b8' }}
                itemStyle={{ color: '#fbbf24' }}
              />
              <Area
                yAxisId="cloud"
                type="monotone"
                dataKey="cloud"
                stroke="none"
                fill="#334155"
                fillOpacity={0.3}
                name="Bewolking %"
              />
              <Line
                yAxisId="temp"
                type="monotone"
                dataKey="temp"
                stroke="#fbbf24"
                strokeWidth={2}
                dot={false}
                name="Temperatuur °C"
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}
