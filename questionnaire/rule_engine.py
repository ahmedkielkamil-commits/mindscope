"""
MindScope pipeline step 2: deterministic intervention recommendations.

Receives a normalized student profile from normalizer.py and returns
knowledge-base recommendations keyed to inferred conditions and age bracket.
"""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_KB_PATH = Path(__file__).resolve().parent.parent / "knowledge_base.json"

with _KB_PATH.open(encoding="utf-8") as _kb_file:
    KNOWLEDGE_BASE = json.load(_kb_file)

CONDITIONS = KNOWLEDGE_BASE.get("conditions", {})

VALID_BRACKETS = ("3-5", "6-12", "13-18")

SEVERITY_RANK = {
    "critical": 3,
    "high": 2,
    "moderate": 1,
}

CANONICAL_THERAPY_MAP = {
    "cognitive behavioral therapy": "CBT",
    "cognitive behavior therapy": "CBT",
    "cognitive behaviour therapy": "CBT",
    "cbt": "CBT",
    "dialectical behavior therapy": "DBT",
    "dbt": "DBT",
    "trauma-focused cognitive behavioral therapy": "TF-CBT",
    "tf-cbt": "TF-CBT",
    "applied behavior analysis": "ABA",
    "aba": "ABA",
    "speech and language therapy": "Speech and Language Therapy",
    "speech therapy": "Speech and Language Therapy",
    "occupational therapy": "Occupational Therapy",
    "ot": "Occupational Therapy",
    "family therapy": "Family Therapy",
    "play therapy": "Play Therapy",
    "social skills training": "Social Skills Training",
    "school-based counseling": "School-Based Counseling",
    "parent management training": "Parent Management Training",
    "pmt": "Parent Management Training",
}

AUTISM_SIGNAL_BEHAVIORS = frozenset(
    {
        "sensory_sensitivity",
        "transition_difficulty",
        "difficulty_with_transitions",
        "communication_difficulty",
        "repetitive_behaviors",
        "limited_eye_contact",
        "delayed_speech",
    }
)

CONDITION_SIGNAL_REQUIREMENTS = {
    "autism_spectrum_disorder": {
        "required_signals": AUTISM_SIGNAL_BEHAVIORS,
        "min_signals": 2,
    },
}

BEHAVIOR_TO_CONDITIONS = {
    "emotional_outbursts": [
        "emotional_dysregulation",
        "adhd",
        "oppositional_defiant_behavior",
        "trauma_response",
    ],
    "transition_difficulty": [
        "autism_spectrum_disorder",
        "adhd",
        "sensory_processing_disorder",
        "emotional_dysregulation",
    ],
    "persistent_avoidance": [
        "anxiety_school_avoidance",
        "trauma_response",
        "depression_withdrawal",
    ],
    "social_withdrawal": [
        "social_isolation",
        "depression_withdrawal",
        "anxiety_school_avoidance",
    ],
    "attention_difficulty": [
        "adhd",
        "anxiety_school_avoidance",
        "depression_withdrawal",
    ],
    "oppositional_defiant": [
        "oppositional_defiant_behavior",
        "emotional_dysregulation",
    ],
    "visible_anxiety": [
        "anxiety_school_avoidance",
        "trauma_response",
    ],
    "mood_persistently_low": [
        "depression_withdrawal",
        "emotional_dysregulation",
        "trauma_response",
    ],
    "impulsive_disruptive": [
        "adhd",
        "emotional_dysregulation",
        "oppositional_defiant_behavior",
    ],
    "sensory_sensitivity": [
        "sensory_processing_disorder",
        "autism_spectrum_disorder",
    ],
    "self_isolating_unstructured": [
        "social_isolation",
        "anxiety_school_avoidance",
    ],
    "concerning_statements": [
        "self_harm_risk",
        "depression_withdrawal",
        "trauma_response",
    ],
    "self_harm_risk": [
        "self_harm_risk",
    ],
}

ESCALATION_RECOMMENDATION = {
    "intervention": "Immediate Mental Health Escalation",
    "category": "escalation",
    "severity_if_missing": "critical",
    "rationale": (
        "Counselor has flagged this case as urgent or observed behaviors "
        "suggesting immediate risk. Standard referral timeline is not appropriate."
    ),
    "sources": [],
    "triggered_by": ["urgency_flag"],
}


def _clamp_age(age: int) -> int:
    return max(3, min(18, age))


def _bracket_for_age(age: int) -> str:
    age = _clamp_age(age)
    if age <= 5:
        return "3-5"
    if age <= 12:
        return "6-12"
    return "13-18"


def age_to_bracket(age: int, functional_age_offset: int = 0) -> tuple[str, bool]:
    """
    Map chronological age (and optional functional offset) to a KB age bracket.

    Returns (bracket, functional_bracket_used).
    """
    chronological_bracket = _bracket_for_age(age)
    functional_age = _clamp_age(age + functional_age_offset)
    functional_bracket = _bracket_for_age(functional_age)

    if functional_bracket != chronological_bracket:
        return functional_bracket, True
    return chronological_bracket, False


