"""
Blood in the Water SEO Scanner
Scans Google SERPs for weak competitor pages and logs opportunities.
"""

import os
import json
import time
import random
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime

# ============================================================
# SECTION 1: CONFIGURATION
# ============================================================

KEYWORD_POOL = [
    "workers comp insurance calculator",
    "general liability insurance estimator",
    "commercial auto insurance quote calculator",
    "business insurance cost calculator",
    "small business liability insurance estimate",
    "contractor insurance calculator",
    "restaurant insurance cost estimator",
    "commercial property insurance calculator",
]

# Pick 2 keywords per day, rotating by day-of-year
_day_index = datetime.now().timetuple().tm_yday
_start = (_day_index * 2) % len(KEYWORD_POOL)
SEED_KEYWORDS = [
    KEYWORD_POOL[_start % len(KEYWORD_POOL)],
    KEYWORD_POOL[(_start + 1) % len(KEYWORD_POOL)],
]

YOUR_DOMAIN = "calccover.com"
MIN_SCORE_TO_LOG = 35
MAX_POSITIONS_TO_CHECK = 10
MAX_KEYWORDS_PER_RUN = 5
CRAWLY_DAILY_BUDGET = 90


# ============================================================
# SECTION 2: SERP FETCHING (via Cloudflare Worker)
# ============================================================

def fetch_serp(keyword, max_retries=3):
    """
    Fetch top Google results via SerpAPI.
    Free tier: 250 searches/month.
    """
    api_key = os.environ.get("SERPAPI_KEY", "")
    if not api_key:
        print("  [ERROR] SERPAPI_KEY not set.")
        return []

    url = "https://serpapi.com/search"
    params = {
        "engine": "google",
        "q": keyword,
        "num": MAX_POSITIONS_TO_CHECK,
        "gl": "us",
        "hl": "en",
        "api_key": api_key,
    }

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=60)

            if resp.status_code == 401:
                print("  [FATAL] SerpAPI key invalid.")
                return []

            if resp.status_code == 429:
                wait = min(2 ** attempt, 60)
                print(f"  [RETRY {attempt}/{max_retries}] HTTP 429 (rate limited). Waiting {wait}s...")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                print(f"  [ERROR] SerpAPI: {data['error']}")
                return []

            results = data.get("organic_results", [])
            normalized = []
            for item in results[:MAX_POSITIONS_TO_CHECK]:
                normalized.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "rank": item.get("position", 99),
                })
            return normalized

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else ""
            print(f"  [HTTP {status}] {body}")
            wait = min(2 ** attempt, 60)
            time.sleep(wait)
        except requests.exceptions.RequestException as e:
            wait = min(2 ** attempt, 60)
            print(f"  [RETRY {attempt}/{max_retries}] {type(e).__name__}. Waiting {wait}s...")
            time.sleep(wait)
        except Exception as e:
            print(f"  [ERROR] SERP fetch failed for '{keyword}': {e}")
            return []

    print(f"  [FAIL] Gave up on '{keyword}'.")
    return []


# ============================================================
# SECTION 3: BACKLINK CHECK (via Crawly API)
# ============================================================

def get_backlink_data(url):
    """
    Crawly is deprecated for this project (free tier removed).
    Return neutral values so scoring falls back to heuristics.
    """
    return {"referring_domains": 50, "harmonic_score": 0.5}

    try:
        domain = url.split("//")[1].split("/")[0]
    except IndexError:
        domain = url

    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"domain": domain}

    try:
        resp = requests.get(
            "https://www.getcrawly.com/api/v1/domain-authority",
            params=params, headers=headers, timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "referring_domains": data.get("summary", {}).get("referring_domains", 999),
                "harmonic_score": data.get("score", {}).get("harmonic_score", 0),
            }
        elif resp.status_code == 401:
            print(f"  [WARN] Crawly 401 (quota or invalid key) for {domain}. Using neutral score.")
            return {"referring_domains": 50, "harmonic_score": 0.5}
        else:
            print(f"  [WARN] Crawly returned {resp.status_code} for {domain}")
    except Exception as e:
        print(f"  [ERROR] Crawly request failed for {domain}: {e}")

    return {"referring_domains": 999, "harmonic_score": 0}


# ============================================================
# SECTION 4: GSC DATA
# ============================================================

