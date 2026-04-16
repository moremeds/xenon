# Sub-Plan 2: Universe Builder + Stage A TA Prefilter

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the triple-source universe builder and Stage A TA trend scoring with 9 indicators, bullish gate, and breakout detection. (Bearish scanning deferred to follow-on.)

**Architecture:** `trend_scan_lib/universe.py` fetches tickers from 3 sources (static index files, UW flow alerts, IB scanner) and unions them. Note: universe floor filters (market cap, price, dollar volume) are applied at Stage A gate time via `passes_bullish_gate()`, not at universe build time. `trend_scan_lib/stages/ta_prefilter.py` computes TA indicators from OHLCV data and scores each ticker 0-1.

**Tech Stack:** Python 3.14, pytest, UWClient, IBClient

**Spec:** `docs/superpowers/specs/2026-04-10-trend-scanner-design.md` (Universe + Stage A sections)

**Depends on:** Sub-Plan 1 (scanner_lib foundation) must be complete.

---

## File Structure

```
scripts/
├── trend_scan_lib/
│   ├── __init__.py                 # CREATE
│   ├── models.py                   # CREATE — TrendCandidate
│   ├── config.py                   # CREATE — TrendScanConfig
│   ├── universe.py                 # CREATE — TrendUniverseBuilder
│   └── stages/
│       ├── __init__.py             # CREATE
│       └── ta_prefilter.py         # CREATE — Stage A scoring
├── tests/
│   ├── test_trend_models.py        # CREATE
│   ├── test_trend_universe.py      # CREATE
│   └── test_ta_prefilter.py        # CREATE
└── data/
    └── universe/
        ├── sp500.json              # CREATE — static ticker list
        └── nasdaq100.json          # CREATE — static ticker list
```

---

### Task 1: Trend Scanner Models (`trend_scan_lib/models.py`)

**Files:**

