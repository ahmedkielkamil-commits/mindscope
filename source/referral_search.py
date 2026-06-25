"""
MindScope pipeline step 5: execute referral searches against external APIs.

Receives the query plan from query_builder.py and returns raw facility results
grouped by therapy type for referral_scorer.py.
"""

import json
import logging
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv
from tavily import TavilyClient

# from query_builder import zip_to_latlong  # SAMHSA: re-enable when adding SAMHSA search

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# SAMHSA_JSON_ENDPOINT = "https://findtreatment.gov/locator/exportsAsJson/v2"
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 1

NAME_SUFFIX_PATTERN = re.compile(
    r"\b(llc|inc|inc\.?|incorporated|associates|group|center|centers|services|"
    r"pc|pllc|corp|corporation|ltd|co)\.?\b",
    re.IGNORECASE,
)
NAME_TRUNCATION_SEPARATORS = (":", "|", "—", "–")
DEDUP_PREFIX_WORD_COUNT = 4

TRUSTED_DIRECTORIES = {
    "psychologytoday.com",
    "therapyden.com",
    "findtreatment.gov",
    "samhsa.gov",
    "therapist.com",
    "zocdoc.com",
    "goodtherapy.org",
    "nimh.nih.gov",
    "aacap.org",
}

BLOCKED_DOMAIN_PATTERNS = {
    "reddit.com",
    "quora.com",
    "yelp.com",
    "facebook.com",
    "pinterest.com",
    "forum",
}

MIN_TAVILY_RELEVANCE_SCORE = 0.5

_KB_PATH = _PROJECT_ROOT / "knowledge_base.json"
with _KB_PATH.open(encoding="utf-8") as _kb_file:
    THERAPY_TO_QUERY_MAP = json.load(_kb_file).get("therapy_to_query_map", {})

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")

if not TAVILY_API_KEY:
    logger.warning("TAVILY_API_KEY not found in environment — Tavily search will be skipped")
if not GOOGLE_PLACES_API_KEY:
    logger.warning("GOOGLE_PLACES_API_KEY not found in environment — Google Places search will be skipped")


def normalize_facility_name(name: str | None) -> str:
    if not name:
        return ""

    text = str(name).strip().lower()
    for separator in NAME_TRUNCATION_SEPARATORS:
        if separator in text:
            text = text.split(separator, 1)[0].strip()
            break

    normalized = NAME_SUFFIX_PATTERN.sub("", text)
    normalized = re.sub(r"[^\w\s]", "", normalized)
    return " ".join(normalized.split())


def _first_n_words_key(name: str | None, word_count: int = DEDUP_PREFIX_WORD_COUNT) -> str | None:
    words = normalize_facility_name(name).split()
    if len(words) < word_count:
        return None
    return " ".join(words[:word_count])


def _dedupe_match_key(name: str | None) -> tuple[str, str | None]:
    normalized = normalize_facility_name(name)
    prefix_key = _first_n_words_key(name)
    return normalized, prefix_key


def _find_dedupe_index(facilities: list[dict], facility: dict) -> int | None:
    normalized, prefix_key = _dedupe_match_key(facility.get("name"))

    for index, existing in enumerate(facilities):
        existing_normalized, existing_prefix = _dedupe_match_key(existing.get("name"))

        if normalized and normalized == existing_normalized:
            return index

        if prefix_key and existing_prefix and prefix_key == existing_prefix:
            return index

    return None


