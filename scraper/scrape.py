"""
Depensa ISP + Cell Scraper — Québec
=====================================
Scrape les prix des forfaits Internet résidentiels et cellulaires au Québec.

Internet — stratégie par site :
  - Sites simples (Oxio, EBOX, VMedia, Start.ca) : HTTP + BeautifulSoup
  - Sites moyens (TekSavvy, Cogeco)               : Playwright headless
  - Sites protégés (Bell, Vidéotron)              : curl_cffi (TLS spoofing) + Playwright fallback

Cellulaire :
  - planhub.ca/cell-phone-plans/quebec            : Playwright headless (React SPA)

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

OUTPUT_PATH      = os.path.join(os.path.dirname(__file__), "..", "data", "isp-prices.json")
CELL_OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "cell-prices.json")

# ──────────────────────────────────────────────────────────────────────────────
#  FALLBACK CELLULAIRE — forfaits ~15 Go comparables, Québec
# ──────────────────────────────────────────────────────────────────────────────
CELL_FALLBACK = [
    {"provider": "Telus",         "data_gb": 15, "price": 95.0, "network": "Telus",     "plan_name": "15 Go",  "url": "https://www.telus.com/en/mobility/plans",        "scraped_ok": False},
    {"provider": "Fido",          "data_gb": 20, "price": 65.0, "network": "Rogers",    "plan_name": "20 Go",  "url": "https://www.fido.ca/en/phones/plans",             "scraped_ok": False},
    {"provider": "Koodo",         "data_gb": 15, "price": 60.0, "network": "Telus",     "plan_name": "15 Go",  "url": "https://www.koodomobile.com/en/plans",            "scraped_ok": False},
    {"provider": "Vidéotron",     "data_gb": 15, "price": 58.0, "network": "Vidéotron", "plan_name": "15 Go",  "url": "https://www.videotron.com/en/mobility/plans",     "scraped_ok": False},
    {"provider": "Public Mobile", "data_gb": 15, "price": 55.0, "network": "Telus",     "plan_name": "15 Go",  "url": "https://www.publicmobile.ca/en/on/plans",         "scraped_ok": False},
    {"provider": "Fizz",          "data_gb": 15, "price": 50.0, "network": "Vidéotron", "plan_name": "15 Go",  "url": "https://fizz.ca/en/cell-plans",                   "scraped_ok": False},
    {"provider": "Lucky Mobile",  "data_gb": 15, "price": 45.0, "network": "Bell",      "plan_name": "15 Go",  "url": "https://www.luckymobile.ca/plans",                "scraped_ok": False},
    {"provider": "Chatr",         "data_gb": 10, "price": 40.0, "network": "Rogers",    "plan_name": "10 Go",  "url": "https://www.chatrwireless.com/plans",             "scraped_ok": False},
]

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

def load_previous_cell_prices() -> dict:
    """Relit le JSON cellulaire existant pour garder les prix précédents."""
    if os.path.exists(CELL_OUTPUT_PATH):
        with open(CELL_OUTPUT_PATH, "r", encoding="utf-8") as f:
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
#  SCRAPER CELLULAIRE — planhub.ca (React SPA, Playwright requis)
# ──────────────────────────────────────────────────────────────────────────────

# Providers qu'on veut comparer — ordre = priorité d'affichage
CELL_PROVIDERS = ["Telus", "Fido", "Koodo", "Vidéotron", "Public Mobile", "Fizz", "Lucky Mobile", "Chatr"]

# Réseau de chaque opérateur virtuel (MVNO)
CELL_NETWORKS = {
    "Fizz":          "Vidéotron",
    "Public Mobile": "Telus",
    "Koodo":         "Telus",
    "Fido":          "Rogers",
    "Lucky Mobile":  "Bell",
    "Chatr":         "Rogers",
    "Vidéotron":     "Vidéotron",
    "Telus":         "Telus",
}

async def scrape_planhub_cell_pw(page) -> list[dict]:
    """
    Scrape les forfaits cellulaires Québec depuis planhub.ca.
    Stratégie :
      1. Tente d'extraire les données JSON-LD (schema.org)
      2. Parse les cartes de forfaits du DOM rendu
    Retourne une liste de dicts {provider, data_gb, price, network, plan_name, url, scraped_ok}.
    """
    TARGET_URL = "https://www.planhub.ca/cell-phone-plans/quebec"
    results = []

    try:
        await page.goto(TARGET_URL, timeout=40000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)  # laisser React rendre le contenu

        # ── Stratégie 1 : JSON-LD ───────────────────────────────────────────
        json_ld_texts = await page.eval_on_selector_all(
            'script[type="application/ld+json"]',
            'els => els.map(el => el.textContent)'
        )
        for raw in json_ld_texts:
            try:
                data = json.loads(raw)
                items = []
                if isinstance(data, dict) and data.get("@type") in ("ItemList", "Product"):
                    items = data.get("itemListElement", [data])
                elif isinstance(data, list):
                    items = data
                for item in items:
                    if isinstance(item, dict):
                        name = item.get("name", "")
                        # Cherche le fournisseur connu dans le nom
                        provider = next((p for p in CELL_PROVIDERS if p.lower() in name.lower()), None)
                        if not provider:
                            continue
                        # Prix depuis offers/price
                        price = None
                        offers = item.get("offers", item.get("Offers", {}))
                        if isinstance(offers, dict):
                            price = extract_price(str(offers.get("price", "")))
                        if not price:
                            price = extract_price(str(item.get("price", "")))
                        # Data GB depuis description ou name
                        data_gb = None
                        desc = item.get("description", "") + " " + name
                        m_gb = re.search(r"(\d+)\s*(?:Go|GB|gb|go)", desc, re.I)
                        if m_gb:
                            data_gb = int(m_gb.group(1))
                        if price and data_gb:
                            results.append({
                                "provider": provider,
                                "data_gb":  data_gb,
                                "price":    price,
                                "network":  CELL_NETWORKS.get(provider, provider),
                                "plan_name": f"{data_gb} Go",
                                "url":      TARGET_URL,
                                "scraped_ok": True,
                            })
            except Exception:
                pass

        if results:
            return _deduplicate_cell(results)

        # ── Stratégie 2 : parse le DOM rendu ───────────────────────────────
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        # planhub utilise des cartes / lignes de tableau
        # Cherche les blocs qui contiennent un nom de provider connu + un prix + une quantité de données
        for tag in soup.find_all(["tr", "li", "div", "article"], limit=2000):
            text = tag.get_text(" ", strip=True)
            if len(text) > 300 or len(text) < 10:
                continue

            provider = next((p for p in CELL_PROVIDERS if p.lower() in text.lower()), None)
            if not provider:
                continue

            price = extract_price(text)
            m_gb  = re.search(r"(\d+)\s*(?:Go|GB)", text, re.I)
            data_gb = int(m_gb.group(1)) if m_gb else None

            if price and data_gb:
                results.append({
                    "provider": provider,
                    "data_gb":  data_gb,
                    "price":    price,
                    "network":  CELL_NETWORKS.get(provider, provider),
                    "plan_name": f"{data_gb} Go",
                    "url":      TARGET_URL,
                    "scraped_ok": True,
                })

    except Exception as e:
        print(f"    planhub cell pw error: {e}")

    return _deduplicate_cell(results)


def _deduplicate_cell(plans: list[dict]) -> list[dict]:
    """
    Garde un seul forfait par provider : celui avec le meilleur rapport
    données/prix (dans la plage 10–30 Go).
    """
    by_provider = {}
    for p in plans:
        if not (8 <= p["data_gb"] <= 35):
            continue
        prov = p["provider"]
        if prov not in by_provider:
            by_provider[prov] = p
        else:
            # Préférer plan moins cher avec >= données
            existing = by_provider[prov]
            if p["price"] < existing["price"] and p["data_gb"] >= existing["data_gb"]:
                by_provider[prov] = p
            elif p["price"] == existing["price"] and p["data_gb"] > existing["data_gb"]:
                by_provider[prov] = p
    return list(by_provider.values())


async def run_cell_scraper() -> tuple[list, int, int]:
    """Lance le scraper de forfaits cellulaires et retourne (plans, scraped, fallback)."""
    prev = load_previous_cell_prices()
    scraped_count = 0
    fallback_count = 0

    print("\n📱  Démarrage du scraping Cellulaire — Québec (planhub.ca)\n")

    scraped_plans = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="fr-CA",
            timezone_id="America/Montreal",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "fr-CA,fr;q=0.9"}
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = await context.new_page()
        try:
            scraped_plans = await scrape_planhub_cell_pw(page)
        except Exception as e:
            print(f"    planhub cell exception: {e}")
        finally:
            await page.close()
        await browser.close()

    # Index des plans scrapés par provider
    scraped_by_prov = {p["provider"]: p for p in scraped_plans}

    results = []
    for fb in CELL_FALLBACK:
        provider = fb["provider"]
        if provider in scraped_by_prov:
            entry = {**fb, **scraped_by_prov[provider]}
            results.append(entry)
            scraped_count += 1
            log("✓", provider, f"{entry['data_gb']} Go — ${entry['price']:.2f}/mois  [planhub]")
        elif provider in prev:
            entry = {**prev[provider], "scraped_ok": False}
            results.append(entry)
            fallback_count += 1
            log("↩", provider, f"échec → prix précédent conservé (${prev[provider]['price']:.2f})")
        else:
            results.append({**fb})
            fallback_count += 1
            log("⚠", provider, f"échec → valeur par défaut (${fb['price']:.2f})")

    # Ajouter les providers scrapés qui ne sont pas dans CELL_FALLBACK
    for provider, plan in scraped_by_prov.items():
        if not any(r["provider"] == provider for r in results):
            results.append(plan)
            scraped_count += 1
            log("✓", provider, f"{plan['data_gb']} Go — ${plan['price']:.2f}/mois  [planhub+]")

    results.sort(key=lambda x: x["price"], reverse=True)

    print(f"\n{'─'*55}")
    print(f"  ✅  {scraped_count} forfait(s) cellulaire(s) scrapé(s)")
    print(f"  ↩   {fallback_count} forfait(s) en fallback")
    print(f"{'─'*55}\n")

    return results, scraped_count, fallback_count


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
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Internet ─────────────────────────────────────────────────────────────
    isp_plans, isp_scraped, isp_fallback = await run_all_scrapers()

    isp_output = {
        "updated_at":    now,
        "scraped_count": isp_scraped,
        "fallback_count": isp_fallback,
        "region":        "quebec",
        "plans":         isp_plans,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(isp_output, f, ensure_ascii=False, indent=2)
    print(f"💾  Internet sauvegardé → {OUTPUT_PATH}")

    # ── Cellulaire ───────────────────────────────────────────────────────────
    cell_plans, cell_scraped, cell_fallback = await run_cell_scraper()

    cell_output = {
        "updated_at":    now,
        "scraped_count": cell_scraped,
        "fallback_count": cell_fallback,
        "region":        "quebec",
        "plans":         cell_plans,
    }
    os.makedirs(os.path.dirname(CELL_OUTPUT_PATH), exist_ok=True)
    with open(CELL_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(cell_output, f, ensure_ascii=False, indent=2)
    print(f"💾  Cellulaire sauvegardé → {CELL_OUTPUT_PATH}")

    # Code de sortie non-zéro si AUCUN scraping internet n'a fonctionné
    if isp_scraped == 0 and cell_scraped == 0:
        print("⚠️  AVERTISSEMENT : aucun scraping réussi — que des fallbacks.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
