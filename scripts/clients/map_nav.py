#!/usr/bin/env python3
"""Map the complete MenthorQ navigation tree."""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), "../.."))
from clients.menthorq_client import MenthorQClient

JS_NAV = """() => {
    var sidebar = document.querySelector(".sidebar-menu, .sidebar, nav, .nav-menu");
    var allLinks = document.querySelectorAll("a.item, .sidebar a, nav a");
    var results = [];
    for (var i = 0; i < allLinks.length; i++) {
        var a = allLinks[i];
        var href = a.href || "";
        var text = a.textContent.trim().replace(/\\s+/g, " ").substring(0, 80);
        var parent = a.parentElement;
        var section = "";
        // Walk up to find section header
        var el = parent;
        for (var j = 0; j < 5; j++) {
            if (!el) break;
            var h = el.querySelector(".section-title, .menu-title, h3, h4, .title");
            if (h && h.textContent.trim()) {
                section = h.textContent.trim().substring(0, 40);
                break;
            }
            el = el.parentElement;
        }
        if (href.indexOf("menthorq.com/account") !== -1) {
            results.push({
                section: section,
                text: text,
                href: href
            });
        }
    }
    return results;
}"""

client = MenthorQClient(headless=True)

# Start at the main dashboard to get full sidebar
client._page.goto("https://menthorq.com/account/?action=data&type=dashboard", 
                   wait_until="domcontentloaded", timeout=60000)
time.sleep(5)

nav = client._page.evaluate(JS_NAV)
print(json.dumps(nav, indent=2))

# Now also check: for the EOD page, does it have sub-navigation (ticker selector)?
client._navigate({"action": "data", "type": "dashboard", "commands": "eod", "tickers": "commons"})
time.sleep(5)

# Check for ticker selectors / sub-nav on EOD page
subnav = client._page.evaluate("""() => {
    // Look for ticker tabs, dropdowns, or sub-navigation
    var tabs = document.querySelectorAll(".tab, .ticker-tab, [data-ticker], .ticker-selector a, .ticker-list a, .ticker-item");
    var tabInfo = [];
    for (var i = 0; i < tabs.length; i++) {
        var t = tabs[i];
        tabInfo.push({
            tag: t.tagName,
            text: t.textContent.trim().substring(0, 40),
            href: (t.href || "").substring(0, 200),
            dataTicker: t.getAttribute("data-ticker") || null,
            cls: (t.className || "").substring(0, 60)
        });
    }
    
    // Look for select/dropdown elements
    var selects = document.querySelectorAll("select");
    var selectInfo = [];
    for (var j = 0; j < selects.length; j++) {
        var s = selects[j];
        var opts = s.querySelectorAll("option");
        var optTexts = [];
        for (var k = 0; k < Math.min(opts.length, 10); k++) {
            optTexts.push(opts[k].textContent.trim());
        }
        selectInfo.push({
            name: s.name || s.id || "(unnamed)",
            optionCount: opts.length,
            firstOptions: optTexts
        });
    }
    
    // Look for links that change the ticker context
    var tickerLinks = document.querySelectorAll("a[href*='ticker='], a[href*='tickers=']");
    var tlInfo = [];
    for (var m = 0; m < tickerLinks.length; m++) {
        tlInfo.push({
            text: tickerLinks[m].textContent.trim().substring(0, 40),
            href: tickerLinks[m].href.substring(0, 200)
        });
    }
    
    return {tabs: tabInfo.slice(0, 20), selects: selectInfo, tickerLinks: tlInfo.slice(0, 20)};
}""")

print("\\n=== EOD SUBNAV ===")
print(json.dumps(subnav, indent=2))

client.close()
