"""
MindScope pipeline — final step: persist enriched referrals and roadmap to MySQL.

Receives output from language/referral_enrichment.py and deterministically/gpt_enrichment.py
and writes rows into roadmap, referrals, parent_tasks, and behavior_checks.
Pure transformation/insertion — no AI in this module.
"""

import logging
import sys
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from mysql.connector import Error as MySQLError
from mysql.connector import errorcode

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

STAGE_ENUM_MAP = {
    1: "stage_1",
    2: "stage_2",
    3: "stage_3",
    4: "stage_4",
    5: "stage_5",
    6: "stage_6",
}

LOCATION_FALLBACK = "Location not available"


class RoadmapAlreadyExistsError(Exception):
    """Raised when a roadmap row already exists for the given studentid."""


def get_connection() -> mysql.connector.MySQLConnection:
    import os

    config = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "database": os.getenv("DB_NAME"),
        "autocommit": False,
    }
    missing = [key for key in ("user", "password", "database") if not config.get(key)]
    if missing:
        raise ValueError(
            f"Missing database configuration in .env: {', '.join(f'DB_{k.upper()}' for k in missing)}"
        )
    return mysql.connector.connect(**config)


def _ensure_referral_notes_column(conn) -> None:
    """Idempotently add referral_notes TEXT to referrals if it is not present."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'referrals'
              AND COLUMN_NAME = 'referral_notes'
            """
        )
        exists = cursor.fetchone()[0] > 0
        if not exists:
            logger.info("Adding referrals.referral_notes column")
            cursor.execute("ALTER TABLE referrals ADD COLUMN referral_notes TEXT NULL")
    finally:
        cursor.close()


def _referral_location(facility: dict) -> str:
    for field in ("address", "website"):
        value = facility.get(field)
        if value not in (None, "", []):
            return str(value)
    return LOCATION_FALLBACK


def _referral_notes(facility: dict) -> str | None:
    why = (facility.get("why_this_fits") or "").strip()
    expect = (facility.get("what_to_expect") or "").strip()
    if not why and not expect:
        return None
    parts = []
    if why:
        parts.append(f"Why this fits: {why}")
    if expect:
        parts.append(f"What to expect: {expect}")
    return "\n\n".join(parts)


