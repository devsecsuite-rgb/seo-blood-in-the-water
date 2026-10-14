# Blood in the Water SEO Scanner

Automated SEO opportunity finder for **calccover.com**. Scans Google SERPs,
identifies weak competitor pages, scores them, and logs opportunities to
Google Sheets.

## Architecture
- **GitHub Actions** — scheduled runner (free)
- **OpenSERP** — self-hosted SERP API (Docker)
- **Crawly API** — backlink data (free tier)
- **Google Sheets** — output database

## Setup
See `SETUP.md`.

## Manual Run

## Files
- `main.py` — the scanner
- `requirements.txt` — Python deps
- `.github/workflows/blood-in-the-water.yml` — automation
