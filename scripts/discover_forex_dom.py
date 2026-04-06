#!/usr/bin/env python3
"""Discover the DOM structure of MenthorQ Forex Levels page.

Scrapes the forex dashboard page comprehensively to understand
what data structures (tables, cards, gamma data, blindspot data)
exist and how they're organized in the DOM.
"""
from __future__ import annotations

import json
import sys
import time

from clients.menthorq_client import MenthorQClient


def pp(label: str, data):
    """Pretty-print with a label."""
    print(f"\n{'='*80}")
    print(f"  {label}")
    print(f"{'='*80}")
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, default=str))
    else:
        print(data)


def scrape_page_structure(page) -> dict:
    """Comprehensive DOM scraping of the current page."""

    results = {}

    # ── 1. All tables: headers, row counts, sample data ──
    tables_info = page.evaluate("""() => {
        const tables = document.querySelectorAll('table');
        const info = [];
        let idx = 0;
        for (const table of tables) {
            idx++;
            const headers = [];
            const headerRow = table.querySelector('thead tr, tr:first-child');
            if (headerRow) {
                for (const th of headerRow.querySelectorAll('th, td')) {
                    headers.push(th.textContent.trim());
                }
            }
            const bodyRows = table.querySelectorAll('tbody tr');
            const allRows = bodyRows.length > 0 ? bodyRows : table.querySelectorAll('tr:not(:first-child)');
            const sampleRows = [];
            for (let i = 0; i < Math.min(3, allRows.length); i++) {
                const cells = allRows[i].querySelectorAll('td');
                const row = [];
                for (const cell of cells) {
                    row.push(cell.textContent.trim().substring(0, 100));
                }
                sampleRows.push(row);
            }
            // Get parent context
            let parent = table.parentElement;
            let parentInfo = '';
            if (parent) {
                parentInfo = parent.tagName + (parent.className ? '.' + parent.className.split(' ').join('.') : '') + (parent.id ? '#' + parent.id : '');
            }
            info.push({
                table_index: idx,
                parent: parentInfo,
                headers: headers,
                total_rows: allRows.length,
                sample_rows: sampleRows,
                table_classes: table.className || '',
                table_id: table.id || '',
            });
        }
        return info;
    }""")
    results['tables'] = tables_info

    # ── 2. All card elements (.command-card, etc.) ──
    cards_info = page.evaluate("""() => {
        const cards = [];
        // Multiple selectors for card-like elements
        const selectors = [
            '.command-card',
            '[data-command-slug]',
            '.card',
            '.dashboard-card',
            '.panel',
            '.widget',
        ];
        const seen = new Set();
        for (const sel of selectors) {
            const elements = document.querySelectorAll(sel);
            for (const el of elements) {
                const key = el.outerHTML.substring(0, 200);
                if (seen.has(key)) continue;
                seen.add(key);
                const title = el.querySelector('h1,h2,h3,h4,h5,h6,.card-title,.command-title,.title');
                const desc = el.querySelector('p,.description,.card-description');
                const img = el.querySelector('img');
                const slug = el.getAttribute('data-command-slug') || '';

                cards.push({
                    selector_matched: sel,
                    tag: el.tagName,
                    classes: el.className || '',
                    id: el.id || '',
                    slug: slug,
                    title: title ? title.textContent.trim() : '',
                    description: desc ? desc.textContent.trim().substring(0, 200) : '',
                    has_image: !!img,
                    image_src: img ? img.src.substring(0, 200) : '',
                    child_count: el.children.length,
                    text_preview: el.textContent.trim().substring(0, 300),
                });
            }
        }
        return cards;
    }""")
    results['cards'] = cards_info

    # ── 3. All links with slug= or category= params ──
    links_info = page.evaluate("""() => {
        const links = [];
        const allLinks = document.querySelectorAll('a[href]');
        for (const link of allLinks) {
            const href = link.href || '';
            if (href.includes('slug=') || href.includes('category=') || href.includes('type=forex') || href.includes('commands=')) {
                links.push({
                    text: link.textContent.trim().substring(0, 100),
                    href: href,
                    classes: link.className || '',
                    parent_tag: link.parentElement ? link.parentElement.tagName : '',
                });
            }
        }
        return links;
    }""")
    results['links'] = links_info

    # ── 4. All heading elements ──
    headings_info = page.evaluate("""() => {
        const headings = [];
        for (let i = 1; i <= 6; i++) {
            const els = document.querySelectorAll('h' + i);
            for (const el of els) {
                headings.push({
                    level: i,
                    text: el.textContent.trim().substring(0, 200),
                    classes: el.className || '',
                    id: el.id || '',
                    parent_classes: el.parentElement ? el.parentElement.className : '',
                });
            }
        }
        return headings;
    }""")
    results['headings'] = headings_info

    # ── 5. Elements with relevant class names ──
    relevant_elements = page.evaluate("""() => {
        const results = [];
        // Search for elements with relevant class names
        const keywords = ['gamma', 'blindspot', 'forex', 'level', 'gex', 'dex',
                         'support', 'resistance', 'strike', 'option', 'put', 'call',
                         'currency', 'pair', 'spot', 'pivot', 'key-level'];
        const allElements = document.querySelectorAll('*');
        const seen = new Set();
        for (const el of allElements) {
            const cls = (el.className || '').toString().toLowerCase();
            const id = (el.id || '').toLowerCase();
            const dataAttrs = [];
            for (const attr of el.attributes) {
                if (attr.name.startsWith('data-')) {
                    dataAttrs.push(attr.name + '=' + attr.value);
                }
            }
            const dataStr = dataAttrs.join(' ').toLowerCase();

            for (const kw of keywords) {
                if (cls.includes(kw) || id.includes(kw) || dataStr.includes(kw)) {
                    const key = el.tagName + '|' + cls + '|' + id;
                    if (seen.has(key)) break;
                    seen.add(key);
                    results.push({
                        tag: el.tagName,
                        classes: el.className || '',
                        id: el.id || '',
                        data_attrs: dataAttrs,
                        matched_keyword: kw,
                        text_preview: el.textContent.trim().substring(0, 300),
                        child_count: el.children.length,
                    });
                    break;
                }
            }
        }
        return results;
    }""")
    results['relevant_elements'] = relevant_elements

    # ── 6. Page structure (tag + class, first few levels) ──
    page_structure = page.evaluate("""() => {
        function getStructure(el, depth, maxDepth) {
            if (depth > maxDepth) return null;
            const children = [];
            for (const child of el.children) {
                // Skip script, style, svg
                if (['SCRIPT', 'STYLE', 'SVG', 'NOSCRIPT'].includes(child.tagName)) continue;
                const childInfo = getStructure(child, depth + 1, maxDepth);
                if (childInfo) children.push(childInfo);
            }
            return {
                tag: el.tagName,
                class: el.className ? el.className.toString().substring(0, 100) : '',
                id: el.id || '',
                childCount: el.children.length,
                children: children.length > 0 ? children : undefined,
            };
        }
        // Start from main content area, not the whole body
        const main = document.querySelector('#main-content, .main-content, main, .content-area, #content')
            || document.querySelector('.dashboard-content, .page-content, .site-main')
            || document.body;
        return getStructure(main, 0, 4);
    }""")
    results['page_structure'] = page_structure

    # ── 7. Tabular data in divs (repeating row patterns) ──
    div_tables = page.evaluate("""() => {
        const results = [];
        // Look for div-based table patterns
        const containers = document.querySelectorAll('.table-responsive, .data-table, .grid, [class*="table"], [class*="grid"], [class*="row-container"]');
        for (const container of containers) {
            const rows = container.querySelectorAll('[class*="row"], tr, li');
            if (rows.length >= 2) {
                const sampleRows = [];
                for (let i = 0; i < Math.min(3, rows.length); i++) {
                    sampleRows.push({
                        tag: rows[i].tagName,
                        classes: rows[i].className || '',
                        text: rows[i].textContent.trim().substring(0, 200),
                        child_count: rows[i].children.length,
                    });
                }
                results.push({
                    container_tag: container.tagName,
                    container_classes: container.className || '',
                    container_id: container.id || '',
                    row_count: rows.length,
                    sample_rows: sampleRows,
                });
            }
        }
        return results;
    }""")
    results['div_tables'] = div_tables

    # ── 8. All images on the page ──
    images_info = page.evaluate("""() => {
        const images = [];
        const imgs = document.querySelectorAll('img');
        for (const img of imgs) {
            images.push({
                src: img.src ? img.src.substring(0, 300) : '',
                alt: img.alt || '',
                width: img.naturalWidth || img.width || 0,
                height: img.naturalHeight || img.height || 0,
                classes: img.className || '',
                parent_classes: img.parentElement ? img.parentElement.className : '',
                parent_tag: img.parentElement ? img.parentElement.tagName : '',
            });
        }
        return images;
    }""")
    results['images'] = images_info

    # ── 9. Sidebar navigation items ──
    sidebar_info = page.evaluate("""() => {
        const items = [];
        // Look for sidebar/nav elements
        const navSelectors = [
            '.sidebar a', '.nav a', '.menu a', '.navigation a',
            '#sidebar a', 'aside a', '.side-nav a', '.menu-item a',
            '.wp-menu a', '.dashboard-sidebar a',
            // WordPress admin-style
            '.vertical-menu a', '.admin-menu a',
        ];
        const seen = new Set();
        for (const sel of navSelectors) {
            const links = document.querySelectorAll(sel);
            for (const link of links) {
                const href = link.href || '';
                const text = link.textContent.trim();
                if (!text || seen.has(text + href)) continue;
                seen.add(text + href);
                items.push({
                    text: text.substring(0, 100),
                    href: href,
                    classes: link.className || '',
                    parent_classes: link.parentElement ? link.parentElement.className : '',
                    is_active: link.classList.contains('active') || link.classList.contains('current'),
                });
            }
        }
        return items;
    }""")
    results['sidebar'] = sidebar_info

    # ── 10. Full body text content (truncated) for keyword analysis ──
    body_text = page.evaluate("""() => {
        // Get text content of the main area, excluding nav/footer
        const main = document.querySelector('#main-content, .main-content, main, .content-area')
            || document.querySelector('.dashboard-content, .page-content')
            || document.body;
        // Remove script/style content
        const clone = main.cloneNode(true);
        for (const el of clone.querySelectorAll('script, style, nav, footer, header')) {
            el.remove();
        }
        return clone.textContent.replace(/\\s+/g, ' ').trim().substring(0, 5000);
    }""")
    results['body_text_preview'] = body_text

    return results