def create_roadmap(conn, studentid: int, counselorid: int) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO roadmap (studentid, counselorid, status)
            VALUES (%s, %s, 'active')
            """,
            (studentid, counselorid),
        )
        roadmapid = cursor.lastrowid
        logger.info("Created roadmap %s for studentid=%s counselorid=%s", roadmapid, studentid, counselorid)
        return roadmapid
    except MySQLError as exc:
        if exc.errno == errorcode.ER_DUP_ENTRY:
            raise RoadmapAlreadyExistsError(
                f"A roadmap already exists for studentid={studentid}"
            ) from exc
        logger.error("Failed to create roadmap for studentid=%s: %s", studentid, exc)
        raise
    finally:
        cursor.close()


def ensure_student_record(
    conn,
    profile: dict,
    counselor_form: dict,
    schoolid: int = 1,
    parentid: int = 1,
    studentid: int | None = None,
) -> int:
    """Create or update a students row before linking a roadmap."""
    cursor = conn.cursor()
    try:
        fname = (
            profile.get("student_name")
            or counselor_form.get("Student's first name")
            or "Unknown"
        )
        lname = (
            counselor_form.get("Student's last name")
            or counselor_form.get("student_last_name")
            or "Student"
        )

        resolved_schoolid = schoolid
        school_name = profile.get("school_name") or counselor_form.get("School name")
        if school_name:
            cursor.execute(
                "SELECT schoolid FROM schools WHERE name = %s LIMIT 1",
                (school_name,),
            )
            row = cursor.fetchone()
            if row:
                resolved_schoolid = row[0]

        if studentid is not None:
            cursor.execute("SELECT studentid FROM students WHERE studentid = %s", (studentid,))
            if not cursor.fetchone():
                raise ValueError(f"studentid {studentid} not found")
            cursor.execute(
                """
                UPDATE students
                SET fname = %s, lname = %s, schoolid = %s, parentid = %s
                WHERE studentid = %s
                """,
                (fname, lname, resolved_schoolid, parentid, studentid),
            )
            logger.info("Updated student %s (%s %s)", studentid, fname, lname)
            return studentid

        cursor.execute(
            """
            INSERT INTO students (fname, lname, schoolid, parentid)
            VALUES (%s, %s, %s, %s)
            """,
            (fname, lname, resolved_schoolid, parentid),
        )
        new_studentid = cursor.lastrowid
        logger.info("Created student %s (%s %s)", new_studentid, fname, lname)
        return new_studentid
    finally:
        cursor.close()


def write_referrals(conn, roadmapid: int, enriched_referral_results: dict) -> int:
    _ensure_referral_notes_column(conn)

    cursor = conn.cursor()
    total_inserted = 0

    try:
        for entry in enriched_referral_results.get("results_by_therapy", []):
            therapy_type = entry.get("therapy_type", "")
            facilities = entry.get("recommended", [])
            count_for_type = 0

            for facility in facilities:
                cursor.execute(
                    """
                    INSERT INTO referrals
                        (roadmapid, therapy_type, name, location, status, referral_notes)
                    VALUES (%s, %s, %s, %s, 'pending', %s)
                    """,
                    (
                        roadmapid,
                        therapy_type,
                        facility.get("name") or "Unknown facility",
                        _referral_location(facility),
                        _referral_notes(facility),
                    ),
                )
                count_for_type += 1
                total_inserted += 1

            logger.info(
                "Wrote %d referral(s) for therapy_type=%r (roadmapid=%s)",
                count_for_type,
                therapy_type,
                roadmapid,
            )
    finally:
        cursor.close()

    logger.info("Total referrals written: %d (roadmapid=%s)", total_inserted, roadmapid)
    return total_inserted


def _round_robin_assignments(items: list, bucket_count: int) -> list[list]:
    buckets: list[list] = [[] for _ in range(bucket_count)]
    if bucket_count == 0:
        return buckets
    for index, item in enumerate(items):
        buckets[index % bucket_count].append(item)
    return buckets


def write_roadmap_stages(conn, roadmapid: int, enriched_roadmap: dict) -> dict:
    cursor = conn.cursor()
    parent_tasks_created = 0
    behavior_checks_created = 0

    try:
        for stage in enriched_roadmap.get("stages", []):
            stage_number = stage.get("stage_number")
            stage_enum = STAGE_ENUM_MAP.get(stage_number)
            if not stage_enum:
                raise ValueError(f"Invalid stage_number: {stage_number!r}")

            actions = [
                str(action).strip()
                for action in (stage.get("parent_support_actions") or [])
                if str(action).strip()
            ]
            markers = [
                str(marker).strip()
                for marker in (stage.get("behavioral_markers") or [])
                if str(marker).strip()
            ]

            if not actions:
                logger.info(
                    "Stage %s has no parent_support_actions — skipping task/check inserts",
                    stage_number,
                )
                continue

            marker_buckets = _round_robin_assignments(markers, len(actions))
            task_ids: list[int] = []

            for action in actions:
                cursor.execute(
                    """
                    INSERT INTO parent_tasks (roadmapid, description, stage, status)
                    VALUES (%s, %s, %s, 'pending')
                    """,
                    (roadmapid, action, stage_enum),
                )
                task_ids.append(cursor.lastrowid)
                parent_tasks_created += 1

            for task_index, task_id in enumerate(task_ids):
                for marker in marker_buckets[task_index]:
                    cursor.execute(
                        """
                        INSERT INTO behavior_checks
                            (parent_taskid, description, stage, status)
                        VALUES (%s, %s, %s, 'not_met')
                        """,
                        (task_id, marker, stage_number),
                    )
                    behavior_checks_created += 1

            logger.info(
                "Stage %s (%s): wrote %d parent_tasks, %d behavior_checks",
                stage_number,
                stage_enum,
                len(task_ids),
                sum(len(bucket) for bucket in marker_buckets),
            )
    finally:
        cursor.close()

    logger.info(
        "Roadmap stages written: %d parent_tasks, %d behavior_checks (roadmapid=%s)",
        parent_tasks_created,
        behavior_checks_created,
        roadmapid,
    )
    return {
        "parent_tasks_created": parent_tasks_created,
        "behavior_checks_created": behavior_checks_created,
    }


def write_full_care_plan(
    studentid: int,
    counselorid: int,
    enriched_referral_results: dict,
    enriched_roadmap: dict,
) -> dict:
    conn = None
    try:
        conn = get_connection()
        roadmapid = create_roadmap(conn, studentid, counselorid)
        referrals_created = write_referrals(conn, roadmapid, enriched_referral_results)
        stage_summary = write_roadmap_stages(conn, roadmapid, enriched_roadmap)
        conn.commit()
        summary = {
            "roadmapid": roadmapid,
            "referrals_created": referrals_created,
            "parent_tasks_created": stage_summary["parent_tasks_created"],
            "behavior_checks_created": stage_summary["behavior_checks_created"],
        }
        logger.info("Care plan committed successfully: %s", summary)
        return summary
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
                logger.error("Transaction rolled back due to error: %s", exc)
            except MySQLError as rollback_exc:
                logger.error("Rollback failed: %s", rollback_exc)
        raise
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


def delete_roadmap_data(conn, roadmapid: int) -> None:
    """Remove a roadmap and all dependent rows (for test cleanup)."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT parent_taskid FROM parent_tasks WHERE roadmapid = %s",
            (roadmapid,),
        )
        task_ids = [row[0] for row in cursor.fetchall()]
        if task_ids:
            placeholders = ", ".join(["%s"] * len(task_ids))
            cursor.execute(
                f"DELETE FROM behavior_checks WHERE parent_taskid IN ({placeholders})",
                tuple(task_ids),
            )
        cursor.execute("DELETE FROM parent_tasks WHERE roadmapid = %s", (roadmapid,))
        cursor.execute("DELETE FROM referrals WHERE roadmapid = %s", (roadmapid,))
        cursor.execute("DELETE FROM roadmap WHERE roadmapid = %s", (roadmapid,))
        conn.commit()
        logger.info("Deleted roadmap %s and dependent rows", roadmapid)
    finally:
        cursor.close()


