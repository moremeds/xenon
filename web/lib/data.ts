import {
  BarChart3,
  Circle,
  ClipboardList,
  LayoutDashboard,
  Wrench,
} from "lucide-react";
import type { WorkspaceNavItem, WorkspaceSection } from "./types";

export const PI_COMMANDS = ["portfolio", "journal", "sync", "help"] as const;
export const PI_COMMAND_SET = new Set<string>(PI_COMMANDS);

export const PI_COMMAND_ALIASES: Record<string, string> = {
  "action items": "/journal --limit 25",
  "what are action items": "/journal --limit 25",
};

export const navItems: WorkspaceNavItem[] = [
  {
    label: "Dashboard",
    route: "dashboard",
    href: "/dashboard",
    icon: LayoutDashboard,
  },
  { label: "Portfolio", route: "portfolio", href: "/portfolio", icon: Circle },
  {
    label: "Performance",
    route: "performance",
    href: "/performance",
    icon: BarChart3,
  },
  { label: "Orders", route: "orders", href: "/orders", icon: ClipboardList },
  { label: "Journal", route: "journal", href: "/journal", icon: Wrench },
];

export const quickPromptsBySection: Record<WorkspaceSection, string[]> = {
  dashboard: ["portfolio", "journal --limit 10", "help"],
  portfolio: ["portfolio", "journal --limit 10", "help"],
  performance: ["portfolio", "journal --limit 10", "help"],
  orders: ["portfolio", "journal --limit 10", "help"],
  journal: ["journal --limit 25", "portfolio", "help"],
  "ticker-detail": ["portfolio", "help"],
};

export const sectionDescription: Record<WorkspaceSection, string> = {
  dashboard: "Portfolio snapshot and account overview.",
  portfolio: "Open positions, exposure, P&L per leg.",
  performance: "Performance attribution and historical P&L.",
  orders: "Open orders and recent fills from IB Gateway.",
  journal: "Trade decision logs and history review.",
  "ticker-detail":
    "Instrument detail — position, orders, options chain, history.",
};
