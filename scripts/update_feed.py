#!/usr/bin/env python3
"""Daily price/stock feed builder for infrarot-fussboden.de (JTL-Shop 5).

Sources (no HTML scraping, 2 HTTP requests per run):
  1. /llms-full.txt   - official machine-readable catalog of the shop: SKU, name,
                        NET price, availability text, category, brand, description,
                        variants (with their own SKU / URL / NET price).
  2. /export/sitemap_0.xml.gz - per-URL lastmod + product image.

Safety: on any sanity-check failure the script exits non-zero BEFORE writing
anything, so the last good feed stays published.
"""
import csv
import difflib
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

BASE = "https://infrarot-fussboden.de"
LLMS_URL = BASE + "/llms-full.txt"
SITEMAP_INDEX = BASE + "/sitemap_index.xml"
UA = "Mozilla/5.0 (compatible; SupplierFeedBot/1.0; daily, 2 requests)"
CURRENCY = "EUR"
VAT_RATE = Decimal("0.19")

# ---- safety thresholds -------------------------------------------------------
MIN_PRODUCTS = 1500          # absolute floor (today: ~2200 sellable SKUs)
MAX_MISSING_PRICE_RATIO = 0.20
MAX_CATALOG_DROP_RATIO = 0.30
MAX_MISSING_URL_RATIO = 0.05
PLACEHOLDER_NET_PRICE = Decimal("40000")   # e.g. SKU 10-R is listed at 100000 EUR gross = "price on request" placeholder

OUT_DIR = os.environ.get("FEED_DIR", "feed")
CHANGELOG_DAYS = 180
FEED_FIELDS = [
    "sku", "name", "price_gross", "regular_price_gross", "on_sale", "discount_percent",
    "in_stock", "availability_text", "weight_kg", "url", "image_url", "last_modified",
    "currency", "price_net", "parent_sku", "category", "brand", "duplicate_sku",
    "duplicate_count", "price_note", "description_hash", "previous_price_gross", "price_changed_at",
    "first_seen", "last_change_detected",
]


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return data
        except Exception as e:  # noqa
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"fetch failed for {url}: {last}")


def d(s):
    """'1.234,56' -> Decimal('1234.56')"""
    s = s.strip().replace(".", "").replace(",", ".")
    return Decimal(s)


def q2(x):
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def norm_ws(s):
    return re.sub(r"\s+", " ", s).strip()


