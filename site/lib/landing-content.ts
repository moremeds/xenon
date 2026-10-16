export type ProofItem = {
  label: string;
  value: string;
  detail: string;
  tone?: "core" | "strong" | "neutral";
};

export type StructureItem = {
  name: string;
  description: string;
  legs: string;
  category: string;
  guard: string;
  bias: string;
  decision: "Allowed" | "Blocked" | "Guard Gap";
  slug: string;
  tone?: "core" | "strong" | "warn" | "violet";
};

export type ExecutionItem = {
  step: string;
  summary: string;
  metadata: string;
};

export type SurfaceItem = {
  name: string;
  headline: string;
  metrics: Array<{ label: string; value: string }>;
  details: string[];
};

export type AuditItem = {
  title: string;
  detail: string;
};

export const headerLinks = [
  { href: "#structures", label: "Structures" },
  { href: "#execution", label: "Execution" },
  { href: "#surfaces", label: "Surfaces" },
  { href: "#methodology", label: "Methodology" },
];

export const proofItems: ProofItem[] = [
  {
    label: "Brokers",
    value: "IB + Futu",
    detail: "Live Interactive Brokers execution; read-only Futu position snapshot.",
    tone: "core",
  },
  {
    label: "Structure Catalog",
    value: "Catalogued",
    detail: "Option structures carry explicit guard decisions and P&L attribution labels.",
    tone: "strong",
  },
  {
    label: "Guard",
    value: "Enforced",
    detail: "UI pre-flight and API gate share one naked-short rule; a post-sync audit re-checks eligible active SELL orders.",
    tone: "strong",
  },
  {
    label: "Store",
    value: "Postgres",
    detail: "Positions, orders, fills, and journal read from the database, not files.",
    tone: "neutral",
  },
  {
    label: "Signal Feed",
    value: "None",
    detail: "No scanners and no discovery layer. Bring your own thesis.",
    tone: "core",
  },
];

export type ModuleTab = "portfolio" | "orders" | "attribution" | "guard";

export type ModuleContent = {
  metrics: Array<{ label: string; value: string; detail: string }>;
  commands: Array<{ label: string; state: string; command: string }>;
  commandLabel: string;
  commandPill: string;
};

export const moduleContents: Record<ModuleTab, ModuleContent> = {
  portfolio: {
    metrics: [
      { label: "Primary Broker", value: "IB", detail: "Live quotes + positions" },
      { label: "Second Tab", value: "Futu", detail: "Read-only snapshot" },
      { label: "State Store", value: "Postgres", detail: "Account snapshots" },
      { label: "Scope", value: "Account", detail: "Broker + env keyed" },
    ],
    commands: [
      { label: "Position Sync", state: "IB Source", command: "xenon-ib-sync" },
      { label: "Futu Snapshot", state: "Observe Only", command: "xenon-futu-sync" },
      { label: "NAV Refresh", state: "Flex Query", command: "xenon-nav-flex-refresh" },
    ],
    commandLabel: "Sync Surface",
    commandPill: "Account Scoped",
  },
  orders: {
    metrics: [
      { label: "Routing", value: "IB", detail: "Direct order path" },
      { label: "Combos", value: "BAG", detail: "One multi-leg order" },
      { label: "Lifecycle", value: "Tracked", detail: "Submission to fill" },
      { label: "Guard", value: "Active", detail: "Naked short block" },
    ],
    commands: [
      { label: "Place Order", state: "Guarded", command: "xenon-ib-place-order" },
      { label: "Cancel / Modify", state: "Owner Client", command: "xenon-ib-order-manage" },
      { label: "Open Orders", state: "Snapshot", command: "xenon-ib-orders" },
    ],
    commandLabel: "Order Surface",
    commandPill: "Guard Enforced",
  },
  attribution: {
    metrics: [
      { label: "Attribution", value: "Greeks", detail: "Per-position split" },
      { label: "Journal", value: "Append", detail: "Postgres rows" },
      { label: "Blotter", value: "Flex", detail: "Fill audit overlay" },
      { label: "Reports", value: "CLI", detail: "Regenerable output" },
    ],
    commands: [
      { label: "Performance", state: "Report", command: "xenon-portfolio-perf" },
      { label: "Attribution", state: "Greeks", command: "xenon-portfolio-attrib" },
      { label: "Fill Audit", state: "Flex Query", command: "xenon-blotter-history" },
    ],
    commandLabel: "Attribution Surface",
    commandPill: "Postgres Backed",
  },
  guard: {
    metrics: [
      { label: "Enforcement", value: "Layered", detail: "UI, API, audit" },
      { label: "Naked Calls", value: "Blocked", detail: "Cover required at entry" },
      { label: "Short Puts", value: "Allowed", detail: "Per guard policy" },
      { label: "Cover Ratio", value: "1:100", detail: "Contract to shares" },
    ],
    commands: [
      { label: "Pre-Trade Guard", state: "UI + API", command: "nakedShortGuard" },
      { label: "Post-Sync Audit", state: "Enforced", command: "xenon-naked-short-audit" },
      { label: "Structure Rules", state: "Catalog", command: "options-structures" },
    ],
    commandLabel: "Guard Surface",
    commandPill: "Fail Closed",
  },
};

