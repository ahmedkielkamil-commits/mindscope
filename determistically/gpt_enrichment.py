"""
MindScope pipeline — "Pass 2": AI enrichment of the care-plan roadmap.

Distinct from language/referral_enrichment.py (which writes facility
justifications). This file receives the deterministic care gaps from
matching.py and writes the roadmap itself: a 6-stage plan of staged parent
guidance and observable behavioral progress markers.

The AI does NOT decide what is wrong with the child and does NOT invent
clinical content. The deterministic layer (build_stage_skeleton) grounds each
stage in already-cited gap rationales/sources; the model only translates that
into plain-language, parent-facing actions and observable markers.

Downstream, db_writer.py reads this roadmap into the `parent_tasks` and
`behavior_checks` tables, keyed by the `stage_1` .. `stage_6` ENUM.
"""

import json
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# Reuse the exact Ollama (OpenAI-compatible) client pattern from
# language/referral_enrichment.py — same env vars, same lazy init.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "ollama")
ENRICHMENT_MODEL = os.getenv("ENRICHMENT_MODEL", "llama3.2")

TOTAL_STAGES = 6

SYSTEM_INSTRUCTION = (
    "You write one stage of a staged, parent-facing care roadmap for a child. "
    "You only use the clinical rationale and sources you are given, never invent "
    "therapies or diagnoses, keep behavioral markers observable and measurable, "
    "write each behavioral marker as one complete sentence (child + action + count/frequency "
    "together — never split frequency or example lists into separate items), "
    "write in short plain sentences for a stressed parent, and always reply with a "
    "single valid JSON object."
)

# Deterministic stage labels by stage number (derived from the severity band,
# not AI-written). Empty stages fall back to the consolidation theme.
STAGE_THEMES = {
    1: "Getting Started: Critical Needs",
    2: "Building Momentum: Critical Needs",
    3: "Expanding Support: High Priorities",
    4: "Strengthening Skills: High Priorities",
    5: "Consolidating Gains: Ongoing Needs",
    6: "Maintaining Progress and Check-Ins",
}
CONSOLIDATION_THEME = "Consolidation and Check-In"

# Severity band -> the two stages it occupies.
_BAND_STAGES = {
    "critical": (1, 2),
    "high": (3, 4),
    "moderate": (5, 6),
}

# Named therapies/interventions the model must not introduce on its own. Matched
# with word boundaries so short acronyms ("aba") don't hit substrings ("abandon").
KNOWN_THERAPY_TERMS = (
    "tf-cbt",
    "cbt",
    "dbt",
    "emdr",
    "aba",
    "play therapy",
    "art therapy",
    "music therapy",
    "exposure therapy",
    "group therapy",
    "family therapy",
    "occupational therapy",
    "speech therapy",
    "neurofeedback",
    "ssri",
    "antidepressant",
    "medication",
    "social skills training",
    "parent management training",
)

# Diagnostic / outcome-claim language banned from behavioral markers.
BANNED_MARKER_PATTERNS = (
    r"diagnos",
    r"\bcured?\b",
    r"disorder\s+resolved",
    r"anxiety\s+eliminated",
    r"depression\s+(?:gone|cured|eliminated|resolved)",
    r"\bremission\b",
    r"symptom[-\s]?free",
    r"no longer (?:has|suffers|experiences)",
    r"\bclinically\b",
    r"\bhealed\b",
)

FREQUENCY_WORDS = (
    "daily",
    "weekly",
    "monthly",
    "per week",
    "per day",
    "each day",
    "each week",
    "every day",
    "every week",
    "times a",
    "times per",
    "a week",
    "a day",
    "once",
    "twice",
)

# Spelled-out counts that make a marker countable.
WRITTEN_NUMBERS = (
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)

# Action verbs that can start a marker with an implied child subject ("Initiates 2+ …").
IMPLIED_SUBJECT_VERBS = (
    "initiates",
    "completes",
    "attends",
    "shows",
    "uses",
    "participates",
    "responds",
    "demonstrates",
    "engages",
    "follows",
    "joins",
    "speaks",
    "asks",
    "reports",
    "finishes",
    "returns",
    "arrives",
    "practices",
    "draws",
    "creates",
    "writes",
    "reads",
    "plays",
    "stays",
    "remains",
    "goes",
    "walks",
    "helps",
    "tries",
    "keeps",
    "listens",
    "chooses",
    "names",
    "identifies",
    "waits",
    "sets",
    "eats",
    "sleeps",
    "wakes",
    "shares",
    "talks",
    "displays",
    "exhibits",
    "takes",
    "expresses",
    "mentions",
    "seeks",
    "maintains",
    "gets",
    "has",
    "begins",
    "starts",
    "continues",
    "handles",
    "manages",
    "calms",
    "focuses",
    "learns",
    "attempts",
    "meets",
)

