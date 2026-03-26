"""
Depensa ISP + Cell Scraper — Québec
=====================================
Scrape les prix des forfaits Internet résidentiels et cellulaires au Québec.

Internet (7 fournisseurs) — Playwright headless :
  Vidéotron · Bell · Cogeco · EBOX · VMedia · Fizz · Start.ca

Cellulaire (8 fournisseurs) — Playwright headless :
  Telus · Fido · Koodo · Vidéotron · Public Mobile · Fizz · Lucky Mobile · Chatr

  Stratégie commune :
  1. Interception des réponses API JSON (networkidle)
  2. Scripts inline (Next.js __NEXT_DATA__, window.__STATE__, etc.)
  3. Texte DOM rendu (BeautifulSoup)
  Surcharge par FAI si la structure le justifie.

Fallback : en cas d'échec, on réutilise le JSON précédent si disponible,
sinon les valeurs par défaut ci-dessous.
"""

import asyncio
import json
import os
import re
import sys
import datetime
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ──────────────────────────────────────────────────────────────────────────────
#  CHEMINS DE SORTIE
# ──────────────────────────────────────────────────────────────────────────────
OUTPUT_PATH      = os.path.join(os.path.dirname(__file__), "..", "data", "isp-prices.json")
CELL_OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "cell-prices.json")

# ──────────────────────────────────────────────────────────────────────────────
#  DATACLASSES — plans normalisés
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ISPPlan:
    provider: str
    speed_down: int
    speed_up: int
    price: float
    is_promo: bool = False
    promo_note: str = ""
    source: str = ""        # "dom", "json_ld", "api", "next_data", "text"
    url: str = ""
    raw_meta: dict = field(default_factory=dict)
    scraped_ok: bool = True


@dataclass
class CellPlan:
    provider: str
    data_gb: int            # Go de données ; 999 = illimité
    price: float
    network: str = ""
    plan_name: str = ""
    source: str = ""        # "dom", "api", "text", "json_ld"
    url: str = ""
    raw_meta: dict = field(default_factory=dict)
    scraped_ok: bool = True


# ──────────────────────────────────────────────────────────────────────────────
#  CONFIG PAR FAI — Internet
# ──────────────────────────────────────────────────────────────────────────────

PROVIDER_CONFIG: dict[str, dict] = {
    "Vidéotron": {
        "min_speed_mbps":       100,
        "preferred_speed_mbps": 400,
        "ignore_keywords":      [],
        "max_price_delta_pct":  25,
    },
    "Bell": {
        "min_speed_mbps":       100,
        "preferred_speed_mbps": 500,
        "ignore_keywords":      [],
        "max_price_delta_pct":  25,
    },
    "Cogeco": {
        "min_speed_mbps":       100,
        "preferred_speed_mbps": 400,
        "ignore_keywords":      [],
        "max_price_delta_pct":  25,
    },
    "Fizz": {
        "min_speed_mbps":       100,
        "preferred_speed_mbps": 200,
        "ignore_keywords":      [
            "bundle", "promo", "save", "économisez",
            "was", "était", "mobile", "cell",
        ],
        "selector_container":   "#internetPlanCards",
        "prefer_source":        "dom",
        "max_price_delta_pct":  20,
    },
    "EBOX": {
        "min_speed_mbps":       50,
        "preferred_speed_mbps": 120,
        "ignore_keywords":      [],
        "max_price_delta_pct":  25,
    },
    "VMedia": {
        "min_speed_mbps":       50,
        "preferred_speed_mbps": 300,
        "ignore_keywords":      [],
        "max_price_delta_pct":  25,
    },
    "Start.ca": {
        "min_speed_mbps":       50,
        "preferred_speed_mbps": 200,
        "ignore_keywords":      [],
        "max_price_delta_pct":  25,
    },
}

_DEFAULT_CFG: dict = {
    "min_speed_mbps":       50,
    "preferred_speed_mbps": 200,
    "ignore_keywords":      [],
    "max_price_delta_pct":  30,
}

# ──────────────────────────────────────────────────────────────────────────────
#  CONFIG PAR FAI — Cellulaire
# ──────────────────────────────────────────────────────────────────────────────

CELL_PROVIDER_CONFIG: dict[str, dict] = {
    "Telus": {
        "target_data_gb":      15,
        "min_data_gb":         8,
        "ignore_keywords":     [],
        "network":             "Telus",
        "max_price_delta_pct": 25,
    },
    "Fido": {
        "target_data_gb":      15,
        "min_data_gb":         8,
        "ignore_keywords":     [],
        "network":             "Rogers",
        "max_price_delta_pct": 25,
    },
    "Koodo": {
        "target_data_gb":      15,
        "min_data_gb":         8,
        "ignore_keywords":     [],
        "network":             "Telus",
        "max_price_delta_pct": 25,
    },
    "Vidéotron": {
        "target_data_gb":      15,
        "min_data_gb":         8,
        "ignore_keywords":     [],
        "network":             "Vidéotron",
        "max_price_delta_pct": 25,
    },
    "Public Mobile": {
        "target_data_gb":      15,
        "min_data_gb":         8,
        "ignore_keywords":     [],
        "network":             "Telus",
        "max_price_delta_pct": 25,
    },
    "Fizz": {
        "target_data_gb":      15,
        "min_data_gb":         8,
        "ignore_keywords":     ["bundle", "promo", "save", "économisez", "internet"],
        "network":             "Vidéotron",
        "max_price_delta_pct": 20,
    },
    "Lucky Mobile": {
        "target_data_gb":      15,
        "min_data_gb":         5,
        "ignore_keywords":     [],
        "network":             "Bell",
        "max_price_delta_pct": 25,
    },
    "Chatr": {
        "target_data_gb":      10,
        "min_data_gb":         5,
        "ignore_keywords":     [],
        "network":             "Rogers",
        "max_price_delta_pct": 25,
    },
}

_DEFAULT_CELL_CFG: dict = {
    "target_data_gb":      15,
    "min_data_gb":         8,
    "ignore_keywords":     [],
    "network":             "",
    "max_price_delta_pct": 30,
}

