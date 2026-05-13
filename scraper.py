"""
Flipkart FSN Scraper
====================
Scrapes price + stock status using a real Chromium browser.
Writes results to data.json (read by index.html dashboard).

Setup (one time):
    pip install playwright
    playwright install chromium

Usage:
    python scraper.py --fsn FSN1 FSN2 --pin 400001 560001
    python scraper.py --file fsns.txt --pin 400001 560001 110001
    python scraper.py --file fsns.txt --pin 400001 --concurrency 5
"""

import asyncio, argparse, json, re, os, random
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE  = os.path.join(SCRIPT_DIR, "data.json")

# ── Data model ─────────────────────────────────────────────────────────────
@dataclass
class Result:
    fsn: str
    pin: str
    name: str = ""
    price: Optional[float] = None
    mrp: Optional[float] = None
    disc: Optional[float] = None
    in_stock: Optional[bool] = None
    seller: str = ""
    rating: str = ""
    error: str = ""
    ts: str = field(default_factory=lambda: datetime.now().strftime("%d %b %y, %I:%M %p"))

    @property
    def status(self):
        if self.error:          return "error"
        if self.in_stock is True:  return "in_stock"
        if self.in_stock is False: return "oos"
        return "unknown"

    def to_dict(self):
        d = asdict(self)
        d["status"]  = self.status
        # ASCII-safe — dashboard formats the rupee symbol itself
        d["price_d"] = str(int(self.price)) if self.price else ""
        d["mrp_d"]   = str(int(self.mrp))   if self.mrp   else ""
        d["disc_d"]  = str(int(self.disc))   if self.disc  else ""
        return d


# ── HTML data extraction ───────────────────────────────────────────────────
def extract(html: str) -> dict:
    d = dict(name="", price=None, mrp=None, in_stock=None, seller="", rating="")

    # 1. JSON-LD (most reliable)
    for raw in re.findall(r'<script[^>]+ld\+json[^>]*>([\s\S]*?)</script>', html):
        try:
            obj = json.loads(raw)
            items = obj if isinstance(obj, list) else [obj]
            for item in items:
                if item.get("@type") != "Product": continue
                d["name"] = d["name"] or re.sub(r'<[^>]+>', '', item.get("name", "")).strip()[:100]
                o = item.get("offers", {})
                if isinstance(o, list): o = o[0] if o else {}
                if o.get("price"):
                    d["price"] = float(o["price"])
                av = (o.get("availability") or "").lower()
                if "instock"    in av: d["in_stock"] = True
                elif "outofstock" in av or "soldout" in av: d["in_stock"] = False
                if isinstance(o.get("seller"), dict):
                    d["seller"] = o["seller"].get("name", "")
        except Exception:
            pass

    # 2. Regex mines on raw HTML
    if d["price"] is None:
        for rx in [r'"finalPrice"\s*:\s*(\d+)', r'"finalPrice"\s*:\s*\{"value"\s*:\s*(\d+)']:
            m = re.search(rx, html)
            if m: d["price"] = float(m.group(1)); break

    if d["mrp"] is None:
        for rx in [r'"mrp"\s*:\s*(\d+)', r'"mrp"\s*:\s*\{"value"\s*:\s*(\d+)']:
            m = re.search(rx, html)
            if m: d["mrp"] = float(m.group(1)); break

    if d["in_stock"] is None:
        if   '"outOfStock":true'  in html or '"isOutOfStock":true'  in html: d["in_stock"] = False
        elif '"outOfStock":false' in html or '"isOutOfStock":false' in html: d["in_stock"] = True

    # 3. Title fallback for name
    if not d["name"]:
        m = re.search(r'<title>([\s\S]*?)</title>', html, re.I)
        if m:
            raw = re.sub(r' - Buy .*| \| Flipkart.*', '', m.group(1)).strip()
            if 5 < len(raw) < 150: d["name"] = raw

    # 4. Text-based OOS fallback
    if d["in_stock"] is None:
        lo = html.lower()
        if any(x in lo for x in ["sold out", "currently unavailable", "out of stock"]):
            d["in_stock"] = False
        elif d["price"] is not None:
            d["in_stock"] = True

    # 5. Rating
    m = re.search(r'"averageRating"\s*:\s*"?([\d.]+)"?', html)
    if m: d["rating"] = m.group(1)

    return d