- Create: `scripts/trend_scan_lib/__init__.py`
- Create: `scripts/trend_scan_lib/models.py`
- Create: `scripts/trend_scan_lib/stages/__init__.py`
- Test: `scripts/tests/test_trend_models.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_trend_models.py
"""Tests for trend scanner models."""
from __future__ import annotations

import pytest


def test_trend_candidate_creation():
    from scripts.trend_scan_lib.models import TrendCandidate

    c = TrendCandidate(
        ticker="NVDA",
        direction="bullish",
        final_score=0.82,
        scores={"trend": 0.91, "structure": 0.75, "volatility": 0.68, "flow": 0.85},
        spot_price=148.30,
        indicators={
            "ma_20": 142.50,
            "rsi": 62.3,
        },
        suggested_trade="debit_call",
        invalidation=142.50,
        holding_window="5-15 trading days",
    )
    assert c.ticker == "NVDA"
    assert c.spot_price == 148.30
    assert c.indicators["rsi"] == 62.3
    assert c.suggested_trade == "debit_call"


def test_trend_candidate_defaults():
    from scripts.trend_scan_lib.models import TrendCandidate

    c = TrendCandidate(
        ticker="AAPL",
        direction="bullish",
        final_score=0.5,
        scores={"trend": 0.5},
        spot_price=185.0,
    )
    assert c.indicators == {}
    assert c.suggested_trade == ""
    assert c.invalidation == 0.0
    assert c.holding_window == "5-15 trading days"
    assert c.flags == []
    assert c.summaries == {}


def test_trend_candidate_to_dict():
    from scripts.trend_scan_lib.models import TrendCandidate

    c = TrendCandidate(
        ticker="AAPL",
        direction="bullish",
        final_score=0.7,
        scores={"trend": 0.8},
        spot_price=185.0,
        indicators={"rsi": 60.0},
    )
    d = c.to_dict()
    assert d["ticker"] == "AAPL"
    assert d["spot_price"] == 185.0
    assert d["indicators"]["rsi"] == 60.0
    assert isinstance(d, dict)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_models.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create trend_scan_lib package and models**

```python
# scripts/trend_scan_lib/__init__.py
"""trend_scan_lib: 3-stage trend scanner for pre-market swing trade identification."""
```

```python
# scripts/trend_scan_lib/stages/__init__.py
"""Trend scanner pipeline stages."""
```

```python
# scripts/trend_scan_lib/models.py
"""Trend scanner data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from scripts.scanner_lib.models import BaseScanCandidate


@dataclass
class TrendCandidate(BaseScanCandidate):
    """A ranked trend scan candidate with full indicator snapshot."""

    spot_price: float = 0.0
    indicators: dict[str, float] = field(default_factory=dict)
    suggested_trade: str = ""
    invalidation: float = 0.0
    holding_window: str = "5-15 trading days"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "direction": self.direction,
            "final_score": self.final_score,
            "scores": self.scores,
            "spot_price": self.spot_price,
            "indicators": self.indicators,
            "summaries": self.summaries,
            "suggested_trade": self.suggested_trade,
            "invalidation": self.invalidation,
            "flags": self.flags,
            "holding_window": self.holding_window,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_models.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/__init__.py scripts/trend_scan_lib/models.py scripts/trend_scan_lib/stages/__init__.py scripts/tests/test_trend_models.py
git commit -m "feat(trend_scan_lib): add TrendCandidate model"
```

---

### Task 2: Config (`trend_scan_lib/config.py`)

**Files:**

- Create: `scripts/trend_scan_lib/config.py`
- Test: `scripts/tests/test_trend_config.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_trend_config.py
"""Tests for trend scanner config."""
from __future__ import annotations

import pytest


def test_default_config():
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig()
    assert cfg.top_n == 25
    assert cfg.max_workers == 15
    assert cfg.weights == {"trend": 0.35, "structure": 0.25, "volatility": 0.20, "flow": 0.20}
    assert abs(sum(cfg.weights.values()) - 1.0) < 0.01


def test_config_min_thresholds():
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig()
    assert cfg.min_thresholds["trend"] == 0.4
    assert cfg.min_thresholds["structure"] == 0.3


def test_config_universe_floor():
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig()
    assert cfg.min_market_cap == 1_000_000_000
    assert cfg.min_dollar_volume == 10_000_000
    assert cfg.min_price == 5.0


def test_config_custom_top_n():
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig(top_n=10)
    assert cfg.top_n == 10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_config.py -v`
Expected: FAIL

- [ ] **Step 3: Implement config**

```python
# scripts/trend_scan_lib/config.py
"""Trend scanner configuration."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TrendScanConfig:
    """Configuration for the trend scanner pipeline."""

    top_n: int = 25
    max_workers: int = 15

    # Scoring weights — must sum to 1.0
    weights: dict[str, float] = field(default_factory=lambda: {
        "trend": 0.35,
        "structure": 0.25,
        "volatility": 0.20,
        "flow": 0.20,
    })

    # Minimum scores to pass final ranking
    min_thresholds: dict[str, float] = field(default_factory=lambda: {
        "trend": 0.4,
        "structure": 0.3,
    })

    # Universe floor filters
    min_market_cap: float = 1_000_000_000  # $1B
    min_dollar_volume: float = 10_000_000  # $10M avg daily
    min_price: float = 5.0

    # Universe source paths
    sp500_path: str = "data/universe/sp500.json"
    nasdaq100_path: str = "data/universe/nasdaq100.json"

    # UW flow alert filters for universe source
    uw_flow_min_premium: float = 100_000  # $100k
    uw_flow_lookback_days: int = 5
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_config.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/config.py scripts/tests/test_trend_config.py
git commit -m "feat(trend_scan_lib): add TrendScanConfig with weights and thresholds"
```

---

### Task 3: Static Universe Files

**Files:**

- Create: `data/universe/sp500.json`
- Create: `data/universe/nasdaq100.json`

- [ ] **Step 1: Create the data/universe directory**

Run: `mkdir -p /Users/chenxi/projects/xenon/data/universe`

- [ ] **Step 2: Generate S&P 500 ticker list**

Create a JSON file with current S&P 500 constituents. **Important:** Remove any delisted/acquired symbols (e.g., ATVI→MSFT, FRC→JPM, SIVB/SBNY→closed, DISH→merged). Verify by spot-checking 5 random tickers are fetchable. Use a script to generate:

```bash
cd /Users/chenxi/projects/xenon && python -c "
import json
# Use Wikipedia S&P 500 list as a known source
# For now, create a curated list of major liquid names
# This file is refreshed manually or via periodic script
tickers = [
    'AAPL','ABBV','ABT','ACN','ADBE','ADI','ADM','ADP','ADSK','AEE',
    'AEP','AES','AFL','AIG','AIZ','AJG','AKAM','ALB','ALGN','ALK',
    'ALL','ALLE','AMAT','AMCR','AMD','AME','AMGN','AMP','AMT','AMZN',
    'ANET','ANSS','AON','AOS','APA','APD','APH','APTV','ARE','ATO',
    'AVGO','AVY','AWK','AXP','AZO','BA','BAC','BAX','BBWI',
    'BBY','BDX','BEN','BF.B','BIO','BIIB','BK','BKNG','BKR','BLK',
    'BMY','BR','BRK.B','BRO','BSX','BWA','BXP','C','CAG','CAH',
    'CARR','CAT','CB','CBOE','CBRE','CCI','CCL','CDNS','CDW','CE',
    'CEG','CF','CFG','CHD','CHRW','CHTR','CI','CINF','CL','CLX',
    'CMA','CMCSA','CME','CMG','CMI','CMS','CNC','CNP','COF','COO',
    'COP','COST','CPB','CPRT','CPT','CRL','CRM','CSCO','CSGP','CSX',
    'CTAS','CTLT','CTRA','CTSH','CTVA','CVS','CVX','CZR','D','DAL',
    'DD','DE','DFS','DG','DGX','DHI','DHR','DIS','DLR',
    'DLTR','DOV','DOW','DPZ','DRI','DTE','DUK','DVA','DVN','DXC',
    'DXCM','EA','EBAY','ECL','ED','EFX','EIX','EL','EMN','EMR',
    'ENPH','EOG','EPAM','EQIX','EQR','EQT','ES','ESS','ETN','ETR',
    'ETSY','EVRG','EW','EXC','EXPD','EXPE','EXR','F','FANG','FAST',
    'FBHS','FCX','FDS','FDX','FE','FFIV','FIS','FISV','FITB','FLT',
    'FMC','FOX','FOXA','FRT','FTNT','FTV','GD','GE','GEHC',
    'GEN','GILD','GIS','GL','GLW','GM','GNRC','GOOG','GOOGL','GPC',
    'GPN','GRMN','GS','GWW','HAL','HAS','HBAN','HCA','HD','PEAK',
    'HES','HIG','HII','HLT','HOLX','HON','HPE','HPQ','HRL','HSIC',
    'HST','HSY','HUM','HWM','IBM','ICE','IDXX','IEX','IFF','ILMN',
    'INCY','INTC','INTU','INVH','IP','IPG','IQV','IR','IRM','ISRG',
    'IT','ITW','IVZ','J','JBHT','JCI','JKHY','JNJ','JNPR','JPM',
    'K','KDP','KEY','KEYS','KHC','KIM','KLAC','KMB','KMI','KMX',
    'KO','KR','L','LDOS','LEN','LH','LHX','LIN','LKQ','LLY',
    'LMT','LNC','LNT','LOW','LRCX','LUMN','LUV','LVS','LW','LYB',
    'LYV','MA','MAA','MAR','MAS','MCD','MCHP','MCK','MCO','MDLZ',
    'MDT','MET','META','MGM','MHK','MKC','MKTX','MLM','MMC','MMM',
    'MNST','MO','MOH','MOS','MPC','MPWR','MRK','MRNA','MRO','MS',
    'MSCI','MSFT','MSI','MTB','MTCH','MTD','MU','NCLH','NDAQ','NDSN',
    'NEE','NEM','NFLX','NI','NKE','NOC','NOW','NRG','NSC','NTAP',
    'NTRS','NUE','NVDA','NVR','NWL','NWS','NWSA','NXPI','O','ODFL',
    'OGN','OKE','OMC','ON','ORCL','ORLY','OTIS','OXY','PARA','PAYC',
    'PAYX','PCAR','PCG','PEAK','PEG','PEP','PFE','PFG','PG','PGR',
    'PH','PHM','PKG','PKI','PLD','PM','PNC','PNR','PNW','POOL',
    'PPG','PPL','PRU','PSA','PSX','PTC','PVH','PWR','PXD','PYPL',
    'QCOM','QRVO','RCL','RE','REG','REGN','RF','RHI','RJF','RL',
    'RMD','ROK','ROL','ROP','ROST','RSG','RTX','SBAC','SBUX',
    'SCHW','SEE','SHW','SJM','SLB','SNA','SNPS','SO','SPG',
    'SPGI','SRE','STE','STT','STX','STZ','SWK','SWKS','SYF','SYK',
    'SYY','T','TAP','TDG','TDY','TECH','TEL','TER','TFC','TFX',
    'TGT','TJX','TMO','TMUS','TPR','TRGP','TRMB','TROW','TRV','TSCO',
    'TSLA','TSN','TT','TTWO','TXN','TXT','TYL','UAL','UDR','UHS',
    'ULTA','UNH','UNP','UPS','URI','USB','V','VFC','VICI','VLO',
    'VMC','VRSK','VRSN','VRTX','VTR','VTRS','VZ','WAB','WAT','WBA',
    'WBD','WDC','WEC','WELL','WFC','WHR','WM','WMB','WMT','WRB',
    'WRK','WST','WTW','WY','WYNN','XEL','XOM','XRAY','XYL','YUM',
    'ZBH','ZBRA','ZION','ZTS'
]
with open('data/universe/sp500.json', 'w') as f:
    json.dump(sorted(set(tickers)), f, indent=2)
