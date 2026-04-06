import { runScript, type ScriptResult } from "../runner";

export interface VCGInput {
  proxy?: string;
  backtest?: boolean;
  days?: number;
}

export interface VCGSignal {
  vcg: number | null;
  vcg_adj: number | null;      // was vcg_div — panic-adjusted z-score
  residual: number | null;
  beta1_vvix: number | null;
  beta2_vix: number | null;
  alpha: number | null;
  vix: number;
  vvix: number;
  credit_price: number;
  credit_5d_return_pct: number;
  ro: number;
  edr: number;                 // Early Divergence Risk (0|1)
  tier: 1 | 2 | 3 | null;     // severity tier when ro=1 or edr=1
  bounce: number;              // counter-signal bounce (0|1)
  vvix_severity: "extreme" | "elevated" | "moderate";
  sign_ok: boolean;
  sign_suppressed: boolean;
  pi_panic: number;
  regime: string;
  interpretation: "RISK_OFF" | "EDR" | "WATCH" | "BOUNCE" | "NORMAL" | "SUPPRESSED" | "PANIC" | string;
  attribution: {
    vvix_pct: number;
    vix_pct: number;
    vvix_component: number;
    vix_component: number;
    model_implied: number;
  };
}

export interface VCGOutput {
  scan_time: string;
  market_open: boolean;
  credit_proxy: string;
  signal: VCGSignal;
  history: Array<{
    date: string;
    residual: number | null;
    vcg: number | null;
    vcg_adj: number | null;    // was vcg_div
    beta1: number | null;
    beta2: number | null;
    vix: number;
    vvix: number;
    credit: number;
  }>;
}

export async function vcgScan(
  input: VCGInput = {},
): Promise<ScriptResult<VCGOutput>> {
  const args: string[] = ["--json"];

  if (input.proxy) {
    args.push("--proxy", input.proxy);
  }
  if (input.backtest) {
    args.push("--backtest");
    if (input.days) {
      args.push("--days", String(input.days));
    }
  }

  return runScript("scripts/vcg_scan.py", {
    args,
    timeout: 60_000,
  }) as Promise<ScriptResult<VCGOutput>>;
}