# ── Single page scraper ────────────────────────────────────────────────────
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

async def scrape_one(browser, fsn: str, pin: str, sem: asyncio.Semaphore) -> Result:
    r = Result(fsn=fsn, pin=pin)
    url = f"https://www.flipkart.com/product/p/itme?pid={fsn}&pincode={pin}"

    async with sem:
        await asyncio.sleep(random.uniform(0.5, 1.8))
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        pg = await ctx.new_page()
        # Block images/fonts to speed up
        await pg.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4}", lambda r: r.abort())

        try:
            await pg.goto(url, wait_until="domcontentloaded", timeout=25_000)
            try:
                await pg.wait_for_selector("._30jeq3, ._16FRp0, ._2KpZ6l", timeout=8_000)
            except PWTimeout:
                pass  # Proceed anyway

            html = await pg.content()
            data = extract(html)
            r.name     = data["name"]
            r.price    = data["price"]
            r.mrp      = data["mrp"]
            r.in_stock = data["in_stock"]
            r.seller   = data["seller"]
            r.rating   = data["rating"]
            if r.price and r.mrp and r.mrp > r.price:
                r.disc = round(((r.mrp - r.price) / r.mrp) * 100, 1)

            # DOM fallbacks for price
            if r.price is None:
                for sel in ["._30jeq3", "._16Jk6d"]:
                    el = pg.locator(sel).first
                    if await el.count():
                        txt = (await el.text_content() or "").replace("₹", "").replace(",", "").strip()
                        try: r.price = float(txt); break
                        except ValueError: pass

            # DOM fallback for MRP
            if r.mrp is None:
                for sel in ["._3I9_wc", "._3qQ9m1"]:
                    el = pg.locator(sel).first
                    if await el.count():
                        txt = (await el.text_content() or "").replace("₹", "").replace(",", "").strip()
                        try: r.mrp = float(txt); break
                        except ValueError: pass

            # DOM fallback for stock status
            if r.in_stock is None:
                oos = await pg.locator("._16FRp0, [class*='soldOut']").count()
                add = await pg.locator("._2KpZ6l, [class*='add-to-cart']").count()
                bt  = (await pg.locator("body").text_content() or "").lower()
                if oos or "sold out" in bt or "currently unavailable" in bt:
                    r.in_stock = False
                elif add or r.price:
                    r.in_stock = True

            # DOM fallback for name
            if not r.name:
                for sel in ["h1.VU-ZEz", "h1", "span.B_NuCI"]:
                    el = pg.locator(sel).first
                    if await el.count():
                        txt = (await el.text_content() or "").strip()
                        if len(txt) > 5: r.name = txt[:100]; break

            # Detect invalid FSN
            if r.price is None and r.in_stock is None:
                bt = (await pg.locator("body").text_content() or "").lower()
                r.error = (
                    "Invalid FSN" if ("page not found" in bt or "oops" in bt or len(bt) < 200)
                    else "Could not parse"
                )

        except PWTimeout:
            r.error = "Page timeout"
        except Exception as e:
            r.error = str(e)[:80]
        finally:
            await pg.close()
            await ctx.close()

    return r


# ── Runner ─────────────────────────────────────────────────────────────────
async def run_all(fsns: list, pins: list, concurrency: int = 3) -> list:
    combos = [(f, p) for f in fsns for p in pins]
    total  = len(combos)
    print(f"\n{'─'*55}")
    print(f"  📦 {len(fsns)} FSN(s) × {len(pins)} pincode(s) = {total} combinations")
    print(f"  ⚡ Concurrency: {concurrency}")
    print(f"{'─'*55}\n")

    sem  = asyncio.Semaphore(concurrency)
    done = [0]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )

        async def fetch(fsn, pin):
            res = await scrape_one(browser, fsn, pin, sem)
            done[0] += 1
            icon  = "✅" if res.in_stock is True else ("❌" if res.in_stock is False else "💥")
            price = f"₹{int(res.price):,}" if res.price else "—"
            label = res.error or res.name[:45] or "—"
            print(f"  [{done[0]:>3}/{total}]  {fsn}  |  {pin}  |  {price}  |  {icon}  {label}")
            return res

        results = await asyncio.gather(*[fetch(f, p) for f, p in combos])
        await browser.close()

    return list(results)


