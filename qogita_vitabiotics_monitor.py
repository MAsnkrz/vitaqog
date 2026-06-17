"""
Qogita Maybelline Monitor — API-based
Uses the Qogita internal API (https://api.qogita.com) instead of browser scraping.

Workflow:
  1. Authenticate via POST /auth/login/ to get a JWT token
  2. Fetch all Maybelline variants via GET /variants/?brand_slug=maybelline
  3. For each variant, fetch offers via GET /variants/{qid}/offers/
  4. Pick the lowest unit price offer
  5. Compare against snapshot and fire Discord alerts

Tracks:
  - New product listings
  - Price drops / increases (lowest unit price across all offers)
  - Restocks / stock drops
  - Out of stock / back in stock

Deps: pip install requests
"""

import json
import os
import re
import time
import random
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

API_BASE        = "https://api.qogita.com"
BRAND_URL       = "https://www.qogita.com/brands/vitabiotics/"
SNAPSHOT_FILE   = "snapshot_qogita_vitabiotics.json"
REQUEST_DELAY   = 1.0   # seconds between API calls (be polite)
RUN_ONCE        = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "1800"))  # 30 min

QOGITA_EMAIL    = os.getenv("QOGITA_EMAIL",    "")
QOGITA_PASSWORD = os.getenv("QOGITA_PASSWORD", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

# Discord colours
COLOUR_NEW        = 0xE91E8C
COLOUR_PRICE_DROP = 0x2ECC71
COLOUR_PRICE_UP   = 0xE74C3C
COLOUR_RESTOCK    = 0x3498DB
COLOUR_LOW_STOCK  = 0xF39C12
COLOUR_OOS        = 0x95A5A6
COLOUR_BACK       = 0x9B59B6

# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

_token_cache = {"token": None, "expires": 0}


def get_token():
    """Get a valid JWT token, refreshing if needed."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires"]:
        return _token_cache["token"]

    print("  Authenticating with Qogita API...")
    r = requests.post(
        f"{API_BASE}/auth/login/",
        json={"email": QOGITA_EMAIL, "password": QOGITA_PASSWORD},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("accessToken") or data.get("access")
    if not token:
        raise ValueError(f"No token in response: {data}")

    _token_cache["token"]   = token
    _token_cache["expires"] = now + 3300  # refresh every 55 mins
    print("  Authenticated successfully")
    return token


def auth_headers():
    return {"Authorization": f"Bearer {get_token()}"}


# ---------------------------------------------------------------------------
# API HELPERS
# ---------------------------------------------------------------------------

def api_get(path, params=None, retries=3):
    """GET from the Qogita API with auth and retry logic."""
    url = f"{API_BASE}{path}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=auth_headers(), params=params, timeout=20)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                print(f"  [!] Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 401:
                # Token expired — force refresh
                _token_cache["token"] = None
                r = requests.get(url, headers=auth_headers(), params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  [!] API error ({path}): {e} — attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(4 * (attempt + 1))
    return None


def paginate(path, params=None):
    """Fetch all pages from a paginated API endpoint."""
    params = params or {}
    params["page_size"] = 100
    page = 1
    all_results = []

    while True:
        params["page"] = page
        data = api_get(path, params=params.copy())
        if not data:
            break

        results = data.get("results", data if isinstance(data, list) else [])
        all_results.extend(results)

        # Check for next page
        if not data.get("next"):
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    return all_results


# ---------------------------------------------------------------------------
# FETCH MAYBELLINE VARIANTS
# ---------------------------------------------------------------------------

def fetch_maybelline_variants_from_page(context):
    """
    Scrape the Maybelline brand page to get all product QIDs and slugs.
    Uses Playwright since the page is SSR (no XHR API call for product listing).
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from bs4 import BeautifulSoup

    print("  Scraping Vitabiotics brand pages for product list...")
    all_products = []
    page_num = 1

    while True:
        url = f"{BRAND_URL}?page={page_num}" if page_num > 1 else BRAND_URL
        page = context.new_page()
        try:
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            time.sleep(2)
            html = page.content()
        except Exception as e:
            print(f"  [!] Page error ({url}): {e}")
            page.close()
            break
        finally:
            try: page.close()
            except: pass

        soup = BeautifulSoup(html, "html.parser")
        found = set()
        for a in soup.find_all("a", href=re.compile(r"/products/[A-Za-z0-9]+/")):
            m = re.search(r"/products/([A-Za-z0-9]+)/([^/?#]+)/?", a["href"])
            if not m: continue
            qid, slug = m.group(1), m.group(2)
            if qid in found: continue
            found.add(qid)
            title = a.get_text(strip=True) or slug.replace("-", " ").title()
            # Get image
            img = ""
            parent = a.find_parent("div") or a.find_parent("li")
            if parent:
                img_tag = parent.find("img")
                if img_tag:
                    src = img_tag.get("src") or img_tag.get("data-src") or ""
                    if "static.prod.qogita.com" in src:
                        img = src
            all_products.append({"qid": qid, "slug": slug, "title": title,
                                  "url": f"https://www.qogita.com/products/{qid}/{slug}/",
                                  "image": img})

        print(f"  Page {page_num}: {len(found)} products (total: {len(all_products)})")
        if not found: break

        # Check for next page link
        if not soup.find("a", href=re.compile(rf"[?&]page={page_num + 1}")):
            break
        page_num += 1
        time.sleep(REQUEST_DELAY + random.uniform(0, 1))

    return all_products


# ---------------------------------------------------------------------------
# FETCH OFFERS FOR A VARIANT
# ---------------------------------------------------------------------------

def fetch_variant_detail(qid):
    """
    Fetch variant detail from /variants/{qid}/offers/ which returns:
    price, inventory, gtin, images, isInStock, sellerCount etc.
    Thread-safe.
    """
    url = f"{API_BASE}/variants/{qid}/offers/"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=auth_headers(), timeout=15)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                time.sleep(wait)
                continue
            if r.status_code == 401:
                _token_cache["token"] = None
                r = requests.get(url, headers=auth_headers(), timeout=15)
            if r.status_code in (404, 403):
                return None
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt < 2:
                time.sleep(2)
    return None

    offers = data if isinstance(data, list) else data.get("results", [])

    parsed = []
    for o in offers:
        try:
            parsed.append({
                "supplier":   o.get("supplierName") or o.get("supplier") or o.get("sellerName", ""),
                "unit_price": float(o.get("unitPrice") or o.get("price") or o.get("unit_price", 0)),
                "mov":        float(o.get("mov") or o.get("minimumOrderValue") or o.get("minimum_order_value", 0)),
                "stock":      int(o.get("quantity") or o.get("stock") or o.get("availableQuantity", 0)),
                "bundle":     int(o.get("bundleSize") or o.get("bundle_size") or 1),
            })
        except (TypeError, ValueError):
            continue

    # Sort by lowest unit price, then lowest MOV as tiebreaker
    return sorted(parsed, key=lambda o: (o["unit_price"], o["mov"]))