print(f'Wrote {len(set(tickers))} tickers')
"
```

- [ ] **Step 3: Generate Nasdaq 100 ticker list**

```bash
cd /Users/chenxi/projects/xenon && python -c "
import json
tickers = [
    'AAPL','ABNB','ADBE','ADI','ADP','ADSK','AEP','AMAT','AMD','AMGN',
    'AMZN','ANSS','ARM','ASML','AVGO','AZN','BIIB','BKNG','BKR','CCEP',
    'CDNS','CDW','CEG','CHTR','CMCSA','COST','CPRT','CRWD','CSCO','CSGP',
    'CSX','CTAS','CTSH','DASH','DDOG','DLTR','DXCM','EA','EXC','FANG',
    'FAST','FTNT','GEHC','GFS','GILD','GOOG','GOOGL','HON','IDXX','ILMN',
    'INTC','INTU','ISRG','KDP','KHC','KLAC','LRCX','LULU','MAR','MCHP',
    'MDB','MDLZ','MELI','META','MNST','MRNA','MRVL','MSFT','MU','NFLX',
    'NVDA','NXPI','ODFL','ON','ORLY','PANW','PAYX','PCAR','PDD','PEP',
    'PYPL','QCOM','REGN','ROP','ROST','SBUX','SMCI','SNPS','SPLK','TEAM',
    'TMUS','TSLA','TTD','TTWO','TXN','VRSK','VRTX','WBD','WDAY','XEL','ZS'
]
with open('data/universe/nasdaq100.json', 'w') as f:
    json.dump(sorted(set(tickers)), f, indent=2)
