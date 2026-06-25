"""
MindScope pipeline step 6: score and rank referral search results.

Receives merged facility results from referral_search.py and filters/ranks them
deterministically before referral_enrichment.py.
"""

import difflib
import logging
import re
import sys
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

TRUSTED_SCORER_DIRECTORIES = {
    "psychologytoday.com",
    "therapyden.com",
    "goodtherapy.org",
    "zocdoc.com",
    "therapist.com",
}

RECOMMENDED_CAP = 5
POSSIBLE_CAP = 5

DIRECTORY_NAME_PATTERNS = (
    "find ",
    "best ",
    "support groups in",
    "therapists in",
    "psychiatrists in",
)

INFORMATIONAL_NAME_PATTERNS = (
    "official website",
    "certification program",
    " guide",
    "overview",
    "what is",
)

LISTING_URL_PATH_PATTERNS = (
    "/search",
    "/find",
    "/listings",
    "/results",
)

# Listicle/roundup titles ("9 Highly Recommended Atlanta Trauma Therapists",
# "10 Best ...", "Top 5 ...") read as curated lists, not individual businesses.
LISTICLE_TITLE_PATTERN = re.compile(
    r"^\s*(?:"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?:of\s+the\s+)?"
    r"(?:highly\s+recommended|highly\s+rated|best|top|leading|recommended|greatest|popular)"
    r"|(?:top|best)\s+\d+"
    r")\b",
    re.IGNORECASE,
)

TRAINING_ORG_DOMAINS = {
    "tfcbt.org",
}

RESULT_TYPE_PENALTIES = {
    "facility": 0,
    "directory_search_page": 25,
    "informational_page": 30,
    "pdf_or_document": 35,
}

RESULT_TYPE_REASONS = {
    "directory_search_page": "Directory search results page, not an individual facility",
    "informational_page": "Informational/training page, not a contactable facility",
    "pdf_or_document": "Document/PDF, not a contactable facility",
}

# A verified Google Places business with a therapy-name match and a strong
# rating is exactly what the recommended tier exists to surface, so give it a
# modest boost to lift genuine contactable facilities over the threshold.
VERIFIED_FACILITY_BOOST = 20


def _therapy_phrases(therapy_type: str, aliases: list[str]) -> list[str]:
    phrases = [therapy_type.lower().strip()]
    phrases.extend(alias.lower().strip() for alias in aliases if alias)
    return [phrase for phrase in phrases if phrase]


def _therapy_words(therapy_type: str, aliases: list[str]) -> set[str]:
    words: set[str] = set()
    for phrase in _therapy_phrases(therapy_type, aliases):
        for word in re.split(r"[^\w]+", phrase):
            if len(word) >= 3:
                words.add(word)
    return words


def _text_match_score(
    text: str | None,
    therapy_type: str,
    aliases: list[str],
    full_points: int,
    partial_points: int,
    reason_prefix: str,
) -> tuple[int, list[str]]:
    if not text:
        return 0, []

    text_lower = text.lower()
    for phrase in _therapy_phrases(therapy_type, aliases):
        if phrase in text_lower:
            return full_points, [f"{reason_prefix}: '{phrase}' found"]

    matched_words = sorted(word for word in _therapy_words(therapy_type, aliases) if word in text_lower)
    if matched_words:
        return partial_points, [
            f"{reason_prefix}: partial match on {', '.join(matched_words)}"
        ]

    return 0, []


