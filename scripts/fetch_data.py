from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
import os
import re
import time
import requests
import pandas as pd

API = "https://api.gdeltproject.org/api/v2/doc/doc"

# Keywords targeting fire and haze hazards
hazard_terms = (
    '(haze OR "transboundary haze" OR smog OR jerebu OR "kabut asap" '
    'OR "forest fire" OR wildfire OR "land fire" OR "peat fire" '
    'OR karhutla OR "biomass burning" OR "open burning")'
)

# Southeast Asia country groups
regions = {
    "Indonesia": "Indonesia",
    "Malaysia": "Malaysia",
    "Singapore_Brunei": "(Singapore OR Brunei)",
    "Thailand_Laos_Myanmar": "(Thailand OR Laos OR Myanmar)",
    "Vietnam_Cambodia": "(Vietnam OR Cambodia)",
    "Philippines": "Philippines",
}

# 30-day rolling time frame
end = datetime.now(timezone.utc)
start = end - timedelta(days=30)
window_days = 5


def gdelt_dt(dt):
    """Format datetime object into GDELT-compliant string."""
    return dt.strftime("%Y%m%d%H%M%S")


def canonical_url(url):
    """Normalize URL by stripping fragments, query params, and trailing slashes."""
    if not isinstance(url, str) or not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def normalize_title(title):
    """Clean title string for duplicate title detection."""
    if not isinstance(title, str):
        return ""
    title = title.lower()
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


rows = []
cursor = start

print("Starting GDELT data retrieval...")

while cursor < end:
    window_end = min(cursor + timedelta(days=window_days), end)

    for region_name, region_query in regions.items():
        query = f"{hazard_terms} {region_query}"

        # Parameter names MUST be STARTDATETIME & ENDDATETIME in uppercase
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
            print(f"Warning: Failed fetching {region_name} ({cursor.date()} - {window_end.date()}): {e}")
            continue

        articles = payload.get("articles", [])
        print(f"[{cursor.date()}] {region_name}: Retrieved {len(articles)} raw items.")

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

        # Polite rate-limiting between API calls
        time.sleep(1)

    cursor = window_end

df = pd.DataFrame(rows)

# Target static deployment folder
output_dir = "docs"
os.makedirs(output_dir, exist_ok=True)

if not df.empty:
    df["canonical_url"] = df["url"].apply(canonical_url)
    df["normalized_title"] = df["title"].apply(normalize_title)

    # Convert date to UTC datetime & sort FIRST to prioritize latest articles during deduplication
    df["seen_date"] = pd.to_datetime(df["seen_date"], errors="coerce", utc=True)
    df = df.sort_values("seen_date", ascending=False, na_position="last")

    # Deduplicate canonical URLs and titles
    df = df.drop_duplicates(subset=["canonical_url"], keep="first")
    df = df.drop_duplicates(subset=["normalized_title"], keep="first")

    # Export dataset to docs/ folder for GitHub Pages
    csv_path = os.path.join(output_dir, "sea_fire_haze_news_30d.csv")
    json_path = os.path.join(output_dir, "data.json")

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    df.to_json(json_path, orient="records", date_format="iso")

    print(f"\nPipeline complete! Successfully saved {len(df):,} de-duplicated articles to '{output_dir}/'.")
else:
    print("\nPipeline complete! No articles retrieved.")
