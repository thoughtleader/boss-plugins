export interface MeterScope {
  label: string;
  done: number;
  total: number;
  elapsedMs: number | null;
  tokens: number | null;
  partialTokens: boolean;
  running: number;
  state: string;
  bar: string;
  ticking: boolean;
  hideElapsed?: boolean;
  waitingMs?: number;
}
export interface Meter {
  forecast?: MeterForecast;
  waves: MeterScope | null;
  current: MeterScope;
  total: MeterScope;
  tasks: MeterTasks;
  attention: string[];
  tokenNote: string | null;
}
export interface MeterTask {
  id: string;
  label: string;
  state: string;
  wave: string | null;
  elapsedMs: number | null;
  tokens: number | null;
  partialTokens: boolean;
  model: string | null;
  effort: string | null;
  result: string | null;
  sha: string | null;
  activity?: string | null;
  instructions?: string | null;
  waitingReason?: string | null;
  detail: string | null;
  action: string | null;
}
export interface MeterTasks {
  now: MeterTask[];
  next: MeterTask[];
  recent: MeterTask[];
  all: MeterTask[];
  nowCount: number;
  nextCount: number;
  recentCount: number;
}
export function meterFromSnapshot(snapshot: unknown, options?: { nowMs?: number; ledger?: unknown[] }): Meter;
export function meterFromBoardRun(run: unknown, options?: { nowMs?: number }): Meter;
export function renderMeterText(meter: Meter, options?: { columns?: number; fenced?: boolean }): string;
export function formatElapsed(value: number | null, options?: { compact?: boolean }): string;
export function formatClock(value: number | null, options?: { hours?: boolean }): string;
export function clockHours(meter: Pick<Meter, 'waves' | 'current' | 'total'>): boolean;
export function clockParts(clock: string): { dim: string; active: string };
export function formatTokens(value: number | null): string;
export function summaryTaskText(task: MeterTask, options?: { completed?: boolean }): string | null;
export function progressLabel(scope: Pick<MeterScope, 'done' | 'total'>): string;
export function displayBar(scope: Pick<MeterScope, 'done' | 'total'> | null, cells?: number): string;

export interface MeterForecast {
  currentWave?: ForecastPoint;
  checkpoint: ForecastPoint;
  finish: ForecastPoint;
  originalFinishAt: number;
  originalCheckpointAt: number;
  waiting: boolean;
  basis: string;
  delayMs: number;
  reason: string | null;
}
export interface ForecastPoint {
  remainingMs: number;
  at: number | null;
  label: string;
  text: string;
  approximate: boolean;
}

export function windowedBar(scope: Pick<MeterScope, "done" | "total"> | null, cells?: number): { text: string; hiddenDone: number; hiddenUpcoming: number };