def delete_roadmap_for_student(conn, studentid: int) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT roadmapid FROM roadmap WHERE studentid = %s", (studentid,))
        rows = cursor.fetchall()
        for (roadmapid,) in rows:
            delete_roadmap_data(conn, roadmapid)
    finally:
        cursor.close()


def _ensure_test_student_and_counselor(conn, studentid: int, counselorid: int) -> None:
    """Insert test student/counselor rows when running the __main__ integration test."""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT studentid FROM students WHERE studentid = %s", (studentid,))
        if not cursor.fetchone():
            cursor.execute(
                """
                INSERT INTO students (studentid, fname, lname, schoolid, parentid)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (studentid, "Test", "Student", 1, 1),
            )
        cursor.execute("SELECT counselorid FROM counselors WHERE counselorid = %s", (counselorid,))
        if not cursor.fetchone():
            cursor.execute(
                """
                INSERT INTO counselors (counselorid, fname, lname, email, schoolid)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (counselorid, "Test", "Counselor", "test.counselor@example.com", 1),
            )
        conn.commit()
    finally:
        cursor.close()


def _cleanup_test_student_and_counselor(conn, studentid: int, counselorid: int) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM students WHERE studentid = %s", (studentid,))
        cursor.execute("DELETE FROM counselors WHERE counselorid = %s", (counselorid,))
        conn.commit()
    finally:
        cursor.close()