CHILD_SUBJECT_TERMS = (
    "marcus",
    "the child",
    " child ",
    " he ",
    " she ",
    " his ",
    " her ",
)

# Fragment patterns: standalone frequency or parenthetical shards, not full sentences.
_PAREN_FRAGMENT_START = re.compile(
    r"^\s*\(\s*(?:e\.?g\.?|such as|including|like)\b",
    re.IGNORECASE,
)
_BARE_FREQUENCY_START = re.compile(
    r"^\s*(?:"
    r"(?:\d+|2\+|\d+\+|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"(?:\s*\+)?"
    r"(?:\s+times?)?"
    r"\s+(?:per|a|each|every|daily|weekly|monthly)"
    r"|(?:\d+|2\+|\d+\+)\s+times\b"
    r")",
    re.IGNORECASE,
)

# An enumerated example list (e.g. "(e.g. calmness, self-soothing)") makes even a
# vague verb like "shows"/"demonstrates" concrete enough to observe and count.
_EXAMPLE_LIST_RE = re.compile(r"\([^)]*(?:e\.?g\.?|such as|including|like|,)[^)]*\)", re.IGNORECASE)

OBSERVABLE_VERBS = (
    "initiates",
    "completes",
    "attends",
    "asks",
    "raises",
    "speaks",
    "participates",
    "responds",
    "sleeps",
    "eats",
    "joins",
    "returns",
    "finishes",
    "reports",
    "uses",
    "practices",
    "talks",
    "shares",
    "follows",
    "arrives",
    "wakes",
    "engages",
    "makes",
    "draws",
    "creates",
    "writes",
    "reads",
    "plays",
    "stays",
    "remains",
    "goes",
    "leaves",
    "walks",
    "helps",
    "tries",
    "keeps",
    "spends",
    "listens",
    "looks",
    "chooses",
    "names",
    "identifies",
    "waits",
    "sets",
)


class EnrichmentAPIError(Exception):
    """Raised when the model cannot be reached or returns no usable content."""


_openai_client = None


def _get_client():
    """Lazily build the Ollama (OpenAI-compatible) client so import never fails."""
    global _openai_client
    if _openai_client is not None:
        return _openai_client

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise EnrichmentAPIError(f"openai package not installed: {exc}") from exc

    _openai_client = OpenAI(base_url=OLLAMA_BASE_URL, api_key=OLLAMA_API_KEY)
    return _openai_client


def _call_model(prompt: str) -> str:
    """Send the prompt to the model and return the raw response content.

    Module-level seam so tests can monkeypatch it to simulate API failure.
    """
    client = _get_client()
    response = client.chat.completions.create(
        model=ENRICHMENT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
        max_tokens=600,
    )
    content = response.choices[0].message.content
    if not content:
        raise EnrichmentAPIError("Model returned empty content")
    return content


# Step 1 — deterministic 6-stage skeleton --------------------------------------


def build_stage_skeleton(gaps: list[dict]) -> list[dict]:
    bands: dict[str, list[dict]] = {"critical": [], "high": [], "moderate": []}
    for gap in gaps or []:
        severity = gap.get("severity_if_missing", "moderate")
        if severity == "critical":
            bands["critical"].append(gap)
        elif severity == "high":
            bands["high"].append(gap)
        else:
            # moderate (and any unexpected value) lands in the ongoing band
            bands["moderate"].append(gap)

    stage_gaps: dict[int, list[dict]] = {n: [] for n in range(1, TOTAL_STAGES + 1)}
    for band, (stage_a, stage_b) in _BAND_STAGES.items():
        items = bands[band]
        # Split across the two stages in the band rather than overloading the
        # first stage; the first stage gets the extra when the count is odd.
        split = (len(items) + 1) // 2
        stage_gaps[stage_a] = items[:split]
        stage_gaps[stage_b] = items[split:]

    skeleton: list[dict] = []
    for stage_number in range(1, TOTAL_STAGES + 1):
        assigned = stage_gaps[stage_number]
        # Empty bands become consolidation/check-in stages, never skipped.
        theme = STAGE_THEMES[stage_number] if assigned else CONSOLIDATION_THEME
        skeleton.append(
            {
                "stage_number": stage_number,
                "gaps": assigned,
                "stage_theme": theme,
            }
        )
    return skeleton


