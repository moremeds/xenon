"use client";

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import type { PriceData, FundamentalsData, OptionContract } from "@/lib/pricesProtocol";
import type { OrdersData, PortfolioData } from "@/lib/types";

type TickerDetailContextValue = {
  activeTicker: string | null;
  activePositionId: number | null;
  setActiveTicker: (ticker: string | null) => void;
  setActivePositionId: (id: number | null) => void;
  getPrices: () => Record<string, PriceData>;
  getFundamentals: () => Record<string, FundamentalsData>;
  getPortfolio: () => PortfolioData | null;
  getOrders: () => OrdersData | null;
  setPrices: (p: Record<string, PriceData>) => void;
  setFundamentals: (f: Record<string, FundamentalsData>) => void;
  setPortfolio: (p: PortfolioData | null) => void;
  setOrders: (o: OrdersData | null) => void;
  chainContracts: OptionContract[];
  setChainContracts: (c: OptionContract[]) => void;
};

const TickerDetailContext = createContext<TickerDetailContextValue | null>(null);

export function TickerDetailProvider({ children }: { children: ReactNode }) {
  const [activeTicker, setActiveTickerState] = useState<string | null>(null);
  const [activePositionId, setActivePositionIdState] = useState<number | null>(null);
  const [chainContracts, setChainContractsState] = useState<OptionContract[]>([]);
  const pricesRef = useRef<Record<string, PriceData>>({});
  const fundamentalsRef = useRef<Record<string, FundamentalsData>>({});
  const portfolioRef = useRef<PortfolioData | null>(null);
  const ordersRef = useRef<OrdersData | null>(null);

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

  const getPrices = useCallback(() => pricesRef.current, []);
  const getFundamentals = useCallback(() => fundamentalsRef.current, []);
  const getPortfolio = useCallback(() => portfolioRef.current, []);
  const getOrders = useCallback(() => ordersRef.current, []);

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

  return (
    <TickerDetailContext.Provider
      value={{ activeTicker, activePositionId, setActiveTicker, setActivePositionId, getPrices, getFundamentals, getPortfolio, getOrders, setPrices, setFundamentals, setPortfolio, setOrders, chainContracts, setChainContracts }}
    >
      {children}
    </TickerDetailContext.Provider>
  );
}

export function useTickerDetail(): TickerDetailContextValue {
  const ctx = useContext(TickerDetailContext);
  if (!ctx) throw new Error("useTickerDetail must be used within TickerDetailProvider");
  return ctx;
}