# --- SAMHSA (disabled — add back later) ----------------------------------------
# def _parse_samhsa_facility(row: dict) -> dict:
#     name_parts = [row.get("name1", ""), row.get("name2", "")]
#     name = " ".join(part for part in name_parts if part).strip()
#     address_parts = [
#         row.get("street1"),
#         row.get("street2"),
#         row.get("city"),
#         row.get("state"),
#         row.get("zip"),
#     ]
#     address = ", ".join(str(part) for part in address_parts if part)
#
#     services: list[str] = []
#     specialty_tags: list[str] = []
#     for service in row.get("services", []):
#         label = service.get("f3")
#         code = service.get("f2")
#         if label:
#             services.extend(part.strip() for part in label.split(";") if part.strip())
#         if code:
#             specialty_tags.append(code)
#
#     return {
#         "source": "samhsa",
#         "name": name,
#         "address": address,
#         "phone": row.get("phone"),
#         "website": row.get("website"),
#         "services": services + specialty_tags,
#         "raw": row,
#     }
#
#
# def _samhsa_fallback_params(query_obj: dict) -> dict | None:
#     params = query_obj.get("params", {})
#     zip_code = params.get("addr")
#     coords = zip_to_latlong(zip_code)
#     if coords is None or isinstance(coords, str):
#         logger.warning("Could not resolve coordinates for SAMHSA search zip %r", zip_code)
#         return None
#
#     longitude, latitude = coords
#     return {
#         "sAddr": f"{longitude},{latitude}",
#         "page": 1,
#         "pageSize": 20,
#         "sort": 0,
#         "_distance_limit": params.get("distance", 25),
#         "_specialty": params.get("specialty", ""),
#         "_service_category": params.get("sCat", ""),
#     }
#
#
# def _filter_samhsa_rows(rows: list[dict], fallback_params: dict) -> list[dict]:
#     distance_limit = float(fallback_params.get("_distance_limit", 25))
#     specialty = str(fallback_params.get("_specialty", "")).replace("_", " ").lower()
#     service_category = str(fallback_params.get("_service_category", "")).replace("_", " ").lower()
#
#     filtered: list[dict] = []
#     for row in rows:
#         miles = float(row.get("miles", 9999))
#         if miles > distance_limit:
#             continue
#
#         if specialty or service_category:
#             service_text = json.dumps(row.get("services", [])).lower()
#             if specialty and specialty not in service_text:
#                 if service_category and service_category not in service_text:
#                     continue
#
#         filtered.append(row)
#
#     return filtered
#
#
# def _request_json(url: str, params: dict | None = None, timeout: int = 20) -> dict | list | None:
#     if params:
#         query = urllib.parse.urlencode({k: v for k, v in params.items() if not str(k).startswith("_")})
#         url = f"{url}?{query}"
#
#     with urllib.request.urlopen(url, timeout=timeout) as response:
#         content_type = response.headers.get("Content-Type", "")
#         body = response.read().decode("utf-8")
#         if "json" not in content_type.lower() and not body.lstrip().startswith(("{", "[")):
#             return None
#         return json.loads(body)
#
#
# def search_samhsa(query_obj: dict) -> list[dict]:
#     endpoint = query_obj.get("endpoint", SAMHSA_JSON_ENDPOINT)
#     params = query_obj.get("params", {})
#     therapy_type = query_obj.get("therapy_type", "unknown")
#
#     def _execute() -> list[dict]:
#         try:
#             payload = _request_json(endpoint, params)
#             if isinstance(payload, dict) and payload.get("rows") is not None:
#                 rows = payload.get("rows", [])
#             elif isinstance(payload, list):
#                 rows = payload
#             else:
#                 payload = None
#                 rows = []
#
#             if payload is None or not rows:
#                 fallback_params = _samhsa_fallback_params(query_obj)
#                 if fallback_params is None:
#                     return []
#                 payload = _request_json(SAMHSA_JSON_ENDPOINT, fallback_params)
#                 rows = payload.get("rows", []) if isinstance(payload, dict) else []
#                 rows = _filter_samhsa_rows(rows, fallback_params)
#
#             return [_parse_samhsa_facility(row) for row in rows if row.get("name1")]
#         except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
#             raise ConnectionError(str(exc)) from exc
#
#     results = _run_with_retry(_execute, "samhsa", therapy_type)
#     logger.info(
#         "SAMHSA search for %s returned %d results",
#         therapy_type,
#         len(results),
#     )
#     return results


def search_samhsa(query_obj: dict) -> list[dict]:
    """SAMHSA search disabled — stub returns empty until integration is added."""
    therapy_type = query_obj.get("therapy_type", "unknown")
    logger.info("SAMHSA search skipped (disabled) for %s", therapy_type)
    return []