def sha(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


# ---- parsing -----------------------------------------------------------------
def parse_llms(text):
    if "\n## Artikel\n" not in text:
        raise RuntimeError("llms-full.txt: '## Artikel' section missing (format changed?)")
    start = text.index("\n## Artikel\n")
    end = text.find("\n## Blogs & News", start)
    art = text[start: end if end > 0 else len(text)]

    # SKU -> URL for every article (parents from category lists, children from variant lists)
    url_by_sku = {}
    for u, s in re.findall(r"\[Artikel: [^\]]*\]\((https://[^)]+)\): Artikelnummer: ([^ |\n]+)", text):
        url_by_sku.setdefault(s.strip(), u)

    blocks = re.split(r"\n### ", art)[1:]
    parents, products = [], []
    for b in blocks:
        name = b.split("\n", 1)[0].strip()
        m = re.search(r"^Artikelnummer: (.+)$", b, re.M)
        if not m:
            continue
        sku = m.group(1).strip()
        head, _, rest = b.partition("\nBeschreibung:")
        fields = dict(re.findall(r"^([A-Za-zÄÖÜäöüß]+): (.*)$", head, re.M))
        availability = fields.get("Verfuegbarkeit", "").strip()
        category = fields.get("Kategorie", "").strip()
        brand = fields.get("Hersteller", "").strip()
        price_net = None
        if fields.get("Preis"):
            mp = re.match(r"([\d.,]+)\s*EUR", fields["Preis"])
            if mp:
                price_net = d(mp.group(1))
        # variants
        variants = []
        desc_part = rest
        if "\nVariationen:" in rest:
            desc_part, _, vpart = rest.partition("\nVariationen:")
            for vl in vpart.splitlines():
                mv = re.match(r"- \[Artikel: (.*?)\]\((https://[^)]+)\): Artikelnummer: ([^ |]+)(?: \| Preis: ([\d.,]+) EUR)?", vl)
                if mv:
                    variants.append({
                        "name": mv.group(1).strip(), "url": mv.group(2),
                        "sku": mv.group(3).strip(),
                        "price_net": d(mv.group(4)) if mv.group(4) else None,
                    })
            # text after the variant list (notes etc.) is also descriptive content
            tail = [l for l in vpart.splitlines() if l.strip() and not l.startswith("- [Artikel:")]
            desc_part += "\n" + "\n".join(tail)
        # stable description text (ratings excluded on purpose, they change daily)
        description = norm_ws(desc_part)
        extra = {k: v for k, v in fields.items() if k not in
                 ("Artikelnummer", "Verfuegbarkeit", "Kategorie", "Hersteller", "Preis", "Bewertung")}
        extra_s = "; ".join(f"{k}={v}" for k, v in sorted(extra.items()))
        wm = re.search(r"(?:Versand)?[Gg]ewicht[^:\d]{0,40}:?\s*(?:ca\.?\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g)\b", description)
        weight = None
        if wm and not variants:
            w = Decimal(wm.group(1).replace(",", "."))
            weight = float(w if wm.group(2) == "kg" else w / 1000)
        parents.append({"sku": sku, "name": name, "description": description, "extra": extra_s,
                        "has_variants": bool(variants)})
        in_stock = not re.search(r"nicht|ausverkauft|vergriffen|nicht verf|out of", availability, re.I)
        base = {"availability_text": availability, "in_stock": in_stock, "category": category,
                "brand": brand, "parent_sku": sku, "parent_url": url_by_sku.get(sku, "")}
        dh = sha(description + "|" + extra_s)
        if variants:
            for v in variants:
                products.append({**base, "sku": v["sku"], "name": v["name"], "url": v["url"],
                                 "price_net": v["price_net"], "weight_kg": None, "description_hash": dh})
        else:
            products.append({**base, "sku": sku, "name": name, "url": url_by_sku.get(sku, ""),
                             "price_net": price_net, "weight_kg": weight, "description_hash": dh,
                             "parent_sku": ""})
    return products, parents


def parse_sitemap():
    meta = {}
    try:
        idx = ET.fromstring(fetch(SITEMAP_INDEX))
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
              "i": "http://www.google.com/schemas/sitemap-image/1.1"}
        for loc in idx.findall(".//s:sitemap/s:loc", ns):
            root = ET.fromstring(fetch(loc.text.strip()))
            for u in root.findall("s:url", ns):
                url = u.findtext("s:loc", namespaces=ns)
                lm = u.findtext("s:lastmod", namespaces=ns)
                img = u.find("i:image/i:loc", ns)
                imgs = img.text.strip() if img is not None and img.text else ""
                if imgs.endswith(".webp"):
                    imgs = imgs[:-5] + ".jpg"
                meta[url] = {"lastmod": lm or "", "image": imgs}
    except Exception as e:  # supplementary source, never fatal
        print(f"WARN sitemap unavailable: {e}", file=sys.stderr)
    return meta


# ---- helpers -----------------------------------------------------------------
def dedupe(rows):
    by = {}
    for r in rows:
        by.setdefault(r["sku"].strip().lower(), []).append(r)
    out = []
    for key, lst in by.items():
        if len(lst) == 1:
            r = lst[0]
            r["duplicate_sku"], r["duplicate_count"] = False, 1
        else:
            lst.sort(key=lambda r: (not r["in_stock"], r["price_net"] is None, ))
            r = lst[0]
            r["duplicate_sku"], r["duplicate_count"] = True, len(lst)
        out.append(r)
    return out


def word_diff(old, new, limit=300):
    a, b = old.split(), new.split()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed.append(" ".join(a[i1:i2]))
        if tag in ("replace", "insert"):
            added.append(" ".join(b[j1:j2]))
    cut = lambda l: (" | ".join(l))[:limit]
    return cut(removed), cut(added)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FEED_FIELDS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            row = dict(r)
            for k in ("on_sale", "in_stock", "duplicate_sku"):
                row[k] = "true" if row.get(k) else "false"
            for k, v in list(row.items()):
                if v is None:
                    row[k] = ""
            w.writerow(row)