def parse_variant(item, detail=None):
    """
    Build our product dict from the brand page scrape (item)
    enriched with the variant detail API response (detail).

    detail = response from /variants/{qid}/offers/ which returns:
      price, inventory, gtin, images, isInStock, sellerCount, fid
    """
    # QID — from brand page scrape (fid) or detail (fid/qid)
    qid  = str(item.get("qid") or "")
    name = item.get("title") or item.get("name") or ""
    slug = item.get("slug") or ""

    # Start with what we know from the brand page
    product = {
        "qid":        qid,
        "slug":       slug,
        "title":      name,
        "url":        item.get("url") or f"https://www.qogita.com/products/{qid}/{slug}/",
        "image":      item.get("image") or "",
        "barcode":    "",
        "price":      "",
        "stock":      None,
        "in_stock":   False,
        "seller_count": 0,
    }

    if detail:
        # Enrich with API detail
        product["title"]    = detail.get("name") or name
        product["barcode"]  = str(detail.get("gtin") or "")
        product["price"]    = str(detail.get("price") or "")
        product["stock"]    = detail.get("inventory")
        product["in_stock"] = bool(detail.get("isInStock", False))
        product["seller_count"] = int(detail.get("sellerCount") or 0)

        # Image from detail (higher quality)
        images = detail.get("images") or []
        if images:
            product["image"] = images[0].get("url") or product["image"]

        # Use fid as the QID if available
        fid = detail.get("fid") or detail.get("qid")
        if fid:
            product["qid"] = str(fid)
            product["url"] = f"https://www.qogita.com/products/{fid}/{slug}/"

    return product


# ---------------------------------------------------------------------------
# PRICING HELPERS
# ---------------------------------------------------------------------------

def vat_price(price_str):
    try:
        return f"{float(price_str) * 1.2:.2f}"
    except (ValueError, TypeError):
        return price_str


def selleramp_url(barcode, cost_price_str):
    if not barcode:
        return None
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={vat_price(cost_price_str)}"
    )


def safe_float(val):
    try:
        return float(str(val).replace(",", ""))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# DISCORD EMBEDS
# ---------------------------------------------------------------------------