def normalize_intervention_name(name: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", "", name).lower().strip()
    cleaned = " ".join(cleaned.split())

    if cleaned in CANONICAL_THERAPY_MAP:
        return CANONICAL_THERAPY_MAP[cleaned]

    for key in sorted(CANONICAL_THERAPY_MAP, key=len, reverse=True):
        if key in cleaned:
            return CANONICAL_THERAPY_MAP[key]

    return cleaned


def validate_conditions(behaviors: list[str], conditions: set[str]) -> set[str]:
    validated = set(conditions)

    for condition_key, requirements in CONDITION_SIGNAL_REQUIREMENTS.items():
        if condition_key not in validated:
            continue

        signal_behaviors = requirements["required_signals"]
        min_signals = requirements.get("min_signals", 2)
        present_signals = [behavior for behavior in behaviors if behavior in signal_behaviors]

        if len(present_signals) < min_signals:
            logger.debug(
                "Removed %s from resolved conditions: only %d/%d required "
                "signal behaviors present (%s)",
                condition_key,
                len(present_signals),
                min_signals,
                present_signals,
            )
            validated.discard(condition_key)

    return validated


def _resolve_conditions(observed_behaviors: list[str]) -> list[str]:
    conditions: set[str] = set()

    for behavior in observed_behaviors:
        mapped = BEHAVIOR_TO_CONDITIONS.get(behavior)
        if mapped is None:
            logger.warning("Unknown observed behavior: %s", behavior)
            continue
        conditions.update(mapped)

    conditions = validate_conditions(observed_behaviors, conditions)
    return sorted(conditions)


def _nearest_available_bracket(requested: str, available: list[str]) -> str | None:
    if not available:
        return None
    if requested in available:
        return requested

    requested_index = VALID_BRACKETS.index(requested)
    return min(
        available,
        key=lambda bracket: abs(VALID_BRACKETS.index(bracket) - requested_index),
    )


def _get_condition_recommendations(condition_key: str, age_bracket: str) -> list[dict]:
    condition = CONDITIONS.get(condition_key)
    if condition is None:
        logger.warning("Condition key missing from knowledge base: %s", condition_key)
        return []

    age_groups = condition.get("age_groups", {})
    available_brackets = [bracket for bracket in VALID_BRACKETS if bracket in age_groups]
    resolved_bracket = _nearest_available_bracket(age_bracket, available_brackets)

    if resolved_bracket is None:
        logger.warning(
            "No age brackets available for condition %s; skipping",
            condition_key,
        )
        return []

    if resolved_bracket != age_bracket:
        logger.warning(
            "Age bracket %s unavailable for %s; falling back to %s",
            age_bracket,
            condition_key,
            resolved_bracket,
        )

    return age_groups[resolved_bracket].get("recommended", [])


def _severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, 0)


def _recommendation_score(recommendation: dict) -> tuple[int, int]:
    return (
        _severity_rank(recommendation["severity_if_missing"]),
        len(recommendation.get("sources", [])),
    )


def _merge_recommendation(existing: dict, incoming: dict) -> dict:
    winner = (
        incoming
        if _recommendation_score(incoming) > _recommendation_score(existing)
        else existing
    )
    merged = dict(winner)
    merged["triggered_by"] = sorted(
        set(existing.get("triggered_by", [])) | set(incoming.get("triggered_by", []))
    )
    return merged


def _format_recommendation(raw: dict, condition_key: str) -> dict:
    return {
        "intervention": raw["intervention"],
        "common_aliases": raw.get("common_aliases", []),
        "category": raw.get("category", ""),
        "severity_if_missing": raw.get("severity_if_missing", "moderate"),
        "rationale": raw.get("rationale", ""),
        "sources": raw.get("sources", []),
        "triggered_by": [condition_key],
    }


def _needs_escalation(profile: dict) -> bool:
    if profile.get("urgency") == "urgent":
        return True

    observed_behaviors = profile.get("observed_behaviors", [])
    return "concerning_statements" in observed_behaviors or "self_harm_risk" in observed_behaviors


def get_recommendations(profile: dict) -> dict:
    observed_behaviors = profile.get("observed_behaviors", [])
    resolved_conditions = _resolve_conditions(observed_behaviors)

    age = profile.get("age", 12)
    functional_age_offset = profile.get("functional_age_offset", 0)
    age_bracket, functional_bracket_used = age_to_bracket(age, functional_age_offset)

    merged: dict[str, dict] = {}

    for condition_key in resolved_conditions:
        for raw in _get_condition_recommendations(condition_key, age_bracket):
            intervention_name = raw.get("intervention")
            if not intervention_name:
                continue

            formatted = _format_recommendation(raw, condition_key)
            dedup_key = normalize_intervention_name(intervention_name)
            if dedup_key in merged:
                merged[dedup_key] = _merge_recommendation(merged[dedup_key], formatted)
            else:
                merged[dedup_key] = formatted

    recommendations = sorted(
        merged.values(),
        key=lambda item: (
            -_severity_rank(item["severity_if_missing"]),
            item["intervention"],
        ),
    )

    if _needs_escalation(profile):
        recommendations = [dict(ESCALATION_RECOMMENDATION)] + recommendations

    return {
        "resolved_conditions": resolved_conditions,
        "age_bracket_used": age_bracket,
        "functional_bracket_used": functional_bracket_used,
        "recommendations": recommendations,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    from normalizer import normalize

    mock_path = Path(__file__).resolve().parent.parent / "mockProfile.json"
    with mock_path.open(encoding="utf-8") as mock_file:
        counselor_form = json.load(mock_file)

    profile = normalize(counselor_form)
    result = get_recommendations(profile)
    print(json.dumps(result, indent=2))
