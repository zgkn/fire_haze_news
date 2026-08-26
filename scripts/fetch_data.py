from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
import os
import re
import time
import requests
import pandas as pd

API = "https://api.gdeltproject.org/api/v2/doc/doc"

# Search terms for haze and fire hazards
hazard_terms = (
    '(haze OR "transboundary haze" OR smog OR jerebu OR "kabut asap" '
    'OR "forest fire" OR wildfire OR "land fire" OR "peat fire" '
    'OR karhutla OR "biomass burning" OR "open burning")'
)

# Target Southeast Asia geographic coverage
regions = {
    "Indonesia": "Indonesia",
    "Malaysia": "Malaysia",
    "Singapore_Brunei": "(Singapore OR Brunei)",
    "Thailand_Laos_Myanmar": "(Thailand OR Laos OR Myanmar)",
    "Vietnam_Cambodia": "(Vietnam OR Cambodia)",
    "Philippines": "Philippines",
}

# 30-day time window
end = datetime.now(timezone.utc)
start = end - timedelta(days=30)
window_days = 5


def gdelt_dt(dt):
    """Formats datetime object to GDELT YYYYMMDDHHMMSS format."""
    return dt.strftime("%Y%m%d%H%M%S")


def canonical_url(url):
    """Normalizes URL by stripping query params, fragments, and trailing slashes."""
    if not isinstance(url, str) or not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def normalize_title(title):
    """Cleans title string for syndicated duplicate detection."""
    if not isinstance(title, str):
        return ""
    title = title.lower()
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


rows = []
cursor = start

print("Starting GDELT data collection...")

while cursor < end:
    window_end = min(cursor + timedelta(days=window_days), end)

    for region_name, region_query in regions.items():
        query = f"{hazard_terms} {region_query}"

        # API Parameter Casing: STARTDATETIME & ENDDATETIME must be uppercase for GDELT
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": 250,
            "sort": "datedesc",
            "STARTDATETIME": gdelt_dt(cursor),
            "ENDDATETIME": gdelt_dt(window_end),
        }

        try:
            response = requests.get(API, params=params, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as e:
            print(f"Warning: Request failed for {region_name} ({cursor.date()} to {window_end.date()}): {e}")
            continue

        articles = payload.get("articles", [])
        print(f"[{cursor.date()}] {region_name}: Retrieved {len(articles)} raw articles.")

        for article in articles:
            rows.append({
                "title": article.get("title"),
                "url": article.get("url"),
                "seen_date": article.get("seendate"),
                "domain": article.get("domain"),
                "language": article.get("language"),
                "source_country": article.get("sourcecountry"),
                "social_image": article.get("socialimage"),
                "matched_region": region_name,
                "window_start_utc": cursor.isoformat(),
                "window_end_utc": window_end.isoformat(),
                "query": query,
            })

        # Polite rate limiting to avoid getting throttled by GDELT API
        time.sleep(1)

    cursor = window_end

df = pd.DataFrame(rows)

# Create hosting directory for GitHub Pages
output_dir = "docs"
os.makedirs(output_dir, exist_ok=True)

if not df.empty:
    df["canonical_url"] = df["url"].apply(canonical_url)
    df["normalized_title"] = df["title"].apply(normalize_title)

    # 1. Convert to UTC Datetime and Sort FIRST so keep="first" preserves the newest article
    df["seen_date"] = pd.to_datetime(df["seen_date"], errors="coerce", utc=True)
    df = df.sort_values("seen_date", ascending=False, na_position="last")

    # 2. Deduplicate exact matching canonical URLs
    df = df.drop_duplicates(subset=["canonical_url"], keep="first")

    # 3. Deduplicate syndicated articles with identical titles
    df = df.drop_duplicates(subset=["normalized_title"], keep="first")

    # Export for GitHub Pages static serving
    csv_path = os.path.join(output_dir, "sea_fire_haze_news_30d.csv")
    json_path = os.path.join(output_dir, "data.json")

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    df.to_json(json_path, orient="records", date_format="iso")

    print(f"\nPipeline complete! Successfully saved {len(df):,} de-duplicated articles to '{output_dir}/'.")
else:
    print("\nPipeline complete! No articles were retrieved from GDELT.")
