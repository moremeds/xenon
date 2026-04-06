#!/usr/bin/env python3
"""Check sub-navigation (ticker tabs, selectors) on each dashboard page."""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), "../.."))
from clients.menthorq_client import MenthorQClient

JS_SUBNAV = """() => {
    var tabs = document.querySelectorAll("[data-ticker], .ticker-item");
    var tabInfo = [];
    for (var i = 0; i < tabs.length; i++) {
        tabInfo.push({
            text: tabs[i].textContent.trim().replace(/\\s+/g, " ").substring(0, 20),
            ticker: tabs[i].getAttribute("data-ticker"),
            selected: tabs[i].className.indexOf("selected") !== -1
        });
    }
    
    var cards = document.querySelectorAll(".command-card");
    var cardSlugs = [];
    for (var j = 0; j < cards.length; j++) {
        cardSlugs.push(cards[j].getAttribute("data-command-slug") || "(none)");
    }
    
    // Also check for summary-type pages (type=summary)
    var tables = document.querySelectorAll("table");
    var tableCount = tables.length;
    
    return {
        tickerTabs: tabInfo,
        cardCount: cards.length,
        cardSlugs: cardSlugs,
        tableCount: tableCount
    };
}"""

client = MenthorQClient(headless=True)

routes = [
    {"label": "EOD", "p": {"action": "data", "type": "dashboard", "commands": "eod", "tickers": "commons"}},
    {"label": "Intraday", "p": {"action": "data", "type": "dashboard", "commands": "intraday", "tickers": "commons"}},
    {"label": "Active Futures (summary)", "p": {"action": "data", "type": "summary", "category": "futures"}},
    {"label": "Futures", "p": {"action": "data", "type": "dashboard", "commands": "futures", "tickers": "futures"}},
    {"label": "CTA", "p": {"action": "data", "type": "dashboard", "commands": "cta"}},
    {"label": "Vol Models", "p": {"action": "data", "type": "dashboard", "commands": "vol"}},
    {"label": "Forex Levels", "p": {"action": "data", "type": "dashboard", "commands": "forex"}},
    {"label": "Crypto Summary", "p": {"action": "data", "type": "summary", "category": "cryptos"}},
    {"label": "Crypto Quant", "p": {"action": "data", "type": "dashboard", "commands": "cryptos_technical", "tickers": "cryptos_technical"}},
    {"label": "Crypto Options", "p": {"action": "data", "type": "dashboard", "commands": "cryptos_options", "tickers": "cryptos_options"}},
    {"label": "Screener: Gamma", "p": {"action": "data", "type": "screeners", "category": "gamma"}},
]

for r in routes:
    client._navigate(r["p"])
    time.sleep(4)
    result = client._page.evaluate(JS_SUBNAV)
    tickers = [t["ticker"] for t in result["tickerTabs"]]
    selected = [t["ticker"] for t in result["tickerTabs"] if t["selected"]]
    print(f"\n{r['label']:25s} | tabs={len(tickers):2d} sel={selected} cards={result['cardCount']:2d} tables={result['tableCount']:2d}")
    if tickers:
        print(f"{'':25s} | tickers: {', '.join(tickers)}")
    if result["cardSlugs"]:
        print(f"{'':25s} | slugs: {', '.join(result['cardSlugs'])}")

client.close()