def _base_fields(product):
    barcode      = product.get("barcode", "")
    stock        = product.get("stock")
    in_stock     = product.get("in_stock", True)
    seller_count = product.get("seller_count", 0)
    price        = product.get("price", "")
    sas_url      = selleramp_url(barcode, price)

    if stock is not None:
        stock_val = f"**{stock:,} units**"
    elif in_stock:
        stock_val = "✅ In stock"
    else:
        stock_val = "❌ Out of stock"

    fields = [
        {"name": "🔢 GTIN / EAN",   "value": f"`{barcode}`" if barcode else "-",      "inline": True},
        {"name": "📊 Stock",         "value": stock_val,                                "inline": True},
        {"name": "🏭 Sellers",       "value": f"{seller_count}" if seller_count else "-", "inline": True},
    ]
    if sas_url:
        fields.append({
            "name":   "🔍 SellerAmp SAS",
            "value":  f"[Open in SellerAmp]({sas_url})",
            "inline": False,
        })
    return fields


def _send_embed(embed):
    payload = {"embeds": [embed]}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _thumbnail(product):
    image = product.get("image", "")
    return {"url": image} if image else None


def notify_new(product):
    price = product.get("price", "")
    fields = [
        {"name": "💰 Lowest Unit Price", "value": f"**£{price}**" if price else "-",   "inline": True},
        {"name": "💷 Price (inc. VAT)",  "value": f"£{vat_price(price)}" if price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🆕  NEW LISTING — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     COLOUR_NEW,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Vitabiotics Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: NEW — {product.get('title', '')[:60]}")


def notify_price_change(product, old_price, new_price, is_drop):
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct   = f"{abs((new_f - old_f) / old_f * 100):.1f}%" if old_f and new_f else "?"

    fields = [
        {"name": "💰 Old Price", "value": f"£{old_price}",                                          "inline": True},
        {"name": "💰 New Price", "value": f"**£{new_price}**",                                      "inline": True},
        {"name": "📉 Change",    "value": f"{'↓' if is_drop else '↑'} {diff} ({pct})",              "inline": True},
        {"name": "💷 New Price (inc. VAT)", "value": f"£{vat_price(new_price)}" if new_price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{'💰  PRICE DROP' if is_drop else '📈  PRICE INCREASE'} — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     COLOUR_PRICE_DROP if is_drop else COLOUR_PRICE_UP,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Vitabiotics Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: PRICE {'DROP' if is_drop else 'UP'} — {product.get('title', '')[:50]}")


def notify_stock_change(product, old_stock, new_stock, is_restock):
    price = product.get("price", "")
    diff  = abs(new_stock - old_stock) if (new_stock is not None and old_stock is not None) else "?"
    fields = [
        {"name": "💰 Lowest Unit Price", "value": f"**£{price}**" if price else "-",        "inline": True},
        {"name": "💷 Price (inc. VAT)",  "value": f"£{vat_price(price)}" if price else "-", "inline": True},
        {"name": "📊 Old Stock", "value": f"{old_stock:,} units" if isinstance(old_stock, int) else "-", "inline": True},
        {"name": "📊 New Stock", "value": f"**{new_stock:,} units**" if isinstance(new_stock, int) else "-", "inline": True},
        {"name": "📉 Change",    "value": f"{'↑ +' if is_restock else '↓ -'}{diff:,}" if isinstance(diff, int) else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{'🟢  RESTOCK' if is_restock else '📉  STOCK DROP'} — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     COLOUR_RESTOCK if is_restock else COLOUR_LOW_STOCK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Vitabiotics Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)


def notify_oos(product):
    embed = {
        "title":     f"🔴  OUT OF STOCK — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     COLOUR_OOS,
        "fields":    _base_fields(product),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Vitabiotics Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: OOS — {product.get('title', '')[:60]}")


def notify_back_in_stock(product):
    price = product.get("price", "")
    fields = [
        {"name": "💰 Lowest Unit Price", "value": f"**£{price}**" if price else "-",       "inline": True},
        {"name": "💷 Price (inc. VAT)",  "value": f"£{vat_price(price)}" if price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  BACK IN STOCK — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     COLOUR_BACK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Vitabiotics Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: BACK IN STOCK — {product.get('title', '')[:60]}")


# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    return {}


def save_snapshot(data):
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def snapshot_entry(product):
    return {
        "title":       product.get("title", ""),
        "url":         product.get("url", ""),
        "image":       product.get("image", ""),
        "barcode":     product.get("barcode", ""),
        "price":       product.get("price", ""),
        "stock":       product.get("stock"),
        "in_stock":    product.get("in_stock", True),
        "seller_count": product.get("seller_count", 0),
        "first_seen":  product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# CHANGE DETECTION
# ---------------------------------------------------------------------------

def check_changes(product, old):
    old_price    = old.get("price", "")
    new_price    = product.get("price", "")
    old_stock    = old.get("stock")
    new_stock    = product.get("stock")
    was_in_stock = old.get("in_stock", True)
    now_in_stock = product.get("in_stock", True)

    for key in ("image", "barcode"):
        if not product.get(key):
            product[key] = old.get(key, "")

    old_f = safe_float(old_price)
    new_f = safe_float(new_price)

    if not was_in_stock and now_in_stock:
        notify_back_in_stock(product)
        time.sleep(1)
    elif was_in_stock and not now_in_stock:
        # Silently record OOS — no Discord alert
        pass
    elif old_f and new_f and new_f < old_f - 0.01:
        notify_price_change(product, old_price, new_price, is_drop=True)
        time.sleep(1)
    elif old_f and new_f and new_f > old_f + 0.01:
        notify_price_change(product, old_price, new_price, is_drop=False)
        time.sleep(1)

    if old_stock is not None and new_stock is not None and now_in_stock:
        threshold = max(50, int(old_stock * 0.05))
        if new_stock > old_stock + threshold:
            notify_stock_change(product, old_stock, new_stock, is_restock=True)
            time.sleep(1)
        elif new_stock < old_stock - threshold:
            notify_stock_change(product, old_stock, new_stock, is_restock=False)
            time.sleep(1)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_check():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Checking Qogita Vitabiotics...")

    snapshot     = load_snapshot()
    known_qids   = set(snapshot.keys())
    # Use a flag file to mark baseline as complete — avoids re-alerting
    # if snapshot partially saved on first run
    baseline_done = os.path.exists("baseline_done_vitabiotics.txt")
    is_first_run  = not baseline_done

    # 1. Scrape brand page for product list (SSR, needs browser)
    #    Then use API for offers per product (needs auth token)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        try:
            variants = fetch_maybelline_variants_from_page(context)
        finally:
            browser.close()

    if not variants:
        print("  [!] No variants found on brand page")
        return

    current_qids = {str(v.get("qid") or v.get("id", "")) for v in variants}
    new_qids     = current_qids - known_qids
    print(f"  {len(variants)} variants, {len(new_qids)} new")

    # 2. Fetch all offers in parallel (10 concurrent workers)
    print(f"  Fetching offers for {len(variants)} products (10 parallel workers)...")

    def fetch_and_parse(item):
        qid = str(item.get("qid") or "")
        if not qid:
            return None
        detail = fetch_variant_detail(qid)
        if detail is None:
            return None
        product = parse_variant(item, detail)
        return product

    products_with_offers = []
    completed = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_and_parse, item): item for item in variants}
        for future in as_completed(futures):
            try:
                product = future.result()
                if product:
                    products_with_offers.append(product)
            except Exception as e:
                print(f"  [!] Error fetching offers: {e}")
            completed += 1
            if completed % 100 == 0:
                print(f"  [{completed}/{len(variants)}] offers fetched...")
                save_snapshot(snapshot)

    print(f"  All offers fetched — processing {len(products_with_offers)} products...")

    # 3. Process results
    for product in products_with_offers:
        qid = product.get("qid", "")
        if not qid:
            continue

        if is_first_run:
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[qid] = entry
        elif qid in new_qids:
            # Skip new listing alert if product is out of stock
            if not product.get("in_stock") or not product.get("stock"):
                pass
            else:
                print(f"  -> NEW: {product['title'][:60]}")
                notify_new(product)
                time.sleep(1.5)
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[qid] = entry
        else:
            check_changes(product, snapshot[qid])
            entry = snapshot_entry(product)
            entry["first_seen"] = snapshot[qid].get("first_seen", entry["first_seen"])
            snapshot[qid] = entry

    save_snapshot(snapshot)
    if is_first_run:
        # Mark baseline as done so subsequent runs fire alerts
        with open("baseline_done_vitabiotics.txt", "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline complete — {len(snapshot)} products. No alerts sent.")
    else:
        print(f"  Done — {len(snapshot)} products tracked")


def main():
    print("=" * 55)
    print("  Qogita Vitabiotics Monitor (API-based)")
    print(f"  Brand: {BRAND_URL}")
    print("  Tracking: new listings, price drops, restocks")
    print("=" * 55)

    if not QOGITA_EMAIL or not QOGITA_PASSWORD:
        print("  [!] QOGITA_EMAIL and QOGITA_PASSWORD must be set")
        return
    if not DISCORD_WEBHOOK:
        print("  [!] DISCORD_WEBHOOK must be set")
        return

    if RUN_ONCE:
        run_check()
    else:
        while True:
            try:
                run_check()
            except Exception as e:
                print(f"  [!] Unexpected error: {e}")
            print(f"  Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