print(f'Wrote {len(set(tickers))} tickers')
"
```

- [ ] **Step 4: Commit**

```bash
git add data/universe/sp500.json data/universe/nasdaq100.json
git commit -m "data: add static S&P 500 and Nasdaq 100 ticker lists"
```

---

### Task 4: Trend Universe Builder (`trend_scan_lib/universe.py`)

**Files:**

- Create: `scripts/trend_scan_lib/universe.py`
- Test: `scripts/tests/test_trend_universe.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_trend_universe.py
"""Tests for trend scanner universe builder."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def test_static_source_loads(tmp_path):
    from scripts.trend_scan_lib.universe import build_static_universe

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["AAPL", "MSFT", "GOOG"]))
    nq.write_text(json.dumps(["AAPL", "NVDA", "TSLA"]))

    result = build_static_universe(sp500_path=sp, nasdaq100_path=nq)
    assert result == ["AAPL", "GOOG", "MSFT", "NVDA", "TSLA"]


def test_static_source_missing_file(tmp_path):
    from scripts.trend_scan_lib.universe import build_static_universe

    result = build_static_universe(
        sp500_path=tmp_path / "missing.json",
        nasdaq100_path=tmp_path / "also_missing.json",
    )
    assert result == []


def test_uw_flow_source_extracts_tickers():
    from scripts.trend_scan_lib.universe import build_uw_flow_universe

    mock_client = MagicMock()
    mock_client.get_flow_alerts.return_value = [
        {"ticker": "AAPL", "premium": 500_000},
        {"ticker": "NVDA", "premium": 200_000},
        {"ticker": "AAPL", "premium": 300_000},  # duplicate
    ]
    mock_client.get_darkpool_flow.return_value = [
        {"ticker": "TSLA", "volume": 1_000_000},
    ]

    result = build_uw_flow_universe(client=mock_client, min_premium=100_000, lookback_days=5)
    assert "AAPL" in result
    assert "NVDA" in result
    assert "TSLA" in result


def test_uw_flow_source_handles_error():
    from scripts.trend_scan_lib.universe import build_uw_flow_universe

    mock_client = MagicMock()
    mock_client.get_flow_alerts.side_effect = Exception("API down")
    mock_client.get_darkpool_flow.side_effect = Exception("API down")

    result = build_uw_flow_universe(client=mock_client, min_premium=100_000, lookback_days=5)
    assert result == []


def test_ib_scanner_source_extracts_tickers():
    from scripts.trend_scan_lib.universe import build_ib_scanner_universe

    mock_client = MagicMock()
    mock_client.run_scanner.side_effect = [
        [{"ticker": "AAPL"}, {"ticker": "AMD"}],  # top gainers
        [{"ticker": "NVDA"}, {"ticker": "AAPL"}],  # most active
    ]

    result = build_ib_scanner_universe(client=mock_client)
    assert sorted(result) == ["AAPL", "AMD", "NVDA"]


def test_ib_scanner_source_handles_error():
    from scripts.trend_scan_lib.universe import build_ib_scanner_universe

    mock_client = MagicMock()
    mock_client.run_scanner.side_effect = Exception("IB Gateway down")

    result = build_ib_scanner_universe(client=mock_client)
    assert result == []


def test_build_full_universe(tmp_path):
    from scripts.trend_scan_lib.config import TrendScanConfig
    from scripts.trend_scan_lib.universe import build_universe

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["AAPL", "MSFT"]))
    nq.write_text(json.dumps(["NVDA"]))

    cfg = TrendScanConfig(sp500_path=str(sp), nasdaq100_path=str(nq))

    mock_uw = MagicMock()
    mock_ib = MagicMock()

    # Mock UW and IB builder functions to return known tickers
    with patch("scripts.trend_scan_lib.universe.build_uw_flow_universe", return_value=["GOOG"]), \
         patch("scripts.trend_scan_lib.universe.build_ib_scanner_universe", return_value=["TSLA"]):
        result = build_universe(cfg, uw_client=mock_uw, ib_client=mock_ib)

    assert result == ["AAPL", "GOOG", "MSFT", "NVDA", "TSLA"]


def test_build_universe_no_clients(tmp_path):
    """When uw_client and ib_client are None, only static sources are used."""
    from scripts.trend_scan_lib.config import TrendScanConfig
    from scripts.trend_scan_lib.universe import build_universe

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["AAPL", "MSFT"]))
    nq.write_text(json.dumps(["NVDA"]))

    cfg = TrendScanConfig(sp500_path=str(sp), nasdaq100_path=str(nq))
    result = build_universe(cfg, uw_client=None, ib_client=None)

    assert result == ["AAPL", "MSFT", "NVDA"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_universe.py -v`
Expected: FAIL

- [ ] **Step 3: Implement universe builder**

```python
# scripts/trend_scan_lib/universe.py
"""Triple-source universe builder for trend scanner."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from scripts.scanner_lib.universe import load_tickers_from_json, union_sources

