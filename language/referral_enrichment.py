"""
MindScope pipeline step 7: AI-written, parent-facing justifications.

This is the second AI-touching file. It receives the scored/tiered facilities
from referral_scorer.py and asks the model to personalize a short explanation
for each *already-recommended* facility. The model:

  - does NOT pick or score facilities (deterministic scoring already did that)
  - does NOT invent facts about a facility (no specialties, credentials,
    insurance, contact details beyond what the input already contains)
  - never sees excluded results

Every facility is guaranteed to end up with both `why_this_fits` and
`what_to_expect` populated — either AI-written or a deterministic fallback.
Downstream, db_writer.py reads this output into the `referrals` table.
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# Local Ollama via its OpenAI-compatible API. No real key is needed; the
# openai client just requires a non-empty placeholder.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "ollama")
ENRICHMENT_MODEL = os.getenv("ENRICHMENT_MODEL", "llama3.2")

# One model call per facility. A local Ollama server has no API rate limit, so
# this defaults to no pause; override via ENRICHMENT_SLEEP_SECONDS if needed.
RATE_LIMIT_SLEEP_SECONDS = float(os.getenv("ENRICHMENT_SLEEP_SECONDS", "0"))

FALLBACK_WHAT_TO_EXPECT = (
    "Contact the facility directly to ask about availability, intake process, and insurance."
)

SYSTEM_INSTRUCTION = (
    "You write concise, factual, parent-facing referral notes for a school "
    "counselor's packet. You only use facts you are given, never invent details, "
    "make no clinical claims or guarantees, and always reply with a single valid "
    "JSON object."
)

# Guardrail vocabularies -------------------------------------------------------

# Superlative / outcome-promise language that must never appear in output.
BANNED_CLAIM_PATTERNS = (
    r"\bbest\b",
    r"\b#?\s*1\s+(?:choice|provider|clinic|option)\b",
    r"\bguarantee[ds]?\b",
    r"\bcure[ds]?\b",
    r"\bproven\b",
    r"\bclinically proven\b",
    r"\bworld[\s-]?class\b",
    r"\btop[\s-]?rated\b",
    r"\bmiracle\b",
    r"\b100%\b",
    r"\bwill (?:heal|fix|solve|resolve)\b",
)

# Insurance carriers the model might invent acceptance of.
INSURANCE_PLAN_NAMES = (
    "aetna",
    "cigna",
    "blue cross",
    "blue shield",
    "bcbs",
    "unitedhealthcare",
    "united healthcare",
    "medicaid",
    "medicare",
    "humana",
    "kaiser",
    "tricare",
    "anthem",
    "optum",
    "ambetter",
    "wellcare",
)

# Credential / license tokens that should not be introduced unless they already
# appear in the facility's own data.
CREDENTIAL_TOKENS = (
    "lcsw",
    "lpc",
    "lmft",
    "lmsw",
    "psyd",
    "ph.d",
    "phd",
    "npi",
)

_PHONE_RE = re.compile(r"\(?\b\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_LICENSE_RE = re.compile(r"\b(?:license|lic\.?|npi)\s*#?\s*\d{3,}\b", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"\b(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]*\.(?:com|org|net|gov|edu|io|co|us|health))\b",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.\s]{2,40}?\b"
    r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|way|"
    r"suite|ste|court|ct|place|pl|parkway|pkwy)\b",
    re.IGNORECASE,
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

    Kept as a module-level seam so tests can monkeypatch it to simulate API
    success/failure without a live key.
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
        max_tokens=400,
    )
    content = response.choices[0].message.content
    if not content:
        raise EnrichmentAPIError("Model returned empty content")
    return content


# Step 1 — prompt construction -------------------------------------------------


def _known_facility_facts(facility: dict) -> dict:
    facts = {
        "name": facility.get("name"),
        "address": facility.get("address"),
        "phone": facility.get("phone"),
        "website": facility.get("website"),
        "rating": facility.get("rating"),
        "review_count": facility.get("review_count"),
    }
    return {key: value for key, value in facts.items() if value not in (None, "", [])}


def _normalize_gap_rationale(gap_rationale) -> dict:
    if isinstance(gap_rationale, str):
        return {"rationale": gap_rationale, "triggered_by": []}
    if isinstance(gap_rationale, dict):
        return {
            "rationale": gap_rationale.get("rationale", ""),
            "triggered_by": gap_rationale.get("triggered_by", []),
        }
    return {"rationale": "", "triggered_by": []}


def build_enrichment_prompt(
    therapy_type: str,
    facility: dict,
    profile: dict,
    gap_rationale,
) -> str:
    rationale = _normalize_gap_rationale(gap_rationale)
    rationale_text = rationale["rationale"] or "(no clinical rationale provided)"
    triggered_by = ", ".join(rationale["triggered_by"]) or "(not specified)"

    student_name = profile.get("student_name") or "the student"
    age = profile.get("age")
    age_text = f"a {age}-year-old" if age is not None else "a school-aged"
    conditions = ", ".join(profile.get("resolved_conditions", [])) or "(not specified)"

    facts_block = json.dumps(_known_facility_facts(facility), indent=2)

    return f"""Write a short, parent-facing note about ONE referral facility for a school \
