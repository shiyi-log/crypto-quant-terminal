import { ftClient, unwrap } from './freqtrade/client';

export type RuntimeHealthState = 'ok' | 'warning' | 'failed' | 'unknown';

export interface RuntimeHealthComponent {
  key: string;
  label: string;
  state: RuntimeHealthState;
  updated_at: string | null;
  last_success_at: string | null;
  age_seconds: number | null;
  error_codes: string[];
  metrics: Record<string, boolean | number | null>;
}

export interface RuntimeHealth {
  generated_at: string;
  ok: boolean;
  components: RuntimeHealthComponent[];
}

export async function getRuntimeHealth(): Promise<RuntimeHealth> {
  return unwrap<RuntimeHealth>(await ftClient.get('/locals/runtime-health'));
}
