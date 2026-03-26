"""
Depensa ISP Scraper — Québec
==============================
Scrape les prix des forfaits Internet résidentiels des principaux FAI québécois.

Stratégie par site :
  - Sites simples (Oxio, EBOX, VMedia, Start.ca) : HTTP + BeautifulSoup
  - Sites moyens (TekSavvy, Cogeco)               : Playwright headless
  - Sites protégés (Bell, Vidéotron)              : curl_cffi (TLS spoofing) + Playwright fallback

Si tous les scrapers échouent pour un fournisseur, le prix de la dernière
exécution est conservé (le JSON existant est relu comme fallback).
"""

import asyncio
import json
import os
import re
import sys
import datetime

from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ──────────────────────────────────────────────────────────────────────────────
#  FALLBACK — prix "de base" si tout échoue et qu'il n'y a pas de JSON existant
# ──────────────────────────────────────────────────────────────────────────────
FALLBACK = [
    {"provider": "Vidéotron", "plan": "Internet 400", "speed_down": 400, "speed_up": 50,  "price": 85.0, "type": "Câble", "note": "",                 "promo": False, "promo_note": "", "url": "https://www.videotron.com/en/internet/internet-packages",           "scraped_ok": False},
    {"provider": "Bell",      "plan": "Fibre 500",    "speed_down": 500, "speed_up": 500, "price": 80.0, "type": "Fibre", "note": "",                 "promo": False, "promo_note": "", "url": "https://www.bell.ca/Bell_Internet/Internet_access",                  "scraped_ok": False},
    {"provider": "Cogeco",    "plan": "Internet 400", "speed_down": 400, "speed_up": 20,  "price": 75.0, "type": "Câble", "note": "",                 "promo": False, "promo_note": "", "url": "https://www.cogeco.ca/en/internet/packages",                         "scraped_ok": False},
    {"provider": "TekSavvy",  "plan": "Cable 300",    "speed_down": 300, "speed_up": 20,  "price": 64.0, "type": "Câble", "note": "Réseau Vidéotron", "promo": False, "promo_note": "", "url": "https://www.teksavvy.com/services/internet/",                        "scraped_ok": False},
    {"provider": "Oxio",      "plan": "400 Mbps",     "speed_down": 400, "speed_up": 20,  "price": 60.0, "type": "Câble", "note": "Réseau Vidéotron", "promo": False, "promo_note": "", "url": "https://oxio.ca/en/internet",                                        "scraped_ok": False},
    {"provider": "EBOX",      "plan": "Internet 250", "speed_down": 250, "speed_up": 15,  "price": 55.0, "type": "Câble", "note": "Réseau Vidéotron", "promo": False, "promo_note": "", "url": "https://www.ebox.ca/en/quebec/residential/internet-packages/",       "scraped_ok": False},
    {"provider": "VMedia",    "plan": "Cable 300",    "speed_down": 300, "speed_up": 20,  "price": 50.0, "type": "Câble", "note": "Réseau Bell",      "promo": False, "promo_note": "", "url": "https://www.vmedia.ca/en/homeinternet",                              "scraped_ok": False},
    {"provider": "Start.ca",  "plan": "Cable 200",    "speed_down": 200, "speed_up": 15,  "price": 45.0, "type": "Câble", "note": "Réseau Vidéotron", "promo": False, "promo_note": "", "url": "https://www.start.ca/services/high-speed-internet",                  "scraped_ok": False},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "isp-prices.json")

# ──────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def extract_price(text: str) -> float | None:
    """'$54.95/mois' → 54.95   '55 $' → 55.0   '79.99' → 79.99"""
    text = text.replace("\xa0", " ").replace(",", ".")
    m = re.search(r"(\d{2,3}(?:\.\d{1,2})?)", text)
    if m:
        val = float(m.group(1))
        if 10 < val < 300:   # sanity check — un forfait internet coûte entre 10 et 300$
            return val
    return None