def main():
    now = datetime.now(timezone(timedelta(hours=2)))  # display only
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs("data", exist_ok=True)

    raw = fetch(LLMS_URL).decode("utf-8", errors="replace")
    products, parents = parse_llms(raw)
    sm = parse_sitemap()

    # previous state (last good feed + descriptions)
    prev_rows, prev_desc = {}, {}
    pf = os.path.join(OUT_DIR, "feed.json")
    if os.path.exists(pf):
        pj = json.load(open(pf, encoding="utf-8"))
        prev_rows = {r["sku"]: r for r in pj.get("products", [])}
    if os.path.exists("data/descriptions.json"):
        prev_desc = json.load(open("data/descriptions.json", encoding="utf-8"))

    # ---- sanity checks (before anything is written) ----
    total_raw = len(products)
    if total_raw < MIN_PRODUCTS:
        sys.exit(f"FAIL: only {total_raw} products parsed (min {MIN_PRODUCTS}). Feed NOT updated.")
    for p in products:
        p["price_note"] = ""
        if p["price_net"] is not None and (p["price_net"] <= 0 or p["price_net"] >= PLACEHOLDER_NET_PRICE):
            p["price_note"] = f"placeholder/implausible price in source ({p['price_net']} net), price withheld"
            p["price_net"] = None
    no_price = sum(1 for p in products if p["price_net"] is None)
    if no_price / total_raw > MAX_MISSING_PRICE_RATIO:
        sys.exit(f"FAIL: {no_price}/{total_raw} products without price (> {MAX_MISSING_PRICE_RATIO:.0%}). Feed NOT updated.")
    no_url = sum(1 for p in products if not p["url"])
    if no_url / total_raw > MAX_MISSING_URL_RATIO:
        sys.exit(f"FAIL: {no_url}/{total_raw} products without URL. Feed NOT updated.")

    rows = dedupe(products)
    if prev_rows and len(rows) < len(prev_rows) * (1 - MAX_CATALOG_DROP_RATIO):
        sys.exit(f"FAIL: catalog dropped {len(prev_rows)} -> {len(rows)} (> {MAX_CATALOG_DROP_RATIO:.0%}). Feed NOT updated.")

    # ---- build rows ----
    changes = []
    out = []
    for r in sorted(rows, key=lambda r: r["sku"]):
        net = r["price_net"]
        gross = q2(net * (1 + VAT_RATE)) if net is not None else None
        s = dict(sm.get(r["url"], {}))
        if r["parent_url"] and r["parent_url"] != r["url"]:   # variants are not in the sitemap: inherit the parent page's data
            ps = sm.get(r["parent_url"], {})
            s.setdefault("image", ps.get("image", ""))
            s.setdefault("lastmod", ps.get("lastmod", ""))
        prev = prev_rows.get(r["sku"])
        row = {
            "sku": r["sku"], "name": r["name"],
            "price_gross": float(gross) if gross is not None else None,
            "regular_price_gross": float(gross) if gross is not None else None,
            "on_sale": False, "discount_percent": 0,
            "in_stock": r["in_stock"], "availability_text": r["availability_text"],
            "weight_kg": r["weight_kg"], "url": r["url"],
            "image_url": s.get("image") or (prev or {}).get("image_url", ""),
            "last_modified": s.get("lastmod") or (prev or {}).get("last_modified", ""),
            "currency": CURRENCY,
            "price_net": float(net) if net is not None else None,
            "parent_sku": r["parent_sku"], "category": r["category"], "brand": r["brand"],
            "duplicate_sku": r["duplicate_sku"], "duplicate_count": r["duplicate_count"],
            "price_note": r["price_note"],
            "description_hash": r["description_hash"],
            "previous_price_gross": None, "price_changed_at": "",
            "first_seen": today, "last_change_detected": today,
        }
        if prev:
            row["first_seen"] = prev.get("first_seen") or today
            row["last_change_detected"] = prev.get("last_change_detected") or today
            row["previous_price_gross"] = prev.get("previous_price_gross")
            row["price_changed_at"] = prev.get("price_changed_at", "")
            changed = False
            if prev.get("price_net") != row["price_net"]:
                row["previous_price_gross"] = prev.get("price_gross")
                row["price_changed_at"] = today
                changes.append({"date": today, "type": "price", "sku": r["sku"], "name": r["name"],
                                "old": prev.get("price_net"), "new": row["price_net"], "detail": "net EUR"})
                changed = True
            if prev.get("in_stock") != row["in_stock"] or prev.get("availability_text") != row["availability_text"]:
                changes.append({"date": today, "type": "stock", "sku": r["sku"], "name": r["name"],
                                "old": prev.get("availability_text"), "new": row["availability_text"], "detail": ""})
                changed = True
            if prev.get("name") != row["name"]:
                changes.append({"date": today, "type": "name", "sku": r["sku"], "name": r["name"],
                                "old": prev.get("name"), "new": row["name"], "detail": ""})
                changed = True
            if prev.get("url") != row["url"] and prev.get("url"):
                changes.append({"date": today, "type": "url", "sku": r["sku"], "name": r["name"],
                                "old": prev.get("url"), "new": row["url"], "detail": ""})
                changed = True
            if prev.get("category") != row["category"]:
                changes.append({"date": today, "type": "category", "sku": r["sku"], "name": r["name"],
                                "old": prev.get("category"), "new": row["category"], "detail": ""})
                changed = True
            if prev.get("description_hash") != row["description_hash"]:
                changed = True  # detail logged once per parent below
            if changed:
                row["last_change_detected"] = today
        else:
            if prev_rows:
                changes.append({"date": today, "type": "new_sku", "sku": r["sku"], "name": r["name"],
                                "old": None, "new": row["price_net"], "detail": r["category"]})
        out.append(row)

    cur = {r["sku"] for r in out}
    for sku, pr in prev_rows.items():
        if sku not in cur:
            changes.append({"date": today, "type": "removed_sku", "sku": sku, "name": pr.get("name"),
                            "old": pr.get("price_net"), "new": None, "detail": pr.get("category", "")})

    # description changes (once per parent / standalone product)
    new_desc = {}
    for p in parents:
        key = p["sku"]
        new_desc[key] = {"name": p["name"], "text": (p["description"] + (" || " + p["extra"] if p["extra"] else ""))}
    if prev_desc:
        for key, nd in new_desc.items():
            od = prev_desc.get(key)
            if od and od["text"] != nd["text"]:
                rem, add = word_diff(od["text"], nd["text"])
                affected = [r["sku"] for r in out if r["sku"] == key or r["parent_sku"] == key]
                changes.append({"date": today, "type": "description", "sku": key, "name": nd["name"],
                                "old": rem, "new": add,
                                "detail": f"{len(affected)} affected SKU(s)" + (": " + ", ".join(affected[:8]) if len(affected) > 1 else "")})

    in_stock = sum(1 for r in out if r["in_stock"])
    meta = {
        "source": LLMS_URL, "shop": BASE, "generated_at": now.isoformat(timespec="seconds"),
        "currency": CURRENCY,
        "price_basis": "price_net = shop's NET price (excl. 19% VAT), exact. price_gross = price_net*1.19 rounded to cents (retail price shown in shop, may deviate by 0.01).",
        "vat_rate": float(VAT_RATE),
        "stock_note": "Shop publishes availability text only (no quantity). Variant rows inherit the parent's availability.",
        "sale_note": "Source does not expose strike-through prices; regular_price_gross == price_gross, on_sale=false. Price moves are tracked via previous_price_gross/price_changed_at and changes.json.",
        "product_count": len(out), "in_stock_count": in_stock,
        "duplicate_sku_count": sum(1 for r in out if r["duplicate_sku"]),
        "raw_rows_before_dedupe": total_raw,
        "sku_unique": len({r["sku"] for r in out}) == len(out),
    }
    write_json(pf, {"meta": meta, "products": out})
    write_csv(os.path.join(OUT_DIR, "feed.csv"), out)
    sub = [r for r in out if r["category"] == "Unter Teppich-Heizmatte"]
    write_json(os.path.join(OUT_DIR, "unter-teppich-heizmatte.json"), {"meta": {**meta, "product_count": len(sub), "in_stock_count": sum(1 for r in sub if r["in_stock"])}, "products": sub})
    write_csv(os.path.join(OUT_DIR, "unter-teppich-heizmatte.csv"), sub)
    write_json("data/descriptions.json", new_desc)

    # rolling changelog
    cl_path = os.path.join(OUT_DIR, "changes.json")
    hist = json.load(open(cl_path, encoding="utf-8")) if os.path.exists(cl_path) else []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CHANGELOG_DAYS)).strftime("%Y-%m-%d")
    hist = [h for h in hist if h["date"] >= cutoff and h["date"] != today] + changes
    write_json(cl_path, hist)
    with open(os.path.join(OUT_DIR, "changes.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "type", "sku", "name", "old", "new", "detail"], lineterminator="\n")
        w.writeheader()
        for h in hist:
            w.writerow({k: ("" if h.get(k) is None else h.get(k)) for k in w.fieldnames})

    # issue text (triggers a GitHub e-mail notification) for content-type changes
    notable = [c for c in changes if c["type"] in ("description", "name", "new_sku", "removed_sku", "url", "category", "stock")]
    issue_path = "issue.md"
    if os.path.exists(issue_path):
        os.remove(issue_path)
    if notable:
        counts = {}
        for c in changes:
            counts[c["type"]] = counts.get(c["type"], 0) + 1
        lines = [f"Változások {today}: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())), ""]
        for c in notable[:150]:
            if c["type"] == "description":
                lines.append(f"- **leírás módosult** `{c['sku']}` {c['name']} ({c['detail']})\n  - eltűnt: {c['old'] or '-'}\n  - új: {c['new'] or '-'}")
            else:
                lines.append(f"- **{c['type']}** `{c['sku']}` {c['name']}: {c['old']} -> {c['new']} {c['detail']}")
        if len(notable) > 150:
            lines.append(f"... és még {len(notable) - 150} tétel, lásd feed/changes.csv")
        open(issue_path, "w", encoding="utf-8").write("\n".join(lines)[:60000])
        open("issue_title.txt", "w", encoding="utf-8").write(f"Változás a beszállítói feedben ({today}): {len(notable)} tartalmi tétel")

    print(json.dumps(meta, ensure_ascii=False, indent=1))
    print(f"changes today: {len(changes)}")


if __name__ == "__main__":
    main()
