"""
MindScope pipeline step 1: normalize counselor intake JSON into a student profile
dict consumed by rule_engine.py.
"""

OBSERVED_BEHAVIOR_MAP = {
    "Frequent emotional outbursts disproportionate to trigger": "emotional_outbursts",
    "Difficulty transitioning between activities": "transition_difficulty",
    "Persistent avoidance of school or specific settings": "persistent_avoidance",
    "Social withdrawal or isolation from peers": "social_withdrawal",
    "Difficulty sustaining attention or completing tasks": "attention_difficulty",
    "Oppositional or defiant responses to authority": "oppositional_defiant",
    "Visible anxiety or worry (excessive reassurance-seeking, physical complaints)": "visible_anxiety",
    "Mood appears persistently low or flat": "mood_persistently_low",
    "Impulsive or disruptive behavior in class": "impulsive_disruptive",
    "Signs of sensory sensitivity (covers ears, avoids textures, overwhelmed in crowds)": "sensory_sensitivity",
    "Self-isolating during unstructured time (lunch, recess)": "self_isolating_unstructured",
    "Concerning statements or behaviors suggesting emotional distress": "concerning_statements",
    "Concerning statements or behaviors suggesting emotional distress (flag for review)": "concerning_statements",
}

TRIGGER_MAP = {
    "Transitions between activities": "transitions",
    "Sensory overload": "sensory_overload",
    "Academic demands": "academic_demands",
    "Social interaction": "social_interaction",
    "Fatigue": "fatigue",
    "Unstructured time": "unstructured_time",
    "Unknown": "unknown",
}

RECOVERY_TIME_MAP = {
    "Less than 10 minutes": "less_than_10_minutes",
    "10–30 minutes": "10-30_minutes",
    "10-30 minutes": "10-30_minutes",
    "30–60 minutes": "30-60_minutes",
    "30-60 minutes": "30-60_minutes",
    "Several hours": "several_hours",
    "The rest of the day": "rest_of_day",
}

SERVICE_MAP = {
    "School counseling services": "school_counseling",
    "Special education services / IEP": "iep",
    "504 plan accommodations": "504_plan",
    "Outside therapy (type unknown)": "outside_therapy",
    "CBT": "cbt",
    "DBT": "dbt",
    "Speech therapy": "speech_therapy",
    "Occupational therapy": "occupational_therapy",
    "Psychiatric medication management": "medication_management",
    "No known services": "none",
    "Unknown": "unknown",
}

DEVELOPMENTAL_LEVEL_MAP = {
    "On track": "on_track",
    "Mild delay": "mild_delay",
    "Moderate delay": "moderate_delay",
    "Significant delay": "significant_delay",
    "Unsure": "unsure",
}

FUNCTIONAL_LEVEL_OFFSET = {
    "Matches chronological age": 0,
    "1-2 years behind": -2,
    "3+ years behind": -3,
    "Unsure": 0,
}

PRIOR_REFERRAL_MAP = {
    "Yes, and they are currently receiving them": "receiving_services",
    "Yes, but the family did not follow through": "family_no_follow_through",
    "Yes, but services were unavailable": "services_unavailable",
    "No prior referral": "no_prior_referral",
    "Unknown": "unknown",
}

PRIMARY_CONCERN_MAP = {
    "Academic failure risk": "academic_failure_risk",
    "Social-emotional wellbeing": "social_emotional_wellbeing",
    "Safety concern": "safety_concern",
    "Family situation impact": "family_situation_impact",
    "Behavioral escalation trend": "behavioral_escalation_trend",
}

URGENCY_MAP = {
    "Routine — can follow standard referral timeline": "routine",
    "Elevated — needs services within the next few weeks": "elevated",
    "Urgent — needs immediate intervention or escalation": "urgent",
}


def _map_list(values, mapping, field_name):
    if not isinstance(values, list):
        values = [values]
    mapped = []
    for value in values:
        if value not in mapping:
            raise ValueError(f"Unknown {field_name} value: {value!r}")
        mapped.append(mapping[value])
    return mapped


def normalize(form: dict) -> dict:
    observed_raw = form.get("Observed Behaviors", [])
    if isinstance(observed_raw, str):
        observed_raw = [observed_raw]

    observed_behaviors = []
    for behavior in observed_raw:
        if behavior.startswith("Other"):
            continue
        if behavior not in OBSERVED_BEHAVIOR_MAP:
            raise ValueError(f"Unknown observed behavior: {behavior!r}")
        observed_behaviors.append(OBSERVED_BEHAVIOR_MAP[behavior])

    developmental_level = form.get("Developmental level", "On track")
    functional_level = form.get("Functional level", "Matches chronological age")

    return {
        "student_name": form.get("Student's first name", ""),
        "age": int(form["Student's age"]),
        "grade": form.get("Grade level", ""),
        "school_name": form.get("School name", ""),
        "functional_age_offset": FUNCTIONAL_LEVEL_OFFSET.get(functional_level, 0),
        "observed_behaviors": observed_behaviors,
        "triggers": _map_list(
            form.get("What most commonly triggers them?", []),
            TRIGGER_MAP,
            "trigger",
        ),
        "recovery_time": RECOVERY_TIME_MAP.get(
            form.get("After a difficult moment, how long does recovery typically take?", ""),
            "unknown",
        ),
        "behavior_frequency": form.get("How often do difficult moments occur?", ""),
        "behavior_settings": form.get("Where do these challenges show up most?", ""),
        "behavior_duration": form.get("How long have these behaviors been observed?", ""),
        "current_services": _map_list(
            form.get("Is this student currently receiving any of the following?", []),
            SERVICE_MAP,
            "current service",
        ),
        "prior_referral_outcome": PRIOR_REFERRAL_MAP.get(
            form.get("Has this student been referred for services before?", ""),
            "unknown",
        ),
        "strengths": form.get("What are this student's strongest observed qualities?", []),
        "has_trusted_adult_at_school": form.get(
            "Does this student have at least one stable, trusting relationship with an adult at school?",
            "Unsure",
        ),
        "urgency": URGENCY_MAP.get(
            form.get("Urgency level in your judgment", ""),
            "routine",
        ),
        "developmental_level": DEVELOPMENTAL_LEVEL_MAP.get(
            developmental_level,
            "unsure",
        ),
        "counselor_primary_concern": PRIMARY_CONCERN_MAP.get(
            form.get("What is your primary concern for this student right now?", ""),
            "other",
        ),
    }