logger = logging.getLogger(__name__)


def build_static_universe(
    *,
    sp500_path: Path | str,
    nasdaq100_path: Path | str,
) -> list[str]:
    """Load tickers from static index constituent files."""
    sp = load_tickers_from_json(Path(sp500_path))
    nq = load_tickers_from_json(Path(nasdaq100_path))
    return union_sources(sp, nq)


def build_uw_flow_universe(
    *,
    client: Any,
    min_premium: float = 100_000,
    lookback_days: int = 5,
) -> list[str]:
    """Extract tickers from recent UW flow alerts and dark pool activity."""
    tickers: list[str] = []
    try:
        alerts = client.get_flow_alerts(min_premium=min_premium, lookback_days=lookback_days)
        tickers.extend(a["ticker"] for a in alerts if "ticker" in a)
    except Exception:
        logger.warning("Failed to fetch UW flow alerts for universe", exc_info=True)

    try:
        dp = client.get_darkpool_flow()
        if isinstance(dp, list):
            tickers.extend(d["ticker"] for d in dp if "ticker" in d)
    except Exception:
        logger.warning("Failed to fetch UW dark pool for universe", exc_info=True)

    from scripts.scanner_lib.universe import dedup_and_normalize

    return dedup_and_normalize(tickers)


def build_ib_scanner_universe(*, client: Any) -> list[str]:
    """Fetch tickers from IB market scanners (top gainers, most active)."""
    tickers: list[str] = []
    scanner_types = ["TOP_PERC_GAIN", "MOST_ACTIVE_USD"]
    for scan_type in scanner_types:
        try:
            results = client.run_scanner(scan_type=scan_type)
            tickers.extend(r["ticker"] for r in results if "ticker" in r)
        except Exception:
            logger.warning("IB scanner %s failed", scan_type, exc_info=True)

    from scripts.scanner_lib.universe import dedup_and_normalize

    return dedup_and_normalize(tickers)


def build_universe(
    cfg: Any,
    *,
    uw_client: Optional[Any] = None,
    ib_client: Optional[Any] = None,
) -> list[str]:
    """Build the full universe from all three sources."""
    static = build_static_universe(
        sp500_path=cfg.sp500_path,
        nasdaq100_path=cfg.nasdaq100_path,
    )

    uw: list[str] = []
    if uw_client is not None:
        uw = build_uw_flow_universe(
            client=uw_client,
            min_premium=cfg.uw_flow_min_premium,
            lookback_days=cfg.uw_flow_lookback_days,
        )

    ib: list[str] = []
    if ib_client is not None:
        ib = build_ib_scanner_universe(client=ib_client)

    universe = union_sources(static, uw, ib)
    logger.info(
        "Universe built: %d tickers (static=%d, uw=%d, ib=%d)",
        len(universe), len(static), len(uw), len(ib),
    )
    return universe
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_universe.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/universe.py scripts/tests/test_trend_universe.py
git commit -m "feat(trend_scan_lib): add triple-source universe builder"
```

---

### Task 5: Stage A — TA Prefilter (`trend_scan_lib/stages/ta_prefilter.py`)

**Files:**

- Create: `scripts/trend_scan_lib/stages/ta_prefilter.py`
- Test: `scripts/tests/test_ta_prefilter.py`

This is the largest task. The TA prefilter computes 9 indicators from OHLCV data and produces a `trend_score` from 0-1.

- [ ] **Step 1: Write failing tests for individual indicator scorers**

```python
# scripts/tests/test_ta_prefilter.py
"""Tests for Stage A TA prefilter."""
from __future__ import annotations

import pytest


