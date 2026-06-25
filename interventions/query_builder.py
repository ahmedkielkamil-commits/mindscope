"""
MindScope pipeline step 4: build API query parameters for care gap searches.

Receives care gaps from matching.py and a normalized student profile, and produces
structured query plans for referral_search.py. No API calls are made here.
"""

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_KB_PATH = _PROJECT_ROOT / "knowledge_base.json"

load_dotenv(_PROJECT_ROOT / ".env")

with _KB_PATH.open(encoding="utf-8") as _kb_file:
    THERAPY_TO_QUERY_MAP = json.load(_kb_file).get("therapy_to_query_map", {})

ZIP_LATLONG_CACHE: dict[str, tuple[float, float]] = {}

COMMON_ZIP_LATLONG = {
    "30301": (33.7490, -84.3880),
    "10001": (40.7506, -73.9971),
    "90210": (34.1030, -118.4105),
    "60601": (41.8857, -87.6180),
    "77001": (29.7604, -95.3698),
    "85001": (33.4484, -112.0740),
    "19101": (39.9526, -75.1652),
    "78201": (29.4241, -98.4936),
    "92101": (32.7157, -117.1611),
    "75201": (32.7767, -96.7970),
    "95101": (37.3382, -121.8863),
    "78701": (30.2672, -97.7431),
    "32201": (30.3322, -81.6557),
    "76101": (32.7555, -97.3308),
    "43201": (39.9612, -82.9988),
    "28201": (35.2271, -80.8431),
    "94102": (37.7799, -122.4194),
    "46201": (39.7684, -86.1581),
    "98101": (47.6062, -122.3321),
    "80201": (39.7392, -104.9903),
}

SKIP_GAP_CATEGORIES = frozenset({"escalation", "school_support"})

TAVILY_INCLUDE_DOMAINS = [
    "psychologytoday.com",
    "therapyden.com",
    "findtreatment.gov",
    "samhsa.gov",
    "therapist.com",
    "zocdoc.com",
]


def _normalize_lookup_text(value: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", "", str(value)).lower().strip()
    return " ".join(cleaned.split())


def resolve_therapy_key(intervention_name: str, aliases: list[str] | None = None) -> str | None:
    texts = [_normalize_lookup_text(intervention_name)]
    texts.extend(_normalize_lookup_text(alias) for alias in (aliases or []))

    for therapy_key in sorted(THERAPY_TO_QUERY_MAP, key=len, reverse=True):
        normalized_key = _normalize_lookup_text(therapy_key)
        for text in texts:
            if normalized_key in text:
                return therapy_key

    logger.warning(
        "No therapy_to_query_map entry matched intervention %r — add a KB mapping",
        intervention_name,
    )
    return None


def age_to_label(age: int) -> dict[str, str]:
    return {
        "samhsa": "child" if age <= 12 else "adolescent",
        "places": "child" if age <= 8 else "teen" if age <= 12 else "adolescent",
        "tavily": "child" if age <= 8 else "teen" if age <= 12 else "adolescent teenager",
    }


def _location_query_suffix(location: str | None) -> str:
    if not location:
        return ""
    return location.replace(",", "").strip()


def zip_to_latlong(zip_code: str | None) -> tuple[float, float] | str | None:
    if not zip_code:
        logger.warning("No zip code provided for lat/lng lookup")
        return None

    zip_code = str(zip_code).strip()
    if not zip_code:
        logger.warning("Empty zip code provided for lat/lng lookup")
        return None

    if zip_code in COMMON_ZIP_LATLONG:
        coords = COMMON_ZIP_LATLONG[zip_code]
        ZIP_LATLONG_CACHE[zip_code] = coords
        return coords

    if zip_code in ZIP_LATLONG_CACHE:
        return ZIP_LATLONG_CACHE[zip_code]

    try:
        url = f"https://api.zippopotam.us/us/{zip_code}"
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.load(response)
        place = payload["places"][0]
        coords = (float(place["latitude"]), float(place["longitude"]))
        ZIP_LATLONG_CACHE[zip_code] = coords
        return coords
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, IndexError) as exc:
        logger.warning("Zip lookup failed for %s: %s", zip_code, exc)
        return None


