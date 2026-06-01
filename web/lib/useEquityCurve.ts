"use client";
import { useEffect, useState } from "react";
import type { CrossBrokerEquityCurve } from "@/lib/futuHistory/types";

interface State {
  data: CrossBrokerEquityCurve | null;
  isLoading: boolean;
  error: Error | null;
}

export function useEquityCurveCrossBroker(
  opts: {
    brokers?: ("IB" | "FUTU")[];
    combined?: boolean;
  } = {},
) {
  const brokers = (opts.brokers ?? ["IB", "FUTU"]).join(",");
  const combined = opts.combined ? "true" : "false";
  const [state, setState] = useState<State>({
    data: null,
    isLoading: true,
    error: null,
  });
  useEffect(() => {
    let aborted = false;
    setState((s) => ({ ...s, isLoading: true }));
    fetch(`/api/equity-curve?brokers=${brokers}&combined=${combined}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`equity-curve: ${r.status}`);
        return (await r.json()) as CrossBrokerEquityCurve;
      })
      .then((data) => {
        if (!aborted) setState({ data, isLoading: false, error: null });
      })
      .catch((error) => {
        if (!aborted)
          setState({ data: null, isLoading: false, error: error as Error });
      });
    return () => {
      aborted = true;
    };
  }, [brokers, combined]);
  return state;
}