def get_gsc_striking_distance():
    """
    Pull queries where YOUR site ranks position 5-20.
    """
    creds_json = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "")
    if not creds_json:
        print("[WARN] GSC_SERVICE_ACCOUNT_JSON not set. Skipping GSC data.")
        return []

    try:
        creds_dict = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
        )
        service = build("searchconsole", "v1", credentials=creds)

        # Only use the domain property — URL-prefix returns 403
        site_candidates = ["sc-domain:calccover.com"]

        for site_url in site_candidates:
            try:
                request = {
                    "startDate": "2026-01-01",
                    "endDate": datetime.now().strftime("%Y-%m-%d"),
                    "dimensions": ["query"],
                    "rowLimit": 5000,
                }
                response = service.searchanalytics().query(
                    siteUrl=site_url, body=request
                ).execute()
            except Exception as inner:
                print(f"[WARN] GSC query failed for {site_url}: {inner}")
                continue

            rows = response.get("rows", [])
            striking = []
            for row in rows:
                kw = row["keys"][0]
                pos = row.get("position", 0)

                # Must be in striking-distance position
                if not (5 <= pos <= 20):
                    continue

                # Skip long AI-prompt style queries
                if len(kw) > 60 or kw.count(" ") > 8:
                    continue

                                # Skip questions, first-person, and preposition-start queries
                low = kw.lower()
                if low.startswith((
                    "how ", "what ", "where ", "why ", "when ",
                    "i am ", "i want ", "i need ", "i work ",
                    "for ", "of ", "to ", "in ", "on ", "with ", "and ",
                    "is ", "are ", "can ", "do ", "does ",
                )):
                    continue

                # Skip queries with 'map' (GIS/ArcGIS noise)
                if " map" in low or "map " in low:
                    continue

                # Skip reversals like "insurance restaurant price"
                if low.startswith(("insurance ", "restaurant insurance ")):
                    # Only keep if 'cost' or 'quote' or 'calculator' is present
                    if not any(m in low for m in ["cost", "quote", "calculator", "estimate", "premium"]):
                        continue

                # Skip brand typos and junk tokens
                junk = ["calcover", "calscover", ".net", "/.net",
                        "app restaurant", "salary insurance", "layton ", "carlisle ",
                        "marquette "]
                if any(tok in low for tok in junk):
                    continue

                # Must contain at least one commercial-intent keyword
                must_have = ["insurance", "calculator", "estimate",
                             "quote", "cost", "premium", "rate"]
                if not any(m in low for m in must_have):
                    continue

                striking.append(kw)

            print(f"[GSC] {site_url}: {len(rows)} total, {len(striking)} usable (after filters).")
            if striking:
                return striking
        return []

    except Exception as e:
        print(f"[ERROR] GSC fetch failed: {e}")
        return []


# ============================================================
# SECTION 5: SCORING LOGIC
# ============================================================

US_STATES = [
    "california", "texas", "florida", "new york", "illinois",
    "pennsylvania", "ohio", "georgia", "arizona", "north carolina",
]
INDUSTRIES = [
    "construction", "restaurant", "trucking", "landscaping",
    "cleaning", "retail", "consulting", "contractor",
]
INSURER_DOMAINS = [
    "geico.com", "statefarm.com", "allstate.com",
    "progressive.com", "libertymutual.com", "nationwide.com",
]
TOOL_INDICATORS = ["calculator", "estimator", "quote", "tool", "calculate"]


def score_opportunity(keyword, result, ref_domains, harmonic_score):
    """Returns (score: int, reasons: list[str]). Heuristic-only mode."""
    score = 0
    reasons = []

    kw_lower = keyword.lower()
    title = (result.get("title") or "").lower()
    url_lower = (result.get("url") or "").lower()

    # Forum / UGC content in top 10 — prime "blood in the water" signal
    if any(s in url_lower for s in ["reddit.com", "quora.com", "stackexchange.com", "forum"]):
        score += 30
        reasons.append("+30: Forum/UGC content")

    # Weak title optimization
    if kw_lower not in title:
        score += 20
        reasons.append("+20: Keyword absent from title")

    # No calculator/tool in title — you have one, they don't
    if not any(i in title for i in TOOL_INDICATORS):
        score += 25
        reasons.append("+25: No calculator/tool in title")

    # Competitor is a huge insurer, not a calculator site
    if any(d in url_lower for d in INSURER_DOMAINS):
        score += 25
        reasons.append("+25: Competitor is an insurer, not a calculator")

    # Static resource formats
    if url_lower.endswith(".pdf") or "/wp-content/uploads/" in url_lower:
        score += 20
        reasons.append("+20: PDF or outdated resource")

    # Blog/article structure instead of tool page
    if any(tok in url_lower for tok in ["/blog/", "/article/", "/news/", "/resources/", "/guide"]):
        score += 15
        reasons.append("+15: Blog/article, not a tool page")

    # State-specific queries = high commercial intent
    if any(s in kw_lower for s in US_STATES):
        score += 15
        reasons.append("+15: State-specific query")

    # Industry-specific queries
    if any(i in kw_lower for i in INDUSTRIES):
        score += 10
        reasons.append("+10: Industry-specific query")

    # Negative factors
    if any(i in title for i in TOOL_INDICATORS):
        score -= 20
        reasons.append("-20: Competitor has calculator/tool")

    if kw_lower in title:
        score -= 10
        reasons.append("-10: Keyword in title")

    if any(d in url_lower for d in [
        "geico.com", "statefarm.com", "allstate.com",
        "progressive.com", "libertymutual.com", "nationwide.com"
    ]) and not any(i in title for i in TOOL_INDICATORS):
        # Already scored above; no double-count
        pass

    return max(0, min(100, score)), reasons

