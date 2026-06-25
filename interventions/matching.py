"""
MindScope pipeline step 3: compare rule engine recommendations against
current services and return deterministic care gaps.
"""

import json
import logging
import re
import sys
from pathlib import Path

from thefuzz import fuzz

logger = logging.getLogger(__name__)

SEVERITY_RANK = {
    "critical": 3,
    "high": 2,
    "moderate": 1,
}

SEVERITY_BASE_SCORE = {
    "critical": 300,
    "high": 200,
    "moderate": 100,
}

FUZZY_MATCH_THRESHOLD = 80
PROTECTED_FUZZY_THRESHOLD = 95

PROTECTED_INTERVENTIONS = {
    "tf-cbt",
    "trauma-focused cognitive behavioral therapy",
    "trauma-focused cbt",
    "dbt",
    "dialectical behavior therapy",
    "trauma-informed",
    "emdr",
    "eye movement desensitization",
    "parent-child interaction therapy",
    "pcit",
}


def normalize_string(value: str | None) -> str:
    if value is None:
        return ""
    normalized = re.sub(r"[^\w\s]", "", str(value).lower().strip())
    normalized = normalized.replace("_", " ")
    return " ".join(normalized.split())


def is_protected(intervention_name: str, aliases: list[str] | None = None) -> bool:
    normalized_texts = [normalize_string(intervention_name)]
    normalized_texts.extend(normalize_string(alias) for alias in (aliases or []))

    for text in normalized_texts:
        for term in PROTECTED_INTERVENTIONS:
            if normalize_string(term) in text:
                return True

    return False


def _find_match(
    current_services: list[str],
    intervention_name: str,
    aliases: list[str] | None,
) -> tuple[bool, str | None, str | None]:
    if not current_services:
        return False, None, None

    candidates = [intervention_name, *(aliases or [])]
    normalized_candidates = [(candidate, normalize_string(candidate)) for candidate in candidates]
    protected = is_protected(intervention_name, aliases)

    if protected:
        logger.debug(
            "Protected intervention %r — substring skipped, fuzzy threshold raised to %d",
            intervention_name,
            PROTECTED_FUZZY_THRESHOLD,
        )

    for service in current_services:
        normalized_service = normalize_string(service)
        if not normalized_service:
            continue
        for candidate, normalized_candidate in normalized_candidates:
            if normalized_service == normalized_candidate:
                return True, "exact", service

    if not protected:
        for service in current_services:
            normalized_service = normalize_string(service)
            if not normalized_service:
                continue
            for candidate, normalized_candidate in normalized_candidates:
                if (
                    normalized_service in normalized_candidate
                    or normalized_candidate in normalized_service
                ):
                    return True, "substring", service

    fuzzy_threshold = PROTECTED_FUZZY_THRESHOLD if protected else FUZZY_MATCH_THRESHOLD
    for service in current_services:
        normalized_service = normalize_string(service)
        if not normalized_service:
            continue
        for candidate, normalized_candidate in normalized_candidates:
            score = fuzz.token_sort_ratio(normalized_service, normalized_candidate)
            if score >= fuzzy_threshold:
                logger.debug(
                    "Fuzzy match: intervention=%r service=%r score=%d threshold=%d",
                    intervention_name,
                    service,
                    score,
                    fuzzy_threshold,
                )
                return True, "fuzzy", service

    return False, None, None


def is_covered(
    current_services: list[str] | None,
    intervention_name: str,
    aliases: list[str] | None = None,
) -> bool:
    services = current_services or []
    matched, _, _ = _find_match(services, intervention_name, aliases)
    return matched


def _gap_score(recommendation: dict) -> int:
    base = SEVERITY_BASE_SCORE.get(recommendation.get("severity_if_missing", ""), 0)
    triggered_by = recommendation.get("triggered_by", [])
    extra = max(0, len(triggered_by) - 1) * 10
    return base + extra


def _sort_gaps(gaps: list[dict]) -> list[dict]:
    escalation_gaps = [gap for gap in gaps if gap.get("category") == "escalation"]
    other_gaps = [gap for gap in gaps if gap.get("category") != "escalation"]

    other_gaps.sort(
        key=lambda gap: (
            -SEVERITY_RANK.get(gap.get("severity_if_missing", ""), 0),
            -len(gap.get("triggered_by", [])),
            -gap.get("gap_score", 0),
            gap.get("intervention", ""),
        )
    )

    return escalation_gaps + other_gaps


