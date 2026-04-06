"use client";

import { IBStatusProvider } from "@/lib/IBStatusContext";
import { OrderActionsProvider } from "@/lib/OrderActionsContext";
import { TickerDetailProvider } from "@/lib/TickerDetailContext";

export default function Providers({ children }: { children: React.ReactNode }) {
  return (
    <IBStatusProvider>
      <OrderActionsProvider>
        <TickerDetailProvider>{children}</TickerDetailProvider>
      </OrderActionsProvider>
    </IBStatusProvider>
  );
}