export const structures: StructureItem[] = [
  {
    name: "Covered Call",
    description:
      "Short call premium against a long stock position. The terminal verifies 100 held shares per contract before the order can be staged.",
    legs: "Long shares + short OTM call",
    category: "Covered",
    guard: "Allow — shares cover calls",
    bias: "Neutral",
    decision: "Allowed",
    slug: "covered-call",
    tone: "core",
  },
  {
    name: "Short Put (Cash-Secured)",
    description:
      "Single-leg short put at or out of the money. Short puts are allowed by the guard; the naked-short rule covers calls and stock shorts.",
    legs: "Short put (ATM or OTM)",
    category: "Single leg",
    guard: "Allow — short put",
    bias: "Bullish",
    decision: "Allowed",
    slug: "short-put-cash-secured",
    tone: "strong",
  },
  {
    name: "Bull Call Spread",
    description:
      "Long call paired with a higher-strike short call at the same expiry. The long leg covers the short; risk is the debit paid.",
    legs: "Long call + short call, higher strike",
    category: "Vertical",
    guard: "Allow — defined risk",
    bias: "Bullish",
    decision: "Allowed",
    slug: "bull-call-spread",
    tone: "core",
  },
  {
    name: "Long Iron Condor",
    description:
      "Two out-of-the-money credit verticals sharing one expiry. All four legs are submitted as one multi-leg BAG order.",
    legs: "Put credit spread + call credit spread",
    category: "Multi-leg",
    guard: "Allow — defined risk",
    bias: "Range",
    decision: "Allowed",
    slug: "long-iron-condor",
    tone: "strong",
  },
  {
    name: "Short Call (Naked)",
    description:
      "An uncovered short call carries unlimited upside loss. Xenon rejects it at order entry and again in the post-sync audit.",
    legs: "Short call, no cover",
    category: "Single leg",
    guard: "Block — uncovered short",
    bias: "Bearish",
    decision: "Blocked",
    slug: "short-call-naked",
    tone: "warn",
  },
  {
    name: "Seagull Spread",
    description:
      "A documented guard gap: the short put is not covered by the long call hedge. The catalog flags the gap instead of pretending coverage.",
    legs: "Long call + short call + short put",
    category: "Multi-leg",
    guard: "Gap — flagged in catalog",
    bias: "Defined-gap",
    decision: "Guard Gap",
    slug: "seagull-spread",
    tone: "violet",
  },
];

