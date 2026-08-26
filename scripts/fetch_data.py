import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, urlunsplit
import pandas as pd
import requests
import yaml

API = "https://api.gdeltproject.org/api/v2/doc/doc"


def load_config(config_path="config.yaml"):
    """Load settings and queries from external YAML config file."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found at '{config_path}'")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Load runtime values from config
CONFIG = load_config()
HAZARD_TERMS = CONFIG["hazard_terms"]
REGIONS = CONFIG["regions"]
SETTINGS = CONFIG.get("gdelt_settings", {})


def canonical_url(url):
    if not isinstance(url, str) or not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", "")
    )


def normalize_title(title):
    if not isinstance(title, str):
        return ""
    title = title.lower()
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def fetch_with_retry(params, max_retries=4):
    """Fetch GDELT data with exponential backoff on timeouts or 429 rate limits."""
    delay = 2
    for attempt in range(max_retries):
        try:
            response = requests.get(API, params=params, timeout=(10, 45))
            if response.status_code == 429:
                print(f"⚠️ Rate limited (429). Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            if attempt == max_retries - 1:
                print(f"❌ Permanent failure after {max_retries} retries: {e}")
                return None
            time.sleep(delay)
            delay *= 2
    return None


def fetch_region_data(region_name, region_query):
    query = f"{HAZARD_TERMS} {region_query}"
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": SETTINGS.get("max_records", 250),
        "sort": SETTINGS.get("sort", "datedesc"),
        "TIMESPAN": SETTINGS.get("timespan", "30d"),
    }

    time.sleep(SETTINGS.get("request_delay", 1.5))
    payload = fetch_with_retry(params)
    results = []

    if payload and "articles" in payload:
        articles = payload.get("articles", [])
        print(f"✓ [{region_name}] Retrieved {len(articles)} articles.")
        for article in articles:
            results.append(
                {
                    "title": article.get("title"),
                    "url": article.get("url"),
                    "seen_date": article.get("seendate"),
                    "domain": article.get("domain"),
                    "language": article.get("language"),
                    "source_country": article.get("sourcecountry"),
                    "social_image": article.get("socialimage"),
                    "matched_region": region_name,
                    "query": query,
                }
            )
    else:
        print(f"⚠️ [{region_name}] No articles returned or request failed.")

    return results


if __name__ == "__main__":
    print("Starting resilient GDELT retrieval...")
    all_rows = []
    max_workers = SETTINGS.get("max_workers", 2)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(fetch_region_data, region_name, region_query)
            for region_name, region_query in REGIONS.items()
        ]
        for future in as_completed(futures):
            all_rows.extend(future.result())

    df = pd.DataFrame(all_rows)
    output_dir = "docs"
    os.makedirs(output_dir, exist_ok=True)

    if not df.empty:
        df["canonical_url"] = df["url"].apply(canonical_url)
        df["normalized_title"] = df["title"].apply(normalize_title)
        df["seen_date"] = pd.to_datetime(df["seen_date"], errors="coerce", utc=True)
        df = df.sort_values("seen_date", ascending=False, na_position="last")
        df = df.drop_duplicates(subset=["canonical_url"], keep="first")
        df = df.drop_duplicates(subset=["normalized_title"], keep="first")

        csv_path = os.path.join(output_dir, "sea_fire_haze_news_30d.csv")
        json_path = os.path.join(output_dir, "data.json")

        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        df.to_json(json_path, orient="records", date_format="iso")

        print(
            f"\n🎉 Successfully saved {len(df):,} de-duplicated articles to '{output_dir}/'."
        )
    else:
        print("\nNo articles retrieved.")