# ──────────────────────────────────────────────────────────────────────────────
#  FALLBACK INTERNET
# ──────────────────────────────────────────────────────────────────────────────
FALLBACK = [
    {"provider": "Vidéotron", "plan": "Internet 400", "speed_down": 400, "speed_up":  50, "price":  85.0, "type": "Câble", "note": "",                 "promo": False, "promo_note": "", "url": "https://www.videotron.com/en/internet",                         "scraped_ok": False},
    {"provider": "Bell",      "plan": "Fibre 500",    "speed_down": 500, "speed_up": 500, "price":  80.0, "type": "Fibre", "note": "",                 "promo": False, "promo_note": "", "url": "https://www.bell.ca/Bell_Internet/Internet_access",             "scraped_ok": False},
    {"provider": "Cogeco",    "plan": "Internet 400", "speed_down": 400, "speed_up":  20, "price":  75.0, "type": "Câble", "note": "",                 "promo": False, "promo_note": "", "url": "https://www.cogeco.ca/en/internet/packages",                    "scraped_ok": False},
    {"provider": "Fizz",      "plan": "Internet 400", "speed_down": 400, "speed_up":  20, "price":  60.0, "type": "Câble", "note": "Réseau Vidéotron", "promo": False, "promo_note": "", "url": "https://fizz.ca/en/internet",                                   "scraped_ok": False},
    {"provider": "EBOX",      "plan": "Internet 120", "speed_down": 120, "speed_up":  20, "price":  55.0, "type": "Câble", "note": "Réseau Vidéotron", "promo": False, "promo_note": "", "url": "https://www.ebox.ca/en/quebec/residential/internet-packages/", "scraped_ok": False},
    {"provider": "Start.ca",  "plan": "Cable 200",    "speed_down": 200, "speed_up":  15, "price":  50.0, "type": "Câble", "note": "Réseau Vidéotron", "promo": False, "promo_note": "", "url": "https://www.start.ca/services/high-speed-internet",             "scraped_ok": False},
    {"provider": "VMedia",    "plan": "Cable 120",    "speed_down": 120, "speed_up":  20, "price":  45.0, "type": "Câble", "note": "Réseau Bell",      "promo": False, "promo_note": "", "url": "https://www.vmedia.ca/en/homeinternet",                         "scraped_ok": False},
]

# ──────────────────────────────────────────────────────────────────────────────
#  FALLBACK CELLULAIRE
# ──────────────────────────────────────────────────────────────────────────────
CELL_FALLBACK = [
    {"provider": "Telus",         "data_gb": 15, "price": 95.0, "network": "Telus",     "plan_name": "15 Go", "url": "https://www.telus.com/en/mobility/plans",                                          "scraped_ok": False},
    {"provider": "Fido",          "data_gb": 15, "price": 65.0, "network": "Rogers",    "plan_name": "15 Go", "url": "https://www.fido.ca/fr/forfaits",                                                  "scraped_ok": False},
    {"provider": "Koodo",         "data_gb": 15, "price": 60.0, "network": "Telus",     "plan_name": "15 Go", "url": "https://www.koodomobile.com/en/shop/mobility/bring-your-own-phone",                "scraped_ok": False},
    {"provider": "Vidéotron",     "data_gb": 15, "price": 58.0, "network": "Vidéotron", "plan_name": "15 Go", "url": "https://www.videotron.com/en/mobile/cell-phone-plans",                             "scraped_ok": False},
    {"provider": "Public Mobile", "data_gb": 15, "price": 55.0, "network": "Telus",     "plan_name": "15 Go", "url": "https://www.publicmobile.ca/en/plans",                                             "scraped_ok": False},
    {"provider": "Fizz",          "data_gb": 15, "price": 50.0, "network": "Vidéotron", "plan_name": "15 Go", "url": "https://fizz.ca/en/mobile",                                                        "scraped_ok": False},
    {"provider": "Lucky Mobile",  "data_gb": 15, "price": 45.0, "network": "Bell",      "plan_name": "15 Go", "url": "https://www.luckymobile.ca/shop/plans",                                            "scraped_ok": False},
    {"provider": "Chatr",         "data_gb": 10, "price": 40.0, "network": "Rogers",    "plan_name": "10 Go", "url": "https://www.chatrwireless.com/plans",                                              "scraped_ok": False},
]

