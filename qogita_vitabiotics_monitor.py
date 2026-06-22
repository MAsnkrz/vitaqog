"""
Qogita Vitabiotics Monitor — Catalog Download API version
Monitors https://www.qogita.com/brands/vitabiotics/

Uses Qogita's official public Buyer API endpoint:
  GET /variants/search/download/?brand_name=Vitabiotics
This single authenticated request returns the ENTIRE brand catalogue as
a CSV in one response — no pagination, no Playwright, no per-product
API looping. A first run that used to take 30+ minutes now takes seconds.

CSV columns returned by this endpoint:
  GTIN, Name, Category, Brand, £ Lowest Price inc. shipping, Unit,
  Lowest Priced Offer Inventory, Is a pre-order?,
  Estimated Delivery Time (weeks), Number of Offers,
  Total Inventory of All Offers, Product URL, Image URL

Tracks (Discord alerts fire ONLY for these):
  - New product listings (in stock only)
  - Price drops (decreased >1% and >£0.02)
  - Back in stock (was OOS, now available)

Does NOT alert on: price increases, stock fluctuations, going OOS.

Deps: pip install requests
"""

import csv
import io
import json
import os
import re
import time
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

API_BASE        = "https://api.qogita.com"
BRAND_NAME       = "Vitabiotics"       # adjust if fallback attempts below are needed
BRAND_NAME_FALLBACKS = ["VITABIOTICS", "Vita Biotics"]
BRAND_PAGE_URL   = "https://www.qogita.com/brands/vitabiotics/"
SNAPSHOT_FILE    = "snapshot_qogita_vitabiotics.json"
BASELINE_FLAG    = "baseline_done_vitabiotics.txt"
RUN_ONCE         = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "1800"))  # 30 min

QOGITA_EMAIL    = os.getenv("QOGITA_EMAIL",    "")
QOGITA_PASSWORD = os.getenv("QOGITA_PASSWORD", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

# Discord colours
COLOUR_NEW  = 0xE91E8C   # pink — new listing
COLOUR_BACK = 0x9B59B6   # purple — back in stock
# Price drop colours are tiered by severity — see notify_price_change()

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
# CATALOG DOWNLOAD — single request for the whole brand catalogue
# ---------------------------------------------------------------------------

def _safe_int(val):
    try:
        return int(float(str(val).replace(",", "")))
    except (TypeError, ValueError):
        return None


def fetch_brand_catalog(brand_name, retries=4):
    """
    Fetch the full brand catalogue in one request via the CSV download
    endpoint. Returns a list of parsed product dicts, or [] on failure
    (including if the endpoint is still rate limited after retrying).

    This endpoint generates a full CSV server-side on every call and
    appears to carry a stricter rate limit than other Qogita endpoints —
    we respect Retry-After and back off rather than crashing the job.
    """
    url = f"{API_BASE}/variants/search/download/"
    last_status = None

    for attempt in range(retries):
        r = requests.get(url, headers=auth_headers(), params={"brand_name": brand_name}, timeout=60)
        last_status = r.status_code

        if r.status_code == 401:
            _token_cache["token"] = None
            r = requests.get(url, headers=auth_headers(), params={"brand_name": brand_name}, timeout=60)
            last_status = r.status_code

        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 30 * (attempt + 1)))
            print(f"  [!] Rate limited (429) on catalog download — waiting {wait}s (attempt {attempt+1}/{retries})")
            time.sleep(wait)
            continue

        if not r.ok:
            print(f"  [!] Catalog download failed: HTTP {r.status_code}")
            return None  # request failure — distinct from a genuinely empty result

        text = r.content.decode("utf-8", errors="replace")
        break
    else:
        print(f"  [!] Catalog download still rate limited after {retries} attempts (last status {last_status}) — skipping this run")
        return None  # request failure — do not try fallback brand names on this
    reader = csv.DictReader(io.StringIO(text))

    products = []
    for row in reader:
        product_url = row.get("Product URL", "") or ""
        qid_m = re.search(r"/products/([A-Za-z0-9]+)/", product_url)
        gtin  = (row.get("GTIN", "") or "").strip()
        qid   = qid_m.group(1) if qid_m else gtin

        cheapest_stock = _safe_int(row.get("Lowest Priced Offer Inventory", ""))
        total_stock    = _safe_int(row.get("Total Inventory of All Offers", ""))
        num_offers     = _safe_int(row.get("Number of Offers", "")) or 0

        # In-stock = any inventory exists across any offer
        in_stock = (total_stock or 0) > 0

        products.append({
            "qid":            qid,
            "title":          row.get("Name", "") or "",
            "url":            product_url or f"https://www.qogita.com/products/{qid}/",
            "image":          row.get("Image URL", "") or "",
            "barcode":        gtin,
            "price":          (row.get("£ Lowest Price inc. shipping", "") or "").strip(),
            "bundle_size":    (row.get("Unit", "") or "").strip(),
            "cheapest_stock": cheapest_stock,
            "stock":          total_stock,
            "all_offers":     num_offers,
            "is_preorder":    (row.get("Is a pre-order?", "") or "").strip().lower() == "yes",
            "delivery_weeks": (row.get("Estimated Delivery Time (weeks)", "") or "").strip(),
            "in_stock":       in_stock,
        })

    return products