# --- Google Places --------------------------------------------------------------
def search_google_places(query_obj: dict) -> list[dict]:
    api_key = query_obj.get("params", {}).get("key") or GOOGLE_PLACES_API_KEY
    therapy_type = query_obj.get("therapy_type", "unknown")

    if not api_key:
        logger.warning("Google Places API key missing — skipping search for %s", therapy_type)
        return []

    endpoint = query_obj.get(
        "endpoint",
        "https://maps.googleapis.com/maps/api/place/textsearch/json",
    )
    params = dict(query_obj.get("params", {}))
    params["key"] = api_key

    def _execute() -> list[dict]:
        response = requests.get(endpoint, params=params, timeout=20)
        if response.status_code == 429:
            raise ConnectionError("Google Places rate limit (429)")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectionError(str(exc)) from exc

        status = payload.get("status")
        if status not in {"OK", "ZERO_RESULTS"}:
            raise ConnectionError(f"Google Places error status: {status}")

        results: list[dict] = []
        for place in payload.get("results", []):
            if place.get("business_status") != "OPERATIONAL":
                continue

            website = None
            if place.get("website"):
                website = place.get("website")

            results.append(
                {
                    "source": "google_places",
                    "name": place.get("name", ""),
                    "address": place.get("formatted_address"),
                    "phone": None,
                    "website": website,
                    "place_id": place.get("place_id"),
                    "rating": place.get("rating"),
                    "review_count": place.get("user_ratings_total"),
                    "business_status": place.get("business_status"),
                    "raw": place,
                }
            )

        return results

    results = _run_with_retry(_execute, "google_places", therapy_type)
    logger.info(
        "Google Places search for %s returned %d results",
        therapy_type,
        len(results),
    )
    return results


def _extract_domain(url: str | None) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _therapy_match_terms(therapy_type: str, aliases: list[str]) -> list[str]:
    terms: set[str] = set()
    for source in [therapy_type, *aliases]:
        normalized = str(source).lower().strip()
        if not normalized:
            continue
        terms.add(normalized)
        for word in re.split(r"[^\w]+", normalized):
            if len(word) >= 2:
                terms.add(word)

    if "tf-cbt" in terms or "tf" in terms and "cbt" in terms:
        terms.add("tfcbt")

    return sorted(terms)


def _aliases_for_therapy(therapy_type: str) -> list[str]:
    mapping = THERAPY_TO_QUERY_MAP.get(therapy_type, {})
    return mapping.get("tavily_search_terms", [])


def _is_acceptable_tavily_result(
    item: dict,
    domain: str,
    therapy_type: str,
    aliases: list[str],
) -> tuple[bool, str]:
    if domain in TRUSTED_DIRECTORIES:
        return True, "trusted_directory"

    for pattern in BLOCKED_DOMAIN_PATTERNS:
        if pattern in domain:
            return False, "blocked_domain_pattern"

    score = float(item.get("score") or 0)
    if score < MIN_TAVILY_RELEVANCE_SCORE:
        return False, "low_relevance_score"

    haystack = f"{item.get('title', '')} {item.get('content', '')}".lower()
    if not any(term in haystack for term in _therapy_match_terms(therapy_type, aliases)):
        return False, "no_therapy_term_match"

    return True, "individual_practice_verified"


def search_tavily(query_obj: dict) -> list[dict]:
    if not TAVILY_API_KEY:
        therapy_type = query_obj.get("therapy_type", "unknown")
        logger.warning("Tavily API key missing — skipping search for %s", therapy_type)
        return []

    params = query_obj.get("params", {})
    therapy_type = query_obj.get("therapy_type", "unknown")
    include_domains = params.get("include_domains", [])
    query_string = params.get("query", "")
    aliases = _aliases_for_therapy(therapy_type)

    def _execute() -> list[dict]:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        search_kwargs = {
            "query": query_string,
            "search_depth": params.get("search_depth", "advanced"),
            "max_results": params.get("max_results", 5),
        }
        if include_domains:
            search_kwargs["include_domains"] = include_domains

        payload = client.search(**search_kwargs)

        raw_items = payload.get("results", [])
        logger.debug(
            "Tavily search for %s | query=%r | raw_results=%d",
            therapy_type,
            query_string,
            len(raw_items),
        )

        results: list[dict] = []
        filtered_out: list[str] = []
        rejection_counts: dict[str, int] = {}

        for item in raw_items:
            domain = _extract_domain(item.get("url"))
            title = item.get("title", "")
            score = float(item.get("score") or 0)
            passed, reason = _is_acceptable_tavily_result(
                item,
                domain,
                therapy_type,
                aliases,
            )

            if not passed:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                detail = f"{title!r} ({domain!r} reason: {reason}"
                if reason == "low_relevance_score":
                    detail += f", score={score}"
                detail += ")"
                filtered_out.append(detail)
                continue

            results.append(
                {
                    "source": "tavily",
                    "name": title,
                    "url": item.get("url"),
                    "domain": domain,
                    "snippet": item.get("content"),
                    "tavily_score": item.get("score"),
                    "raw": item,
                }
            )

        logger.debug(
            "Tavily search for %s | after_trust_filter=%d | filtered_out=%d",
            therapy_type,
            len(results),
            len(filtered_out),
        )
        if rejection_counts:
            logger.debug(
                "Tavily rejection breakdown for %s: %s",
                therapy_type,
                rejection_counts,
            )
        for reason in filtered_out:
            logger.debug("Tavily filtered out for %s: %s", therapy_type, reason)

        if len(results) < 3:
            logger.warning(
                "Tavily underperformed for %s: %d raw results, %d after trust filter; "
                "rejections=%s",
                therapy_type,
                len(raw_items),
                len(results),
                rejection_counts,
            )

        return results

    results = _run_with_retry(_execute, "tavily", therapy_type)
    logger.info(
        "Tavily search for %s returned %d results",
        therapy_type,
        len(results),
    )
    return results