def detect_gaps(rule_engine_output: dict, current_services: list[str] | None) -> dict:
    services = current_services or []
    recommendations = rule_engine_output.get("recommendations", [])

    covered: list[dict] = []
    gaps: list[dict] = []

    for recommendation in recommendations:
        intervention_name = recommendation.get("intervention", "")
        aliases = recommendation.get("common_aliases", [])
        is_escalation = recommendation.get("category") == "escalation"

        if is_escalation:
            gap_entry = dict(recommendation)
            gap_entry["gap_score"] = _gap_score(recommendation)
            gaps.append(gap_entry)
            continue

        matched, match_tier, matched_to = _find_match(
            services,
            intervention_name,
            aliases,
        )

        if matched:
            covered.append(
                {
                    "intervention": intervention_name,
                    "match_tier": match_tier,
                    "matched_to": matched_to,
                    "severity_if_missing": recommendation.get("severity_if_missing", ""),
                }
            )
            continue

        gap_entry = dict(recommendation)
        gap_entry["gap_score"] = _gap_score(recommendation)
        gaps.append(gap_entry)

    gaps = _sort_gaps(gaps)

    total_recommendations = len(recommendations)
    total_covered = len(covered)
    total_gaps = len(gaps)
    coverage_rate = round(total_covered / total_recommendations, 2) if total_recommendations else 0.0

    return {
        "total_recommendations": total_recommendations,
        "total_covered": total_covered,
        "total_gaps": total_gaps,
        "coverage_rate": coverage_rate,
        "covered": covered,
        "gaps": gaps,
        "resolved_conditions": rule_engine_output.get("resolved_conditions", []),
        "age_bracket_used": rule_engine_output.get("age_bracket_used", ""),
        "functional_bracket_used": rule_engine_output.get("functional_bracket_used", False),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root / "questionnaire"))

    from normalizer import normalize
    from rule_engine import get_recommendations

    mock_path = project_root / "mockProfile.json"
    with mock_path.open(encoding="utf-8") as mock_file:
        counselor_form = json.load(mock_file)

    profile = normalize(counselor_form)
    rule_engine_output = get_recommendations(profile)

    current_services = [
        "cbt",
        "school counseling",
        "therapy family caregiver",
    ]

    result = detect_gaps(rule_engine_output, current_services)
    print(json.dumps(result, indent=2))

    tf_cbt_name = "Trauma-Focused Cognitive Behavioral Therapy (TF-CBT)"
    trauma_informed_name = "School-Based Trauma-Informed Support"
    cbt_name = "Cognitive Behavioral Therapy (CBT) with Graduated Exposure"
    dbt_name = "Dialectical Behavior Therapy (DBT)"
    dbt_aliases = ["DBT", "dialectical behavior therapy"]

    def _in_gaps(services: list[str], intervention: str) -> bool:
        gaps_result = detect_gaps(rule_engine_output, services)
        return any(item["intervention"] == intervention for item in gaps_result["gaps"])

    def _is_covered(services: list[str], intervention: str, aliases: list[str] | None = None) -> bool:
        return is_covered(services, intervention, aliases)

    checks = [
        (
            _in_gaps(["cbt"], tf_cbt_name),
            f"{tf_cbt_name} appears in gaps when current_services is ['cbt']",
        ),
        (
            _in_gaps(["CBT"], tf_cbt_name),
            f"{tf_cbt_name} appears in gaps when current_services is ['CBT']",
        ),
        (
            _in_gaps(["school counseling"], trauma_informed_name),
            f"{trauma_informed_name} appears in gaps when current_services is ['school counseling']",
        ),
        (
            _is_covered(["cbt"], cbt_name, ["CBT", "cognitive behavioral therapy"]),
            f"{cbt_name} is covered when current_services contains 'cbt'",
        ),
        (
            not _is_covered(["cbt"], dbt_name, dbt_aliases),
            f"{dbt_name} is not covered by 'cbt'",
        ),
        (
            not _is_covered(["school counseling"], dbt_name, dbt_aliases),
            f"{dbt_name} is not covered by 'school counseling'",
        ),
        (
            _is_covered(["dbt"], dbt_name, dbt_aliases),
            f"{dbt_name} is covered when current_services contains 'dbt'",
        ),
    ]

    print("\nProtected intervention verification:")
    for passed, message in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {message}")