# ── Save data.json ──────────────────────────────────────────────────────────
def save_json(results: list, path: str):
    payload = {
        "last_updated": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "total":    len(results),
        "in_stock": sum(1 for r in results if r.in_stock is True),
        "oos":      sum(1 for r in results if r.in_stock is False),
        "errors":   sum(1 for r in results if r.error),
        "results":  [r.to_dict() for r in results],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)
    print(f"\n  💾 data.json saved → {path}")


# ── Auto git push ────────────────────────────────────────────────────────────
def git_push(repo_dir: str):
    import subprocess

    def run(cmd):
        r = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    print(f"\n{'─'*55}")
    print("  🚀 Pushing to GitHub...")

    # Make sure we're inside a git repo
    code, out, err = run(["git", "status"])
    if code != 0:
        print(f"  ⚠ Not a git repo — skipping push. Run 'git init' first.")
        return

    # Stage data.json
    run(["git", "add", "data.json"])

    # Commit
    msg = f"data: update {datetime.now().strftime('%d %b %Y %I:%M %p')}"
    code, out, err = run(["git", "commit", "-m", msg])
    if code != 0:
        if "nothing to commit" in out or "nothing to commit" in err:
            print("  ℹ No changes to commit — data.json unchanged.")
        else:
            print(f"  ⚠ Commit failed: {err or out}")
        return

    print(f"  ✅ Committed: {msg}")

    # Push
    code, out, err = run(["git", "push"])
    if code == 0:
        print("  ✅ Pushed to GitHub!")
        print("  🌐 Dashboard live at: https://manmeetsingh7-lab.github.io/fk-fsn-tracker/")
    else:
        print(f"  ⚠ Push failed: {err or out}")
        print("  💡 Make sure you've set the remote with your token:")
        print("     git remote set-url origin https://<TOKEN>@github.com/manmeetsingh7-lab/fk-fsn-tracker.git")

    print(f"{'─'*55}\n")


# ── CLI ─────────────────────────────────────────────────────────────────────
def load_fsns(path: str) -> list:
    with open(path) as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]

def parse_args():
    p = argparse.ArgumentParser(description="Flipkart FSN Scraper → data.json → auto git push")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--fsn",  nargs="+", help="FSNs inline")
    g.add_argument("--file", metavar="FILE", help="Text file with FSNs (one per line)")
    p.add_argument("--pin",         nargs="+", required=True, help="Pincodes")
    p.add_argument("--concurrency", type=int,  default=3,     help="Parallel tabs (default 3)")
    p.add_argument("--out",         default=DATA_FILE,        help=f"Output path (default: {DATA_FILE})")
    p.add_argument("--no-push",     action="store_true",      help="Skip auto git push")
    return p.parse_args()

async def main():
    try:
        args   = parse_args()
        fsns   = list(dict.fromkeys(args.fsn or load_fsns(args.file)))
        pins   = list(dict.fromkeys(args.pin))
        out    = os.path.abspath(args.out)

        results = await run_all(fsns, pins, args.concurrency)
        save_json(results, out)

        inS = sum(1 for r in results if r.in_stock is True)
        oos = sum(1 for r in results if r.in_stock is False)
        err = sum(1 for r in results if r.error)
        print(f"\n{'─'*55}")
        print(f"  ✅ {inS} In Stock  |  ❌ {oos} OOS  |  💥 {err} Errors")
        print(f"{'─'*55}")

        # Auto push unless --no-push flag given
        if not args.no_push:
            git_push(SCRIPT_DIR)
        else:
            print("\n  ⏭ Skipped git push (--no-push flag set)\n")

    except Exception as e:
        import traceback
        print(f"\n💥 ERROR: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
