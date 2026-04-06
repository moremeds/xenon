#!/usr/bin/env python3
"""Inspect MenthorQ dashboard page DOM structure to find chart containers."""
import sys
import os
import json
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), "../.."))

from clients.menthorq_client import MenthorQClient

JS_INSPECT = """() => {
    var selectors = [
        ".command-card",
        ".card-body",
        "canvas",
        ".main-container",
        "[class*=chart]",
        "[class*=card]",
        "[id*=chart]"
    ];

    var found = {};
    for (var i = 0; i < selectors.length; i++) {
        var sel = selectors[i];
        var els = document.querySelectorAll(sel);
        if (els.length > 0) {
            var first = els[0];
            found[sel] = {
                count: els.length,
                firstClasses: first.className || "",
                firstTag: first.tagName,
                firstSlug: first.getAttribute("data-command-slug") || null,
                hasImg: first.querySelector("img") !== null,
                hasCanvas: first.querySelector("canvas") !== null,
                boundingBox: {
                    w: first.getBoundingClientRect().width,
                    h: first.getBoundingClientRect().height
                }
            };
        }
    }

    // Count S3 images specifically
    var allImgs = document.querySelectorAll("img");
    var s3Imgs = [];
    for (var j = 0; j < allImgs.length; j++) {
        var src = allImgs[j].src || "";
        if (src.indexOf("s3.amazonaws") !== -1 || src.indexOf("mq-inf") !== -1) {
            s3Imgs.push(src.substring(0, 150));
        }
    }

    // Command card details
    var cards = document.querySelectorAll(".command-card");
    var cardDetails = [];
    for (var k = 0; k < cards.length; k++) {
        var card = cards[k];
        var slug = card.getAttribute("data-command-slug") || "(none)";
        var imgs = card.querySelectorAll("img");
        var imgSrcs = [];
        for (var m = 0; m < imgs.length; m++) {
            var s = imgs[m].src || "";
            imgSrcs.push(s.substring(0, 120));
        }
        cardDetails.push({slug: slug, imgCount: imgs.length, imgSrcs: imgSrcs});
    }

    return {
        found: found,
        s3ImageCount: s3Imgs.length,
        s3ImgSrcs: s3Imgs,
        commandCards: cardDetails
    };
}"""

client = MenthorQClient(headless=True)

for cmd in ["gex", "dix", "cta", "cta-flows", "vol-models"]:
    client._navigate({"action": "data", "type": "dashboard", "commands": cmd})
    time.sleep(5)

    result = client._page.evaluate(JS_INSPECT)
    print(f"\n{'='*60}")
    print(f"  {cmd.upper()}")
    print(f"{'='*60}")
    print(json.dumps(result, indent=2))

client.close()