def fetch_brand_catalog_with_fallback():
    """
    Try the confirmed brand name first. Only fall back to alternate
    spellings if the call genuinely succeeded but returned zero rows
    (wrong name) — NOT if the call failed/was rate limited (None),
    since retrying with different names during a rate-limit window
    would just make things worse.
    """
    print(f"  Fetching catalog for brand_name='{BRAND_NAME}'...")
    products = fetch_brand_catalog(BRAND_NAME)

    if products is None:
        print("  [!] Request failed/rate limited — not trying fallback names this run")
        return []
    if products:
        print(f"  Got {len(products)} products")
        return products

    for alt in BRAND_NAME_FALLBACKS:
        print(f"  No results for '{BRAND_NAME}' — trying brand_name='{alt}'...")
        products = fetch_brand_catalog(alt)
        if products is None:
            print("  [!] Request failed/rate limited on fallback — stopping fallback attempts this run")
            return []
        if products:
            print(f"  Got {len(products)} products with '{alt}'")
            return products

    return []


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
    barcode        = product.get("barcode", "")
    stock          = product.get("stock")
    cheapest_stock = product.get("cheapest_stock")
    in_stock       = product.get("in_stock", True)
    all_offers     = product.get("all_offers", 0)
    bundle_size    = product.get("bundle_size", "")
    delivery_weeks = product.get("delivery_weeks", "")
    is_preorder    = product.get("is_preorder", False)
    price          = product.get("price", "")
    sas_url        = selleramp_url(barcode, price)

    if stock is not None:
        stock_val = f"**{stock:,} units**"
    elif in_stock:
        stock_val = "✅ In stock"
    else:
        stock_val = "❌ Out of stock"

    fields = [
        {"name": "🔢 GTIN / EAN",        "value": f"`{barcode}`" if barcode else "-",          "inline": True},
        {"name": "📊 Total Stock",        "value": stock_val,                                    "inline": True},
        {"name": "📊 Cheapest Offer Stock", "value": f"{cheapest_stock:,} units" if cheapest_stock is not None else "-", "inline": True},
        {"name": "🏭 Sellers",            "value": f"{all_offers}" if all_offers else "-",       "inline": True},
        {"name": "📦 Unit / Bundle",      "value": bundle_size if bundle_size else "-",          "inline": True},
        {"name": "🚚 Delivery",           "value": f"{delivery_weeks} wks" if delivery_weeks else "-", "inline": True},
    ]
    if is_preorder:
        fields.append({"name": "⏳ Pre-order", "value": "Yes", "inline": True})
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
        {"name": "💰 Lowest Price (incl. shipping)", "value": f"**£{price}**" if price else "-",   "inline": True},
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