# Step 2 — per-stage prompt ----------------------------------------------------


def build_stage_prompt(stage: dict, profile: dict, previous_stage_summary: str | None) -> str:
    stage_number = stage.get("stage_number")
    theme = stage.get("stage_theme")
    gaps = stage.get("gaps", [])

    student_name = profile.get("student_name") or "the student"
    age = profile.get("age")
    age_text = f"a {age}-year-old" if age is not None else "a school-aged"
    strengths = ", ".join(profile.get("strengths", []) or []) or "(not specified)"
    parent_state = (
        profile.get("parent_state")
        or profile.get("parent_current_state")
        or "(not specified)"
    )

    if gaps:
        gap_view = [
            {
                "intervention": gap.get("intervention"),
                "severity": gap.get("severity_if_missing"),
                "rationale": gap.get("rationale"),
                "sources": gap.get("sources", []),
            }
            for gap in gaps
        ]
        gaps_block = json.dumps(gap_view, indent=2)
        focus_instruction = (
            "Base the parent actions ONLY on the rationale and sources in these gap(s). "
            "Do not introduce any therapy, intervention, medication, or diagnosis not listed here."
        )
    else:
        gaps_block = "[]   (no new clinical gaps assigned to this stage)"
        focus_instruction = (
            "This is a consolidation / check-in stage with NO new clinical gaps. Write "
            "actions that help the family maintain and reinforce the progress made in "
            "earlier stages. Do not introduce any new therapy, intervention, or diagnosis."
        )

    if previous_stage_summary:
        previous_block = (
            f'Previous stage summary (continue logically from this; do NOT repeat it): '
            f'"{previous_stage_summary}"'
        )
    else:
        previous_block = "This is the first stage; there is no previous stage."

    return f"""Write ONE stage of a 6-stage, parent-facing care roadmap for a child. The \
stages and the clinical needs were already decided by a deterministic system. Your \
job is only to translate THIS stage's needs into plain-language parent guidance — \
not to evaluate, diagnose, or choose new treatments.

STAGE: {stage_number} of {TOTAL_STAGES}
STAGE THEME: {theme}

CLINICAL GAP(S) ASSIGNED TO THIS STAGE (the ONLY clinical content you may use):
{gaps_block}

{focus_instruction}

CHILD CONTEXT (refer to the child by first name only):
- First name: {student_name}
- Age: {age_text} child
- Strengths to build on: {strengths}
- Current parent/family state: {parent_state}

{previous_block}

Return EXACTLY this JSON object and nothing else:
{{
  "parent_support_actions": ["<2 to 4 concrete things the parent DOES during this stage>"],
  "behavioral_markers": ["<one observation>", "<one observation>", "<one observation>"],
  "stage_summary": "<1 short sentence summarizing this stage, for continuity into the next>"
}}

STRICT RULES:
- Use only the rationale/sources above. Never name a therapy, program, medication, or \
diagnosis that is not in the gap list above.
- behavioral_markers: give 2 to 4 markers, each as a SEPARATE array string (one observation \
per string — NEVER combine several observations into one string with commas or semicolons).
- Each behavioral_marker must be ONE complete sentence naming the child, the action, and the \
frequency/count together — never split a frequency phrase or an example list into its own list \
item (BAD: separate items "2+ times per week" and "(e.g. calmness, self-soothing)").
- EVERY behavioral_marker MUST contain either a specific count/frequency (e.g. "2+", \
"3 times per week", "5 days each week") OR a concrete parenthetical example list \
(e.g. "(e.g. calmness, self-soothing)"). 
- GOOD: "{student_name} initiates 2+ peer interactions per week". \
GOOD: "{student_name} shows 2+ signs of self-soothing (e.g. deep breathing, taking a break) per week". \
BAD (no count, no examples): "{student_name} shows increased confidence" or "feels happier".
- Write short, plain sentences for a parent under stress. No jargon, no clinical claims, \
no promises of cures or outcomes.
"""