# --- Indicator scorer tests ---

def test_score_ma_alignment_full_stack():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_ma_alignment

    # close > ma20 > ma50 > ma200
    assert score_ma_alignment(close=150, ma_20=145, ma_50=140, ma_200=130) == 1.0


def test_score_ma_alignment_partial():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_ma_alignment

    # close > ma20 > ma50, but ma50 < ma200
    assert score_ma_alignment(close=150, ma_20=145, ma_50=125, ma_200=130) == 0.5


def test_score_ma_alignment_inverted():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_ma_alignment

    # close below all MAs
    assert score_ma_alignment(close=120, ma_20=130, ma_50=140, ma_200=150) == 0.0


def test_score_rsi_constructive():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_rsi

    # Peak score at 58-65
    assert score_rsi(62.0) == 1.0
    assert score_rsi(58.0) == 1.0


def test_score_rsi_outside_range():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_rsi

    # Below 40 → very low, above 80 → overbought penalty
    assert score_rsi(35.0) < 0.3
    assert score_rsi(85.0) < 0.3


def test_score_adx_strong_trend():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_adx

    assert score_adx(35.0) > 0.7
    assert score_adx(45.0) > 0.9


def test_score_adx_no_trend():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_adx

    assert score_adx(10.0) < 0.3


def test_score_macd_bullish():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_macd

    # MACD above signal, positive histogram
    assert score_macd(macd=1.5, signal=1.0, histogram=0.5) == 1.0


def test_score_macd_bearish():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_macd

    assert score_macd(macd=-1.0, signal=0.5, histogram=-1.5) == 0.0


def test_score_relative_strength_outperforming():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_relative_strength

    assert score_relative_strength(1.15) > 0.7


def test_score_relative_strength_underperforming():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_relative_strength

    assert score_relative_strength(0.85) < 0.3


def test_score_slope_positive():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_slope

    # MA values increasing over 5 days
    assert score_slope([140, 141, 142, 143, 145]) > 0.7


def test_score_slope_flat():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_slope

    assert score_slope([140, 140, 140, 140, 140]) == 0.5


def test_score_slope_negative():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_slope

    assert score_slope([145, 144, 143, 142, 140]) < 0.3


def test_score_volume_profile_above_avg():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_volume_profile

    # Recent volume 1.5x avg on up days
    assert score_volume_profile(recent_avg_volume=1_500_000, avg_20d_volume=1_000_000, recent_up_ratio=0.7) > 0.7


def test_score_bbw_squeeze():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_bbw

    # Narrow BBW = squeeze = bonus
    assert score_bbw(0.03) > 0.7


def test_score_bbw_wide():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_bbw

    assert score_bbw(0.20) < 0.4


# --- Breakout detection ---

def test_breakout_near_52w_high():
    from scripts.trend_scan_lib.stages.ta_prefilter import detect_breakout

    assert detect_breakout(close=148, high_52w=150, range_20d_pct=0.05, atr_pct=0.02) is True


def test_breakout_consolidation_break():
    from scripts.trend_scan_lib.stages.ta_prefilter import detect_breakout

    # 20-day range < 10% ATR, but we check range_20d_pct < atr_pct * 10
    # This simulates tight consolidation with breakout above range
    assert detect_breakout(close=100, high_52w=120, range_20d_pct=0.03, atr_pct=0.015) is True


def test_no_breakout():
    from scripts.trend_scan_lib.stages.ta_prefilter import detect_breakout

    assert detect_breakout(close=100, high_52w=150, range_20d_pct=0.15, atr_pct=0.02) is False


# --- Bullish gate ---

def test_bullish_gate_passes():
    from scripts.trend_scan_lib.stages.ta_prefilter import passes_bullish_gate

    assert passes_bullish_gate(close=150, ma_20=145, rsi=55, dollar_volume=20_000_000, min_dollar_volume=10_000_000) is True


def test_bullish_gate_fails_below_ma():
    from scripts.trend_scan_lib.stages.ta_prefilter import passes_bullish_gate

    assert passes_bullish_gate(close=140, ma_20=145, rsi=55, dollar_volume=20_000_000, min_dollar_volume=10_000_000) is False


def test_bullish_gate_fails_low_rsi():
    from scripts.trend_scan_lib.stages.ta_prefilter import passes_bullish_gate

    assert passes_bullish_gate(close=150, ma_20=145, rsi=35, dollar_volume=20_000_000, min_dollar_volume=10_000_000) is False


def test_bullish_gate_fails_low_volume():
    from scripts.trend_scan_lib.stages.ta_prefilter import passes_bullish_gate

    assert passes_bullish_gate(close=150, ma_20=145, rsi=55, dollar_volume=5_000_000, min_dollar_volume=10_000_000) is False


# --- Composite trend score ---