def notify_price_change(product, old_price, new_price, pct_change):
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct_display = f"{pct_change * 100:.1f}%"

    if pct_change >= 0.20:
        colour = 0x00C853
        tier   = "🔥"
    elif pct_change >= 0.10:
        colour = 0x2ECC71
        tier   = "💰"
    else:
        colour = 0x82E0AA
        tier   = "💵"

    fields = [
        {"name": "💰 Old Price", "value": f"£{old_price}",     "inline": True},
        {"name": "💰 New Price", "value": f"**£{new_price}**", "inline": True},
        {"name": "📉 Drop",      "value": f"↓ {diff} (**{pct_display}**)", "inline": True},
        {"name": "💷 New Price (inc. VAT)", "value": f"£{vat_price(new_price)}" if new_price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{tier}  PRICE DROP -{pct_display} — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Vitabiotics Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: PRICE DROP -{pct_display} — {product.get('title', '')[:50]}")


def notify_back_in_stock(product):
    price = product.get("price", "")
    fields = [
        {"name": "💰 Lowest Price (incl. shipping)", "value": f"**£{price}**" if price else "-",       "inline": True},
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
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"  [!] Snapshot file is corrupted ({e}) — backing it up and starting fresh.")
            try:
                backup_name = f"{SNAPSHOT_FILE}.corrupted.{int(time.time())}"
                os.rename(SNAPSHOT_FILE, backup_name)
                print(f"  [!] Corrupted file saved as {backup_name}")
            except OSError as backup_err:
                print(f"  [!] Could not back up corrupted file: {backup_err}")
            return {}
    return {}


def save_snapshot(data):
    """Write atomically — write to a temp file then rename, so a crash
    mid-write never leaves a corrupted snapshot.json behind."""
    tmp_file = f"{SNAPSHOT_FILE}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_file, SNAPSHOT_FILE)


def snapshot_entry(product):
    return {
        "title":          product.get("title", ""),
        "url":            product.get("url", ""),
        "image":          product.get("image", ""),
        "barcode":        product.get("barcode", ""),
        "price":          product.get("price", ""),
        "stock":          product.get("stock"),
        "cheapest_stock": product.get("cheapest_stock"),
        "all_offers":     product.get("all_offers", 0),
        "bundle_size":    product.get("bundle_size", ""),
        "delivery_weeks": product.get("delivery_weeks", ""),
        "is_preorder":    product.get("is_preorder", False),
        "in_stock":       product.get("in_stock", True),
        "first_seen":     product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated":   datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# CHANGE DETECTION
# ---------------------------------------------------------------------------

def check_changes(product, old):
    """
    Only fires alerts for:
      - Back in stock (was OOS, now has stock)
      - Price drop (lowest unit price decreased by more than 1%
        AND more than £0.02 absolute, to avoid rounding noise)
    No alerts for: price increases, stock fluctuations, going OOS.
    """
    old_price    = old.get("price", "")
    new_price    = product.get("price", "")
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
        return

    if old_f and new_f and old_f > 0:
        pct_change = (old_f - new_f) / old_f
        abs_change = old_f - new_f
        if pct_change > 0.01 and abs_change > 0.02:
            notify_price_change(product, old_price, new_price, pct_change)
            time.sleep(1)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_check():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Checking Qogita Vitabiotics...")

    snapshot      = load_snapshot()
    known_qids    = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    products = fetch_brand_catalog_with_fallback()
    if not products:
        print("  [!] No products returned from catalog API")
        return

    current_qids = {p["qid"] for p in products if p.get("qid")}
    new_qids     = current_qids - known_qids

    if is_first_run:
        print(f"  First run — building baseline from {len(products)} products (no alerts)...")
    else:
        print(f"  {len(products)} products fetched, {len(new_qids)} new")

    for product in products:
        qid = product.get("qid")
        if not qid:
            continue

        if is_first_run:
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[qid] = entry
        elif qid in new_qids:
            if product.get("in_stock", True):
                print(f"  -> NEW: {product['title'][:60]}")
                notify_new(product)
                time.sleep(1.5)
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[qid] = entry
        else:
            old = snapshot[qid]
            check_changes(product, old)
            entry = snapshot_entry(product)
            entry["first_seen"] = old.get("first_seen", entry["first_seen"])
            snapshot[qid] = entry

    save_snapshot(snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline complete — {len(snapshot)} products. No alerts sent.")
    else:
        print(f"  Done — {len(snapshot)} products tracked")


def main():
    print("=" * 55)
    print("  Qogita Vitabiotics Monitor (Catalog Download API)")
    print(f"  Brand: {BRAND_PAGE_URL}")
    print("  Tracking: new listings, price drops, back in stock")
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