counselor's packet. The facility was already selected by a deterministic scoring \
system — your job is only to personalize the explanation, not to evaluate or \
re-rank it.

FACILITY FACTS (the ONLY facts you may use about this facility — do not add or \
imply anything not listed here, including specialties, credentials, insurance \
acceptance, hours, or contact details):
{facts_block}

WHY THIS THERAPY TYPE WAS RECOMMENDED FOR THIS CHILD:
- Therapy type: {therapy_type}
- Clinical rationale (base "why_this_fits" on THIS, not the facility's marketing): {rationale_text}
- Triggered by the child's needs: {triggered_by}

CHILD CONTEXT (refer to the child by first name only):
- First name: {student_name}
- Age: {age_text} child
- Resolved needs/conditions: {conditions}

Return EXACTLY this JSON object and nothing else:
{{
  "why_this_fits": "<1-2 sentences connecting {therapy_type} (per the clinical rationale) to {student_name}'s specific needs>",
  "what_to_expect": "<1 sentence of GENERIC first-contact guidance, e.g. calling to ask about intake, verifying insurance, or requesting an availability check>"
}}

STRICT RULES:
- Use only the facility facts above. Never invent specialties, credentials, \
license numbers, insurance plans, phone numbers, addresses, or websites.
- "why_this_fits" must tie the therapy type's clinical rationale to this child's \
needs — not to the facility's reputation, rating, or marketing.
- "what_to_expect" must stay generic: you do NOT know this facility's actual \
intake process, so describe what contacting any such facility typically involves.
- No diagnoses, no clinical claims, no outcome guarantees, and no superlatives \
such as "best", "top-rated", "proven", or "guaranteed".
"""


# Step 3 — hallucination guardrail ---------------------------------------------


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def validate_enrichment(enriched_text: str, facility: dict) -> bool:
    """Return True if the AI text is safe; False (with a logged reason) otherwise."""
    text = enriched_text or ""
    text_lower = text.lower()

    facility_blob = " ".join(
        str(facility.get(field) or "")
        for field in ("name", "address", "phone", "website", "domain", "snippet")
    ).lower()

    for pattern in BANNED_CLAIM_PATTERNS:
        if re.search(pattern, text_lower):
            logger.warning("Guardrail failed: superlative/outcome claim matched %r", pattern)
            return False

    for plan in INSURANCE_PLAN_NAMES:
        if plan in text_lower and plan not in facility_blob:
            logger.warning("Guardrail failed: invented insurance plan %r", plan)
            return False

    for token in CREDENTIAL_TOKENS:
        if token in text_lower and token not in facility_blob:
            logger.warning("Guardrail failed: invented credential token %r", token)
            return False

    if _LICENSE_RE.search(text) and not _LICENSE_RE.search(facility_blob):
        logger.warning("Guardrail failed: invented license/NPI number")
        return False

    facility_phone = _digits(facility.get("phone"))[-10:]
    for match in _PHONE_RE.findall(text):
        if _digits(match)[-10:] != facility_phone or not facility_phone:
            logger.warning("Guardrail failed: phone number not present in facility data")
            return False

    facility_domain = (facility.get("domain") or "").lower()
    facility_site = (facility.get("website") or "").lower()
    for match in _DOMAIN_RE.findall(text):
        domain = match.lower()
        if domain not in facility_domain and domain not in facility_site and domain not in facility_blob:
            logger.warning("Guardrail failed: website/domain not present in facility data")
            return False

    facility_address = (facility.get("address") or "").lower()
    address_match = _ADDRESS_RE.search(text)
    if address_match and address_match.group(0).lower() not in facility_address:
        logger.warning("Guardrail failed: street address not present in facility data")
        return False

    return True


# Step 2 — single-facility enrichment ------------------------------------------


def _fallback_fields(therapy_type: str, facility: dict) -> dict:
    name = facility.get("name") or "This facility"
    return {
        "why_this_fits": f"{name} matches the recommended {therapy_type} care path for this child.",
        "what_to_expect": FALLBACK_WHAT_TO_EXPECT,
        "ai_enriched": False,
    }


def enrich_facility(
    therapy_type: str,
    facility: dict,
    profile: dict,
    gap_rationale,
) -> dict:
    enriched = dict(facility)
    name = facility.get("name") or "(unnamed facility)"

    prompt = build_enrichment_prompt(therapy_type, facility, profile, gap_rationale)

    try:
        raw = _call_model(prompt)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — never let enrichment crash the pipeline
        logger.warning("Enrichment fallback for %r — API/parse error: %s", name, exc)
        enriched.update(_fallback_fields(therapy_type, facility))
        return enriched

    why = (data.get("why_this_fits") or "").strip() if isinstance(data, dict) else ""
    expect = (data.get("what_to_expect") or "").strip() if isinstance(data, dict) else ""

    if not why or not expect:
        logger.warning(
            "Enrichment fallback for %r — validation failure: missing/empty fields", name
        )
        enriched.update(_fallback_fields(therapy_type, facility))
        return enriched

    if not validate_enrichment(f"{why} {expect}", facility):
        logger.warning("Enrichment fallback for %r — guardrail validation failed", name)
        enriched.update(_fallback_fields(therapy_type, facility))
        return enriched

    enriched.update(
        {
            "why_this_fits": why,
            "what_to_expect": expect,
            "ai_enriched": True,
        }
    )
    return enriched


# Step 4 — run across all therapy types ----------------------------------------


def _build_rationale_lookup(gaps_with_rationale: list[dict]) -> dict[str, dict]:
    sys.path.insert(0, str(_PROJECT_ROOT / "interventions"))
    try:
        from query_builder import resolve_therapy_key
    except ImportError:
        resolve_therapy_key = None  # type: ignore[assignment]

    lookup: dict[str, dict] = {}
    for gap in gaps_with_rationale or []:
        intervention = gap.get("intervention", "")
        aliases = gap.get("common_aliases", [])
        key = None
        if resolve_therapy_key is not None:
            key = resolve_therapy_key(intervention, aliases)
        key = key or intervention
        if not key:
            continue
        lookup[key] = {
            "rationale": gap.get("rationale", ""),
            "triggered_by": gap.get("triggered_by", []),
        }
    return lookup


def enrich_all_referrals(
    scored_results: dict,
    profile: dict,
    gaps_with_rationale: list[dict],
    include_possible: bool = False,
    top_n: int = 3,
) -> dict:
    rationale_lookup = _build_rationale_lookup(gaps_with_rationale)
    tiers = ("recommended", "possible") if include_possible else ("recommended",)

    enriched_therapies: list[dict] = []
    for entry in scored_results.get("results_by_therapy", []):
        therapy_type = entry.get("therapy_type", "")
        gap_rationale = rationale_lookup.get(
            therapy_type, {"rationale": "", "triggered_by": []}
        )

        new_entry = dict(entry)
        ai_count = 0
        fallback_count = 0
        original_recommended = len(entry.get("recommended", []))

        for tier in tiers:
            facilities = list(entry.get(tier, []))

            # The top-N cap applies only to recommended referrals (what gets
            # written downstream). Re-sort defensively rather than trusting the
            # upstream ordering, then slice. Possible-tier facilities, if
            # included, are enriched in full and never padded/capped.
            if tier == "recommended":
                facilities.sort(key=lambda f: f.get("total_score", 0), reverse=True)
                facilities = facilities[:top_n]

            enriched_facilities = []
            for facility in facilities:
                enriched = enrich_facility(therapy_type, facility, profile, gap_rationale)
                enriched_facilities.append(enriched)
                if enriched.get("ai_enriched"):
                    ai_count += 1
                else:
                    fallback_count += 1
                if RATE_LIMIT_SLEEP_SECONDS > 0:
                    time.sleep(RATE_LIMIT_SLEEP_SECONDS)
            new_entry[tier] = enriched_facilities

        capped_recommended = len(new_entry.get("recommended", []))
        logger.info(
            "Enriched %s: %d via AI, %d via fallback (capped to %d from %d recommended)",
            therapy_type,
            ai_count,
            fallback_count,
            capped_recommended,
            original_recommended,
        )
        enriched_therapies.append(new_entry)

    result = dict(scored_results)
    result["results_by_therapy"] = enriched_therapies
    return result


# --- test block ---------------------------------------------------------------


def _print_enriched(scored_results: dict) -> None:
    for entry in scored_results.get("results_by_therapy", []):
        print(f"\n{entry.get('therapy_type')} (gap_score={entry.get('gap_score')})")
        for facility in entry.get("recommended", []):
            print(f"  [{facility.get('total_score')}] {facility.get('name')}")
            print(f"    ai_enriched : {facility.get('ai_enriched')}")
            print(f"    why_this_fits : {facility.get('why_this_fits')}")
            print(f"    what_to_expect: {facility.get('what_to_expect')}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    sys.path.insert(0, str(_PROJECT_ROOT / "interventions"))
    sys.path.insert(0, str(_PROJECT_ROOT / "questionnaire"))
    # referral_enrichment.py lives in language/, so source/ (referral_search,
    # referral_scorer) is no longer the script directory — add it explicitly.
    sys.path.insert(0, str(_PROJECT_ROOT / "source"))

    from matching import detect_gaps
    from normalizer import normalize
    from query_builder import build_all_queries
    from referral_search import run_search_plan
    from rule_engine import get_recommendations

    from referral_scorer import score_all_results

    mock_path = _PROJECT_ROOT / "mockProfile.json"
    with mock_path.open(encoding="utf-8") as mock_file:
        counselor_form = json.load(mock_file)

    profile = normalize(counselor_form)
    profile.update({"zip": "30301", "location": "Atlanta, GA"})

    rule_engine_output = get_recommendations(profile)
    current_services = ["cbt", "school counseling", "therapy family caregiver"]
    gaps_output = detect_gaps(rule_engine_output, current_services)
    query_plan = build_all_queries(gaps_output["gaps"], profile)
    search_results = run_search_plan(query_plan)
    scored_results = score_all_results(search_results, gaps_output["gaps"])

    # Focus the enrichment test on TF-CBT and family therapy (5 recommended each
    # -> capped to 3) plus social skills training (only 1 recommended -> stays 1).
    focus = {"TF-CBT", "family therapy", "social skills training"}
    focused_results = {
        "results_by_therapy": [
            entry
            for entry in scored_results.get("results_by_therapy", [])
            if entry.get("therapy_type") in focus
        ]
    }

    enrich_profile = {
        "student_name": profile.get("student_name")
        or counselor_form.get("student_name")
        or "Marcus",
        "age": profile.get("age"),
        "resolved_conditions": gaps_output.get("resolved_conditions", []),
    }

    print("\nReferral enrichment — live run (Ollama if reachable, else fallback)")
    print("=" * 70)
    print(f"Ollama endpoint: {OLLAMA_BASE_URL} | model: {ENRICHMENT_MODEL}")
    enriched = enrich_all_referrals(focused_results, enrich_profile, gaps_output["gaps"])
    _print_enriched(enriched)

    print("\nTop-3 cap verification (enriched recommended count per therapy type)")
    print("=" * 70)
    for entry in enriched.get("results_by_therapy", []):
        therapy_type = entry.get("therapy_type", "")
        before = next(
            (
                len(e.get("recommended", []))
                for e in focused_results["results_by_therapy"]
                if e.get("therapy_type") == therapy_type
            ),
            0,
        )
        after = len(entry.get("recommended", []))
        print(f"  {therapy_type}: enriched {after} (from {before} recommended)")

    # Pick one real facility to drive the simulation tests below.
    sample_facility = None
    sample_therapy = None
    sample_rationale = {"rationale": "", "triggered_by": []}
    rationale_lookup = _build_rationale_lookup(gaps_output["gaps"])
    for entry in focused_results["results_by_therapy"]:
        if entry.get("recommended"):
            sample_facility = entry["recommended"][0]
            sample_therapy = entry["therapy_type"]
            sample_rationale = rationale_lookup.get(sample_therapy, sample_rationale)
            break

    if sample_facility is not None:
        _original_call_model = _call_model

        print("\nSimulation 1 — AI success path (stubbed valid response)")
        print("=" * 70)

        def _stub_success(prompt: str) -> str:
            return json.dumps(
                {
                    "why_this_fits": (
                        f"{sample_therapy} is the recommended evidence-based path for "
                        f"{enrich_profile['student_name']}'s needs, and this facility offers "
                        f"that type of care nearby."
                    ),
                    "what_to_expect": (
                        "Call to ask about availability, the intake process, and whether "
                        "they accept your insurance."
                    ),
                }
            )

        _call_model = _stub_success  # type: ignore[assignment]
        ok = enrich_facility(sample_therapy, sample_facility, enrich_profile, sample_rationale)
        print(f"  ai_enriched : {ok.get('ai_enriched')} (expected True)")
        print(f"  why_this_fits : {ok.get('why_this_fits')}")
        print(f"  what_to_expect: {ok.get('what_to_expect')}")

        print("\nSimulation 2 — API failure path (stub raises)")
        print("=" * 70)

        def _stub_failure(prompt: str) -> str:
            raise EnrichmentAPIError("simulated API outage")

        _call_model = _stub_failure  # type: ignore[assignment]
        failed = enrich_facility(sample_therapy, sample_facility, enrich_profile, sample_rationale)
        print(f"  ai_enriched : {failed.get('ai_enriched')} (expected False)")
        print(f"  why_this_fits : {failed.get('why_this_fits')}")
        print(f"  what_to_expect: {failed.get('what_to_expect')}")
        assert failed.get("ai_enriched") is False
        assert failed.get("why_this_fits") and failed.get("what_to_expect")

        print("\nSimulation 3 — guardrail rejection (stub returns hallucinated claim)")
        print("=" * 70)

        def _stub_hallucination(prompt: str) -> str:
            return json.dumps(
                {
                    "why_this_fits": "This is the best clinic and accepts Aetna, call 555-123-4567.",
                    "what_to_expect": "They guarantee results for your child.",
                }
            )

        _call_model = _stub_hallucination  # type: ignore[assignment]
        guarded = enrich_facility(sample_therapy, sample_facility, enrich_profile, sample_rationale)
        print(f"  ai_enriched : {guarded.get('ai_enriched')} (expected False)")
        print(f"  why_this_fits : {guarded.get('why_this_fits')}")
        print(f"  what_to_expect: {guarded.get('what_to_expect')}")
        assert guarded.get("ai_enriched") is False

        _call_model = _original_call_model  # type: ignore[assignment]
        print("\nAll simulations completed without crashing.")
    else:
        print("\nNo recommended facilities available to run simulation tests.")
