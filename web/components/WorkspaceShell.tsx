"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { RefreshCw } from "lucide-react";
import type { OrdersData, WorkspaceSection } from "@/lib/types";
import { navItems } from "@/lib/data";
import { resolveSectionFromPath } from "@/lib/chat";
import { usePortfolio } from "@/lib/usePortfolio";
import { useFutuPortfolio } from "@/lib/useFutuPortfolio";
import { useActiveAccount } from "@/lib/accountContext";
import { useOrders } from "@/lib/useOrders";
import { useMarketHours, MarketState } from "@/lib/useMarketHours";
import { useToast } from "@/lib/useToast";
import { useOrderActions } from "@/lib/OrderActionsContext";
import { usePrices } from "@/lib/usePrices";
import { computeRealizedPnlFromFills } from "@/lib/realized-pnl";
import { usePreviousClose } from "@/lib/usePreviousClose";
import { useSubscriberHealth } from "@/lib/useSubscriberHealth";
import {
  type OptionContract,
  type IndexContract,
  optionKey,
  portfolioLegToContract,
  uniqueOptionContracts,
} from "@/lib/pricesProtocol";
import Sidebar from "@/components/Sidebar";
import Header from "@/components/Header";
import DashboardSurface from "@/components/dashboard/DashboardSurface";
import type { DashboardAccount } from "@/components/dashboard/PortfolioSnapshotCard";
import MetricCards from "@/components/MetricCards";
import AccountTabBar, {
  type AccountTabState,
} from "@/components/AccountTabBar";
import { computeFutuStaleness } from "@/lib/futuStaleness";
import ToastContainer from "@/components/Toast";

const WorkspaceSections = dynamic(
  () => import("@/components/WorkspaceSections"),
  {
    loading: () => null,
  },
);
import ConnectionBanner from "@/components/ConnectionBanner";
import FlexTokenBanner from "@/components/FlexTokenBanner";
import { useTickerDetail } from "@/lib/TickerDetailContext";

type WorkspaceShellProps = {
  section?: WorkspaceSection;
  tickerParam?: string;
};