def explore_page(client, label, params):
    """Navigate to a page and scrape its structure."""
    print(f"\n\n{'#'*80}")
    print(f"# EXPLORING: {label}")
    print(f"# URL params: {params}")
    print(f"{'#'*80}")

    try:
        page = client._navigate(params)
        # Extra wait for dynamic content
        time.sleep(3)

        results = scrape_page_structure(page)

        # Print the URL we're on
        pp("CURRENT URL", page.url)

        # Print results
        for key, value in results.items():
            if key == 'page_structure':
                pp(f"PAGE STRUCTURE (tag.class hierarchy, 4 levels)", value)
            elif key == 'body_text_preview':
                pp(f"BODY TEXT PREVIEW (first 5000 chars)", value)
            else:
                count = len(value) if isinstance(value, list) else 'N/A'
                pp(f"{key.upper()} (count: {count})", value)

        return results

    except Exception as exc:
        print(f"\n  ERROR: {exc}")
        return None


def main():
    print("=" * 80)
    print("  MenthorQ Forex DOM Discovery Script")
    print("=" * 80)

    with MenthorQClient(headless=True) as client:
        # ── Page 1: Forex Dashboard (commands=forex) ──
        explore_page(client, "FOREX DASHBOARD (commands=forex)", {
            "action": "data",
            "type": "dashboard",
            "commands": "forex",
        })

        # ── Page 2: Forex List (type=forex, commands=list) ──
        explore_page(client, "FOREX LIST (type=forex, commands=list)", {
            "action": "data",
            "type": "forex",
            "commands": "list",
        })

        # ── Page 3: Forex Detail for EURUSD ──
        explore_page(client, "FOREX DETAIL EURUSD (type=forex, commands=detail, ticker=EURUSD)", {
            "action": "data",
            "type": "forex",
            "commands": "detail",
            "ticker": "EURUSD",
        })

        # ── Page 4: Check if there's a forex gamma page ──
        explore_page(client, "FOREX GAMMA? (type=forex, commands=gamma)", {
            "action": "data",
            "type": "forex",
            "commands": "gamma",
        })

        # ── Page 5: Check if there's a forex levels page ──
        explore_page(client, "FOREX LEVELS? (type=forex, commands=levels)", {
            "action": "data",
            "type": "forex",
            "commands": "levels",
        })

        # ── Page 6: Check if there's a forex blindspot page ──
        explore_page(client, "FOREX BLINDSPOT? (type=forex, commands=blindspot)", {
            "action": "data",
            "type": "forex",
            "commands": "blindspot",
        })

        # ── Page 7: Try dashboard with forex and additional params ──
        explore_page(client, "FOREX DASHBOARD WITH TICKER=EURUSD", {
            "action": "data",
            "type": "dashboard",
            "commands": "forex",
            "ticker": "EURUSD",
        })

        # ── Page 8: Take a look at sidebar for forex-related links ──
        # Navigate back to dashboard forex and specifically check sidebar
        print(f"\n\n{'#'*80}")
        print(f"# EXPLORING: SIDEBAR FOREX LINKS (from dashboard)")
        print(f"{'#'*80}")

        page = client._navigate({
            "action": "data",
            "type": "dashboard",
            "commands": "forex",
        })
        time.sleep(3)

        # Deep sidebar scrape for forex
        sidebar_forex = page.evaluate("""() => {
            const results = [];
            // Get ALL links on the page
            const allLinks = document.querySelectorAll('a[href]');
            for (const link of allLinks) {
                const href = link.href || '';
                const text = link.textContent.trim();
                // Filter for forex-related links
                if (href.toLowerCase().includes('forex') ||
                    text.toLowerCase().includes('forex') ||
                    text.toLowerCase().includes('level') ||
                    text.toLowerCase().includes('gamma') ||
                    text.toLowerCase().includes('blindspot') ||
                    text.toLowerCase().includes('currency') ||
                    text.toLowerCase().includes('fx')) {
                    results.push({
                        text: text.substring(0, 200),
                        href: href,
                        classes: link.className || '',
                        parent_tag: link.parentElement ? link.parentElement.tagName : '',
                        parent_classes: link.parentElement ? link.parentElement.className : '',
                        grandparent_tag: link.parentElement && link.parentElement.parentElement ?
                            link.parentElement.parentElement.tagName : '',
                        grandparent_classes: link.parentElement && link.parentElement.parentElement ?
                            link.parentElement.parentElement.className : '',
                    });
                }
            }
            return results;
        }""")
        pp("SIDEBAR/NAV FOREX-RELATED LINKS", sidebar_forex)

        # ── Page 9: Full outer HTML of the main content area (first 20K chars) ──
        print(f"\n\n{'#'*80}")
        print(f"# EXPLORING: FULL MAIN CONTENT HTML DUMP (forex dashboard)")
        print(f"{'#'*80}")

        main_html = page.evaluate("""() => {
            const main = document.querySelector('#main-content, .main-content, main, .content-area')
                || document.querySelector('.dashboard-content, .page-content, .site-content')
                || document.body;
            return main.innerHTML.substring(0, 30000);
        }""")
        pp("MAIN CONTENT HTML (first 30K chars)", main_html)

    print("\n\n" + "=" * 80)
    print("  DISCOVERY COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
