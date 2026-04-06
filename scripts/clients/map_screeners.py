#!/usr/bin/env python3
"""Discover all screener sub-slugs and test ticker tab clicking."""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), "../.."))
from clients.menthorq_client import MenthorQClient

JS_SCREENER_SLUGS = """() => {
    var links = document.querySelectorAll("a[href*='slug=']");
    var slugs = [];
    for (var i = 0; i < links.length; i++) {
        var href = links[i].href;
        var text = links[i].textContent.trim().replace(/\\s+/g, " ").substring(0, 60);
        var match = href.match(/slug=([^&]+)/);
        if (match) slugs.push({slug: match[1], text: text, href: href.substring(0, 200)});
    }
    return slugs;
}"""

client = MenthorQClient(headless=True)

# 1. Discover all screener sub-slugs by visiting each category
categories = ["gamma", "gamma_levels", "open_interest", "volatility", "volume", "qscore"]
for cat in categories:
    client._navigate({"action": "data", "type": "screeners", "category": cat})
    time.sleep(3)
    slugs = client._page.evaluate(JS_SCREENER_SLUGS)
    cat_slugs = [s for s in slugs if cat in s.get("href", "")]
    print(f"\n{cat:20s} ({len(cat_slugs)} slugs):")
    for s in cat_slugs:
        print(f"  {s['slug']:30s} | {s['text']}")

# 2. Test clicking a ticker tab on EOD page
print("\n\n=== TICKER TAB CLICK TEST (EOD) ===")
client._navigate({"action": "data", "type": "dashboard", "commands": "eod", "tickers": "commons"})
time.sleep(5)

# Current ticker
current = client._page.evaluate('() => { var s = document.querySelector(".ticker-item.selected"); return s ? s.getAttribute("data-ticker") : null; }')
print(f"Default selected: {current}")

# Click NVDA tab
client._page.evaluate('() => { var t = document.querySelector("[data-ticker=\\"nvda\\"]"); if (t) t.click(); }')
time.sleep(4)

# Check what changed
after = client._page.evaluate("""() => {
    var selected = document.querySelector(".ticker-item.selected");
    var selTicker = selected ? selected.getAttribute("data-ticker") : null;
    var cards = document.querySelectorAll(".command-card");
    var firstImg = null;
    for (var i = 0; i < cards.length; i++) {
        var img = cards[i].querySelector("img");
        if (img && img.src.indexOf("s3") !== -1) {
            firstImg = img.src.substring(0, 120);
            break;
        }
    }
    return {selectedTicker: selTicker, cardCount: cards.length, firstS3Img: firstImg};
}""")
print(f"After click NVDA: {json.dumps(after, indent=2)}")

# 3. Check the summary pages 
print("\n\n=== SUMMARY PAGES ===")
for cat in ["futures", "cryptos"]:
    client._navigate({"action": "data", "type": "summary", "category": cat})
    time.sleep(4)
    result = client._page.evaluate("""() => {
        var tables = document.querySelectorAll("table");
        var rowCount = 0;
        var headers = [];
        if (tables.length > 0) {
            var rows = tables[0].querySelectorAll("tbody tr, tr");
            rowCount = rows.length;
            var ths = tables[0].querySelectorAll("th, thead td");
            for (var i = 0; i < ths.length; i++) headers.push(ths[i].textContent.trim().substring(0, 20));
        }
        return {tableCount: tables.length, rowCount: rowCount, headers: headers.slice(0, 10)};
    }""")
    print(f"{cat:10s}: {json.dumps(result)}")

client.close()