# ──────────────────────────────────────────────────────────────────────────────
#  STEALTH
# ──────────────────────────────────────────────────────────────────────────────
STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver',          { get: () => undefined });
    Object.defineProperty(navigator, 'languages',          { get: () => ['fr-CA','fr','en-CA','en'] });
    Object.defineProperty(navigator, 'platform',           { get: () => 'MacIntel' });
    Object.defineProperty(navigator, 'hardwareConcurrency',{ get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory',       { get: () => 8 });
    Object.defineProperty(navigator, 'plugins',            { get: () => [1, 2, 3, 4, 5] });
    window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
"""

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ──────────────────────────────────────────────────────────────────────────────
#  SÉLECTION & SANITY — Internet
# ──────────────────────────────────────────────────────────────────────────────

def select_plan_for_provider(provider: str, plans: list[ISPPlan]) -> ISPPlan | None:
    if not plans:
        return None
    cfg      = PROVIDER_CONFIG.get(provider, _DEFAULT_CFG)
    ignore   = [kw.lower() for kw in cfg.get("ignore_keywords", [])]
    preferred = cfg.get("preferred_speed_mbps", 200)
    min_spd   = cfg.get("min_speed_mbps", 50)

    filtered: list[ISPPlan] = []
    for p in plans:
        ctx = p.raw_meta.get("context_text", "").lower()
        if ignore and any(kw in ctx for kw in ignore):
            continue
        filtered.append(p)
    if not filtered:
        filtered = plans

    filtered = [p for p in filtered if 25 < p.price < 250]
    if not filtered:
        return None

    fast = [p for p in filtered if p.speed_down >= preferred]
    if fast:
        return min(fast, key=lambda p: p.price)

    ok = [p for p in filtered if p.speed_down >= min_spd]
    if ok:
        return min(ok, key=lambda p: p.price)

    return min(filtered, key=lambda p: p.price)


def check_price_sanity(provider: str, new_plan: ISPPlan, prev_prices: dict) -> bool:
    cfg       = PROVIDER_CONFIG.get(provider, _DEFAULT_CFG)
    max_delta = cfg.get("max_price_delta_pct", 30)
    if provider not in prev_prices:
        return True
    prev_price = prev_prices[provider].get("price", 0)
    if prev_price <= 0:
        return True
    delta_pct = abs(new_plan.price - prev_price) / prev_price * 100
    if delta_pct > max_delta:
        print(f"    ⚠ {provider}: sanity ISP — ${new_plan.price:.2f} vs ${prev_price:.2f} ({delta_pct:.1f}%)")
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
#  SÉLECTION & SANITY — Cellulaire
# ──────────────────────────────────────────────────────────────────────────────

def select_cell_plan_for_provider(provider: str, plans: list[CellPlan]) -> CellPlan | None:
    if not plans:
        return None
    cfg     = CELL_PROVIDER_CONFIG.get(provider, _DEFAULT_CELL_CFG)
    ignore  = [kw.lower() for kw in cfg.get("ignore_keywords", [])]
    target  = cfg.get("target_data_gb", 15)
    min_gb  = cfg.get("min_data_gb", 8)

    filtered: list[CellPlan] = []
    for p in plans:
        ctx = p.raw_meta.get("context_text", "").lower()
        if ignore and any(kw in ctx for kw in ignore):
            continue
        filtered.append(p)
    if not filtered:
        filtered = plans

    # Exclure "illimité" (999) et prix hors plage
    filtered = [p for p in filtered if p.data_gb < 900 and 15 < p.price < 200]
    if not filtered:
        return None

    at_target = [p for p in filtered if p.data_gb >= target]
    if at_target:
        return min(at_target, key=lambda p: p.price)

    at_min = [p for p in filtered if p.data_gb >= min_gb]
    if at_min:
        return min(at_min, key=lambda p: p.price)

    return min(filtered, key=lambda p: p.price)


def check_cell_price_sanity(provider: str, new_plan: CellPlan, prev_prices: dict) -> bool:
    cfg       = CELL_PROVIDER_CONFIG.get(provider, _DEFAULT_CELL_CFG)
    max_delta = cfg.get("max_price_delta_pct", 30)
    if provider not in prev_prices:
        return True
    prev_price = prev_prices[provider].get("price", 0)
    if prev_price <= 0:
        return True
    delta_pct = abs(new_plan.price - prev_price) / prev_price * 100
    if delta_pct > max_delta:
        print(f"    ⚠ {provider}: sanity cell — ${new_plan.price:.2f} vs ${prev_price:.2f} ({delta_pct:.1f}%)")
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
#  HELPERS — extraction Internet
# ──────────────────────────────────────────────────────────────────────────────

def extract_price(text: str) -> float | None:
    text = text.replace("\xa0", " ").replace(",", ".")
    for m in re.finditer(r"\b(\d{2,3}(?:\.\d{1,2})?)\b", text):
        val = float(m.group(1))
        if 25 < val < 250:
            return val
    return None


def extract_speed_mbps(text: str) -> int | None:
    text = text.upper()
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*GBPS", text):
        val = int(float(m.group(1)) * 1000)
        if 100 <= val <= 10000:
            return val
    for m in re.finditer(r"(\d+)\s*MBPS", text):
        val = int(m.group(1))
        if 20 <= val <= 10000:
            return val
    return None


def plans_from_text(text: str) -> list[dict]:
    plans = []
    for m in re.finditer(
        r"(\d{2,4})\s*Mbps.{0,300}?\$\s*(\d{2,3}(?:\.\d{1,2})?)",
        text, re.S | re.I
    ):
        speed, price = int(m.group(1)), float(m.group(2))
        if 20 <= speed <= 5000 and 25 < price < 250:
            plans.append({"speed_down": speed, "price": price, "plan": f"{speed} Mbps"})
    for m in re.finditer(
        r"\$\s*(\d{2,3}(?:\.\d{1,2})?).{0,300}?(\d{2,4})\s*Mbps",
        text, re.S | re.I
    ):
        price, speed = float(m.group(1)), int(m.group(2))
        if 20 <= speed <= 5000 and 25 < price < 250:
            plans.append({"speed_down": speed, "price": price, "plan": f"{speed} Mbps"})
    for m in re.finditer(
        r"(\d(?:\.\d)?)\s*Gbps.{0,300}?\$\s*(\d{2,3}(?:\.\d{1,2})?)",
        text, re.S | re.I
    ):
        speed = int(float(m.group(1)) * 1000)
        price = float(m.group(2))
        if 100 <= speed <= 10000 and 25 < price < 250:
            plans.append({"speed_down": speed, "price": price, "plan": f"{speed} Mbps"})
    seen: set = set()
    unique = []
    for p in plans:
        k = (p["speed_down"], round(p["price"]))
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique


def plans_from_json(json_text: str) -> list[dict]:
    plans = []
    for m in re.finditer(
        r'"(?:speed_down|download|downloadSpeed|speed|bandwidth|megabits|download_speed)"\s*:\s*"?(\d+)"?',
        json_text, re.I
    ):
        speed = int(m.group(1))
        if not (20 <= speed <= 5000):
            continue
        ctx = json_text[max(0, m.start()-800): min(len(json_text), m.end()+800)]
        for pm in re.finditer(
            r'"(?:price|amount|cost|monthly|monthlyPrice|pricePerMonth|regularPrice|salePrice|basePrice)"\s*:\s*"?(\d{2,3}(?:\.\d{1,2})?)"?',
            ctx, re.I
        ):
            price = float(pm.group(1))
            if 25 < price < 250:
                plans.append({"speed_down": speed, "price": price, "plan": f"{speed} Mbps"})
    for m in re.finditer(r'"(\d{2,4})\s*[Mm]bps"', json_text):
        speed = int(m.group(1))
        if not (20 <= speed <= 5000):
            continue
        ctx = json_text[max(0, m.start()-600): min(len(json_text), m.end()+600)]
        for pm in re.finditer(r'"(\d{2,3}(?:\.\d{1,2})?)"', ctx):
            price = float(pm.group(1))
            if 25 < price < 250:
                plans.append({"speed_down": speed, "price": price, "plan": f"{speed} Mbps"})
    seen: set = set()
    unique = []
    for p in plans:
        k = (p["speed_down"], round(p["price"]))
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique


def plans_from_displayed_price(text: str) -> list[dict]:
    plans = []
    for m in re.finditer(
        r"(\d{2,4})\s*Mbps.{0,400}?\$\s*(\d{2,3}(?:\.\d{1,2})?)\s*/\s*(?:month|mois)",
        text, re.S | re.I
    ):
        speed, price = int(m.group(1)), float(m.group(2))
        if 20 <= speed <= 5000 and 25 < price < 250:
            plans.append({"speed_down": speed, "price": price, "plan": f"{speed} Mbps"})
    for m in re.finditer(
        r"\$\s*(\d{2,3}(?:\.\d{1,2})?)\s*/\s*(?:month|mois).{0,400}?(\d{2,4})\s*Mbps",
        text, re.S | re.I
    ):
        price, speed = float(m.group(1)), int(m.group(2))
        if 20 <= speed <= 5000 and 25 < price < 250:
            plans.append({"speed_down": speed, "price": price, "plan": f"{speed} Mbps"})
    seen: set = set()
    unique = []
    for p in plans:
        k = (p["speed_down"], round(p["price"]))
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique


# Alias pour compatibilité tests
_plans_from_displayed_price = plans_from_displayed_price


def _dicts_to_isplans(provider: str, url: str, dicts: list[dict],
                      source: str = "text", context_text: str = "") -> list[ISPPlan]:
    return [
        ISPPlan(
            provider=provider,
            speed_down=d["speed_down"],
            speed_up=0,
            price=d["price"],
            source=source,
            url=url,
            raw_meta={"context_text": context_text.lower()},
        )
        for d in dicts
    ]


# ──────────────────────────────────────────────────────────────────────────────
#  HELPERS — extraction Cellulaire
# ──────────────────────────────────────────────────────────────────────────────

def cell_plans_from_json(json_text: str) -> list[dict]:
    """
    Extrait des plans cellulaires depuis le texte d'une réponse JSON.
    Cherche les champs data (Go) co-localisés avec un champ prix.
    """
    plans = []

    # Champ données numérique (en Go/GB)
    for m in re.finditer(
        r'"(?:data|dataAllowance|dataAmount|dataInGB|dataGB|dataGb|gb|gigabytes|'
        r'mobileData|dataLimitGB|dataSize|includedData|totalData|dataCapacity|'
        r'dataValue|dataIncluded|planData)"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        json_text, re.I
    ):
        gb = float(m.group(1))
        if not (1 <= gb <= 200):
            continue
        gb_int = int(gb)
        ctx = json_text[max(0, m.start()-800): min(len(json_text), m.end()+800)]
        for pm in re.finditer(
            r'"(?:price|amount|cost|monthly|monthlyPrice|pricePerMonth|regularPrice|'
            r'salePrice|basePrice|monthlyFee|monthlyCharge|totalMonthly|monthlyRate|'
            r'recurringCharge|rateAmount|planPrice|rate|fee)"\s*:\s*"?(\d{2,3}(?:\.\d{1,2})?)"?',
            ctx, re.I
        ):
            price = float(pm.group(1))
            if 15 < price < 200:
                plans.append({"data_gb": gb_int, "price": price,
                              "plan_name": f"{gb_int} Go"})

    # Champ données sous forme "15 GB" dans le JSON
    for m in re.finditer(r'"(\d+)\s*(?:GB|Go|gb|go)"', json_text):
        gb_int = int(m.group(1))
        if not (1 <= gb_int <= 200):
            continue
        ctx = json_text[max(0, m.start()-600): min(len(json_text), m.end()+600)]
        for pm in re.finditer(r'"(\d{2,3}(?:\.\d{1,2})?)"', ctx):
            price = float(pm.group(1))
            if 15 < price < 200:
                plans.append({"data_gb": gb_int, "price": price,
                              "plan_name": f"{gb_int} Go"})

    seen: set = set()
    unique = []
    for p in plans:
        k = (p["data_gb"], round(p["price"]))
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique


def cell_plans_from_text(text: str) -> list[dict]:
    """
    Extrait des plans cellulaires depuis le texte d'une page rendue.
    Cherche les paires (données en Go, prix en $).
    """
    plans = []

    # Pattern 1 : "15 Go ... $XX/mois"
    for m in re.finditer(
        r"(\d+)\s*(?:GB|Go)\b.{0,300}?\$\s*(\d{2,3}(?:\.\d{1,2})?)\s*/\s*(?:month|mois|mo\b)",
        text, re.S | re.I
    ):
        gb, price = int(m.group(1)), float(m.group(2))
        if 1 <= gb <= 200 and 15 < price < 200:
            plans.append({"data_gb": gb, "price": price, "plan_name": f"{gb} Go"})

    # Pattern 2 : "$XX/mois ... 15 Go"
    for m in re.finditer(
        r"\$\s*(\d{2,3}(?:\.\d{1,2})?)\s*/\s*(?:month|mois|mo\b).{0,300}?(\d+)\s*(?:GB|Go)\b",
        text, re.S | re.I
    ):
        price, gb = float(m.group(1)), int(m.group(2))
        if 1 <= gb <= 200 and 15 < price < 200:
            plans.append({"data_gb": gb, "price": price, "plan_name": f"{gb} Go"})

    # Pattern 3 : prix sans "/mois" explicite mais avec Go proche (± 50 chars)
    for m in re.finditer(
        r"(\d+)\s*(?:GB|Go)\b.{0,50}?\$\s*(\d{2,3}(?:\.\d{1,2})?)\b",
        text, re.S | re.I
    ):
        gb, price = int(m.group(1)), float(m.group(2))
        if 1 <= gb <= 200 and 15 < price < 200:
            plans.append({"data_gb": gb, "price": price, "plan_name": f"{gb} Go"})

    seen: set = set()
    unique = []
    for p in plans:
        k = (p["data_gb"], round(p["price"]))
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique


def _dicts_to_cellplans(provider: str, url: str, dicts: list[dict],
                        source: str = "text", context_text: str = "") -> list[CellPlan]:
    cfg     = CELL_PROVIDER_CONFIG.get(provider, _DEFAULT_CELL_CFG)
    network = cfg.get("network", "")
    return [
        CellPlan(
            provider=provider,
            data_gb=d["data_gb"],
            price=d["price"],
            network=network,
            plan_name=d.get("plan_name", f"{d['data_gb']} Go"),
            source=source,
            url=url,
            raw_meta={"context_text": context_text.lower()},
        )
        for d in dicts
    ]


def load_previous_prices() -> dict:
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            return {p["provider"]: p for p in json.load(f).get("plans", [])}
    return {}


def load_previous_cell_prices() -> dict:
    if os.path.exists(CELL_OUTPUT_PATH):
        with open(CELL_OUTPUT_PATH, "r", encoding="utf-8") as f:
            return {p["provider"]: p for p in json.load(f).get("plans", [])}
    return {}


def log(symbol: str, provider: str, msg: str):
    print(f"  {symbol}  {provider:<14} {msg}")


# ──────────────────────────────────────────────────────────────────────────────
#  HELPERS PLAYWRIGHT COMMUNS
# ──────────────────────────────────────────────────────────────────────────────

async def _navigate_and_intercept(page, url: str, extra_wait_ms: int = 0) -> list[dict]:
    """Navigue + intercepte JSON → retourne list[dict] (speed_down, price)."""
    captured: list[dict] = []

    async def on_response(response):
        if response.status != 200:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            body = await response.text()
            if len(body) < 30 or len(body) > 3_000_000:
                return
            found = plans_from_json(body)
            captured.extend(found)
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await page.goto(url, timeout=45000, wait_until="networkidle")
    except Exception:
        try:
            await page.wait_for_timeout(extra_wait_ms or 8000)
        except Exception:
            pass

    if extra_wait_ms:
        await page.wait_for_timeout(extra_wait_ms)

    return captured


async def _navigate_and_intercept_cell(page, url: str,
                                        extra_wait_ms: int = 0) -> list[dict]:
    """Navigue + intercepte JSON → retourne list[dict] (data_gb, price)."""
    captured: list[dict] = []

    async def on_response(response):
        if response.status != 200:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            body = await response.text()
            if len(body) < 30 or len(body) > 3_000_000:
                return
            found = cell_plans_from_json(body)
            captured.extend(found)
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await page.goto(url, timeout=45000, wait_until="networkidle")
    except Exception:
        try:
            await page.wait_for_timeout(extra_wait_ms or 8000)
        except Exception:
            pass

    if extra_wait_ms:
        await page.wait_for_timeout(extra_wait_ms)

    return captured


async def _dom_fallback_dicts(page) -> list[dict]:
    """Fallback DOM → list[dict] (speed_down, price)."""
    try:
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")
        for script in soup.find_all("script"):
            raw = script.string or ""
            if not raw or len(raw) < 50:
                continue
            found = plans_from_json(raw)
            if found:
                return found
        text = soup.get_text(" ")
        return plans_from_text(text)
    except Exception:
        return []


async def _dom_fallback_cell_dicts(page) -> list[dict]:
    """Fallback DOM → list[dict] (data_gb, price) pour cellulaire."""
    try:
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")
        for script in soup.find_all("script"):
            raw = script.string or ""
            if not raw or len(raw) < 50:
                continue
            found = cell_plans_from_json(raw)
            if found:
                return found
        text = soup.get_text(" ")
        return cell_plans_from_text(text)
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────────────
#  SCRAPERS INTERNET — retournent list[ISPPlan]
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_videotron_pw(page) -> list[ISPPlan]:
    URL = "https://www.videotron.com/en/internet"
    try:
        captured = await _navigate_and_intercept(page, URL)
        if captured:
            return _dicts_to_isplans("Vidéotron", URL, captured, source="api")
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")
        next_data = soup.find("script", {"id": "__NEXT_DATA__"})
        if next_data and next_data.string:
            found = plans_from_json(next_data.string)
            if found:
                return _dicts_to_isplans("Vidéotron", URL, found, source="next_data")
        for script in soup.find_all("script"):
            raw = script.string or ""
            if not raw or len(raw) < 100:
                continue
            if not any(k in raw for k in ("price", "prix", "speed", "vitesse", "Mbps")):
                continue
            found = plans_from_json(raw)
            if found:
                return _dicts_to_isplans("Vidéotron", URL, found, source="json_ld")
        text = soup.get_text(" ")
        return _dicts_to_isplans("Vidéotron", URL, plans_from_text(text), source="text")
    except Exception as e:
        print(f"    videotron error: {e}")
    return []


async def scrape_bell_pw(page) -> list[ISPPlan]:
    URL = "https://www.bell.ca/Bell_Internet/Internet_access"
    try:
        captured = await _navigate_and_intercept(page, URL, extra_wait_ms=5000)
        if captured:
            return _dicts_to_isplans("Bell", URL, captured, source="api")
        fallback = await _dom_fallback_dicts(page)
        return _dicts_to_isplans("Bell", URL, fallback, source="text")
    except Exception as e:
        print(f"    bell error: {e}")
    return []


async def scrape_cogeco_pw(page) -> list[ISPPlan]:
    URL = "https://www.cogeco.ca/en/internet/packages"
    try:
        captured = await _navigate_and_intercept(page, URL, extra_wait_ms=5000)
        if captured:
            return _dicts_to_isplans("Cogeco", URL, captured, source="api")
        fallback = await _dom_fallback_dicts(page)
        return _dicts_to_isplans("Cogeco", URL, fallback, source="text")
    except Exception as e:
        print(f"    cogeco error: {e}")
    return []


async def scrape_ebox_pw(page) -> list[ISPPlan]:
    URL = "https://www.ebox.ca/en/quebec/residential/internet-packages/"
    try:
        captured = await _navigate_and_intercept(page, URL)
        if captured:
            return _dicts_to_isplans("EBOX", URL, captured, source="api")
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")
        plans: list[dict] = []
        for el in soup.find_all(attrs={"data-speed": True}):
            speed = extract_speed_mbps(str(el.get("data-speed", "")) + " Mbps")
            price = extract_price(str(el.get("data-price", "") or el.get("data-amount", "")))
            if speed and price:
                plans.append({"speed_down": speed, "price": price, "plan": f"Internet {speed}"})
        if plans:
            return _dicts_to_isplans("EBOX", URL, plans, source="dom")
        fallback = await _dom_fallback_dicts(page)
        return _dicts_to_isplans("EBOX", URL, fallback, source="text")
    except Exception as e:
        print(f"    ebox error: {e}")
    return []


async def scrape_vmedia_pw(page) -> list[ISPPlan]:
    URL = "https://www.vmedia.ca/en/homeinternet"
    try:
        await page.goto(URL, timeout=40000, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(".new-internet-package", timeout=15000)
        except PWTimeout:
            await page.wait_for_timeout(10000)
        cards = await page.locator(".new-internet-package").all()
        plans: list[ISPPlan] = []
        for card in cards:
            try:
                pi_els = await card.locator(".homeinternet-price__integer").all_inner_texts()
                pd_els = await card.locator(".homeinternet-price__decimal").all_inner_texts()
                sp_els = await card.locator(".plans-tile__speed-item-count").all_inner_texts()
                if not pi_els:
                    continue
                int_part = re.sub(r"[^\d]", "", pi_els[0])
                dec_part = re.sub(r"[^\d]", "", pd_els[0]) if pd_els else "0"
                if not int_part:
                    continue
                price = float(f"{int_part}.{dec_part}" if dec_part else int_part)
                speed_val = None
                if sp_els:
                    raw_spd = re.sub(r"[^\d]", "", sp_els[0])
                    if raw_spd:
                        speed_val = int(raw_spd)
                if 25 < price < 250 and speed_val and 20 <= speed_val <= 1000:
                    plans.append(ISPPlan(
                        provider="VMedia", speed_down=speed_val, speed_up=0,
                        price=price, source="dom", url=URL,
                    ))
            except Exception:
                continue
        if plans:
            return plans
        fallback = await _dom_fallback_dicts(page)
        return _dicts_to_isplans("VMedia", URL, fallback, source="text")
    except Exception as e:
        print(f"    vmedia error: {e}")
    return []


async def scrape_fizz_pw(page) -> list[ISPPlan]:
    """Fizz internet — DOM visible uniquement via page.evaluate()."""
    URL = "https://fizz.ca/en/internet"
    try:
        await page.goto(URL, timeout=45000, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(
                "#internetPlanCards > *, #internetPlanCards .card, "
                "#internetPlanCards [class*='plan']",
                timeout=20000
            )
        except PWTimeout:
            await page.wait_for_timeout(10000)

        card_texts: list[str] = await page.evaluate("""() => {
            const container = document.querySelector('#internetPlanCards');
            if (!container) return [document.body.innerText || ''];
            const selectors = ['[class*="plan"]','[class*="card"]','[class*="package"]','article','[data-plan]'];
            for (const sel of selectors) {
                const cards = Array.from(container.querySelectorAll(sel)).filter(el => {
                    const s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                });
                if (cards.length >= 2) return cards.map(el => el.innerText || el.textContent || '');
            }
            return [container.innerText || container.textContent || ''];
        }""")

        plans: list[ISPPlan] = []
        for card_text in card_texts:
            if not card_text.strip():
                continue
            dicts = plans_from_displayed_price(card_text) or plans_from_text(card_text)
            for d in dicts:
                plans.append(ISPPlan(
                    provider="Fizz", speed_down=d["speed_down"], speed_up=0,
                    price=d["price"], source="dom", url=URL,
                    raw_meta={"context_text": card_text.lower()},
                ))
        if plans:
            return plans
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")
        container = soup.find(id="internetPlanCards")
        search_text = (
            container.get_text(" ")
            if (container and container.get_text(strip=True))
            else soup.get_text(" ")
        )
        dicts = plans_from_displayed_price(search_text) or plans_from_text(search_text)
        return _dicts_to_isplans("Fizz", URL, dicts, source="text", context_text=search_text)
    except Exception as e:
        print(f"    fizz isp error: {e}")
    return []


async def scrape_startca_pw(page) -> list[ISPPlan]:
    URL = "https://www.start.ca/services/high-speed-internet"
    POSTAL = "H3A 1A1"
    start_captured: list[dict] = []

    async def on_start_response(response):
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            body = await response.text()
            if len(body) < 30:
                return
            found = plans_from_json(body)
            start_captured.extend(found)
        except Exception:
            pass

    page.on("response", on_start_response)

    try:
        await page.goto(URL, timeout=40000, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        if start_captured:
            return _dicts_to_isplans("Start.ca", URL, start_captured, source="api")
        for sel in [
            "input[name*='postal' i]", "input[name*='address' i]",
            "input[placeholder*='postal' i]", "input[placeholder*='code' i]",
            "#postal", "#postalCode", "#postal_code",
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(POSTAL, timeout=5000)
                    await page.wait_for_timeout(500)
                    await el.press("Enter")
                    await page.wait_for_timeout(5000)
                    break
            except Exception:
                continue
        if start_captured:
            return _dicts_to_isplans("Start.ca", URL, start_captured, source="api")
        fallback = await _dom_fallback_dicts(page)
        return _dicts_to_isplans("Start.ca", URL, fallback, source="text")
    except Exception as e:
        print(f"    startca error: {e}")
    return []


# ──────────────────────────────────────────────────────────────────────────────
#  ORCHESTRATEUR INTERNET
# ──────────────────────────────────────────────────────────────────────────────

ISP_SCRAPERS = [
    ("Vidéotron", scrape_videotron_pw, "Câble", "",                  "https://www.videotron.com/en/internet"),
    ("Bell",      scrape_bell_pw,      "Fibre", "",                  "https://www.bell.ca/Bell_Internet/Internet_access"),
    ("Cogeco",    scrape_cogeco_pw,    "Câble", "",                  "https://www.cogeco.ca/en/internet/packages"),
    ("Fizz",      scrape_fizz_pw,      "Câble", "Réseau Vidéotron",  "https://fizz.ca/en/internet"),
    ("EBOX",      scrape_ebox_pw,      "Câble", "Réseau Vidéotron",  "https://www.ebox.ca/en/quebec/residential/internet-packages/"),
    ("VMedia",    scrape_vmedia_pw,    "Câble", "Réseau Bell",       "https://www.vmedia.ca/en/homeinternet"),
    ("Start.ca",  scrape_startca_pw,  "Câble", "Réseau Vidéotron",  "https://www.start.ca/services/high-speed-internet"),
]


async def run_all_scrapers() -> tuple[list, int, int]:
    prev = load_previous_prices()
    results = []
    scraped_count = 0
    fallback_count = 0

    print("\n📡  Démarrage du scraping Internet — Québec\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage", "--disable-gpu", "--window-size=1440,900",
            ]
        )
        context = await browser.new_context(
            user_agent=USER_AGENT, locale="fr-CA", timezone_id="America/Montreal",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            }
        )
        await context.add_init_script(STEALTH_SCRIPT)

        for provider, scrape_fn, conn_type, note, url in ISP_SCRAPERS:
            fb   = next((f for f in FALLBACK if f["provider"] == provider), {})
            page = await context.new_page()
            plans: list[ISPPlan] = []
            try:
                plans = await scrape_fn(page)
            except Exception as e:
                print(f"    {provider} exception: {e}")
            finally:
                await page.close()

            selected = select_plan_for_provider(provider, plans)
            if selected and not check_price_sanity(provider, selected, prev):
                selected = None

            if selected:
                entry = {
                    **fb,
                    "plan": f"{selected.speed_down} Mbps", "speed_down": selected.speed_down,
                    "speed_up": selected.speed_up, "price": selected.price,
                    "type": conn_type, "note": note, "url": url,
                    "scraped_ok": True, "promo": selected.is_promo,
                    "promo_note": selected.promo_note,
                }
                results.append(entry)
                scraped_count += 1
                log("✓", provider, f"{selected.speed_down} Mbps — ${selected.price:.2f}/mois")
            else:
                if provider in prev:
                    entry = {**prev[provider], "url": url, "scraped_ok": False}
                    log("↩", provider, f"échec → prix précédent (${prev[provider]['price']:.2f})")
                else:
                    entry = {**fb, "scraped_ok": False}
                    log("⚠", provider, f"échec → valeur par défaut (${fb.get('price', '?')})")
                results.append(entry)
                fallback_count += 1

        await browser.close()

    results.sort(key=lambda x: x.get("price", 0), reverse=True)
    print(f"\n{'─'*55}")
    print(f"  ✅  {scraped_count} forfait(s) internet scrapé(s)")
    print(f"  ↩   {fallback_count} forfait(s) en fallback / prix précédent")
    print(f"{'─'*55}\n")

    return results, scraped_count, fallback_count


# ──────────────────────────────────────────────────────────────────────────────
#  SCRAPERS CELLULAIRES — retournent list[CellPlan]
# ──────────────────────────────────────────────────────────────────────────────

async def _scrape_cell_generic(page, provider: str, url: str,
                                extra_wait_ms: int = 0) -> list[CellPlan]:
    """
    Scraper générique cellulaire :
    1. Interception des réponses API JSON
    2. Scripts inline (Next.js, window.__STATE__, etc.)
    3. Texte DOM complet
    """
    try:
        captured = await _navigate_and_intercept_cell(page, url,
                                                       extra_wait_ms=extra_wait_ms)
        if captured:
            return _dicts_to_cellplans(provider, url, captured, source="api")

        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        # Scripts inline
        for script in soup.find_all("script"):
            raw = script.string or ""
            if not raw or len(raw) < 50:
                continue
            found = cell_plans_from_json(raw)
            if found:
                return _dicts_to_cellplans(provider, url, found, source="json_ld")

        # Texte complet
        text = soup.get_text(" ")
        found = cell_plans_from_text(text)
        return _dicts_to_cellplans(provider, url, found, source="text",
                                    context_text=text)
    except Exception as e:
        print(f"    {provider} cell error: {e}")
    return []


async def scrape_telus_cell_pw(page) -> list[CellPlan]:
    """Telus — React SPA, interception API."""
    return await _scrape_cell_generic(
        page, "Telus", "https://www.telus.com/en/mobility/plans",
        extra_wait_ms=3000
    )


async def scrape_fido_cell_pw(page) -> list[CellPlan]:
    """Fido — React SPA, site français."""
    return await _scrape_cell_generic(
        page, "Fido", "https://www.fido.ca/fr/forfaits",
        extra_wait_ms=3000
    )


async def scrape_koodo_cell_pw(page) -> list[CellPlan]:
    """Koodo — BYOP page (plans sans appareil)."""
    return await _scrape_cell_generic(
        page, "Koodo",
        "https://www.koodomobile.com/en/shop/mobility/bring-your-own-phone",
        extra_wait_ms=3000
    )


async def scrape_videotron_cell_pw(page) -> list[CellPlan]:
    """
    Vidéotron mobile — Next.js.
    Essaie __NEXT_DATA__ en priorité, puis interception API.
    """
    URL = "https://www.videotron.com/en/mobile/cell-phone-plans"
    try:
        captured = await _navigate_and_intercept_cell(page, URL, extra_wait_ms=2000)
        if captured:
            return _dicts_to_cellplans("Vidéotron", URL, captured, source="api")

        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        next_data = soup.find("script", {"id": "__NEXT_DATA__"})
        if next_data and next_data.string:
            found = cell_plans_from_json(next_data.string)
            if found:
                return _dicts_to_cellplans("Vidéotron", URL, found, source="next_data")

        for script in soup.find_all("script"):
            raw = script.string or ""
            if not raw or len(raw) < 100:
                continue
            if not any(k in raw for k in ("price", "prix", "GB", "Go", "data")):
                continue
            found = cell_plans_from_json(raw)
            if found:
                return _dicts_to_cellplans("Vidéotron", URL, found, source="json_ld")

        text = soup.get_text(" ")
        found = cell_plans_from_text(text)
        return _dicts_to_cellplans("Vidéotron", URL, found, source="text",
                                    context_text=text)
    except Exception as e:
        print(f"    videotron cell error: {e}")
    return []


async def scrape_publicmobile_cell_pw(page) -> list[CellPlan]:
    """Public Mobile — SPA Telus budget."""
    return await _scrape_cell_generic(
        page, "Public Mobile", "https://www.publicmobile.ca/en/plans",
        extra_wait_ms=4000
    )


async def scrape_fizz_cell_pw(page) -> list[CellPlan]:
    """
    Fizz mobile — même Drupal que l'internet.
    Utilise page.evaluate() pour extraire uniquement les éléments visibles,
    par carte de forfait.
    """
    URL = "https://fizz.ca/en/mobile"
    try:
        await page.goto(URL, timeout=45000, wait_until="domcontentloaded")

        try:
            await page.wait_for_selector(
                "#mobilePlanCards > *, #mobilePlanCards .card, "
                "#mobilePlanCards [class*='plan'], "
                "[class*='plan-card'], [class*='planCard']",
                timeout=20000
            )
        except PWTimeout:
            await page.wait_for_timeout(10000)

        card_texts: list[str] = await page.evaluate("""() => {
            // Cherche le container des forfaits mobiles
            const container = (
                document.querySelector('#mobilePlanCards') ||
                document.querySelector('#cellPlanCards') ||
                document.querySelector('[class*="plan-cards"]') ||
                document.querySelector('[class*="planCards"]') ||
                document.querySelector('main')
            );
            if (!container) return [document.body.innerText || ''];
            const selectors = ['[class*="plan"]','[class*="card"]','[class*="package"]','article'];
            for (const sel of selectors) {
                const cards = Array.from(container.querySelectorAll(sel)).filter(el => {
                    const s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                });
                if (cards.length >= 2) return cards.map(el => el.innerText || el.textContent || '');
            }
            return [container.innerText || container.textContent || ''];
        }""")

        plans: list[CellPlan] = []
        cfg     = CELL_PROVIDER_CONFIG["Fizz"]
        network = cfg["network"]
        for card_text in card_texts:
            if not card_text.strip():
                continue
            dicts = cell_plans_from_text(card_text)
            for d in dicts:
                plans.append(CellPlan(
                    provider="Fizz", data_gb=d["data_gb"], price=d["price"],
                    network=network, plan_name=d.get("plan_name", f"{d['data_gb']} Go"),
                    source="dom", url=URL,
                    raw_meta={"context_text": card_text.lower()},
                ))
        if plans:
            return plans

        # Fallback texte complet
        fallback = await _dom_fallback_cell_dicts(page)
        return _dicts_to_cellplans("Fizz", URL, fallback, source="text")

    except Exception as e:
        print(f"    fizz cell error: {e}")
    return []


async def scrape_luckymobile_cell_pw(page) -> list[CellPlan]:
    """Lucky Mobile — Bell budget brand."""
    return await _scrape_cell_generic(
        page, "Lucky Mobile", "https://www.luckymobile.ca/shop/plans",
        extra_wait_ms=3000
    )


async def scrape_chatr_cell_pw(page) -> list[CellPlan]:
    """Chatr — Rogers budget brand."""
    return await _scrape_cell_generic(
        page, "Chatr", "https://www.chatrwireless.com/plans",
        extra_wait_ms=3000
    )


# ──────────────────────────────────────────────────────────────────────────────
#  ORCHESTRATEUR CELLULAIRE
# ──────────────────────────────────────────────────────────────────────────────

CELL_SCRAPERS = [
    ("Telus",         scrape_telus_cell_pw,        "Telus",     "https://www.telus.com/en/mobility/plans"),
    ("Fido",          scrape_fido_cell_pw,          "Rogers",    "https://www.fido.ca/fr/forfaits"),
    ("Koodo",         scrape_koodo_cell_pw,         "Telus",     "https://www.koodomobile.com/en/shop/mobility/bring-your-own-phone"),
    ("Vidéotron",     scrape_videotron_cell_pw,     "Vidéotron", "https://www.videotron.com/en/mobile/cell-phone-plans"),
    ("Public Mobile", scrape_publicmobile_cell_pw,  "Telus",     "https://www.publicmobile.ca/en/plans"),
    ("Fizz",          scrape_fizz_cell_pw,          "Vidéotron", "https://fizz.ca/en/mobile"),
    ("Lucky Mobile",  scrape_luckymobile_cell_pw,   "Bell",      "https://www.luckymobile.ca/shop/plans"),
    ("Chatr",         scrape_chatr_cell_pw,         "Rogers",    "https://www.chatrwireless.com/plans"),
]


async def run_cell_scraper() -> tuple[list, int, int]:
    prev = load_previous_cell_prices()
    results: list[dict] = []
    scraped_count  = 0
    fallback_count = 0

    print("\n📱  Démarrage du scraping Cellulaire — Québec\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage", "--disable-gpu", "--window-size=1440,900",
            ]
        )
        context = await browser.new_context(
            user_agent=USER_AGENT, locale="fr-CA", timezone_id="America/Montreal",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            }
        )
        await context.add_init_script(STEALTH_SCRIPT)

        for provider, scrape_fn, network, url in CELL_SCRAPERS:
            fb   = next((f for f in CELL_FALLBACK if f["provider"] == provider), {})
            page = await context.new_page()
            plans: list[CellPlan] = []
            try:
                plans = await scrape_fn(page)
            except Exception as e:
                print(f"    {provider} cell exception: {e}")
            finally:
                await page.close()

            selected = select_cell_plan_for_provider(provider, plans)
            if selected and not check_cell_price_sanity(provider, selected, prev):
                selected = None

            if selected:
                entry = {
                    **fb,
                    "provider":   provider,
                    "data_gb":    selected.data_gb,
                    "price":      selected.price,
                    "network":    network,
                    "plan_name":  selected.plan_name or f"{selected.data_gb} Go",
                    "url":        url,
                    "scraped_ok": True,
                }
                results.append(entry)
                scraped_count += 1
                log("✓", provider, f"{selected.data_gb} Go — ${selected.price:.2f}/mois")
            else:
                if provider in prev:
                    entry = {**prev[provider], "url": url, "scraped_ok": False}
                    log("↩", provider, f"échec → prix précédent (${prev[provider]['price']:.2f})")
                else:
                    entry = {**fb, "scraped_ok": False}
                    log("⚠", provider, f"échec → valeur par défaut (${fb.get('price', '?')})")
                results.append(entry)
                fallback_count += 1

        await browser.close()

    results.sort(key=lambda x: x.get("price", 0), reverse=True)
    print(f"\n{'─'*55}")
    print(f"  ✅  {scraped_count} forfait(s) cellulaire(s) scrapé(s)")
    print(f"  ↩   {fallback_count} forfait(s) en fallback / prix précédent")
    print(f"{'─'*55}\n")

    return results, scraped_count, fallback_count


# ──────────────────────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    isp_plans, isp_scraped, isp_fallback = await run_all_scrapers()
    isp_output = {
        "updated_at":     now,
        "scraped_count":  isp_scraped,
        "fallback_count": isp_fallback,
        "region":         "quebec",
        "plans":          isp_plans,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(isp_output, f, ensure_ascii=False, indent=2)
    print(f"💾  Internet sauvegardé → {OUTPUT_PATH}")

    cell_plans, cell_scraped, cell_fallback = await run_cell_scraper()
    cell_output = {
        "updated_at":     now,
        "scraped_count":  cell_scraped,
        "fallback_count": cell_fallback,
        "region":         "quebec",
        "plans":          cell_plans,
    }
    os.makedirs(os.path.dirname(CELL_OUTPUT_PATH), exist_ok=True)
    with open(CELL_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(cell_output, f, ensure_ascii=False, indent=2)
    print(f"💾  Cellulaire sauvegardé → {CELL_OUTPUT_PATH}")

    if isp_scraped == 0 and cell_scraped == 0:
        print("⚠️  AVERTISSEMENT : aucun scraping réussi — que des fallbacks.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