def verify_roadmap_counts(conn, roadmapid: int) -> dict:
    cursor = conn.cursor()
    try:
        counts = {}
        cursor.execute("SELECT COUNT(*) FROM referrals WHERE roadmapid = %s", (roadmapid,))
        counts["referrals"] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM parent_tasks WHERE roadmapid = %s", (roadmapid,))
        counts["parent_tasks"] = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM behavior_checks bc
            JOIN parent_tasks pt ON bc.parent_taskid = pt.parent_taskid
            WHERE pt.roadmapid = %s
            """,
            (roadmapid,),
        )
        counts["behavior_checks"] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM roadmap WHERE roadmapid = %s", (roadmapid,))
        counts["roadmap"] = cursor.fetchone()[0]
        return counts
    finally:
        cursor.close()


def print_referral_notes_content_check(conn, roadmapid: int) -> tuple[int, int]:
    """Print referral_notes content for each referral row; return (non_empty, total)."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT therapy_type, name, referral_notes
            FROM referrals
            WHERE roadmapid = %s
            ORDER BY therapy_type, name
            """,
            (roadmapid,),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    print(f"\nReferral notes content check (roadmapid={roadmapid})")
    print("=" * 70)

    non_empty = 0
    total = len(rows)
    for therapy_type, name, notes in rows:
        empty_or_null = notes is None or str(notes).strip() == ""
        if not empty_or_null:
            non_empty += 1
        length = 0 if notes is None else len(str(notes))
        if empty_or_null:
            preview = "(EMPTY)"
        else:
            text = str(notes).replace("\n", " | ")
            if len(text) > 150:
                preview = f'"{text[:150]}..."'
            else:
                preview = f'"{text}"'
        print(f"  [{therapy_type}] {name}")
        print(f"    referral_notes length: {length} chars | empty_or_null: {empty_or_null}")
        print(f"    preview: {preview}")

    print(f"\n{non_empty} of {total} referrals have non-empty referral_notes")
    if non_empty < total:
        logger.warning(
            "%d of %d referrals have empty or NULL referral_notes (roadmapid=%s)",
            total - non_empty,
            total,
            roadmapid,
        )
    return non_empty, total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    sys.path.insert(0, str(_PROJECT_ROOT / "interventions"))
    sys.path.insert(0, str(_PROJECT_ROOT / "questionnaire"))
    sys.path.insert(0, str(_PROJECT_ROOT / "source"))
    sys.path.insert(0, str(_PROJECT_ROOT / "language"))
    sys.path.insert(0, str(_PROJECT_ROOT / "determistically"))

    import json

    from gpt_enrichment import build_roadmap
    from matching import detect_gaps
    from normalizer import normalize
    from query_builder import build_all_queries
    from referral_enrichment import enrich_all_referrals
    from referral_scorer import score_all_results
    from referral_search import run_search_plan
    from rule_engine import get_recommendations

    TEST_STUDENT_ID = 9999
    TEST_COUNSELOR_ID = 9999

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

    enrich_profile = {
        "student_name": profile.get("student_name")
        or counselor_form.get("student_name")
        or "Marcus",
        "age": profile.get("age"),
        "resolved_conditions": gaps_output.get("resolved_conditions", []),
    }

    enriched_referrals = enrich_all_referrals(scored_results, enrich_profile, gaps_output["gaps"])
    enriched_roadmap = build_roadmap(gaps_output["gaps"], enrich_profile)

    print("\nDB writer — full Marcus pipeline write test")
    print("=" * 70)

    conn = get_connection()
    try:
        delete_roadmap_for_student(conn, TEST_STUDENT_ID)
        _ensure_test_student_and_counselor(conn, TEST_STUDENT_ID, TEST_COUNSELOR_ID)
    finally:
        conn.close()

    summary = write_full_care_plan(
        TEST_STUDENT_ID,
        TEST_COUNSELOR_ID,
        enriched_referrals,
        enriched_roadmap,
    )
    print("\nWrite summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")

    conn = get_connection()
    try:
        counts = verify_roadmap_counts(conn, summary["roadmapid"])
        print("\nVerification counts (database):")
        print(f"  roadmap rows        : {counts['roadmap']} (expected 1)")
        print(f"  referrals rows      : {counts['referrals']} (expected {summary['referrals_created']})")
        print(
            f"  parent_tasks rows   : {counts['parent_tasks']} "
            f"(expected {summary['parent_tasks_created']})"
        )
        print(
            f"  behavior_checks rows: {counts['behavior_checks']} "
            f"(expected {summary['behavior_checks_created']})"
        )

        assert counts["roadmap"] == 1
        assert counts["referrals"] == summary["referrals_created"]
        assert counts["parent_tasks"] == summary["parent_tasks_created"]
        assert counts["behavior_checks"] == summary["behavior_checks_created"]
        print("\nAll verification counts match.")

        print_referral_notes_content_check(conn, summary["roadmapid"])

        delete_roadmap_data(conn, summary["roadmapid"])
        post_delete = verify_roadmap_counts(conn, summary["roadmapid"])
        print("\nPost-cleanup counts (should all be 0):")
        print(f"  roadmap={post_delete['roadmap']} referrals={post_delete['referrals']} "
              f"parent_tasks={post_delete['parent_tasks']} behavior_checks={post_delete['behavior_checks']}")
        assert post_delete["roadmap"] == 0
        assert post_delete["referrals"] == 0
        assert post_delete["parent_tasks"] == 0
        assert post_delete["behavior_checks"] == 0
        print("Test data cleaned up successfully.")

        _cleanup_test_student_and_counselor(conn, TEST_STUDENT_ID, TEST_COUNSELOR_ID)
        print("Test student/counselor fixture rows removed.")
    finally:
        conn.close()