# Step 4 — guardrail validation ------------------------------------------------


def _has_term(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


def _marker_is_structurally_complete(marker: str) -> bool:
    """Reject fragmentary list items (bare frequency shards, parenthetical shards,
    or clauses with no child subject and no action verb). Runs before observability."""
    text = marker.strip()
    if not text:
        return False

    words = text.split()
    if len(words) < 4:
        return False

    if _PAREN_FRAGMENT_START.match(text):
        return False

    if _BARE_FREQUENCY_START.match(text):
        return False

    lower = f" {text.lower()} "
    first_word = words[0].lower().rstrip(",.;:")

    if first_word in IMPLIED_SUBJECT_VERBS:
        return True

    has_subject = any(term in lower for term in CHILD_SUBJECT_TERMS)
    verb_terms = set(OBSERVABLE_VERBS) | set(IMPLIED_SUBJECT_VERBS)
    has_verb = any(_has_term(lower, verb) for verb in verb_terms)

    return has_subject and has_verb


def _segment_is_observable(segment: str) -> bool:
    """A clause is observable if it carries any concrete, countable signal:
    a digit, a spelled-out number, a frequency/duration word, an enumerated
    example list in parentheses, or a concrete action verb. A digit or example
    list is what lets vague verbs like "shows 2+ signs" / "shows signs (e.g. ...)"
    pass, while bare "shows improvement" (none of these) still fails."""
    text = segment.lower()
    if re.search(r"\d", segment):
        return True
    if any(_has_term(text, number) for number in WRITTEN_NUMBERS):
        return True
    if any(_has_term(text, word) for word in FREQUENCY_WORDS):
        return True
    if _EXAMPLE_LIST_RE.search(segment):
        return True
    if any(_has_term(text, verb) for verb in OBSERVABLE_VERBS):
        return True
    return False


def _atomic_segments(marker: str) -> list[str]:
    """Split a marker into atomic clauses. Models sometimes cram several markers
    into one comma-separated string, so each real clause must stand on its own —
    but commas INSIDE parentheses (example lists like "(e.g. a, b)") must not be
    split, or a valid enumerated example would be torn apart."""
    segments: list[str] = []
    current: list[str] = []
    depth = 0
    for char in marker:
        if char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char in ";,\n" and depth == 0:
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
    segments.append("".join(current))

    cleaned = [segment.strip() for segment in segments if len(segment.split()) >= 3]
    return cleaned or [marker.strip()]


def _marker_is_observable(marker: str) -> bool:
    """A marker is observable only if EVERY atomic clause within it is observable
    (so a concatenated blob with one vague clause still fails)."""
    return all(_segment_is_observable(segment) for segment in _atomic_segments(marker))


def validate_stage_content(
    parent_actions: list[str],
    behavioral_markers: list[str],
    gaps: list[dict],
) -> bool:
    actions_text = " ".join(parent_actions).lower()
    gap_text = " ".join(
        f"{gap.get('intervention', '')} {' '.join(gap.get('common_aliases', []) or [])}"
        for gap in gaps
    ).lower()

    # 1) No therapy name in parent actions unless it's in this stage's gaps.
    for term in KNOWN_THERAPY_TERMS:
        if _has_term(actions_text, term) and not _has_term(gap_text, term):
            logger.warning("Guardrail failed: invented therapy/intervention %r in parent actions", term)
            return False

    # Every marker is checked INDIVIDUALLY: if even one marker in the list fails
    # any check, the whole stage fails validation and falls back.
    for marker in behavioral_markers:
        marker_lower = marker.lower()

        # 2) Structural completeness — reject fragmentary shards before content checks.
        if not _marker_is_structurally_complete(marker):
            logger.warning("Guardrail failed: malformed behavioral marker fragment: %r", marker)
            return False

        # 3) No clinical/diagnostic language in behavioral markers.
        for pattern in BANNED_MARKER_PATTERNS:
            if re.search(pattern, marker_lower):
                logger.warning("Guardrail failed: clinical/diagnostic language in marker: %r", marker)
                return False

        # 4) This marker must individually read as observable/countable.
        if not _marker_is_observable(marker):
            logger.warning("Guardrail failed: behavioral marker not observable/measurable: %r", marker)
            return False

    return True


# Step 3 — generate one stage --------------------------------------------------


def _fallback_stage_fields(stage: dict) -> dict:
    gaps = stage.get("gaps", [])
    if gaps:
        actions = [
            f"Follow through on starting {gap.get('intervention')} as recommended."
            for gap in gaps
        ]
    else:
        actions = [
            "Keep up the routines and supports started in earlier stages.",
            "Check in with your counselor about your child's overall progress.",
        ]
    return {
        "parent_support_actions": actions,
        "behavioral_markers": ["Check in with your counselor about progress at this stage."],
        "stage_summary": stage.get("stage_theme", ""),
        "ai_enriched": False,
    }


def generate_stage(stage: dict, profile: dict, previous_stage_summary: str | None) -> dict:
    stage_number = stage.get("stage_number")
    gaps = stage.get("gaps", [])
    base = {
        "stage_number": stage_number,
        "stage_theme": stage.get("stage_theme"),
        "gaps_addressed": [gap.get("intervention") for gap in gaps if gap.get("intervention")],
    }

    prompt = build_stage_prompt(stage, profile, previous_stage_summary)

    try:
        raw = _call_model(prompt)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — never let a stage crash the chain
        logger.warning("Stage %s fallback — API/parse error: %s", stage_number, exc)
        return {**base, **_fallback_stage_fields(stage)}

    if not isinstance(data, dict):
        logger.warning("Stage %s fallback — validation failure: response was not an object", stage_number)
        return {**base, **_fallback_stage_fields(stage)}

    raw_actions = data.get("parent_support_actions")
    raw_markers = data.get("behavioral_markers")
    raw_summary = data.get("stage_summary")

    actions = (
        [str(a).strip() for a in raw_actions if str(a).strip()]
        if isinstance(raw_actions, list)
        else []
    )
    markers = (
        [str(m).strip() for m in raw_markers if str(m).strip()]
        if isinstance(raw_markers, list)
        else []
    )
    summary = raw_summary.strip() if isinstance(raw_summary, str) else ""

    if not actions or not markers or not summary:
        logger.warning(
            "Stage %s fallback — validation failure: missing/empty fields", stage_number
        )
        return {**base, **_fallback_stage_fields(stage)}

    if not validate_stage_content(actions, markers, gaps):
        logger.warning("Stage %s fallback — guardrail validation failed", stage_number)
        return {**base, **_fallback_stage_fields(stage)}

    return {
        **base,
        "parent_support_actions": actions,
        "behavioral_markers": markers,
        "stage_summary": summary,
        "ai_enriched": True,
    }


# Step 5 — full roadmap --------------------------------------------------------


def build_roadmap(gaps: list[dict], profile: dict) -> dict:
    skeleton = build_stage_skeleton(gaps)

    stages_out: list[dict] = []
    previous_summary: str | None = None
    ai_count = 0
    fallback_count = 0

    # Sequential by design: each stage's prompt threads the prior stage summary.
    for stage in skeleton:
        enriched = generate_stage(stage, profile, previous_summary)
        stages_out.append(enriched)
        previous_summary = enriched.get("stage_summary")
        if enriched.get("ai_enriched"):
            ai_count += 1
        else:
            fallback_count += 1

    logger.info(
        "Roadmap complete: %d of %d stages via AI, %d via fallback",
        ai_count,
        len(stages_out),
        fallback_count,
    )

    return {
        "student_name": profile.get("student_name") or "the student",
        "total_stages": len(stages_out),
        "stages": stages_out,
    }


# --- test block ---------------------------------------------------------------


def _print_stage_marker_audit(roadmap: dict, stage_number: int, title: str) -> None:
    print(f"\n{title}")
    print("=" * 70)
    stage = next((s for s in roadmap.get("stages", []) if s.get("stage_number") == stage_number), None)
    if not stage:
        print(f"  Stage {stage_number} not found in roadmap.")
        return

    print(f"  stage_theme : {stage.get('stage_theme')}")
    print(f"  ai_enriched : {stage.get('ai_enriched')}")
    print("  behavioral_markers:")
    for marker in stage.get("behavioral_markers", []):
        print(
            f"    - {marker}  "
            f"(complete={_marker_is_structurally_complete(marker)}, "
            f"observable={_marker_is_observable(marker)})"
        )
    if stage.get("ai_enriched"):
        markers = stage.get("behavioral_markers", [])
        assert all(_marker_is_structurally_complete(m) for m in markers), (
            f"AI-enriched Stage {stage_number} cannot contain malformed fragments"
        )
        assert all(_marker_is_observable(m) for m in markers), (
            f"AI-enriched Stage {stage_number} cannot contain non-observable markers"
        )
        print("  -> AI-enriched: all markers are complete sentences with observable structure.")
    else:
        print("  -> fell back to the deterministic template (acceptable).")


def _print_roadmap(roadmap: dict) -> None:
    print(f"\nRoadmap for {roadmap.get('student_name')} ({roadmap.get('total_stages')} stages)")
    print("=" * 70)
    for stage in roadmap.get("stages", []):
        print(f"\nStage {stage.get('stage_number')}: {stage.get('stage_theme')}")
        print(f"  ai_enriched   : {stage.get('ai_enriched')}")
        print(f"  gaps_addressed: {stage.get('gaps_addressed')}")
        print("  parent_support_actions:")
        for action in stage.get("parent_support_actions", []):
            print(f"    - {action}")
        print("  behavioral_markers:")
        for marker in stage.get("behavioral_markers", []):
            print(f"    - {marker}")
        print(f"  stage_summary : {stage.get('stage_summary')}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # --- Guardrail unit checks (no model needed): each marker must pass on its own.
    print("\nGuardrail unit checks — validate_stage_content observable markers")
    print("=" * 70)
    _unit_gaps = [
        {"intervention": "Trauma-Focused Cognitive Behavioral Therapy (TF-CBT)", "common_aliases": ["TF-CBT"]}
    ]
    _unit_actions = ["Keep a steady routine at home and attend appointments."]
    _marker_cases = [
        ("Marcus shows increased ability to regulate his emotions and behaviors", False),
        ("Marcus demonstrates improved problem-solving skills and increased confidence", False),
        ("Marcus feels more confident", False),
        ("Marcus initiates 2+ peer interactions per week", True),
        ("Marcus attends school 5 days each week", True),
        ("Marcus completes his homework without reminders", True),
        # Vague verb made concrete by a count or a parenthetical example list -> PASS.
        ("shows interest in and participates in 1+ new activity or hobby for more than 1 week", True),
        ("shows 2+ signs of emotional regulation (e.g. calmness, self-soothing) during stressful situations", True),
        # Vague verb with no count and no example list -> still FAIL.
        ("shows improvement in his ability to express emotions and needs to others", False),
        ("shows interest in discussing his social interactions with you", False),
        # Genuinely vague markers must stay rejected.
        ("increased confidence", False),
        ("feels happier", False),
    ]
    for _marker, _expected in _marker_cases:
        _result = validate_stage_content(_unit_actions, [_marker], _unit_gaps)
        _status = "OK" if _result == _expected else "MISMATCH"
        print(f"  [{_status}] expected={_expected} got={_result} :: {_marker!r}")
        assert _result == _expected, f"marker check mismatch for {_marker!r}"

    # A list mixing one good + one bad marker must fail as a whole (per-marker rule).
    _mixed = validate_stage_content(
        _unit_actions,
        ["Marcus initiates 2+ peer interactions per week", "Marcus feels happier overall"],
        _unit_gaps,
    )
    print(f"  [{'OK' if _mixed is False else 'MISMATCH'}] mixed good+bad list rejected: got={_mixed} (expected False)")
    assert _mixed is False, "a list with any non-observable marker must fail"

    print("\nGuardrail unit checks — structural completeness (malformed fragments)")
    print("=" * 70)
    _structural_cases = [
        ("2+ times per week", False),
        ("(e.g. calmness, self-soothing)", False),
        ("Marcus shows 2+ signs of calmness (e.g. deep breathing) per week", True),
    ]
    for _marker, _expected in _structural_cases:
        _result = validate_stage_content(_unit_actions, [_marker], _unit_gaps)
        _status = "OK" if _result == _expected else "MISMATCH"
        print(f"  [{_status}] expected={_expected} got={_result} :: {_marker!r}")
        assert _result == _expected, f"structural check mismatch for {_marker!r}"

    _fragment_mix = validate_stage_content(
        _unit_actions,
        ["Marcus initiates 2+ peer interactions per week", "2+ times per week"],
        _unit_gaps,
    )
    print(
        f"  [{'OK' if _fragment_mix is False else 'MISMATCH'}] "
        f"good marker + frequency fragment rejected: got={_fragment_mix} (expected False)"
    )
    assert _fragment_mix is False, "a list with any malformed fragment must fail"

    sys.path.insert(0, str(_PROJECT_ROOT / "interventions"))
    sys.path.insert(0, str(_PROJECT_ROOT / "questionnaire"))

    from matching import detect_gaps
    from normalizer import normalize
    from rule_engine import get_recommendations

    mock_path = _PROJECT_ROOT / "mockProfile.json"
    with mock_path.open(encoding="utf-8") as mock_file:
        counselor_form = json.load(mock_file)

    profile = normalize(counselor_form)
    profile.update({"zip": "30301", "location": "Atlanta, GA"})

    rule_engine_output = get_recommendations(profile)
    current_services = ["cbt", "school counseling", "therapy family caregiver"]
    gaps_output = detect_gaps(rule_engine_output, current_services)
    gaps = gaps_output["gaps"]

    roadmap_profile = {
        "student_name": profile.get("student_name")
        or counselor_form.get("student_name")
        or "Marcus",
        "age": profile.get("age"),
        "strengths": profile.get("strengths", []),
        "parent_state": profile.get("parent_state"),
        "resolved_conditions": gaps_output.get("resolved_conditions", []),
    }

    print("\nCare-plan roadmap — live run (Ollama if reachable, else fallback)")
    print("=" * 70)
    print(f"Ollama endpoint: {OLLAMA_BASE_URL} | model: {ENRICHMENT_MODEL}")
    roadmap = build_roadmap(gaps, roadmap_profile)
    _print_roadmap(roadmap)

    _print_stage_marker_audit(
        roadmap,
        1,
        "Final Stage 1 output (complete-sentence markers or deterministic fallback)",
    )
    _print_stage_marker_audit(
        roadmap,
        2,
        "Final Stage 2 output (complete-sentence markers or deterministic fallback)",
    )
    _print_stage_marker_audit(
        roadmap,
        6,
        "Final Stage 6 output (complete-sentence markers or deterministic fallback)",
    )

    # Simulation — a single stage's API call fails mid-chain. Confirm the chain
    # continues and later stages still enrich (do not parallelize / do not abort).
    print("\nSimulation — API failure on stage 3 only (chain must continue)")
    print("=" * 70)

    _original_call_model = _call_model
    _sim_state = {"calls": 0}

    def _stub_fail_stage_3(prompt: str) -> str:
        _sim_state["calls"] += 1
        if _sim_state["calls"] == 3:
            raise EnrichmentAPIError("simulated API outage on stage 3")
        return json.dumps(
            {
                "parent_support_actions": [
                    "Keep a steady daily routine at home.",
                    "Attend each scheduled appointment with your child.",
                ],
                "behavioral_markers": [
                    "Child attends sessions 1 time per week.",
                    "Child completes 2 home activities each week.",
                ],
                "stage_summary": "The family keeps routines steady and stays engaged with care.",
            }
        )

    _call_model = _stub_fail_stage_3  # type: ignore[assignment]
    sim_roadmap = build_roadmap(gaps, roadmap_profile)
    for stage in sim_roadmap.get("stages", []):
        print(
            f"  Stage {stage.get('stage_number')}: ai_enriched={stage.get('ai_enriched')} "
            f"({stage.get('stage_theme')})"
        )
    _call_model = _original_call_model  # type: ignore[assignment]

    stage_3 = next(s for s in sim_roadmap["stages"] if s["stage_number"] == 3)
    later_stages = [s for s in sim_roadmap["stages"] if s["stage_number"] > 3]
    assert stage_3["ai_enriched"] is False, "stage 3 should have fallen back"
    assert all(
        s.get("parent_support_actions") and s.get("behavioral_markers")
        for s in sim_roadmap["stages"]
    ), "every stage must be fully populated"
    print(
        f"\n  Stage 3 fell back (ai_enriched=False); "
        f"{sum(1 for s in later_stages if s['ai_enriched'])}/{len(later_stages)} "
        f"later stages still enriched via AI — chain continued without crashing."
    )