def score_relevance(
    facility: dict,
    therapy_type: str,
    aliases: list[str],
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0

    name_score, name_reasons = _text_match_score(
        facility.get("name"),
        therapy_type,
        aliases,
        full_points=40,
        partial_points=20,
        reason_prefix="Therapy term in facility name",
    )
    score += name_score
    reasons.extend(name_reasons)

    snippet_score, snippet_reasons = _text_match_score(
        facility.get("snippet"),
        therapy_type,
        aliases,
        full_points=25,
        partial_points=10,
        reason_prefix="Therapy term in snippet/content",
    )
    score += snippet_score
    reasons.extend(snippet_reasons)

    sources = facility.get("sources_found_in") or []
    if len(sources) > 1:
        score += 15
        reasons.append(f"Corroborated across {len(sources)} sources: {', '.join(sorted(sources))}")
    elif len(sources) == 1:
        score += 5
        reasons.append(f"Found in single source: {sources[0]}")

    domain = (facility.get("domain") or "").lower()
    tavily_score = facility.get("tavily_score")
    trust_score = 0

    if domain in TRUSTED_SCORER_DIRECTORIES:
        trust_score = 20
        reasons.append(f"Listed on trusted directory ({domain})")
    elif tavily_score is not None and float(tavily_score) >= 0.7:
        trust_score = 20
        reasons.append(f"High Tavily relevance score ({float(tavily_score):.2f})")
    elif tavily_score is not None and float(tavily_score) >= 0.5:
        trust_score = 10
        reasons.append(f"Moderate Tavily relevance score ({float(tavily_score):.2f})")
    elif facility.get("place_id"):
        trust_score = 5
        reasons.append("Verified Google Places business listing")

    score += trust_score

    return score, reasons


def score_operational_quality(facility: dict) -> tuple[int, list[str]]:
    rating = facility.get("rating")
    review_count = facility.get("review_count")

    if rating is None:
        return 5, ["No Google rating data available (neutral score)"]

    try:
        rating_value = float(rating)
    except (TypeError, ValueError):
        return 5, ["Invalid rating data (neutral score)"]

    try:
        reviews = int(review_count or 0)
    except (TypeError, ValueError):
        reviews = 0

    if rating_value >= 4.5 and reviews >= 10:
        return 20, [f"Strong Google rating ({rating_value}) with {reviews} reviews"]
    if rating_value >= 4.0 and reviews >= 5:
        return 15, [f"Good Google rating ({rating_value}) with {reviews} reviews"]
    return 8, [f"Limited Google rating data ({rating_value}, {reviews} reviews)"]


def _result_url(facility: dict) -> str:
    return (facility.get("url") or facility.get("website") or "").strip()


def _result_domain(facility: dict) -> str:
    domain = (facility.get("domain") or "").lower().strip()
    if domain:
        return domain.replace("www.", "")

    parsed = urllib.parse.urlparse(_result_url(facility))
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def classify_result_type(facility: dict) -> str:
    name = facility.get("name") or ""
    name_lower = name.lower()
    url = _result_url(facility).lower()
    website = (facility.get("website") or "").lower()
    domain = _result_domain(facility)
    path = urllib.parse.urlparse(url).path.lower()

    if name_lower.startswith("[pdf]") or url.endswith(".pdf") or website.endswith(".pdf"):
        return "pdf_or_document"

    if any(pattern in name_lower for pattern in DIRECTORY_NAME_PATTERNS):
        return "directory_search_page"

    if LISTICLE_TITLE_PATTERN.match(name_lower):
        return "directory_search_page"

    if any(pattern in path for pattern in LISTING_URL_PATH_PATTERNS):
        return "directory_search_page"

    informational_name_match = any(
        pattern in name_lower for pattern in INFORMATIONAL_NAME_PATTERNS
    )
    if domain in TRAINING_ORG_DOMAINS:
        return "informational_page"

    if informational_name_match:
        return "informational_page"

    if (
        not facility.get("place_id")
        and domain.endswith((".org", ".gov"))
        and (
            informational_name_match
            or "certification" in name_lower
            or "training program" in name_lower
        )
    ):
        return "informational_page"

    return "facility"


def score_facility(facility: dict, therapy_type: str, aliases: list[str]) -> dict:
    relevance_score, relevance_reasons = score_relevance(facility, therapy_type, aliases)
    operational_score, operational_reasons = score_operational_quality(facility)
    # Single, authoritative classification step. This runs once, here, after all
    # cross-source merging/dedup in referral_search.py is complete. No earlier
    # stage assigns result_type. None is never a valid classification state.
    result_type = classify_result_type(facility) or "facility"
    penalty = RESULT_TYPE_PENALTIES.get(result_type, 0)

    score_reasons = relevance_reasons + operational_reasons
    if penalty:
        score_reasons.append(RESULT_TYPE_REASONS[result_type])

    has_name_match = any(
        reason.startswith("Therapy term in facility name") for reason in relevance_reasons
    )
    boost = 0
    if (
        result_type == "facility"
        and facility.get("place_id")
        and has_name_match
        and operational_score >= 20
    ):
        boost = VERIFIED_FACILITY_BOOST
        score_reasons.append(
            "Verified contactable facility with strong rating and therapy-name match"
        )

    total_score = relevance_score + operational_score - penalty + boost

    if total_score >= 70:
        tier = "recommended"
    elif total_score >= 45:
        tier = "possible"
    else:
        tier = "excluded"

    scored = dict(facility)
    scored.update(
        {
            "relevance_score": relevance_score,
            "operational_score": operational_score,
            "total_score": total_score,
            "result_type": result_type,
            "tier": tier,
            "score_reasons": score_reasons,
        }
    )

    # Hard guard: a scored facility must always carry a definite classification.
    # Raise loudly rather than letting a None silently flow through to output.
    if scored.get("result_type") not in RESULT_TYPE_PENALTIES:
        raise ValueError(
            f"score_facility produced an invalid result_type "
            f"{scored.get('result_type')!r} for facility {facility.get('name')!r}"
        )

    return scored


def rank_facilities(therapy_entry: dict, aliases_lookup: dict) -> dict:
    therapy_type = therapy_entry.get("therapy_type", "")
    aliases = aliases_lookup.get(therapy_type, [])

    scored_facilities = [
        score_facility(facility, therapy_type, aliases)
        for facility in therapy_entry.get("facilities", [])
    ]
    scored_facilities.sort(key=lambda item: item.get("total_score", 0), reverse=True)

    for facility in scored_facilities:
        logger.debug(
            "Scored %s for %s: total=%s relevance=%s operational=%s type=%s tier=%s reasons=%s",
            facility.get("name"),
            therapy_type,
            facility.get("total_score"),
            facility.get("relevance_score"),
            facility.get("operational_score"),
            facility.get("result_type"),
            facility.get("tier"),
            facility.get("score_reasons"),
        )

    recommended_all = [f for f in scored_facilities if f.get("tier") == "recommended"]
    possible_all = [f for f in scored_facilities if f.get("tier") == "possible"]
    excluded_count = sum(1 for f in scored_facilities if f.get("tier") == "excluded")

    recommended = recommended_all[:RECOMMENDED_CAP]
    possible = possible_all[:POSSIBLE_CAP]
    additional_options = recommended_all[RECOMMENDED_CAP:] + possible_all[POSSIBLE_CAP:]

    logger.info(
        "Scored %s: recommended=%d possible=%d additional=%d excluded=%d",
        therapy_type,
        len(recommended),
        len(possible),
        len(additional_options),
        excluded_count,
    )

    return {
        "therapy_type": therapy_type,
        "gap_score": therapy_entry.get("gap_score", 0),
        "severity": therapy_entry.get("severity", ""),
        "recommended": recommended,
        "possible": possible,
        "additional_options": additional_options,
        "excluded_count": excluded_count,
    }


def build_aliases_lookup(gaps_with_aliases: list[dict]) -> dict[str, list[str]]:
    sys.path.insert(0, str(_PROJECT_ROOT / "interventions"))
    from query_builder import resolve_therapy_key

    aliases_lookup: dict[str, list[str]] = {}
    for gap in gaps_with_aliases:
        therapy_key = resolve_therapy_key(
            gap.get("intervention", ""),
            gap.get("common_aliases", []),
        )
        if therapy_key:
            aliases_lookup[therapy_key] = gap.get("common_aliases", [])

    return aliases_lookup


def score_all_results(search_results: dict, gaps_with_aliases: list[dict]) -> dict:
    aliases_lookup = build_aliases_lookup(gaps_with_aliases)
    ranked = [
        rank_facilities(therapy_entry, aliases_lookup)
        for therapy_entry in search_results.get("results_by_therapy", [])
    ]

    return {
        "total_therapy_types_searched": search_results.get(
            "total_therapy_types_searched",
            len(ranked),
        ),
        "results_by_therapy": ranked,
    }


def _print_scored_summary(scored_results: dict) -> None:
    print("\nReferral scoring summary")
    print("=" * 60)

    for therapy_entry in scored_results.get("results_by_therapy", []):
        therapy_type = therapy_entry.get("therapy_type")
        print(f"\n{therapy_type} (gap_score={therapy_entry.get('gap_score')})")
        print(f"  excluded: {therapy_entry.get('excluded_count', 0)}")

        for label in ("recommended", "possible"):
            facilities = therapy_entry.get(label, [])
            print(f"\n  {label.upper()} ({len(facilities)})")
            for facility in facilities:
                print(
                    f"    [{facility.get('total_score')}] "
                    f"({facility.get('result_type')}) "
                    f"{facility.get('name')}"
                )
                for reason in facility.get("score_reasons", []):
                    print(f"      - {reason}")


def _find_facility(scored_results: dict, therapy_type: str, name_fragment: str) -> dict | None:
    name_fragment = name_fragment.lower()
    for therapy_entry in scored_results.get("results_by_therapy", []):
        if therapy_entry.get("therapy_type") != therapy_type:
            continue

        for bucket in ("recommended", "possible", "additional_options"):
            for facility in therapy_entry.get(bucket, []):
                if name_fragment in (facility.get("name") or "").lower():
                    return facility

    return None


def _find_facility_tier(scored_results: dict, therapy_type: str, name_fragment: str) -> str | None:
    name_fragment = name_fragment.lower()
    for therapy_entry in scored_results.get("results_by_therapy", []):
        if therapy_entry.get("therapy_type") != therapy_type:
            continue

        for bucket in ("recommended", "possible", "additional_options"):
            for facility in therapy_entry.get(bucket, []):
                if name_fragment in (facility.get("name") or "").lower():
                    return bucket if bucket != "additional_options" else "additional_options"

        return "excluded"

    return None


def _find_facility_anywhere(scored_results: dict, name_fragment: str) -> tuple[str | None, str | None, dict | None]:
    name_fragment = name_fragment.lower()
    for therapy_entry in scored_results.get("results_by_therapy", []):
        for bucket in ("recommended", "possible", "additional_options"):
            for facility in therapy_entry.get(bucket, []):
                if name_fragment in (facility.get("name") or "").lower():
                    return therapy_entry.get("therapy_type"), bucket, facility
    return None, None, None


def _facilities_in_therapy(scored_results: dict, therapy_type: str) -> list[tuple[str, dict]]:
    entries: list[tuple[str, dict]] = []
    for therapy_entry in scored_results.get("results_by_therapy", []):
        if therapy_entry.get("therapy_type") != therapy_type:
            continue
        for bucket in ("recommended", "possible", "additional_options"):
            for facility in therapy_entry.get(bucket, []):
                entries.append((bucket, facility))
    return entries


def _find_facility_fuzzy(
    scored_results: dict, name: str, threshold: float = 0.6
) -> tuple[str | None, str | None, dict | None, float]:
    target = name.lower()
    best_therapy: str | None = None
    best_bucket: str | None = None
    best_facility: dict | None = None
    best_ratio = 0.0

    for therapy_entry in scored_results.get("results_by_therapy", []):
        for bucket in ("recommended", "possible", "additional_options"):
            for facility in therapy_entry.get(bucket, []):
                candidate = (facility.get("name") or "").lower()
                ratio = difflib.SequenceMatcher(None, target, candidate).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_therapy = therapy_entry.get("therapy_type")
                    best_bucket = bucket
                    best_facility = facility

    if best_ratio >= threshold:
        return best_therapy, best_bucket, best_facility, best_ratio
    return None, None, None, best_ratio


def _facilities_missing_result_type(scored_results: dict) -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    for therapy_entry in scored_results.get("results_by_therapy", []):
        for bucket in ("recommended", "possible", "additional_options"):
            for facility in therapy_entry.get(bucket, []):
                if facility.get("result_type") is None:
                    missing.append((therapy_entry.get("therapy_type"), facility.get("name")))
    return missing


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    sys.path.insert(0, str(_PROJECT_ROOT / "interventions"))
    sys.path.insert(0, str(_PROJECT_ROOT / "questionnaire"))

    from matching import detect_gaps
    from normalizer import normalize
    from query_builder import build_all_queries
    from referral_search import run_search_plan
    from rule_engine import get_recommendations

    mock_path = _PROJECT_ROOT / "mockProfile.json"
    with mock_path.open(encoding="utf-8") as mock_file:
        import json

        counselor_form = json.load(mock_file)

    profile = normalize(counselor_form)
    profile.update({"zip": "30301", "location": "Atlanta, GA"})

    rule_engine_output = get_recommendations(profile)
    current_services = ["cbt", "school counseling", "therapy family caregiver"]
    gaps_output = detect_gaps(rule_engine_output, current_services)
    query_plan = build_all_queries(gaps_output["gaps"], profile)

    search_results = run_search_plan(query_plan)
    scored_results = score_all_results(search_results, gaps_output["gaps"])

    print("\nUpdated tiers: TF-CBT and family therapy")
    print("=" * 60)
    for therapy_type in ("TF-CBT", "family therapy"):
        therapy_entry = next(
            (
                entry
                for entry in scored_results.get("results_by_therapy", [])
                if entry.get("therapy_type") == therapy_type
            ),
            None,
        )
        if not therapy_entry:
            continue

        print(f"\n{therapy_type}")
        for label in ("recommended", "possible"):
            facilities = therapy_entry.get(label, [])
            print(f"  {label.upper()} ({len(facilities)})")
            for facility in facilities:
                print(
                    f"    [{facility.get('total_score')}] "
                    f"({facility.get('result_type')}) "
                    f"{facility.get('name')}"
                )
                for reason in facility.get("score_reasons", []):
                    print(f"      - {reason}")

    print("\nReclassification verification")
    print("=" * 60)
    tf_cbt_checks = [
        "TF-CBT Certification Program - Official Website",
        "[PDF] TRAUMA-FOCUSED COGNITIVE BEHAVIORAL THERAPY",
    ]
    for name in tf_cbt_checks:
        tier = _find_facility_tier(scored_results, "TF-CBT", name)
        print(f"  TF-CBT | {name}: {tier}")

    listicle_tier = _find_facility_tier(scored_results, "TF-CBT", "9 Highly Recommended Atlanta Trauma Therapists")
    listicle_facility = _find_facility(scored_results, "TF-CBT", "9 Highly Recommended Atlanta Trauma Therapists")
    listicle_type = listicle_facility.get("result_type") if listicle_facility else None
    print(
        f"  TF-CBT | 9 Highly Recommended Atlanta Trauma Therapists: "
        f"tier={listicle_tier}, type={listicle_type} (expected type=directory_search_page)"
    )

    family_checks = [
        ("Find Family Therapy Psychiatrists in Atlanta, GA", "directory_search_page"),
        ("Georgia Family Therapy", "facility"),
    ]
    for name, expected_type in family_checks:
        tier = _find_facility_tier(scored_results, "family therapy", name)
        facility = _find_facility(scored_results, "family therapy", name)
        result_type = facility.get("result_type") if facility else None
        print(
            f"  family therapy | {name}: tier={tier}, "
            f"type={result_type} (expected type={expected_type})"
        )

    # Print the full candidate inventory first so it's clear whether the
    # facility was renamed during dedup or is genuinely absent from this run.
    print("\nFacility name inventory (TF-CBT, family therapy)")
    print("=" * 60)
    for therapy_type in ("TF-CBT", "family therapy"):
        print(f"  {therapy_type}:")
        for bucket, facility in _facilities_in_therapy(scored_results, therapy_type):
            print(f"    [{bucket}] {facility.get('name')}")

    # This is an individual provider profile and may surface under any therapy
    # type (and may be renamed during dedup), so fuzzy-match across the whole
    # output. If it is genuinely absent post-merge, skip rather than printing a
    # false type=None failure that contradicts the integrity check below.
    pt_name = "Trauma-Focused Cognitive Behavior Therapy - Psychology Today"
    pt_therapy, pt_tier, pt_facility, pt_ratio = _find_facility_fuzzy(scored_results, pt_name)
    if pt_facility is not None:
        print(
            f"\n  any | {pt_name}: matched '{pt_facility.get('name')}' "
            f"(similarity={pt_ratio:.2f}) therapy={pt_therapy}, tier={pt_tier}, "
            f"type={pt_facility.get('result_type')} (expected type=facility)"
        )
    else:
        print(
            f"\n  any | {pt_name}: not present in this run's results "
            f"(best similarity={pt_ratio:.2f}) — skipping, no false failure"
        )

    print("\nresult_type integrity check")
    print("=" * 60)
    missing_types = _facilities_missing_result_type(scored_results)
    print(f"  facilities with result_type=None across all therapy types: {len(missing_types)}")
    for therapy_type, name in missing_types:
        print(f"    - {therapy_type}: {name}")

    _print_scored_summary(scored_results)

    print("\nRelevance verification (parent management training)")
    print("=" * 60)
    checks = [
        "Prevent Child Abuse Georgia",
        "Anger Management Assessments",
    ]
    for name in checks:
        tier = _find_facility_tier(scored_results, "parent management training", name)
        print(f"  {name}: {tier}")

    print("\nSocial skills training diagnostic (possible tier)")
    print("=" * 60)
    sst_entry = next(
        (
            entry
            for entry in scored_results.get("results_by_therapy", [])
            if entry.get("therapy_type") == "social skills training"
        ),
        None,
    )
    if not sst_entry:
        print("  no social skills training results in this run")
    else:
        for facility in sst_entry.get("possible", []):
            name_match = next(
                (
                    reason
                    for reason in facility.get("score_reasons", [])
                    if reason.startswith("Therapy term in facility name")
                ),
                None,
            )
            print(f"  [{facility.get('total_score')}] ({facility.get('result_type')}) {facility.get('name')}")
            print(
                f"      relevance={facility.get('relevance_score')} "
                f"operational={facility.get('operational_score')}"
            )
            print(f"      name_match: {name_match or 'NONE — no therapy term in facility name'}")

    print("\nConsolidated scoring health summary")
    print("=" * 60)
    print(f"  {'therapy type':<30} {'rec':>4} {'pos':>4} {'exc':>4}")
    print("  " + "-" * 46)
    total_rec = total_pos = total_exc = 0
    for entry in scored_results.get("results_by_therapy", []):
        rec = pos = 0
        for bucket in ("recommended", "possible", "additional_options"):
            for facility in entry.get(bucket, []):
                if facility.get("tier") == "recommended":
                    rec += 1
                elif facility.get("tier") == "possible":
                    pos += 1
        exc = entry.get("excluded_count", 0)
        total_rec += rec
        total_pos += pos
        total_exc += exc
        print(f"  {entry.get('therapy_type', ''):<30} {rec:>4} {pos:>4} {exc:>4}")
    print("  " + "-" * 46)
    print(f"  {'TOTAL':<30} {total_rec:>4} {total_pos:>4} {total_exc:>4}")