def test_compute_trend_score_strong_trend():
    from scripts.trend_scan_lib.stages.ta_prefilter import compute_trend_score

    indicators = {
        "close": 150, "ma_20": 145, "ma_50": 140, "ma_200": 130,
        "rsi": 62, "adx": 32,
        "macd": 1.5, "macd_signal": 1.0, "macd_histogram": 0.5,
        "rs_vs_spy": 1.15,
        "ma_20_series": [140, 141, 142, 143, 145],
        "recent_avg_volume": 1_500_000, "avg_20d_volume": 1_000_000, "recent_up_ratio": 0.7,
        "bbw": 0.05,
        "high_52w": 152, "range_20d_pct": 0.04, "atr_pct": 0.015,
    }
    score = compute_trend_score(indicators)
    assert 0.7 < score <= 1.0


def test_compute_trend_score_weak_trend():
    from scripts.trend_scan_lib.stages.ta_prefilter import compute_trend_score

    indicators = {
        "close": 130, "ma_20": 135, "ma_50": 140, "ma_200": 150,
        "rsi": 38, "adx": 12,
        "macd": -1.0, "macd_signal": 0.5, "macd_histogram": -1.5,
        "rs_vs_spy": 0.85,
        "ma_20_series": [145, 144, 143, 142, 140],
        "recent_avg_volume": 800_000, "avg_20d_volume": 1_000_000, "recent_up_ratio": 0.3,
        "bbw": 0.18,
        "high_52w": 170, "range_20d_pct": 0.12, "atr_pct": 0.02,
    }
    score = compute_trend_score(indicators)
    assert score < 0.4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_ta_prefilter.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement TA prefilter**

