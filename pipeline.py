"""
MindScope pipeline orchestration layer.

Single entry point for the questionnaire-submission handler. Sequences the nine
pipeline stages in order with stage-level logging, timing, and error handling.
Contains no clinical/business logic — only wiring between existing modules.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent

# Match the sys.path layout used across the individual stage test blocks.
for _path in (
    "interventions",
    "questionnaire",
    "source",
    "language",
    "determistically",
    "facilities",
):
    sys.path.insert(0, str(_PROJECT_ROOT / _path))

from db_writer import write_full_care_plan  # noqa: E402
from gpt_enrichment import build_roadmap  # noqa: E402
from matching import detect_gaps  # noqa: E402
from normalizer import normalize  # noqa: E402
from query_builder import build_all_queries  # noqa: E402
from referral_enrichment import enrich_all_referrals  # noqa: E402
from referral_scorer import score_all_results  # noqa: E402
from referral_search import run_search_plan  # noqa: E402
from rule_engine import get_recommendations  # noqa: E402


class PipelineStageError(Exception):
    """Raised when a deterministic pipeline stage (1-6) or DB write (9) fails."""

    def __init__(self, stage_name: str, original: Exception):
        self.stage_name = stage_name
        self.original = original
        super().__init__(f"Pipeline stage '{stage_name}' failed: {original}")


def _merge_parent_form(profile: dict, parent_form: dict | None) -> dict:
    if not parent_form:
        return profile

    merged = dict(profile)
    for key in ("zip", "location", "student_name", "parent_state", "parent_current_state"):
        value = parent_form.get(key)
        if value not in (None, "", []):
            merged[key] = value

    parent_services = parent_form.get("current_services")
    if parent_services:
        merged["parent_reported_services"] = parent_services

    return merged


def _build_enrich_profile(profile: dict, counselor_form: dict, gaps_output: dict) -> dict:
    return {
        "student_name": profile.get("student_name")
        or counselor_form.get("student_name")
        or counselor_form.get("Student's first name")
        or "Student",
        "age": profile.get("age"),
        "strengths": profile.get("strengths", []),
        "parent_state": profile.get("parent_state") or profile.get("parent_current_state"),
        "resolved_conditions": gaps_output.get("resolved_conditions", []),
    }


def _collect_referral_enrichment_warnings(enriched_referrals: dict) -> list[str]:
    warnings: list[str] = []
    fallback_count = 0
    total_count = 0

    for entry in enriched_referrals.get("results_by_therapy", []):
        for facility in entry.get("recommended", []):
            total_count += 1
            if not facility.get("ai_enriched"):
                fallback_count += 1

    if fallback_count:
        warnings.append(
            f"referral enrichment used fallback for {fallback_count} of {total_count} facilities"
        )
    return warnings


def _collect_roadmap_warnings(enriched_roadmap: dict) -> list[str]:
    warnings: list[str] = []
    stages = enriched_roadmap.get("stages", [])
    fallback_stages = [stage for stage in stages if not stage.get("ai_enriched")]

    if fallback_stages:
        stage_numbers = ", ".join(str(stage.get("stage_number")) for stage in fallback_stages)
        warnings.append(
            f"roadmap generation used fallback for {len(fallback_stages)} of "
            f"{len(stages)} stages (stage numbers: {stage_numbers})"
        )
    return warnings


def _minimal_enriched_referrals(scored_results: dict) -> dict:
    """Emergency degradation when referral enrichment raises entirely."""
    enriched = dict(scored_results)
    enriched_therapies: list[dict] = []

    for entry in scored_results.get("results_by_therapy", []):
        therapy_type = entry.get("therapy_type", "")
        recommended = []
        facilities = sorted(
            entry.get("recommended", []),
            key=lambda item: item.get("total_score", 0),
            reverse=True,
        )[:3]

        for facility in facilities:
            name = facility.get("name") or "This facility"
            enriched_facility = dict(facility)
            enriched_facility.update(
                {
                    "why_this_fits": (
                        f"{name} matches the recommended {therapy_type} care path for this child."
                    ),
                    "what_to_expect": (
                        "Contact the facility directly to ask about availability, "
                        "intake process, and insurance."
                    ),
                    "ai_enriched": False,
                }
            )
            recommended.append(enriched_facility)

        new_entry = dict(entry)
        new_entry["recommended"] = recommended
        enriched_therapies.append(new_entry)

    enriched["results_by_therapy"] = enriched_therapies
    return enriched


def _minimal_enriched_roadmap(profile: dict, gaps: list[dict]) -> dict:
    """Emergency degradation when roadmap generation raises entirely."""
    from gpt_enrichment import build_stage_skeleton

    skeleton = build_stage_skeleton(gaps)
    stages_out: list[dict] = []

    for stage in skeleton:
        gaps_in_stage = stage.get("gaps", [])
        if gaps_in_stage:
            actions = [
                f"Follow through on starting {gap.get('intervention')} as recommended."
                for gap in gaps_in_stage
            ]
        else:
            actions = [
                "Keep up the routines and supports started in earlier stages.",
                "Check in with your counselor about your child's overall progress.",
            ]

        stages_out.append(
            {
                "stage_number": stage.get("stage_number"),
                "stage_theme": stage.get("stage_theme"),
                "gaps_addressed": [
                    gap.get("intervention") for gap in gaps_in_stage if gap.get("intervention")
                ],
                "parent_support_actions": actions,
                "behavioral_markers": [
                    "Check in with your counselor about progress at this stage."
                ],
                "stage_summary": stage.get("stage_theme", ""),
                "ai_enriched": False,
            }
        )

    return {
        "student_name": profile.get("student_name") or "Student",
        "total_stages": len(stages_out),
        "stages": stages_out,
    }


def _run_required_stage(
    stage_name: str,
    stage_fn: Callable[..., Any],
    *args,
    **kwargs,
) -> tuple[Any, float]:
    start = time.perf_counter()
    logger.info("Stage %s started", stage_name)
    try:
        result = stage_fn(*args, **kwargs)
    except Exception as exc:
        logger.error("Stage %s failed: %s", stage_name, exc)
        raise PipelineStageError(stage_name, exc) from exc

    elapsed = time.perf_counter() - start
    logger.info("Stage %s completed in %.2fs", stage_name, elapsed)
    return result, elapsed


def _run_optional_stage(
    stage_name: str,
    stage_fn: Callable[..., Any],
    *args,
    degraded_label: str,
    on_failure: Callable[[], Any],
    **kwargs,
) -> tuple[Any, float, str, list[str]]:
    """Run an AI stage; on total failure return degraded output and a warning."""
    start = time.perf_counter()
    logger.info("Stage %s started", stage_name)
    stage_warnings: list[str] = []

    try:
        result = stage_fn(*args, **kwargs)
        completed_label = stage_name
    except Exception as exc:
        logger.error(
            "Stage %s failed even though internal fallbacks exist; continuing degraded: %s",
            stage_name,
            exc,
        )
        result = on_failure()
        completed_label = degraded_label
        stage_warnings.append(
            f"{stage_name} failed entirely; using template fallback ({exc})"
        )

    elapsed = time.perf_counter() - start
    logger.info("Stage %s completed in %.2fs", completed_label, elapsed)
    return result, elapsed, completed_label, stage_warnings


def run_full_pipeline(
    counselor_form: dict,
    current_services: list[str],
    studentid: int,
    counselorid: int,
    parent_form: dict | None = None,
) -> dict:
    pipeline_start = time.perf_counter()
    stages_completed: list[str] = []
    warnings: list[str] = []

    profile, elapsed = _run_required_stage("normalize", normalize, counselor_form)
    stages_completed.append("normalize")
    profile = _merge_parent_form(profile, parent_form)

    rule_engine_output, elapsed = _run_required_stage(
        "rule_engine", get_recommendations, profile
    )
    stages_completed.append("rule_engine")

    gaps_output, elapsed = _run_required_stage(
        "gap_detection", detect_gaps, rule_engine_output, current_services
    )
    stages_completed.append("gap_detection")
    profile["resolved_conditions"] = gaps_output.get("resolved_conditions", [])

    query_plan, elapsed = _run_required_stage(
        "query_builder", build_all_queries, gaps_output["gaps"], profile
    )
    stages_completed.append("query_builder")

    search_results, elapsed = _run_required_stage(
        "referral_search", run_search_plan, query_plan
    )
    stages_completed.append("referral_search")

    scored_results, elapsed = _run_required_stage(
        "referral_scoring", score_all_results, search_results, gaps_output["gaps"]
    )
    stages_completed.append("referral_scoring")

    enrich_profile = _build_enrich_profile(profile, counselor_form, gaps_output)
    enriched_referrals, _, completed_label, stage_warnings = _run_optional_stage(
        "referral_enrichment",
        enrich_all_referrals,
        scored_results,
        enrich_profile,
        gaps_output["gaps"],
        degraded_label="referral_enrichment(degraded)",
        on_failure=lambda: _minimal_enriched_referrals(scored_results),
    )
    stages_completed.append(completed_label)
    warnings.extend(stage_warnings)
    warnings.extend(_collect_referral_enrichment_warnings(enriched_referrals))

    enriched_roadmap, _, completed_label, stage_warnings = _run_optional_stage(
        "roadmap_generation",
        build_roadmap,
        gaps_output["gaps"],
        profile,
        degraded_label="roadmap_generation(degraded)",
        on_failure=lambda: _minimal_enriched_roadmap(profile, gaps_output["gaps"]),
    )
    stages_completed.append(completed_label)
    warnings.extend(stage_warnings)
    warnings.extend(_collect_roadmap_warnings(enriched_roadmap))

    db_summary, elapsed = _run_required_stage(
        "database_write",
        write_full_care_plan,
        studentid,
        counselorid,
        enriched_referrals,
        enriched_roadmap,
    )
    stages_completed.append("database_write")

    total_duration = time.perf_counter() - pipeline_start
    logger.info("Full pipeline completed in %.2fs", total_duration)

    return {
        "success": True,
        "roadmapid": db_summary["roadmapid"],
        "referrals_created": db_summary["referrals_created"],
        "parent_tasks_created": db_summary["parent_tasks_created"],
        "behavior_checks_created": db_summary["behavior_checks_created"],
        "stages_completed": stages_completed,
        "total_duration_seconds": round(total_duration, 2),
        "warnings": warnings,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    from db_writer import (  # noqa: E402
        _cleanup_test_student_and_counselor,
        _ensure_test_student_and_counselor,
        delete_roadmap_data,
        delete_roadmap_for_student,
        get_connection,
        print_referral_notes_content_check,
        verify_roadmap_counts,
    )

    TEST_STUDENT_ID = 9999
    TEST_COUNSELOR_ID = 9999

    mock_path = _PROJECT_ROOT / "mockProfile.json"
    with mock_path.open(encoding="utf-8") as mock_file:
        counselor_form = json.load(mock_file)

    current_services = ["cbt", "school counseling", "therapy family caregiver"]
    parent_form = {"zip": "30301", "location": "Atlanta, GA"}

    conn = get_connection()
    try:
        delete_roadmap_for_student(conn, TEST_STUDENT_ID)
        _ensure_test_student_and_counselor(conn, TEST_STUDENT_ID, TEST_COUNSELOR_ID)
    finally:
        conn.close()

    print("\nMindScope full pipeline test")
    print("=" * 70)

    result = run_full_pipeline(
        counselor_form=counselor_form,
        current_services=current_services,
        studentid=TEST_STUDENT_ID,
        counselorid=TEST_COUNSELOR_ID,
        parent_form=parent_form,
    )

    print("\nPipeline result:")
    print(json.dumps(result, indent=2))

    if result.get("warnings"):
        print("\nPipeline warnings:")
        for warning in result["warnings"]:
            print(f"  - {warning}")

    conn = get_connection()
    try:
        roadmapid = result["roadmapid"]
        counts = verify_roadmap_counts(conn, roadmapid)
        print("\nVerification counts (database):")
        print(f"  roadmap rows        : {counts['roadmap']} (expected 1)")
        print(f"  referrals rows      : {counts['referrals']} (expected {result['referrals_created']})")
        print(
            f"  parent_tasks rows   : {counts['parent_tasks']} "
            f"(expected {result['parent_tasks_created']})"
        )
        print(
            f"  behavior_checks rows: {counts['behavior_checks']} "
            f"(expected {result['behavior_checks_created']})"
        )

        assert counts["roadmap"] == 1
        assert counts["referrals"] == result["referrals_created"]
        assert counts["parent_tasks"] == result["parent_tasks_created"]
        assert counts["behavior_checks"] == result["behavior_checks_created"]
        print("\nAll verification counts match.")

        print_referral_notes_content_check(conn, roadmapid)

        delete_roadmap_data(conn, roadmapid)
        post_delete = verify_roadmap_counts(conn, roadmapid)
        print("\nPost-cleanup counts (should all be 0):")
        print(
            f"  roadmap={post_delete['roadmap']} referrals={post_delete['referrals']} "
            f"parent_tasks={post_delete['parent_tasks']} "
            f"behavior_checks={post_delete['behavior_checks']}"
        )
        assert post_delete["roadmap"] == 0
        assert post_delete["referrals"] == 0
        assert post_delete["parent_tasks"] == 0
        assert post_delete["behavior_checks"] == 0
        print("Test data cleaned up successfully.")

        _cleanup_test_student_and_counselor(conn, TEST_STUDENT_ID, TEST_COUNSELOR_ID)
        print("Test student/counselor fixture rows removed.")
    finally:
        conn.close()