def _run_with_retry(search_fn, source_name: str, therapy_type: str) -> list[dict]:
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            return search_fn()
        except (ConnectionError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                logger.warning(
                    "Retrying %s search for %s after failure (attempt %d/%d): %s",
                    source_name,
                    therapy_type,
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                )
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            break
        except Exception as exc:
            logger.warning(
                "%s search for %s failed: %s",
                source_name,
                therapy_type,
                exc,
            )
            return []

    logger.warning(
        "%s search for %s failed after %d retries: %s",
        source_name,
        therapy_type,
        MAX_RETRIES,
        last_error,
    )
    return []


def _facility_from_source_result(result: dict) -> dict:
    facility = {
        "name": result.get("name", ""),
        "address": result.get("address"),
        "phone": result.get("phone"),
        "website": result.get("website"),
        "rating": result.get("rating"),
        "review_count": result.get("review_count"),
        "place_id": result.get("place_id"),
        "sources_found_in": [result.get("source", "")],
        "tavily_score": result.get("tavily_score"),
        "snippet": result.get("snippet"),
        "url": result.get("url"),
        "domain": result.get("domain"),
        "services": result.get("services"),
        # NOTE: result_type is intentionally NOT set here. Classification is a
        # single, authoritative step performed in referral_scorer.score_facility
        # AFTER all cross-source merging/dedup is complete, so it must never be
        # assigned (or carried) at this pre-merge stage.
        "raw_sources": {},
    }

    source = result.get("source")
    if source:
        facility["raw_sources"][source] = result.get("raw", result)

    return facility


def _merge_facility(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)

    for field in (
        "address",
        "phone",
        "website",
        "rating",
        "review_count",
        "place_id",
        "tavily_score",
        "snippet",
        "url",
        "domain",
        "services",
    ):
        if merged.get(field) in (None, "", []):
            merged[field] = incoming.get(field)

    merged_sources = set(existing.get("sources_found_in", []))
    merged_sources.update(incoming.get("sources_found_in", []))
    merged["sources_found_in"] = sorted(merged_sources)

    raw_sources = dict(existing.get("raw_sources", {}))
    raw_sources.update(incoming.get("raw_sources", {}))
    merged["raw_sources"] = raw_sources

    existing_name = existing.get("name", "") or ""
    incoming_name = incoming.get("name", "") or ""
    merged["name"] = incoming_name if len(incoming_name) > len(existing_name) else existing_name

    return merged


def _dedupe_facilities(source_results: list[dict]) -> list[dict]:
    facilities: list[dict] = []

    for result in source_results:
        facility = _facility_from_source_result(result)
        match_index = _find_dedupe_index(facilities, facility)

        if match_index is not None:
            facilities[match_index] = _merge_facility(facilities[match_index], facility)
        else:
            facilities.append(facility)

    return facilities


def run_search_plan(query_plan_obj: dict) -> dict:
    plan_entries = query_plan_obj.get("query_plan", [])
    results_by_therapy: list[dict] = []

    for entry in plan_entries:
        therapy_type = entry.get("therapy_type", "")
        queries = entry.get("queries", {})

        samhsa_results = search_samhsa(queries.get("samhsa", {}))
        places_results = search_google_places(queries.get("google_places", {}))
        tavily_results = search_tavily(queries.get("tavily", {}))

        combined_source_results = samhsa_results + places_results + tavily_results
        facilities = _dedupe_facilities(combined_source_results)

        results_by_therapy.append(
            {
                "therapy_type": therapy_type,
                "gap_score": entry.get("gap_score", 0),
                "severity": entry.get("severity", ""),
                "source_counts": {
                    "samhsa": len(samhsa_results),
                    "google_places": len(places_results),
                    "tavily": len(tavily_results),
                },
                "facilities": facilities,
            }
        )

    results_by_therapy.sort(key=lambda item: item.get("gap_score", 0), reverse=True)

    return {
        "total_therapy_types_searched": len(results_by_therapy),
        "results_by_therapy": results_by_therapy,
    }


def _print_search_summary(search_results: dict) -> None:
    print("\nReferral search summary")
    print("=" * 60)

    for therapy_result in search_results.get("results_by_therapy", []):
        therapy_type = therapy_result.get("therapy_type")
        gap_score = therapy_result.get("gap_score")
        source_counts = therapy_result.get("source_counts", {})
        facilities = therapy_result.get("facilities", [])

        print(f"\n{therapy_type} (gap_score={gap_score})")
        print(
            "  samhsa: {samhsa} | google_places: {google_places} | tavily: {tavily}".format(
                **{
                    "samhsa": source_counts.get("samhsa", 0),
                    "google_places": source_counts.get("google_places", 0),
                    "tavily": source_counts.get("tavily", 0),
                }
            )
        )
        print(f"  merged facilities: {len(facilities)}")
        for facility in facilities:
            sources = ", ".join(facility.get("sources_found_in", []))
            print(f"    - {facility.get('name')} [{sources}]")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    sys.path.insert(0, str(_PROJECT_ROOT / "interventions"))
    sys.path.insert(0, str(_PROJECT_ROOT / "questionnaire"))

    from matching import detect_gaps
    from normalizer import normalize
    from query_builder import build_all_queries
    from rule_engine import get_recommendations

    mock_path = _PROJECT_ROOT / "mockProfile.json"
    with mock_path.open(encoding="utf-8") as mock_file:
        counselor_form = json.load(mock_file)

    profile = normalize(counselor_form)
    profile.update(
        {
            "zip": "30301",
            "location": "Atlanta, GA",
        }
    )

    rule_engine_output = get_recommendations(profile)
    current_services = ["cbt", "school counseling", "therapy family caregiver"]
    gaps_output = detect_gaps(rule_engine_output, current_services)
    query_plan = build_all_queries(gaps_output["gaps"], profile)

    search_results = run_search_plan(query_plan)
    _print_search_summary(search_results)

    print("\nPost-fix verification")
    print("=" * 60)

    for therapy_result in search_results.get("results_by_therapy", []):
        therapy_type = therapy_result.get("therapy_type")
        facilities = therapy_result.get("facilities", [])
        source_counts = therapy_result.get("source_counts", {})
        print(
            f"\n{therapy_type}: merged={len(facilities)} "
            f"(places={source_counts.get('google_places', 0)}, "
            f"tavily={source_counts.get('tavily', 0)})"
        )

        if therapy_type == "parent management training":
            my_team_matches = [
                facility
                for facility in facilities
                if "my team aba therapy in georgia"
                in normalize_facility_name(facility.get("name"))
            ]
            print(f"  'My Team ABA Therapy in Georgia' merged entries: {len(my_team_matches)}")
            for facility in my_team_matches:
                print(
                    f"    - {facility.get('name')} "
                    f"[{', '.join(facility.get('sources_found_in', []))}]"
                )

    tf_cbt_entry = next(
        (
            item
            for item in search_results.get("results_by_therapy", [])
            if item.get("therapy_type") == "TF-CBT"
        ),
        None,
    )
    if tf_cbt_entry:
        print("\nTF-CBT Tavily diagnostics (see DEBUG logs above for full detail):")
        print(f"  tavily source count: {tf_cbt_entry.get('source_counts', {}).get('tavily', 0)}")
        print(f"  merged facilities: {len(tf_cbt_entry.get('facilities', []))}")