export default function WorkspaceShell({
  section,
  tickerParam,
}: WorkspaceShellProps) {
  const [theme, setTheme] = useState<"dark" | "light" | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const pathname = usePathname();
  const activeSection: WorkspaceSection =
    section ?? resolveSectionFromPath(pathname, "dashboard");
  const navLabel =
    navItems.find((item) => item.route === activeSection)?.label ?? "Dashboard";
  const activeLabel =
    activeSection === "ticker-detail" && tickerParam ? tickerParam : navLabel;
  const { toasts, addToast, removeToast } = useToast();
  const marketState = useMarketHours();
  const isMarketActive = marketState !== MarketState.CLOSED;

  const { activeAccount, setActiveAccount } = useActiveAccount();

  // Both accounts stay hot in the background so tab switching is instant.
  const ibData = usePortfolio(isMarketActive);
  const futuData = useFutuPortfolio(isMarketActive);

  // The `portfolio` variable the rest of the shell consumes is the
  // broker-appropriate one. Every downstream component sees a normal
  // PortfolioData and branches on nothing.
  const portfolio = activeAccount === "ib" ? ibData.data : futuData.data;
  const portfolioSyncing =
    activeAccount === "ib" ? ibData.syncing : futuData.syncing;
  const portfolioError = activeAccount === "ib" ? ibData.error : futuData.error;
  const portfolioLastSync =
    activeAccount === "ib" ? ibData.lastSync : futuData.lastSync;
  const portfolioSyncNow =
    activeAccount === "ib" ? ibData.syncNow : futuData.syncNow;

  const portfolioSymbols = useMemo(
    () => (portfolio?.positions ?? []).map((p) => p.ticker),
    [portfolio?.positions],
  );

  const portfolioContracts = useMemo<OptionContract[]>(() => {
    const contracts: OptionContract[] = [];
    for (const pos of portfolio?.positions ?? []) {
      if (pos.structure_type === "Stock") continue;
      for (const leg of pos.legs) {
        const c = portfolioLegToContract(pos.ticker, pos.expiry, leg);
        if (c) contracts.push(c);
      }
    }
    return contracts;
  }, [portfolio?.positions]);

  // Bridge order-actions context → toasts & orders updater
  const { drainNotifications, setOrdersUpdater } = useOrderActions();

  const isOrdersPage = activeSection === "orders";
  // Fetch orders polling on orders page (always), and on other pages only during market hours.
  const shouldAutoSyncOrders = isOrdersPage || isMarketActive;
  // Fetch orders polling based on context (market hours for non-order views, always for orders)
  // initial fetch always happens on mount
  const {
    data: orders,
    syncing: ordersSyncing,
    error: ordersError,
    lastSync: ordersLastSync,
    syncNow: ordersSyncNow,
    updateData: updateOrdersData,
  } = useOrders(shouldAutoSyncOrders);

  // Trigger a fresh IB sync every time the user navigates TO the orders page.
  // place/modify/cancel all sync orders.json immediately after the action, so
  // this primarily catches IB-side changes (partial fills, status updates, etc.)
  // that happened while the user was on another page.
  useEffect(() => {
    if (isOrdersPage) {
      ordersSyncNow();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOrdersPage]);

  const orderSymbols = useMemo(
    () => (orders?.open_orders ?? []).map((o) => o.contract.symbol),
    [orders?.open_orders],
  );

  const orderContracts = useMemo<OptionContract[]>(() => {
    const contracts: OptionContract[] = [];
    for (const o of orders?.open_orders ?? []) {
      const c = o.contract;
      // OPT: subscribe to the single option contract
      if (c.secType === "OPT" && c.strike != null && c.right && c.expiry) {
        const right =
          c.right === "C" || c.right === "P"
            ? c.right
            : c.right === "CALL"
              ? "C"
              : c.right === "PUT"
                ? "P"
                : null;
        if (!right) continue;
        const expiryClean = c.expiry.replace(/-/g, "");
        if (expiryClean.length !== 8) continue;
        contracts.push({
          symbol: c.symbol.toUpperCase(),
          expiry: expiryClean,
          strike: c.strike,
          right,
        });
      }
      // BAG: subscribe to each combo leg's option contract
      if (c.secType === "BAG" && c.comboLegs) {
        for (const cl of c.comboLegs) {
          if (!cl.symbol || cl.strike == null || !cl.right || !cl.expiry)
            continue;
          const right =
            cl.right === "C" || cl.right === "P"
              ? cl.right
              : cl.right === "CALL"
                ? "C"
                : cl.right === "PUT"
                  ? "P"
                  : null;
          if (!right) continue;
          const expiryClean = cl.expiry.replace(/-/g, "");
          if (expiryClean.length !== 8) continue;
          contracts.push({
            symbol: cl.symbol.toUpperCase(),
            expiry: expiryClean,
            strike: cl.strike,
            right,
          });
        }
      }
    }
    return contracts;
  }, [orders?.open_orders]);

  const tickerSymbols = useMemo(
    () => (tickerParam ? [tickerParam] : []),
    [tickerParam],
  );

  const allSymbols = useMemo(
    () => [
      ...new Set([...portfolioSymbols, ...orderSymbols, ...tickerSymbols]),
    ],
    [portfolioSymbols, orderSymbols, tickerSymbols],
  );

  const tickerDetail = useTickerDetail();

  const allContracts = useMemo(
    () =>
      uniqueOptionContracts([
        ...portfolioContracts,
        ...orderContracts,
        ...tickerDetail.chainContracts,
      ]),
    [portfolioContracts, orderContracts, tickerDetail.chainContracts],
  );

  const {
    prices: rawPrices,
    fundamentals,
    connected: wsConnected,
    ibConnected: rawIbConnected,
    ibIssue,
    ibStatusMessage,
  } = usePrices({
    symbols: allSymbols,
    contracts: allContracts,
    indexes: [],
  });

  // Debounce ibConnected: disconnections must persist >2s before surfacing to UI.
  // IB farm connectivity checks fire brief disconnected→connected sequences that
  // would otherwise flash the banner/toast every few seconds.
  const [ibConnected, setIbConnected] = useState(rawIbConnected);
  const ibDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (ibDebounceRef.current) clearTimeout(ibDebounceRef.current);
    if (rawIbConnected) {
      // Reconnection: propagate immediately (user wants to know it's back)
      setIbConnected(true);
    } else {
      // Disconnection: delay 2s to filter out brief farm-check flickers
      ibDebounceRef.current = setTimeout(() => setIbConnected(false), 2000);
    }
    return () => {
      if (ibDebounceRef.current) clearTimeout(ibDebounceRef.current);
    };
  }, [rawIbConnected]);

  // Realtime WS subscriber health (external ?id= price-stream clients; the
  // realtime relay binds :8765 in prod, :8866 in dev — resolved via runtime file).
  const subscriberHealth = useSubscriberHealth();

  // Backfill missing previous-close from Yahoo Finance / UW for day-change calc
  const prices = usePreviousClose(rawPrices);

  // Realized P&L derived from today's session fills (executed_orders), not IB account summary.
  // IB's reqPnL().realizedPnL can include non-trade events and diverges from fill-level data.
  const executedOrders = useMemo(
    () => orders?.executed_orders ?? [],
    [orders?.executed_orders],
  );
  const todayRealizedPnl = useMemo(
    () => computeRealizedPnlFromFills(executedOrders),
    [executedOrders],
  );

  // Both accounts for the dashboard's merged Portfolio card (IB + FUTU).
  const dashboardAccounts: DashboardAccount[] = [
    {
      source: "ib",
      label: "IB",
      accountId: ibData.data?.account_summary ? "IB Account" : null,
      status: ibConnected ? "live" : "down",
      portfolio: ibData.data,
    },
    {
      source: "futu",
      label: "FUTU",
      accountId: futuData.envelope?.account_id ?? null,
      status: computeFutuStaleness({
        envelope: futuData.envelope,
        error: futuData.error,
        neverSynced: futuData.neverSynced,
        marketOpen: isMarketActive,
      }),
      portfolio: futuData.data,
    },
  ];

  // Sync prices + portfolio into ticker-detail context (refs, no re-renders)
  const {
    setActiveTicker,
    setPrices: setTickerPrices,
    setFundamentals: setTickerFundamentals,
    setPortfolio: setTickerPortfolio,
    setOrders: setTickerOrders,
  } = tickerDetail;
  useEffect(() => {
    setTickerPrices(prices);
  }, [prices, setTickerPrices]);
  useEffect(() => {
    setTickerFundamentals(fundamentals);
  }, [fundamentals, setTickerFundamentals]);
  useEffect(() => {
    setTickerPortfolio(portfolio);
  }, [portfolio, setTickerPortfolio]);
  useEffect(() => {
    setTickerOrders(orders);
  }, [orders, setTickerOrders]);

  // Sync tickerParam to context
  useEffect(() => {
    setActiveTicker(tickerParam ?? null);
  }, [tickerParam, setActiveTicker]);

  const prevIbConnectedRef = useRef<boolean | null>(null);
  useEffect(() => {
    if (
      prevIbConnectedRef.current !== null &&
      prevIbConnectedRef.current !== ibConnected
    ) {
      if (ibConnected) {
        addToast("success", "IB Gateway reconnected", 4000);
      } else if (ibIssue === "ibc_mfa_required") {
        addToast(
          "warning",
          ibStatusMessage ??
            "Interactive Brokers Gateway is reconnecting. Check the push notification from Interactive Brokers on your phone to approve MFA.",
          8000,
        );
      } else {
        addToast("error", "IB Gateway connection lost", 6000);
      }
    }
    prevIbConnectedRef.current = ibConnected;
  }, [ibConnected, ibIssue, ibStatusMessage, addToast]);
  const syncing = isOrdersPage ? ordersSyncing : portfolioSyncing;
  const error = isOrdersPage ? ordersError : portfolioError;
  const lastSync = isOrdersPage ? ordersLastSync : portfolioLastSync;
  const syncNow = isOrdersPage ? ordersSyncNow : portfolioSyncNow;
  const syncTarget = isOrdersPage ? "orders" : "portfolio";

  // Register the orders-data updater so the cancel provider can push fresh data
  useEffect(() => {
    setOrdersUpdater(updateOrdersData);
    return () => setOrdersUpdater(null);
  }, [setOrdersUpdater, updateOrdersData]);

  // Drain cancel-context notifications into the toast system
  useEffect(() => {
    const id = setInterval(() => {
      const notes = drainNotifications();
      for (const n of notes) addToast(n.type, n.message, n.duration);
    }, 500);
    return () => clearInterval(id);
  }, [drainNotifications, addToast]);

  useEffect(() => {
    const saved = localStorage.getItem("theme");
    if (saved === "light" || saved === "dark") {
      setTheme(saved);
      document.documentElement.setAttribute("data-theme", saved);
    } else {
      const prefersDark = window.matchMedia(
        "(prefers-color-scheme: dark)",
      ).matches;
      const systemTheme = prefersDark ? "dark" : "light";
      setTheme(systemTheme);
      document.documentElement.setAttribute("data-theme", systemTheme);
    }
  }, []);

  useEffect(() => {
    const syncFullscreenState = () => {
      setIsFullscreen(Boolean(document.fullscreenElement));
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && document.fullscreenElement) {
        event.preventDefault();
        void document.exitFullscreen().catch(() => {});
      }
    };

    syncFullscreenState();
    document.addEventListener("fullscreenchange", syncFullscreenState);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("fullscreenchange", syncFullscreenState);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  const resolvedTheme = theme ?? "dark";

  const actionTone = useMemo(() => {
    return resolvedTheme === "dark" ? "#e2e8f0" : "#0a0f14";
  }, [resolvedTheme]);

  const toggleTheme = () => {
    const next = resolvedTheme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
  };

  const toggleFullscreen = useCallback(async () => {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen();
      }
    } catch {
      // Ignore denied fullscreen requests; the button stays in sync via fullscreenchange.
    }
  }, []);

  const syncLabel = lastSync
    ? `Last sync: ${new Date(lastSync).toLocaleTimeString()}`
    : error
      ? `Sync error`
      : "No sync yet";

  return (
    <div className="app-shell" suppressHydrationWarning>
      <Sidebar
        activeSection={activeSection}
        actionTone={actionTone}
        ibConnected={ibConnected}
        lastSync={lastSync}
        subscribers={subscriberHealth.subscribers}
        subscribersReachable={subscriberHealth.reachable}
        anonymousCount={subscriberHealth.anonymousCount}
      />

      <main className="main">
        <Header
          activeLabel={activeLabel}
          isFullscreen={isFullscreen}
          onToggleFullscreen={toggleFullscreen}
          onToggleTheme={toggleTheme}
          theme={resolvedTheme}
        >
          <div className="sync-controls">
            <span
              className={`sync-status ${error ? "sync-error" : syncing ? "sync-active" : ""}`}
            >
              {syncLabel}
            </span>
            <button
              className="sync-button"
              onClick={syncNow}
              disabled={syncing}
              title={`Sync ${syncTarget} from ${activeAccount === "futu" && !isOrdersPage ? "Futu OpenD" : "IB Gateway"}`}
            >
              <RefreshCw size={14} className={syncing ? "spin" : ""} />
              {syncing ? "Syncing..." : "Sync Now"}
            </button>
          </div>
        </Header>

        <ConnectionBanner
          ibConnected={ibConnected}
          wsConnected={wsConnected}
          ibIssue={ibIssue}
          ibStatusMessage={ibStatusMessage}
        />
        <FlexTokenBanner />

        <div className="content">
          {activeSection !== "dashboard" &&
          activeSection !== "ticker-detail" ? (
            <AccountTabBar
              active={activeAccount}
              onChange={setActiveAccount}
              ib={{
                label: "IB",
                accountId: ibData.data?.account_summary ? "IB Account" : null,
                environment: "real",
                positionCount: ibData.data?.positions.length ?? 0,
                lastSync: ibData.lastSync,
                netLiquidation:
                  ibData.data?.account_summary?.net_liquidation ?? null,
                status: ibConnected ? "live" : "down",
              }}
              futu={{
                label: "FUTU",
                accountId: futuData.envelope?.account_id ?? null,
                environment: "real",
                positionCount: futuData.data?.positions.length ?? 0,
                lastSync: futuData.lastSync,
                netLiquidation:
                  futuData.data?.account_summary?.net_liquidation ?? null,
                status: computeFutuStaleness({
                  envelope: futuData.envelope,
                  error: futuData.error,
                  neverSynced: futuData.neverSynced,
                  marketOpen: isMarketActive,
                }),
              }}
            />
          ) : null}

          {activeSection === "dashboard" ? (
            <DashboardSurface
              accounts={dashboardAccounts}
              orders={orders}
              prices={prices}
            />
          ) : null}

          {activeSection !== "dashboard" &&
          activeSection !== "ticker-detail" ? (
            <MetricCards
              portfolio={portfolio}
              prices={prices}
              realizedPnl={todayRealizedPnl}
              executedOrders={executedOrders}
              section={activeSection}
            />
          ) : null}

          {activeSection !== "dashboard" ? (
            <WorkspaceSections
              section={activeSection}
              portfolio={portfolio}
              portfolioLastSync={portfolioLastSync}
              orders={orders}
              prices={prices}
              tickerParam={tickerParam}
              theme={resolvedTheme}
              marketState={marketState}
              activeAccount={activeAccount}
            />
          ) : null}
        </div>
      </main>

      <ToastContainer toasts={toasts} onDismiss={removeToast} />
    </div>
  );
}
