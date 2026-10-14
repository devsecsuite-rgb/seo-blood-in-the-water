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
MIN_SCORE_TO_LOG = 50
MAX_POSITIONS_TO_CHECK = 10
MAX_KEYWORDS_PER_RUN = 20
CRAWLY_DAILY_BUDGET = 90


# ============================================================
# SECTION 2: SERP FETCHING (via Cloudflare Worker)
# ============================================================

def fetch_serp(keyword, max_retries=3):
    """
    Fetch top search results via the Brave Search API.
    """
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        print("  [ERROR] BRAVE_API_KEY not set.")
        return []

    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    }
    params = {
        "q": keyword,
        "count": MAX_POSITIONS_TO_CHECK,
        "country": "us",
        "search_lang": "en",
    }

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)

            if resp.status_code == 429:
                wait = min(2 ** attempt, 60)
                print(f"  [RETRY {attempt}/{max_retries}] HTTP 429 (rate limited). Waiting {wait}s...")
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                print("  [FATAL] Brave API key invalid or revoked.")
                return []

            resp.raise_for_status()
            data = resp.json()

            results = data.get("web", {}).get("results", [])
            normalized = []
            for i, item in enumerate(results[:MAX_POSITIONS_TO_CHECK], 1):
                normalized.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "rank": i,
                })
            return normalized

               except requests.exceptions.HTTPError as e:
            # Print the actual status code and response body
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else ""
            print(f"  [HTTP {status}] {body}")

            if status in (401, 403, 422):
                print(f"  [FATAL] Brave rejected the request. Check API key and plan.")
                return []
            if status == 429:
                wait = min(2 ** attempt, 60)
                print(f"  [RETRY {attempt}/{max_retries}] Rate limited. Waiting {wait}s...")
                time.sleep(wait)
                continue
            return []

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
    Query Crawly for a domain's referring domains and authority score.
    """
    api_key = os.environ.get("CRAWLY_API_KEY", "")
    if not api_key:
        print("  [WARN] CRAWLY_API_KEY not set. Skipping backlink check.")
        return {"referring_domains": 999, "harmonic_score": 0}

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
            striking = [
                row["keys"][0]
                for row in rows
                if 5 <= row.get("position", 0) <= 20
            ]
            print(f"[GSC] {site_url}: {len(rows)} total, {len(striking)} in position 5-20.")
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
    """Returns (score: int, reasons: list[str])."""
    score = 0
    reasons = []

    kw_lower = keyword.lower()
    title = (result.get("title") or "").lower()
    url_lower = (result.get("url") or "").lower()

    # Positive factors
    if ref_domains <= 3:
        score += 25
        reasons.append(f"+25: Only {ref_domains} referring domains")
    elif ref_domains <= 10:
        score += 15
        reasons.append(f"+15: Only {ref_domains} referring domains")
    elif ref_domains <= 30:
        score += 5
        reasons.append(f"+5: {ref_domains} referring domains")

    if harmonic_score < 0.3:
        score += 15
        reasons.append(f"+15: Low harmonic score ({harmonic_score:.2f})")
    elif harmonic_score < 0.5:
        score += 8
        reasons.append(f"+8: Moderate harmonic score ({harmonic_score:.2f})")

    if any(s in url_lower for s in ["reddit.com", "quora.com", "stackexchange.com", "forum"]):
        score += 15
        reasons.append("+15: Forum/UGC content")

    if kw_lower not in title:
        score += 10
        reasons.append("+10: Keyword absent from title")

    if not any(i in title for i in TOOL_INDICATORS):
        score += 12
        reasons.append("+12: No calculator/tool in title")

    if any(s in kw_lower for s in US_STATES):
        score += 10
        reasons.append("+10: State-specific query")

    if any(i in kw_lower for i in INDUSTRIES):
        score += 8
        reasons.append("+8: Industry-specific query")

    if any(d in url_lower for d in INSURER_DOMAINS):
        score += 15
        reasons.append("+15: Competitor is an insurer, not a calculator")

    if url_lower.endswith(".pdf") or "/wp-content/uploads/" in url_lower:
        score += 12
        reasons.append("+12: PDF or outdated resource")

    # Negative factors
    if any(i in title for i in TOOL_INDICATORS):
        score -= 10
        reasons.append("-10: Competitor has calculator/tool")

    if kw_lower in title:
        score -= 5
        reasons.append("-5: Keyword in title")

    if ref_domains > 100:
        score -= 15
        reasons.append(f"-15: {ref_domains} referring domains (strong)")

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