def extract_speed_mbps(text: str) -> int | None:
    """'400 Mbps' → 400   '1 Gbps' → 1000   '1.5 Gbps' → 1500"""
    text = text.upper()
    g = re.search(r"(\d+(?:\.\d+)?)\s*GBPS", text)
    if g:
        return int(float(g.group(1)) * 1000)
    m = re.search(r"(\d+)\s*MBPS", text)
    if m:
        return int(m.group(1))
    return None

def load_previous_prices() -> dict:
    """Relit le JSON existant pour garder les prix précédents en cas d'échec."""
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {p["provider"]: p for p in data.get("plans", [])}
    return {}

def log(symbol: str, provider: str, msg: str):
    print(f"  {symbol}  {provider:<12} {msg}")

# ──────────────────────────────────────────────────────────────────────────────
#  SCRAPERS — HTTP (curl_cffi, contourne la plupart des Cloudflare)
# ──────────────────────────────────────────────────────────────────────────────

def scrape_oxio_http() -> dict | None:
    """
    Oxio charge ses prix en SSR dans la page HTML initiale.
    On cherche les patterns de prix dans le source.
    """
    try:
        r = cf_requests.get("https://oxio.ca/en/internet", impersonate="chrome120", timeout=20)
        soup = BeautifulSoup(r.text, "lxml")

        # Chercher les cartes de forfaits — Oxio utilise des blocs avec prix visibles
        price_blocks = soup.find_all(string=re.compile(r"\$\s*\d{2,3}"))
        prices = [extract_price(p) for p in price_blocks if extract_price(p)]

        speed_blocks = soup.find_all(string=re.compile(r"\d{2,4}\s*[Mm]bps|\d\s*[Gg]bps", re.I))
        speeds = [extract_speed_mbps(s) for s in speed_blocks if extract_speed_mbps(s)]

        if prices and speeds:
            # Prendre le forfait intermédiaire (ni le plus cher ni le moins cher)
            prices_sorted = sorted(set(prices))
            speeds_sorted = sorted(set(speeds))
            target_price = prices_sorted[len(prices_sorted) // 2]
            target_speed = speeds_sorted[len(speeds_sorted) // 2]
            return {"price": target_price, "speed_down": target_speed, "plan": f"{target_speed} Mbps"}
    except Exception as e:
        print(f"    oxio http error: {e}")
    return None


def scrape_ebox_http() -> dict | None:
    try:
        r = cf_requests.get(
            "https://www.ebox.ca/en/quebec/residential/internet-packages/",
            impersonate="chrome120", timeout=20
        )
        soup = BeautifulSoup(r.text, "lxml")

        # EBOX a des cartes .package-card ou similaire
        cards = soup.select(".package-card, .plan-card, [class*='package'], [class*='plan']")
        plans = []
        for card in cards:
            text = card.get_text(" ", strip=True)
            price = extract_price(text)
            speed = extract_speed_mbps(text)
            if price and speed:
                plans.append({"price": price, "speed_down": speed, "plan": f"Internet {speed}"})

        if not plans:
            # Fallback : recherche directe dans le texte de la page
            all_text = soup.get_text(" ")
            prices = sorted(set(p for p in [extract_price(t) for t in all_text.split()] if p), reverse=True)
            speeds = sorted(set(s for s in [extract_speed_mbps(t) for t in all_text.split() if t] if s))
            if prices and speeds:
                return {"price": prices[0], "speed_down": speeds[-1], "plan": f"Internet {speeds[-1]}"}

        if plans:
            # Retourner le plus rapide disponible
            plans.sort(key=lambda p: p["speed_down"], reverse=True)
            return plans[0]

    except Exception as e:
        print(f"    ebox http error: {e}")
    return None


def scrape_vmedia_http() -> dict | None:
    try:
        r = cf_requests.get("https://www.vmedia.ca/en/homeinternet", impersonate="chrome120", timeout=20)
        soup = BeautifulSoup(r.text, "lxml")
        all_text = soup.get_text(" ")
        prices = sorted(set(p for p in [extract_price(t) for t in re.findall(r'\$[\d,.]+', all_text)] if p))
        speeds = sorted(set(s for s in [extract_speed_mbps(t) for t in re.findall(r'\d+\s*[MGmg]bps', all_text)] if s), reverse=True)
        if prices and speeds:
            return {"price": sorted(prices)[0], "speed_down": speeds[0], "plan": f"Cable {speeds[0]}"}
    except Exception as e:
        print(f"    vmedia http error: {e}")
    return None


def scrape_startca_http() -> dict | None:
    try:
        r = cf_requests.get(
            "https://www.start.ca/services/high-speed-internet",
            impersonate="chrome120", timeout=20
        )
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select("[class*='plan'], [class*='package'], [class*='tier']")
        plans = []
        for card in cards:
            text = card.get_text(" ", strip=True)
            price = extract_price(text)
            speed = extract_speed_mbps(text)
            if price and speed:
                plans.append({"price": price, "speed_down": speed, "plan": f"Cable {speed}"})
        if plans:
            plans.sort(key=lambda p: p["speed_down"])
            return plans[len(plans) // 2]  # forfait médian
    except Exception as e:
        print(f"    startca http error: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────────────
#  SCRAPERS — PLAYWRIGHT (sites avec JS obligatoire)
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_teksavvy_pw(page) -> dict | None:
    try:
        await page.goto("https://www.teksavvy.com/services/internet/", timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        cards = soup.select("[class*='plan'], [class*='package'], [class*='product'], article")
        plans = []
        for card in cards:
            text = card.get_text(" ", strip=True)
            price = extract_price(text)
            speed = extract_speed_mbps(text)
            if price and speed:
                plans.append({"price": price, "speed_down": speed, "plan": f"Cable {speed}"})

        if not plans:
            # Cherche les prix directement dans le texte de la page
            all_text = soup.get_text(" ")
            for m in re.finditer(r"(\d{3})\s*Mbps[^$]*\$\s*(\d{2,3}(?:\.\d{2})?)", all_text):
                speed, price = int(m.group(1)), float(m.group(2))
                plans.append({"price": price, "speed_down": speed, "plan": f"Cable {speed}"})

        if plans:
            plans.sort(key=lambda p: p["price"])
            return plans[len(plans) // 2]
    except Exception as e:
        print(f"    teksavvy pw error: {e}")
    return None


async def scrape_cogeco_pw(page) -> dict | None:
    try:
        await page.goto("https://www.cogeco.ca/en/internet/packages", timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        # Intercepter les réponses d'API interne si Cogeco charge ses prix via fetch
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        plans = []
        for card in soup.select("[class*='plan'], [class*='package'], [class*='offer'], article"):
            text = card.get_text(" ", strip=True)
            price = extract_price(text)
            speed = extract_speed_mbps(text)
            if price and speed:
                plans.append({"price": price, "speed_down": speed, "plan": f"Internet {speed}"})

        if plans:
            plans.sort(key=lambda p: p["speed_down"])
            return plans[len(plans) // 2]
    except Exception as e:
        print(f"    cogeco pw error: {e}")
    return None


async def scrape_bell_pw(page) -> dict | None:
    """
    Bell utilise Cloudflare Advanced. On essaie d'intercepter les appels API
    que la page fait pour charger les prix.
    """
    captured = []

    async def on_response(response):
        url = response.url.lower()
        if any(k in url for k in ["plan", "offer", "product", "price", "internet"]):
            ct = response.headers.get("content-type", "")
            if "json" in ct:
                try:
                    data = await response.json()
                    text = json.dumps(data)
                    prices = [extract_price(m) for m in re.findall(r'[\d.]{4,7}', text)]
                    speeds = [extract_speed_mbps(m) for m in re.findall(r'\d+\s*[MGmg]bps', text)]
                    prices = [p for p in prices if p]
                    speeds = [s for s in speeds if s]
                    if prices and speeds:
                        captured.append({"price": min(prices), "speed_down": max(speeds), "plan": f"Fibre {max(speeds)}"})
                except Exception:
                    pass

    page.on("response", on_response)
    try:
        await page.goto("https://www.bell.ca/Bell_Internet/Internet_access", timeout=35000, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)

        if captured:
            captured.sort(key=lambda p: p["speed_down"])
            return captured[0]

        # Fallback : lire le HTML rendu
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")
        all_text = soup.get_text(" ")
        for m in re.finditer(r"(\d{3,4})\s*Mbps[^$\d]*\$\s*(\d{2,3}(?:\.\d{2})?)", all_text, re.I):
            speed, price = int(m.group(1)), float(m.group(2))
            return {"price": price, "speed_down": speed, "plan": f"Fibre {speed}"}

    except Exception as e:
        print(f"    bell pw error: {e}")
    return None


async def scrape_videotron_pw(page) -> dict | None:
    """
    Vidéotron : React + Cloudflare. Même stratégie d'interception API.
    """
    captured = []

    async def on_response(response):
        url = response.url.lower()
        if any(k in url for k in ["forfait", "package", "plan", "internet", "offer"]):
            ct = response.headers.get("content-type", "")
            if "json" in ct:
                try:
                    data = await response.json()
                    text = json.dumps(data)
                    # Cherche des prix typiques (deux chiffres, parfois avec décimales)
                    prices = []
                    for m in re.finditer(r'"(?:price|prix|montant|amount|cost)":\s*"?(\d{2,3}(?:\.\d{1,2})?)"?', text, re.I):
                        p = extract_price(m.group(1))
                        if p:
                            prices.append(p)
                    speeds = [extract_speed_mbps(m) for m in re.findall(r'\d+\s*[MGmg]bps', text)]
                    speeds = [s for s in speeds if s]
                    if prices and speeds:
                        captured.append({"price": min(prices), "speed_down": max(speeds), "plan": f"Internet {max(speeds)}"})
                except Exception:
                    pass

    page.on("response", on_response)
    try:
        await page.goto(
            "https://www.videotron.com/en/internet/internet-packages",
            timeout=35000, wait_until="domcontentloaded"
        )
        await page.wait_for_timeout(6000)

        if captured:
            captured.sort(key=lambda p: p["speed_down"])
            return captured[0]

        content = await page.content()
        soup = BeautifulSoup(content, "lxml")
        all_text = soup.get_text(" ")
        for m in re.finditer(r"(\d{3})\s*Mbps[^$\d]*\$\s*(\d{2,3}(?:\.\d{2})?)", all_text, re.I):
            speed, price = int(m.group(1)), float(m.group(2))
            return {"price": price, "speed_down": speed, "plan": f"Internet {speed}"}

    except Exception as e:
        print(f"    videotron pw error: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────────────
#  ORCHESTRATEUR PRINCIPAL
# ──────────────────────────────────────────────────────────────────────────────

async def run_all_scrapers() -> list[dict]:
    prev = load_previous_prices()
    results = []
    scraped_count = 0
    fallback_count = 0

    print("\n📡  Démarrage du scraping ISP — Québec\n")

    # ── 1. Sites HTTP simples (pas besoin de navigateur) ────────────────────
    http_scrapers = [
        ("Oxio",     scrape_oxio_http,    "Câble", "Réseau Vidéotron", "https://oxio.ca/en/internet"),
        ("EBOX",     scrape_ebox_http,    "Câble", "Réseau Vidéotron", "https://www.ebox.ca/en/quebec/residential/internet-packages/"),
        ("VMedia",   scrape_vmedia_http,  "Câble", "Réseau Bell",      "https://www.vmedia.ca/en/homeinternet"),
        ("Start.ca", scrape_startca_http, "Câble", "Réseau Vidéotron", "https://www.start.ca/services/high-speed-internet"),
    ]

    for provider, fn, conn_type, note, url in http_scrapers:
        result = fn()
        fb = next((f for f in FALLBACK if f["provider"] == provider), {})
        if result:
            entry = {**fb, **result, "type": conn_type, "note": note, "url": url, "scraped_ok": True}
            results.append(entry)
            scraped_count += 1
            log("✓", provider, f"{result['speed_down']} Mbps — ${result['price']:.2f}/mois  [HTTP]")
        else:
            # Garder le prix précédent si disponible, sinon fallback
            if provider in prev:
                entry = {**prev[provider], "scraped_ok": False}
                log("↩", provider, f"échec HTTP → prix précédent conservé (${prev[provider]['price']:.2f})")
            else:
                entry = {**fb, "scraped_ok": False}
                log("⚠", provider, f"échec HTTP → valeur par défaut (${fb['price']:.2f})")
            results.append(entry)
            fallback_count += 1

    # ── 2. Sites Playwright (JS obligatoire) ────────────────────────────────
    pw_scrapers = [
        ("TekSavvy", scrape_teksavvy_pw, "Câble", "Réseau Vidéotron", "https://www.teksavvy.com/services/internet/"),
        ("Cogeco",   scrape_cogeco_pw,   "Câble", "",                 "https://www.cogeco.ca/en/internet/packages"),
        ("Bell",     scrape_bell_pw,     "Fibre", "",                 "https://www.bell.ca/Bell_Internet/Internet_access"),
        ("Vidéotron",scrape_videotron_pw,"Câble", "",                 "https://www.videotron.com/en/internet/internet-packages"),
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="fr-CA",
            timezone_id="America/Montreal",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "fr-CA,fr;q=0.9"}
        )
        # Masque les traces d'automation
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        for provider, fn, conn_type, note, url in pw_scrapers:
            page = await context.new_page()
            fb = next((f for f in FALLBACK if f["provider"] == provider), {})
            try:
                result = await fn(page)
            except Exception as e:
                result = None
                print(f"    {provider} exception: {e}")
            finally:
                await page.close()

            if result:
                entry = {**fb, **result, "type": conn_type, "note": note, "url": url, "scraped_ok": True}
                results.append(entry)
                scraped_count += 1
                log("✓", provider, f"{result['speed_down']} Mbps — ${result['price']:.2f}/mois  [Playwright]")
            else:
                if provider in prev:
                    entry = {**prev[provider], "scraped_ok": False}
                    log("↩", provider, f"échec PW → prix précédent conservé (${prev[provider]['price']:.2f})")
                else:
                    entry = {**fb, "scraped_ok": False}
                    log("⚠", provider, f"échec PW → valeur par défaut (${fb['price']:.2f})")
                results.append(entry)
                fallback_count += 1

        await browser.close()

    # ── Tri final : du plus cher au moins cher ───────────────────────────────
    results.sort(key=lambda x: x["price"], reverse=True)

    print(f"\n{'─'*55}")
    print(f"  ✅  {scraped_count} forfait(s) scrapé(s) en direct")
    print(f"  ↩   {fallback_count} forfait(s) en fallback / prix précédent")
    print(f"{'─'*55}\n")

    return results, scraped_count, fallback_count


async def main():
    plans, scraped, fallback = await run_all_scrapers()

    output = {
        "updated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scraped_count": scraped,
        "fallback_count": fallback,
        "region": "quebec",
        "plans": plans
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"💾  Sauvegardé dans {OUTPUT_PATH}")

    # Code de sortie non-zéro si AUCUN scraping n'a fonctionné (pour alertes CI)
    if scraped == 0:
        print("⚠️  AVERTISSEMENT : aucun scraping réussi — que des fallbacks.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
