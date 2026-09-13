import os
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit
import pandas as pd
import requests
import yaml

API = "https://api.gdeltproject.org/api/v2/doc/doc"

# Resolve paths relative to the repo root rather than the current working
# directory, so the script behaves the same whether invoked as
# `python scripts/fetch_data.py` from the repo root or from elsewhere.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")
DEFAULT_OUTPUT_DIR = os.path.join(REPO_ROOT, "docs")


def load_config(config_path=DEFAULT_CONFIG_PATH):
    """Load settings and queries from YAML configuration file."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Configuration file not found at '{config_path}'"
        )
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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


# Reused across requests so TCP/TLS connections to the GDELT API are
# kept alive instead of being re-established for every region.
SESSION = requests.Session()
SESSION.headers.update(
    {
        # Explicit browser user-agent to bypass basic firewall blocks
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
)


def fetch_with_retry(params, session=SESSION, max_retries=3):
    """Executes a request with retry/backoff for transient failures only."""
    delay = 5

    for attempt in range(max_retries):
        try:
            # Extended connect and read timeouts (30s, 60s) for heavy queries
            response = session.get(API, params=params, timeout=(30, 60))

            if response.status_code in (429, 503, 504):
                print(
                    f"⚠️ Received status {response.status_code}. Pausing for {delay}s..."
                )
                time.sleep(delay)
                delay *= 2
                continue

            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status is not None and 400 <= status < 500:
                # Client errors (bad query, not found, etc.) won't succeed on
                # retry, so fail fast instead of burning the retry budget.
                print(f"❌ Client error {status}, not retrying: {e}")
                return None
            if attempt == max_retries - 1:
                print(f"❌ Failed after {max_retries} retries: {e}")
                return None
            time.sleep(delay)
            delay *= 2
        except requests.exceptions.Timeout:
            print(
                f"⏱️ Attempt {attempt + 1}/{max_retries} timed out. Retrying in {delay}s..."
            )
            time.sleep(delay)
            delay *= 2
        except (requests.exceptions.RequestException, ValueError) as e:
            if attempt == max_retries - 1:
                print(f"❌ Failed after {max_retries} retries: {e}")
                return None
            time.sleep(delay)
            delay *= 2

    return None


def fetch_region_data(region_name, region_query, hazard_terms, settings):
    query = f"{hazard_terms} {region_query}"
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": settings.get("max_records", 250),
        "sort": settings.get("sort", "datedesc"),
        "TIMESPAN": settings.get("timespan", "30d"),
    }

    # Delay between batch execution loops
    time.sleep(settings.get("request_delay", 3.5))

    payload = fetch_with_retry(params)
    results = []
    succeeded = payload is not None

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
        print(f"⚠️ [{region_name}] No articles returned or query dropped.")

    return results, succeeded


def main():
    config = load_config()
    hazard_terms = config["hazard_terms"]
    regions = config["regions"]
    settings = config.get("gdelt_settings", {})

    print("Starting sequential GDELT extraction...")
    all_rows = []
    failed_regions = []

    for region_name, region_query in regions.items():
        print(f"Fetching data for: {region_name}...")
        region_results, succeeded = fetch_region_data(
            region_name, region_query, hazard_terms, settings
        )
        all_rows.extend(region_results)
        if not succeeded:
            failed_regions.append(region_name)

    if failed_regions and len(failed_regions) == len(regions):
        print(f"\n❌ All {len(regions)} region requests failed; aborting.")
        sys.exit(1)

    df = pd.DataFrame(all_rows)
    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)

    if not df.empty:
        df["canonical_url"] = df["url"].apply(canonical_url)
        df["normalized_title"] = df["title"].apply(normalize_title)
        df["seen_date"] = pd.to_datetime(df["seen_date"], errors="coerce", utc=True)
        df = df.sort_values("seen_date", ascending=False, na_position="last")
        df = df.drop_duplicates(subset=["canonical_url"], keep="first")
        df = df.drop_duplicates(subset=["normalized_title"], keep="first")

        csv_path = os.path.join(DEFAULT_OUTPUT_DIR, "sea_fire_haze_news_30d.csv")
        json_path = os.path.join(DEFAULT_OUTPUT_DIR, "data.json")

        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        df.to_json(json_path, orient="records", date_format="iso")

        print(
            f"\n🎉 Extraction successful. Saved {len(df):,} deduplicated articles "
            f"to '{DEFAULT_OUTPUT_DIR}/'."
        )
    else:
        print("\nNo articles retrieved.")

    if failed_regions:
        print(
            f"\n⚠️ {len(failed_regions)}/{len(regions)} region requests failed: "
            f"{', '.join(failed_regions)}"
        )


if __name__ == "__main__":
    main()