```python
# scripts/trend_scan_lib/stages/ta_prefilter.py
"""Stage A: Technical Analysis trend prefilter and scoring."""
from __future__ import annotations

from scripts.scanner_lib.scoring import normalize_score


# --- Individual indicator scorers ---

def score_ma_alignment(*, close: float, ma_20: float, ma_50: float, ma_200: float) -> float:
    """Score moving average stack alignment. Full stack (close > 20 > 50 > 200) = 1.0."""
    if close > ma_20 > ma_50 > ma_200:
        return 1.0
    if close > ma_20 > ma_50:
        return 0.7
    if close > ma_20:
        return 0.5
    if close < ma_20 < ma_50 < ma_200:
        return 0.0
    return 0.2


def score_rsi(rsi: float) -> float:
    """Score RSI. Peak at 58-65 (constructive trend), tapers outside."""
    if 58 <= rsi <= 65:
        return 1.0
    if 50 <= rsi < 58:
        return 0.7 + (rsi - 50) * 0.0375  # 0.7 → 1.0
    if 65 < rsi <= 70:
        return 1.0 - (rsi - 65) * 0.06  # 1.0 → 0.7
    if 45 <= rsi < 50:
        return 0.5
    if 70 < rsi <= 80:
        return 0.4
    if 40 <= rsi < 45:
        return 0.3
    # Below 40 or above 80
    return 0.1


def score_adx(adx: float) -> float:
    """Score ADX trend strength. >25 = strong, >40 = very strong."""
    if adx >= 40:
        return 1.0
    if adx >= 25:
        return 0.6 + (adx - 25) * 0.0267  # 0.6 → 1.0
    if adx >= 20:
        return 0.4 + (adx - 20) * 0.04  # 0.4 → 0.6
    return normalize_score(adx / 20 * 0.4)


def score_macd(*, macd: float, signal: float, histogram: float) -> float:
    """Score MACD. Above signal + positive histogram = 1.0."""
    if macd > signal and histogram > 0:
        return 1.0
    if macd > signal:
        return 0.7
    if histogram > 0:
        return 0.5
    return 0.0


def score_relative_strength(rs_ratio: float) -> float:
    """Score relative strength vs SPY. RS > 1.0 = outperforming."""
    if rs_ratio >= 1.2:
        return 1.0
    if rs_ratio >= 1.0:
        return 0.5 + (rs_ratio - 1.0) * 2.5  # 0.5 → 1.0
    if rs_ratio >= 0.9:
        return 0.3
    return 0.1


def score_slope(ma_series: list[float]) -> float:
    """Score 20DMA slope over recent days. Positive slope = good."""
    if len(ma_series) < 2:
        return 0.5
    first, last = ma_series[0], ma_series[-1]
    if first == 0:
        return 0.5
    pct_change = (last - first) / first
    if pct_change > 0.02:
        return 1.0
    if pct_change > 0.005:
        return 0.7
    if pct_change > -0.005:
        return 0.5
    if pct_change > -0.02:
        return 0.3
    return 0.1


def score_volume_profile(
    *, recent_avg_volume: float, avg_20d_volume: float, recent_up_ratio: float
) -> float:
    """Score volume profile. Above-average volume on up days = confirmation."""
    if avg_20d_volume == 0:
        return 0.5
    vol_ratio = recent_avg_volume / avg_20d_volume
    vol_score = normalize_score(vol_ratio - 0.5)  # 0.5x → 0.0, 1.5x → 1.0
    up_score = normalize_score(recent_up_ratio * 1.5 - 0.25)
    return (vol_score + up_score) / 2


def score_bbw(bbw: float) -> float:
    """Score Bollinger Band Width. Narrow = squeeze = pending breakout."""
    if bbw <= 0.03:
        return 1.0
    if bbw <= 0.06:
        return 0.8
    if bbw <= 0.10:
        return 0.5
    if bbw <= 0.15:
        return 0.3
    return 0.1


# --- Breakout detection ---

def detect_breakout(
    *, close: float, high_52w: float, range_20d_pct: float, atr_pct: float
) -> bool:
    """Detect breakout: within 3% of 52w high OR breaking above tight consolidation."""
    near_high = high_52w > 0 and (high_52w - close) / high_52w <= 0.03
    consolidation_break = atr_pct > 0 and range_20d_pct < atr_pct * 3
    return near_high or consolidation_break


# --- Gate ---

def passes_bullish_gate(
    *,
    close: float,
    ma_20: float,
    rsi: float,
    dollar_volume: float,
    min_dollar_volume: float,
) -> bool:
    """Hard gate: close > 20DMA, RSI > 40, dollar volume above floor."""
    return close > ma_20 and rsi > 40 and dollar_volume >= min_dollar_volume


# --- Composite score ---

INDICATOR_WEIGHTS = {
    "ma_alignment": 0.20,
    "slope": 0.10,
    "rsi": 0.15,
    "adx": 0.15,
    "macd": 0.10,
    "relative_strength": 0.10,
    "volume_profile": 0.10,
    "bbw": 0.10,
}
BREAKOUT_BONUS = 0.1


def _validate_ohlcv(indicators: dict) -> bool:
    """Check that required OHLCV fields are present."""
    required = ["close", "ma_20", "ma_50", "ma_200", "rsi", "adx", "macd", "macd_signal", "macd_histogram"]
    return all(k in indicators for k in required)


def compute_trend_score(indicators: dict) -> float:
    """Compute composite trend score from raw indicators.

    Required keys: close, ma_20, ma_50, ma_200, rsi, adx, macd, macd_signal, macd_histogram.
    Optional keys (with defaults): rs_vs_spy, ma_20_series, recent_avg_volume, avg_20d_volume,
    recent_up_ratio, bbw, high_52w, range_20d_pct, atr_pct.
    """
    if not _validate_ohlcv(indicators):
        return 0.0

    scores = {
        "ma_alignment": score_ma_alignment(
            close=indicators["close"],
            ma_20=indicators["ma_20"],
            ma_50=indicators["ma_50"],
            ma_200=indicators["ma_200"],
        ),
        "slope": score_slope(indicators.get("ma_20_series", [])),
        "rsi": score_rsi(indicators["rsi"]),
        "adx": score_adx(indicators["adx"]),
        "macd": score_macd(
            macd=indicators["macd"],
            signal=indicators["macd_signal"],
            histogram=indicators["macd_histogram"],
        ),
        "relative_strength": score_relative_strength(indicators.get("rs_vs_spy", 1.0)),
        "volume_profile": score_volume_profile(
            recent_avg_volume=indicators.get("recent_avg_volume", 0),
            avg_20d_volume=indicators.get("avg_20d_volume", 1),
            recent_up_ratio=indicators.get("recent_up_ratio", 0.5),
        ),
        "bbw": score_bbw(indicators.get("bbw", 0.10)),
    }

    composite = sum(scores[k] * w for k, w in INDICATOR_WEIGHTS.items())

    if detect_breakout(
        close=indicators["close"],
        high_52w=indicators.get("high_52w", 0),
        range_20d_pct=indicators.get("range_20d_pct", 1.0),
        atr_pct=indicators.get("atr_pct", 0),
    ):
        composite += BREAKOUT_BONUS

    return normalize_score(composite)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_ta_prefilter.py -v`
Expected: All 27 tests pass

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/stages/ta_prefilter.py scripts/tests/test_ta_prefilter.py
git commit -m "feat(trend_scan_lib): add Stage A TA prefilter with 9 indicators and breakout detection"
```

---

### Task 6: Run Full Test Suite

- [ ] **Step 1: Run all trend_scan_lib tests**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_trend_*.py scripts/tests/test_ta_*.py -v`
Expected: All pass (3 models + 4 config + 7 universe + 27 TA = 41 tests)

- [ ] **Step 2: Run all scanner_lib tests**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib*.py -v`
Expected: All 24 pass

- [ ] **Step 3: Run uw_scan tests (regression check)**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_uw_scan*.py -v`
Expected: All pass, no regressions