export const executionItems: ExecutionItem[] = [
  {
    step: "Sync",
    summary:
      "IB positions and account state land in Postgres; Futu snapshots stay read-only on their own account tab.",
    metadata: "ib_sync, account scope, futu observe-only",
  },
  {
    step: "Stage",
    summary:
      "Orders are built against live IB quotes — single legs or multi-leg BAG combos with explicit leg actions and ratios.",
    metadata: "Chains, quotes, combo legs, limit price",
  },
  {
    step: "Guard",
    summary:
      "Coverage is verified before routing: no uncovered short calls and no stock shorts; short puts are allowed.",
    metadata: "1 contract = 100 shares, fail closed",
  },
  {
    step: "Execute",
    summary:
      "Placement, cancel, and modify run through IB on the owning client id; fills mirror back to Postgres.",
    metadata: "IB routing, fills, lifecycle states",
  },
  {
    step: "Attribute",
    summary:
      "Positions keep structure classification; P&L and Greeks decompose per position so the book explains itself.",
    metadata: "Greeks attribution, journal, Flex audit",
  },
];

export const surfaceItems: SurfaceItem[] = [
  {
    name: "Xenon Portfolio",
    headline: "Positions, exposure, and account state read as one book.",
    metrics: [
      { label: "Sources", value: "IB + Futu" },
      { label: "Store", value: "Postgres" },
    ],
    details: [
      "Live IB positions with quotes, chains, and Greeks on the account scope.",
      "Futu OpenD positions render on a separate read-only tab — no orders, no fills.",
      "Every row carries broker, environment, and account; paper and live never blend.",
      "Recognized multi-leg structures stay classified instead of flattening into legs.",
    ],
  },
  {
    name: "Xenon Orders",
    headline: "Placement, cancel, and modify run one guarded path to the broker.",
    metrics: [
      { label: "Routing", value: "IB" },
      { label: "Guard", value: "Enforced" },
    ],
    details: [
      "Single-leg and multi-leg BAG orders with explicit leg actions and ratios; a BAG submits as one order, and legs may fill separately.",
      "Cancel and modify reconnect on the placing client id.",
      "The naked-short rule applies at the UI and the API gate; a post-sync audit re-checks eligible active single-leg SELL orders.",
      "Order state is tracked through a defined lifecycle, including unknown outcomes.",
    ],
  },
  {
    name: "Xenon Attribution",
    headline: "Fills feed trade accounting; closed trades create automatic journal entries.",
    metrics: [
      { label: "Attribution", value: "Greeks" },
      { label: "Journal", value: "Append-only" },
    ],
    details: [
      "Daily P&L and Greeks decompose per position and per structure.",
      "Orders, fills, and journal entries persist in Postgres — the UI only reads.",
      "IB Flex queries audit historical fills against the blotter.",
    ],
  },
];

export const auditItems: AuditItem[] = [
  {
    title: "Naked-Short Guard",
    detail:
      "Coverage is checked before the broker sees the order; a post-sync audit re-checks eligible active single-leg SELL orders after each sync. Uncovered short calls are rejected, not flagged.",
  },
  {
    title: "Account Scoping",
    detail:
      "Every position, order, and fill carries broker, environment, and account. Paper and live data never blend.",
  },
  {
    title: "Structure Catalog",
    detail:
      "Catalogued option structures with guard decisions, risk profiles, and P&L attribution labels — including documented guard gaps.",
  },
  {
    title: "Postgres Truth",
    detail:
      "Portfolio, orders, fills, and journal read from the database. No silent file fallbacks on runtime paths.",
  },
  {
    title: "Open Architecture",
    detail:
      "Source-visible and operator-controlled. The terminal explains its state; it does not ask for blind trust.",
  },
];

export const footerColumns = [
  {
    title: "Terminal",
    links: [
      { href: "#structures", label: "Structures" },
      { href: "#execution", label: "Execution" },
      { href: "#surfaces", label: "Surfaces" },
    ],
  },
  {
    title: "Protocol",
    links: [
      { href: "#methodology", label: "Methodology" },
      { href: "https://github.com/joemccann/xenon", label: "Source" },
      {
        href: "https://github.com/joemccann/xenon/blob/main/docs/trading/options-structures.md",
        label: "Catalog",
      },
    ],
  },
];
