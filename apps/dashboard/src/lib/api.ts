/**
 * Typed Cloud Run API client.
 *
 * Every call automatically attaches the current user's Firebase ID token
 * as ``Authorization: Bearer <token>``. The Cloud Run service ignores it
 * today (--allow-unauthenticated) but server-side gating lands in a
 * follow-up PR; sending the header now keeps the client honest.
 *
 * Calls that don't need auth (``GET /health``) are still routed through
 * here so we have one base-URL location.
 */

import { type User } from 'firebase/auth';
import { API_BASE_URL } from './firebase';

// ---------------------------------------------------------------------------
// Types — mirror the FastAPI server's JSON shapes
// ---------------------------------------------------------------------------

export type Strategy =
  | 'max_saving'
  | 'comfort_first'
  | 'max_self_consumption'
  | 'eco_green_hours'
  | 'custom';

export interface StrategyWeights {
  cost: number;
  comfort: number;
  self_consumption: number;
  renewable_share: number;
}

export interface TempBand {
  min_c: number;
  max_c: number;
}

export interface SystemLimits {
  floor_max_flow_c: number;
  bathroom_max_flow_c: number;
  radiator_max_flow_c: number;
  boiler_legionella_floor_c: number;
  boiler_max_c: number;
  living_room: TempBand;
  bedroom: TempBand;
  bathroom: TempBand;
  dompelaar_max_price_eur_kwh: number;
  dompelaar_only_with_pv_above_w: number;
  hp_min_run_minutes: number;
}

export interface Policy {
  limits: SystemLimits;
  strategy: Strategy;
  custom_weights: StrategyWeights | null;
  learning_enabled: boolean;
  overrides: Record<string, unknown>;
  updated_at: string;
}

export interface PolicyUpdate {
  strategy?: Strategy;
  custom_weights?: StrategyWeights;
  limits?: Partial<SystemLimits>;
}

export interface HealthWiring {
  firestore: boolean;
  homewizard_connector: boolean;
  entsoe_connector: boolean;
  openmeteo_connector: boolean;
  weheat_connector: boolean;
  resideo_connector: boolean;
  shelly_connector: boolean;
  growatt_connector: boolean;
  ai_chat: boolean;
  optimizer_v0: boolean;
}

export interface HealthResponse {
  status: string;
  service: string;
  wiring: HealthWiring;
}

export interface LearningRespondResponse {
  status: 'activated' | 'dismissed';
  count?: number;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------


export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, msg: string) {
    super(msg);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  path: string,
  user: User | null,
  init: RequestInit = {},
): Promise<T> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  };
  if (user) {
    headers.authorization = `Bearer ${await user.getIdToken()}`;
  }
  const res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  if (!res.ok) {
    const body = await res.text();
    let parsed: unknown = body;
    try {
      parsed = JSON.parse(body);
    } catch {
      // not JSON, keep raw text
    }
    throw new ApiError(res.status, parsed, `${res.status} on ${path}`);
  }
  // Some 204s have no body; only call .json() if we expect one.
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Public surface
// ---------------------------------------------------------------------------

export async function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/health', null);
}

export async function getPolicy(user: User): Promise<Policy> {
  return request<Policy>('/policy', user);
}

export async function updatePolicy(
  user: User,
  update: PolicyUpdate,
): Promise<{ status: string; policy: Policy }> {
  return request('/policy', user, {
    method: 'PUT',
    body: JSON.stringify(update),
  });
}

export async function respondToLearning(
  user: User,
  accepted: boolean,
): Promise<LearningRespondResponse> {
  return request<LearningRespondResponse>('/learning/respond', user, {
    method: 'POST',
    body: JSON.stringify({ accepted }),
  });
}
