# isp-scraper

Scraper quotidien des prix des forfaits Internet résidentiels des principaux FAI québécois.
Résultat publié dans `data/isp-prices.json`, lu directement par l'app **Depensa**.

## Fournisseurs couverts

| FAI | Méthode | Fiabilité |
|---|---|---|
| Oxio | HTTP | 🟢 Stable |
| EBOX | HTTP | 🟢 Stable |
| VMedia | HTTP | 🟢 Stable |
| Start.ca | HTTP | 🟢 Stable |
| TekSavvy | Playwright | 🟡 Bonne |
| Cogeco | Playwright | 🟡 Bonne |
| Bell | Playwright + interception API | 🟠 Variable |
| Vidéotron | Playwright + interception API | 🟠 Variable |

> Bell et Vidéotron utilisent Cloudflare + React. Si le scrape échoue, le **prix du jour précédent est conservé** dans le JSON — l'app ne voit jamais de données vides.

## Structure

```
isp-scraper/
├── .github/
│   └── workflows/
│       └── scrape.yml      ← cron GitHub Actions (6h00 Montréal)
├── scraper/
│   └── scrape.py           ← script principal
├── data/
│   └── isp-prices.json     ← résultat lu par Depensa
├── requirements.txt
└── README.md
```

## Lancer localement

```bash
# 1. Cloner le repo
git clone https://github.com/TON-USER/isp-scraper.git
cd isp-scraper

# 2. Environnement Python
python -m venv .venv
source .venv/bin/activate      # macOS/Linux
# .venv\Scripts\activate       # Windows

# 3. Dépendances
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium   # Linux seulement

# 4. Scraper
python scraper/scrape.py

# Le résultat est dans data/isp-prices.json
```

## Mettre à jour un prix manuellement

Si un scraper échoue pour Bell ou Vidéotron, tu peux éditer `data/isp-prices.json`
directement et commiter — l'app lira la nouvelle valeur le lendemain.

```json
{
  "provider": "Bell",
  "price": 79.95,
  "scraped_ok": false
}
```

## Format du JSON

```json
{
  "updated_at": "2026-03-25T10:00:00Z",
  "scraped_count": 6,
  "fallback_count": 2,
  "region": "quebec",
  "plans": [
    {
      "provider": "Vidéotron",
      "plan": "Internet 400",
      "speed_down": 400,
      "speed_up": 50,
      "price": 85.00,
      "type": "Câble",
      "note": "",
      "promo": false,
      "promo_note": "",
      "url": "https://...",
      "scraped_ok": true
    }
  ]
}
```

## Dépannage

| Symptôme | Solution |
|---|---|
| `scraped_ok: false` pour Bell/Vidéotron | Normal — Cloudflare bloque. Prix précédent conservé. |
| JSON vide après scrape | Vérifier les logs dans l'onglet Actions de GitHub |
| Prix obsolète | Éditer `data/isp-prices.json` manuellement et commiter |
| `playwright install` échoue en CI | Vérifier que `playwright install-deps chromium` est bien dans le workflow |