def build_samhsa_query(
    therapy_key: str,
    gap: dict,
    profile: dict,
    age_labels: dict[str, str],
) -> dict:
    mapping = THERAPY_TO_QUERY_MAP.get(therapy_key, {})
    zip_code = profile.get("zip", "")

    return {
        "source": "samhsa",
        "endpoint": "https://findtreatment.gov/locator/listing",
        "params": {
            "sType": 4,
            "addr": zip_code,
            "distance": 25,
            "age": age_labels.get("samhsa", "adolescent"),
            "sCat": mapping.get("samhsa_service_type", ""),
            "specialty": mapping.get("samhsa_specialty", ""),
        },
        "therapy_type": therapy_key,
        "gap_score": gap.get("gap_score", 0),
        "severity": gap.get("severity_if_missing", ""),
    }


def build_places_query(
    therapy_key: str,
    gap: dict,
    profile: dict,
    age_labels: dict[str, str],
) -> dict:
    mapping = THERAPY_TO_QUERY_MAP.get(therapy_key, {})
    location_suffix = _location_query_suffix(profile.get("location"))
    places_term = mapping.get("places_query_term", therapy_key)
    query = f"{places_term} {location_suffix}".strip()

    zip_code = profile.get("zip")
    coords = zip_to_latlong(zip_code)
    if coords is None:
        location_value = location_suffix or str(zip_code or "")
        logger.warning(
            "Using location string %r for Google Places because zip lookup failed",
            location_value,
        )
    else:
        location_value = f"{coords[0]},{coords[1]}"

    return {
        "source": "google_places",
        "endpoint": "https://maps.googleapis.com/maps/api/place/textsearch/json",
        "params": {
            "query": query,
            "location": location_value,
            "radius": 20000,
            "type": "health",
            "key": os.getenv("GOOGLE_PLACES_API_KEY", ""),
        },
        "therapy_type": therapy_key,
        "gap_score": gap.get("gap_score", 0),
        "severity": gap.get("severity_if_missing", ""),
    }


def build_tavily_query(
    therapy_key: str,
    gap: dict,
    profile: dict,
    age_labels: dict[str, str],
) -> dict:
    mapping = THERAPY_TO_QUERY_MAP.get(therapy_key, {})
    search_terms = mapping.get("tavily_search_terms", [])
    base_term = search_terms[0] if search_terms else therapy_key
    location_suffix = _location_query_suffix(profile.get("location"))
    age_label = age_labels.get("tavily", "adolescent")
    query = f"{base_term} {location_suffix} {age_label}".strip()

    return {
        "source": "tavily",
        "params": {
            "query": query,
            "include_domains": TAVILY_INCLUDE_DOMAINS,
            "search_depth": "advanced",
            "max_results": 5,
        },
        "therapy_type": therapy_key,
        "gap_score": gap.get("gap_score", 0),
        "severity": gap.get("severity_if_missing", ""),
    }


def build_queries_for_gap(gap: dict, profile: dict, age_labels: dict[str, str]) -> dict | None:
    therapy_key = resolve_therapy_key(gap.get("intervention", ""), gap.get("common_aliases", []))
    if therapy_key is None:
        return None

    return {
        "therapy_type": therapy_key,
        "gap_score": gap.get("gap_score", 0),
        "severity": gap.get("severity_if_missing", ""),
        "queries": {
            "samhsa": build_samhsa_query(therapy_key, gap, profile, age_labels),
            "google_places": build_places_query(therapy_key, gap, profile, age_labels),
            "tavily": build_tavily_query(therapy_key, gap, profile, age_labels),
        },
    }


def build_all_queries(gaps: list[dict], profile: dict) -> dict:
    age = int(profile.get("age", 12))
    age_labels = age_to_label(age)
    query_plan: list[dict] = []

    for gap in gaps:
        category = gap.get("category", "")
        if category in SKIP_GAP_CATEGORIES:
            logger.debug(
                "Skipping gap %r — category %r does not need facility search",
                gap.get("intervention"),
                category,
            )
            continue

        gap_queries = build_queries_for_gap(gap, profile, age_labels)
        if gap_queries is None:
            continue

        query_plan.append(gap_queries)

    query_plan.sort(key=lambda item: item.get("gap_score", 0), reverse=True)

    return {
        "student_zip": profile.get("zip", ""),
        "student_location": profile.get("location", ""),
        "age_labels": age_labels,
        "total_gaps_to_search": len(query_plan),
        "query_plan": query_plan,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    sys.path.insert(0, str(_PROJECT_ROOT / "questionnaire"))

    from matching import detect_gaps
    from normalizer import normalize
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
    print(json.dumps(query_plan, indent=2))
