"use client";

import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type {
  PriceData,
  FundamentalsData,
  DepthBook,
  Trade,
  OptionContract,
} from "@/lib/pricesProtocol";
import type { OrdersData, PortfolioData } from "@/lib/types";

/**
 * Click-to-fill payload: a price (and, when unambiguous, a side) sent from the
 * order book / tape to the order ticket. `nonce` is stamped by the setter and
 * monotonically increases so two clicks on the SAME price+side still re-fire
 * the consuming effect (which keys on nonce, never on object identity).
 * `action` is OMITTED whenever the side is ambiguous — a price-only fill is
 * always safe; the ticket keeps its current action and the user still confirms.
 * Ported from radon TickerDetailContext `:15-20`.
 */
export type OrderPrefill = {
  price: number;
  action?: "BUY" | "SELL";
  quantity?: number;
  source: "montage" | "ladder" | "tape";
  nonce: number;
};

type TickerDetailContextValue = {
  activeTicker: string | null;
  activePositionId: number | null;
  setActiveTicker: (ticker: string | null) => void;
  setActivePositionId: (id: number | null) => void;
  getPrices: () => Record<string, PriceData>;
  getFundamentals: () => Record<string, FundamentalsData>;
  getPortfolio: () => PortfolioData | null;
  getOrders: () => OrdersData | null;
  /** L2 depth-of-book + tape, keyed by depth key. Transport refs mirroring
   *  prices: WorkspaceShell (the usePrices callsite) writes, the ticker-detail
   *  tree reads. Refs (no re-render); the cockpit re-renders off the WS state. */
  getDepths: () => Record<string, DepthBook>;
  getTape: () => Record<string, Trade[]>;
  setPrices: (p: Record<string, PriceData>) => void;
  setFundamentals: (f: Record<string, FundamentalsData>) => void;
  setPortfolio: (p: PortfolioData | null) => void;
  setOrders: (o: OrdersData | null) => void;
  setDepths: (d: Record<string, DepthBook>) => void;
  setTape: (t: Record<string, Trade[]>) => void;
  chainContracts: OptionContract[];
  setChainContracts: (c: OptionContract[]) => void;
  /** The depth key the detail view wants L2 depth for (bare symbol for stocks,
   *  composite optionKey for options). Published by TickerDetailContent and read
   *  by WorkspaceShell to drive `usePrices({ depthSymbol })`. State (not a ref)
   *  so the callsite re-renders and re-subscribes on a focus change. */
  focusedBookKey: string | null;
  setFocusedBookKey: (key: string | null) => void;
  /** Click-to-fill: latest price/side published from the book/tape, or null. */
  orderPrefill: OrderPrefill | null;
  /** Publish a click-to-fill; the nonce is stamped here (monotonic). */
  setOrderPrefill: (p: Omit<OrderPrefill, "nonce">) => void;
};

const TickerDetailContext = createContext<TickerDetailContextValue | null>(
  null,
);

export function TickerDetailProvider({ children }: { children: ReactNode }) {
  const [activeTicker, setActiveTickerState] = useState<string | null>(null);
  const [activePositionId, setActivePositionIdState] = useState<number | null>(
    null,
  );
  const [chainContracts, setChainContractsState] = useState<OptionContract[]>(
    [],
  );
  const [orderPrefill, setOrderPrefillState] = useState<OrderPrefill | null>(
    null,
  );
  const [focusedBookKey, setFocusedBookKeyState] = useState<string | null>(
    null,
  );
  const prefillNonceRef = useRef(0);
  const pricesRef = useRef<Record<string, PriceData>>({});
  const fundamentalsRef = useRef<Record<string, FundamentalsData>>({});
  const portfolioRef = useRef<PortfolioData | null>(null);
  const ordersRef = useRef<OrdersData | null>(null);
  const depthsRef = useRef<Record<string, DepthBook>>({});
  const tapeRef = useRef<Record<string, Trade[]>>({});

  const setActiveTicker = useCallback((ticker: string | null) => {
    setActiveTickerState(ticker ? ticker.toUpperCase() : null);
    if (!ticker) {
      setActivePositionIdState(null);
      setChainContractsState([]);
    }
  }, []);

  const setActivePositionId = useCallback((id: number | null) => {
    setActivePositionIdState(id);
  }, []);

  const setChainContracts = useCallback((c: OptionContract[]) => {
    setChainContractsState(c);
  }, []);

  const setOrderPrefill = useCallback((p: Omit<OrderPrefill, "nonce">) => {
    prefillNonceRef.current += 1;
    setOrderPrefillState({ ...p, nonce: prefillNonceRef.current });
  }, []);

  const setFocusedBookKey = useCallback((key: string | null) => {
    setFocusedBookKeyState((prev) => (prev === key ? prev : key));
  }, []);

  const getPrices = useCallback(() => pricesRef.current, []);
  const getFundamentals = useCallback(() => fundamentalsRef.current, []);
  const getPortfolio = useCallback(() => portfolioRef.current, []);
  const getOrders = useCallback(() => ordersRef.current, []);
  const getDepths = useCallback(() => depthsRef.current, []);
  const getTape = useCallback(() => tapeRef.current, []);

  const setPrices = useCallback((p: Record<string, PriceData>) => {
    pricesRef.current = p;
  }, []);

  const setFundamentals = useCallback((f: Record<string, FundamentalsData>) => {
    fundamentalsRef.current = f;
  }, []);

  const setPortfolio = useCallback((p: PortfolioData | null) => {
    portfolioRef.current = p;
  }, []);

  const setOrders = useCallback((o: OrdersData | null) => {
    ordersRef.current = o;
  }, []);

  const setDepths = useCallback((d: Record<string, DepthBook>) => {
    depthsRef.current = d;
  }, []);

  const setTape = useCallback((t: Record<string, Trade[]>) => {
    tapeRef.current = t;
  }, []);

  return (
    <TickerDetailContext.Provider
      value={{
        activeTicker,
        activePositionId,
        setActiveTicker,
        setActivePositionId,
        getPrices,
        getFundamentals,
        getPortfolio,
        getOrders,
        getDepths,
        getTape,
        setPrices,
        setFundamentals,
        setPortfolio,
        setOrders,
        setDepths,
        setTape,
        chainContracts,
        setChainContracts,
        focusedBookKey,
        setFocusedBookKey,
        orderPrefill,
        setOrderPrefill,
      }}
    >
      {children}
    </TickerDetailContext.Provider>
  );
}

export function useTickerDetail(): TickerDetailContextValue {
  const ctx = useContext(TickerDetailContext);
  if (!ctx)
    throw new Error("useTickerDetail must be used within TickerDetailProvider");
  return ctx;
}

/**
 * Non-throwing accessor: returns null when there is no provider. Use this for
 * progressive-enhancement consumers (click-to-fill) that must not crash when
 * the component is rendered outside the ticker-detail tree (modals, tests).
 * Ported from radon TickerDetailContext.
 */
export function useTickerDetailOptional(): TickerDetailContextValue | null {
  return useContext(TickerDetailContext);
}
