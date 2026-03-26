"""
Tests unitaires pour scraper/scrape.py
Couvre les fonctions pures (pas de Playwright requis).
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from scraper.scrape import (
    ISPPlan,
    CellPlan,
    PROVIDER_CONFIG,
    CELL_PROVIDER_CONFIG,
    plans_from_text,
    plans_from_displayed_price,
    select_plan_for_provider,
    check_price_sanity,
    _dicts_to_isplans,
    cell_plans_from_text,
    cell_plans_from_json,
    select_cell_plan_for_provider,
    check_cell_price_sanity,
    _dicts_to_cellplans,
)


# ──────────────────────────────────────────────────────────────────────────────
#  plans_from_text
# ──────────────────────────────────────────────────────────────────────────────

def test_plans_from_text_basic():
    text = "Internet 400 Mbps for $65.00/month — best value"
    plans = plans_from_text(text)
    assert any(p["speed_down"] == 400 and p["price"] == 65.0 for p in plans)


def test_plans_from_text_reverse_order():
    text = "Only $49.95 per month — 200 Mbps download"
    plans = plans_from_text(text)
    assert any(p["speed_down"] == 200 and p["price"] == 49.95 for p in plans)


def test_plans_from_text_gbps():
    text = "1 Gbps blazing fast — $89.99/month"
    plans = plans_from_text(text)
    assert any(p["speed_down"] == 1000 and p["price"] == 89.99 for p in plans)


def test_plans_from_text_ignores_low_price():
    # Prices under 25 should be ignored (not internet plan prices)
    text = "200 Mbps for $10/month"
    plans = plans_from_text(text)
    assert not any(p["price"] == 10.0 for p in plans)


# ──────────────────────────────────────────────────────────────────────────────
#  plans_from_displayed_price
# ──────────────────────────────────────────────────────────────────────────────

def test_fizz_displayed_price_basic():
    """Prix avec '/month' doit être extrait correctement."""
    text = "200 Mbps Download\n$49.00 /month\nUnlimited data"
    plans = plans_from_displayed_price(text)
    assert len(plans) >= 1
    assert any(p["speed_down"] == 200 and p["price"] == 49.0 for p in plans)


def test_fizz_displayed_price_no_suffix_ignored():
    """Un prix sans '/month' ou '/mois' ne doit pas être extrait."""
    text = "Save $45 when you bundle 200 Mbps"
    plans = plans_from_displayed_price(text)
    # Le $45 n'a pas de /month — doit être ignoré
    assert not any(p["price"] == 45.0 for p in plans)


def test_fizz_displayed_price_mois():
    """Supporte '/mois' (français)."""
    text = "$59.00 /mois — 400 Mbps"
    plans = plans_from_displayed_price(text)
    assert any(p["speed_down"] == 400 and p["price"] == 59.0 for p in plans)


# ──────────────────────────────────────────────────────────────────────────────
#  select_plan_for_provider — Fizz
# ──────────────────────────────────────────────────────────────────────────────

def _make_fizz_plan(speed: int, price: float, context: str = "") -> ISPPlan:
    return ISPPlan(
        provider="Fizz",
        speed_down=speed,
        speed_up=0,
        price=price,
        source="dom",
        url="https://fizz.ca/en/internet",
        raw_meta={"context_text": context.lower()},
    )


def test_fizz_ignores_promo_price():
    """Le plan avec 'bundle' dans context_text doit être écarté."""
    plans = [
        _make_fizz_plan(200, 45.0, context="bundle save $45/month 200 Mbps"),
        _make_fizz_plan(200, 49.0, context="200 Mbps $49.00 /month unlimited"),
    ]
    selected = select_plan_for_provider("Fizz", plans)
    assert selected is not None
    assert selected.price == 49.0


def test_fizz_ignores_promo_price_mobile():
    """Le mot 'mobile' dans context doit aussi être filtré."""
    plans = [
        _make_fizz_plan(200, 45.0, context="bundle mobile plan $45 200 Mbps"),
        _make_fizz_plan(200, 49.0, context="internet 200 Mbps $49.00 /month"),
    ]
    selected = select_plan_for_provider("Fizz", plans)
    assert selected is not None
    assert selected.price == 49.0


def test_fizz_select_preferred_speed():
    """Doit préférer 200 Mbps (preferred_speed) sur 100 Mbps."""
    plans = [
        _make_fizz_plan(100, 35.0, context="100 Mbps $35/month"),
        _make_fizz_plan(200, 49.0, context="200 Mbps $49.00 /month"),
        _make_fizz_plan(400, 65.0, context="400 Mbps $65.00 /month"),
    ]
    selected = select_plan_for_provider("Fizz", plans)
    assert selected is not None
    # Doit choisir le moins cher parmi >= 200 Mbps, soit 200 Mbps à $49
    assert selected.speed_down == 200
    assert selected.price == 49.0


def test_fizz_fallback_when_all_filtered():
    """Si tous sont filtrés, retourne quand même un résultat (défense-en-profondeur)."""
    plans = [
        _make_fizz_plan(200, 45.0, context="bundle mobile promo save économisez"),
    ]
    # Tous filtrés → fallback sur la liste complète
    selected = select_plan_for_provider("Fizz", plans)
    assert selected is not None


# ──────────────────────────────────────────────────────────────────────────────
#  select_plan_for_provider — VMedia
# ──────────────────────────────────────────────────────────────────────────────

def _make_vmedia_plan(speed: int, price: float) -> ISPPlan:
    return ISPPlan(
        provider="VMedia",
        speed_down=speed,
        speed_up=0,
        price=price,
        source="dom",
        url="https://www.vmedia.ca/en/homeinternet",
    )


def test_vmedia_select_cheapest_fast():
    """Parmi les plans >= 300 Mbps (preferred), choisit le moins cher."""
    plans = [
        _make_vmedia_plan(120, 45.0),
        _make_vmedia_plan(300, 65.0),
        _make_vmedia_plan(500, 79.0),
        _make_vmedia_plan(1000, 99.0),
    ]
    selected = select_plan_for_provider("VMedia", plans)
    assert selected is not None
    assert selected.speed_down == 300
    assert selected.price == 65.0


def test_vmedia_falls_back_to_min_speed():
    """Si aucun plan >= 300 Mbps, retourne le moins cher >= 50 Mbps."""
    plans = [
        _make_vmedia_plan(50,  39.0),
        _make_vmedia_plan(120, 55.0),
    ]
    selected = select_plan_for_provider("VMedia", plans)
    assert selected is not None
    assert selected.speed_down == 50
    assert selected.price == 39.0


# ──────────────────────────────────────────────────────────────────────────────
#  check_price_sanity
# ──────────────────────────────────────────────────────────────────────────────

def _plan(price: float, provider: str = "Fizz") -> ISPPlan:
    return ISPPlan(provider=provider, speed_down=200, speed_up=0, price=price)


def test_sanity_check_passes():
    prev = {"Fizz": {"price": 49.0}}
    # 5% delta — well within the 20% limit
    assert check_price_sanity("Fizz", _plan(51.0), prev) is True


def test_sanity_check_fails_spike():
    prev = {"Fizz": {"price": 49.0}}
    # 60% spike — exceeds 20% limit
    assert check_price_sanity("Fizz", _plan(80.0), prev) is False


def test_sanity_check_passes_no_previous():
    # No previous price → always OK
    assert check_price_sanity("Fizz", _plan(49.0), {}) is True


def test_sanity_check_passes_small_drop():
    prev = {"Bell": {"price": 80.0}}
    # 10% drop — within 25% limit for Bell
    assert check_price_sanity("Bell", _plan(72.0, provider="Bell"), prev) is True


def test_sanity_check_fails_large_drop():
    prev = {"Bell": {"price": 80.0}}
    # 50% drop — exceeds 25% limit
    assert check_price_sanity("Bell", _plan(40.0, provider="Bell"), prev) is False


# ──────────────────────────────────────────────────────────────────────────────
#  _dicts_to_isplans
# ──────────────────────────────────────────────────────────────────────────────

def test_dicts_to_isplans_basic():
    dicts = [{"speed_down": 400, "price": 65.0, "plan": "400 Mbps"}]
    plans = _dicts_to_isplans("Vidéotron", "https://example.com", dicts, source="api")
    assert len(plans) == 1
    assert isinstance(plans[0], ISPPlan)
    assert plans[0].speed_down == 400
    assert plans[0].price == 65.0
    assert plans[0].source == "api"
    assert plans[0].provider == "Vidéotron"


def test_dicts_to_isplans_context():
    dicts = [{"speed_down": 200, "price": 49.0, "plan": "200 Mbps"}]
    plans = _dicts_to_isplans("Fizz", "https://fizz.ca", dicts,
                              context_text="200 Mbps $49.00 /month unlimited data")
    assert "200 mbps" in plans[0].raw_meta["context_text"]


# ──────────────────────────────────────────────────────────────────────────────
#  PROVIDER_CONFIG sanity
# ──────────────────────────────────────────────────────────────────────────────

def test_provider_config_fizz_has_ignore_keywords():
    cfg = PROVIDER_CONFIG["Fizz"]
    assert "bundle" in cfg["ignore_keywords"]
    assert "promo"  in cfg["ignore_keywords"]
    assert cfg.get("prefer_source") == "dom"


def test_provider_config_all_isp_present():
    for provider in ["Vidéotron", "Bell", "Cogeco", "Fizz", "EBOX", "VMedia", "Start.ca"]:
        assert provider in PROVIDER_CONFIG, f"{provider} missing from PROVIDER_CONFIG"


# ──────────────────────────────────────────────────────────────────────────────
#  cell_plans_from_text
# ──────────────────────────────────────────────────────────────────────────────

def test_cell_plans_from_text_basic():
    text = "15 Go de données $45.00 /mois LTE+"
    plans = cell_plans_from_text(text)
    assert any(p["data_gb"] == 15 and p["price"] == 45.0 for p in plans)


def test_cell_plans_from_text_reverse():
    text = "$55.00 /month — 20 GB data"
    plans = cell_plans_from_text(text)
    assert any(p["data_gb"] == 20 and p["price"] == 55.0 for p in plans)


def test_cell_plans_from_text_gb_short_distance():
    text = "10 Go $35"
    plans = cell_plans_from_text(text)
    assert any(p["data_gb"] == 10 and p["price"] == 35.0 for p in plans)


def test_cell_plans_from_text_ignores_low_price():
    text = "5 Go $8/month"
    plans = cell_plans_from_text(text)
    assert not any(p["price"] == 8.0 for p in plans)


# ──────────────────────────────────────────────────────────────────────────────
#  cell_plans_from_json
# ──────────────────────────────────────────────────────────────────────────────

def test_cell_plans_from_json_basic():
    json_text = '{"data": 15, "price": "45.00", "name": "15 GB Plan"}'
    plans = cell_plans_from_json(json_text)
    assert any(p["data_gb"] == 15 and p["price"] == 45.0 for p in plans)


def test_cell_plans_from_json_gb_string():
    json_text = '{"plan": "15 GB", "monthlyPrice": "55.00"}'
    plans = cell_plans_from_json(json_text)
    assert any(p["data_gb"] == 15 and p["price"] == 55.0 for p in plans)


def test_cell_plans_from_json_ignores_high_price():
    json_text = '{"data": 15, "price": "500.00"}'
    plans = cell_plans_from_json(json_text)
    assert not any(p["price"] == 500.0 for p in plans)


# ──────────────────────────────────────────────────────────────────────────────
#  select_cell_plan_for_provider
# ──────────────────────────────────────────────────────────────────────────────

def _cell(provider: str, gb: int, price: float, ctx: str = "") -> CellPlan:
    return CellPlan(
        provider=provider, data_gb=gb, price=price,
        network="Telus", url="https://example.com",
        raw_meta={"context_text": ctx.lower()},
    )


def test_cell_select_prefers_target_gb():
    """Doit choisir le moins cher parmi >= 15 Go (target Telus)."""
    plans = [
        _cell("Telus", 5,  29.0),
        _cell("Telus", 15, 55.0),
        _cell("Telus", 20, 65.0),
    ]
    selected = select_cell_plan_for_provider("Telus", plans)
    assert selected is not None
    assert selected.data_gb == 15
    assert selected.price == 55.0


def test_cell_select_fallback_min_gb():
    """Si aucun plan >= 15 Go, prend le moins cher >= 8 Go."""
    plans = [
        _cell("Telus", 8,  39.0),
        _cell("Telus", 10, 45.0),
    ]
    selected = select_cell_plan_for_provider("Telus", plans)
    assert selected is not None
    assert selected.data_gb == 8
    assert selected.price == 39.0


def test_cell_select_fizz_ignores_bundle():
    """Fizz : plans avec 'bundle' dans context filtrés."""
    plans = [
        _cell("Fizz", 15, 35.0, ctx="bundle save $35 15 Go"),
        _cell("Fizz", 15, 50.0, ctx="15 Go $50 /mois"),
    ]
    selected = select_cell_plan_for_provider("Fizz", plans)
    assert selected is not None
    assert selected.price == 50.0


def test_cell_select_chatr_lower_target():
    """Chatr a target_data_gb=10, pas 15."""
    plans = [
        _cell("Chatr", 5,  25.0),
        _cell("Chatr", 10, 40.0),
        _cell("Chatr", 15, 55.0),
    ]
    selected = select_cell_plan_for_provider("Chatr", plans)
    assert selected is not None
    assert selected.data_gb == 10
    assert selected.price == 40.0


def test_cell_select_ignores_unlimited():
    """Plans avec data_gb=999 (illimité) doivent être ignorés."""
    plans = [
        _cell("Telus", 999, 55.0),   # illimité
        _cell("Telus", 15,  65.0),
    ]
    selected = select_cell_plan_for_provider("Telus", plans)
    assert selected is not None
    assert selected.data_gb == 15


# ──────────────────────────────────────────────────────────────────────────────
#  check_cell_price_sanity
# ──────────────────────────────────────────────────────────────────────────────

def _cplan(price: float, provider: str = "Telus") -> CellPlan:
    return CellPlan(provider=provider, data_gb=15, price=price, network="Telus")


def test_cell_sanity_passes():
    prev = {"Telus": {"price": 55.0}}
    assert check_cell_price_sanity("Telus", _cplan(58.0), prev) is True


def test_cell_sanity_fails_spike():
    prev = {"Telus": {"price": 55.0}}
    assert check_cell_price_sanity("Telus", _cplan(90.0), prev) is False


def test_cell_sanity_no_previous():
    assert check_cell_price_sanity("Telus", _cplan(55.0), {}) is True


# ──────────────────────────────────────────────────────────────────────────────
#  CELL_PROVIDER_CONFIG sanity
# ──────────────────────────────────────────────────────────────────────────────

def test_cell_config_all_providers_present():
    for p in ["Telus", "Fido", "Koodo", "Vidéotron", "Public Mobile",
              "Fizz", "Lucky Mobile", "Chatr"]:
        assert p in CELL_PROVIDER_CONFIG, f"{p} missing from CELL_PROVIDER_CONFIG"


def test_cell_config_chatr_lower_target():
    assert CELL_PROVIDER_CONFIG["Chatr"]["target_data_gb"] == 10


def test_cell_config_fizz_ignore_keywords():
    assert "bundle" in CELL_PROVIDER_CONFIG["Fizz"]["ignore_keywords"]