# ============================================================
# SECTION 6: LOGGING TO GOOGLE SHEETS
# ============================================================

def log_to_sheet(opportunities):
    creds_json = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "")
    sheet_id = os.environ.get("SHEET_ID", "")

    if not creds_json or not sheet_id:
        print("[ERROR] Missing GSC_SERVICE_ACCOUNT_JSON or SHEET_ID.")
        return

    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(creds_json), scope
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sheet_id).sheet1

        for opp in opportunities:
            sheet.append_row([
                opp["score"], opp["keyword"], opp["position"],
                opp["domain"], opp["url"], opp["ref_domains"],
                opp["harmonic_score"], opp["intent"], opp["why_weak"],
                opp["cluster"], datetime.now().strftime("%Y-%m-%d"),
            ])
        print(f"  [OK] Logged {len(opportunities)} opportunities.")
    except Exception as e:
        print(f"  [ERROR] Sheet logging failed: {e}")


# ============================================================
# MAIN EXECUTION
# ============================================================

def main():
    print(f"[START] Blood in the Water scan - {datetime.now().isoformat()}")

    gsc_kws = get_gsc_striking_distance()
    combined = list(dict.fromkeys(SEED_KEYWORDS + gsc_kws))
    random.shuffle(combined)
    all_keywords = combined[:MAX_KEYWORDS_PER_RUN]
    print(f"[INFO] Scanning {len(all_keywords)} of {len(combined)} keywords (rotated)...")

    opportunities = []
    crawly_calls = 0

    for i, keyword in enumerate(all_keywords, 1):
        print(f"\n[{i}/{len(all_keywords)}] Scanning: '{keyword}'")

        if i > 1:
            pause = random.uniform(15, 25)
            print(f"  [WAIT] Sleeping {pause:.1f}s before next keyword...")
            time.sleep(pause)

        results = fetch_serp(keyword)
        if not results:
            print("  [SKIP] No results.")
            continue

        for result in results:
            url = result.get("url", "")
            if not url or YOUR_DOMAIN in url:
                continue

            if crawly_calls < CRAWLY_DAILY_BUDGET:
                ref = get_backlink_data(url)
                crawly_calls += 1
            else:
                ref = {"referring_domains": 50, "harmonic_score": 0.5}

            score, reasons = score_opportunity(
                keyword, result,
                ref["referring_domains"], ref["harmonic_score"],
            )

            if score >= MIN_SCORE_TO_LOG:
                opportunities.append({
                    "score": score,
                    "keyword": keyword,
                    "position": result.get("rank", 99),
                    "domain": url.split("//")[1].split("/")[0] if "//" in url else url,
                    "url": url,
                    "ref_domains": ref["referring_domains"],
                    "harmonic_score": round(ref["harmonic_score"], 3),
                    "intent": "commercial",
                    "why_weak": "; ".join(reasons),
                    "cluster": keyword.split()[0] if keyword else "general",
                })
                print(f"  [FOUND] Score {score} - {url}")
            else:
                print(f"  [SKIP] Score {score} - {url}")

            time.sleep(0.5)

    opportunities.sort(key=lambda x: x["score"], reverse=True)

    if opportunities:
        log_to_sheet(opportunities)
    else:
        print("\n[INFO] No opportunities met the threshold.")

    print(f"\n[DONE] {len(opportunities)} opportunities found.")


if __name__ == "__main__":
    main()
