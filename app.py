"""
Flask API entry point for the MindScope pipeline.

The counselor questionnaire handler POSTs JSON here; this endpoint ensures the
student exists in the database, runs the full pipeline, and returns a success
summary linked to the new roadmap.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import pipeline first so sys.path is configured for stage modules.
from pipeline import PipelineStageError, run_full_pipeline  # noqa: E402
from db_writer import RoadmapAlreadyExistsError, ensure_student_record, get_connection  # noqa: E402
from normalizer import normalize  # noqa: E402

app = Flask(__name__)


@app.post("/api/pipeline/run")
def run_pipeline_endpoint():
    data = request.get_json(silent=True) or {}
    counselor_form = data.get("counselor_form")
    counselorid = data.get("counselorid")

    if not counselor_form:
        return jsonify({"ok": False, "error": "counselor_form is required"}), 400
    if counselorid is None:
        return jsonify({"ok": False, "error": "counselorid is required"}), 400

    try:
        counselorid = int(counselorid)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "counselorid must be an integer"}), 400

    parent_form = data.get("parent_form") or {}
    current_services = data.get("current_services")
    schoolid = int(data.get("schoolid", os.getenv("DB_DEFAULT_SCHOOL_ID", 1)))
    parentid = int(data.get("parentid", os.getenv("DB_DEFAULT_PARENT_ID", 1)))
    studentid = data.get("studentid")
    if studentid is not None:
        studentid = int(studentid)

    try:
        profile = normalize(counselor_form)
    except Exception as exc:
        logger.error("Questionnaire normalization failed: %s", exc)
        return jsonify({"ok": False, "error": f"Invalid questionnaire: {exc}"}), 400

    conn = get_connection()
    try:
        studentid = ensure_student_record(
            conn,
            profile,
            counselor_form,
            schoolid=schoolid,
            parentid=parentid,
            studentid=studentid,
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        conn.rollback()
        logger.error("Student record setup failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()

    if current_services is None:
        current_services = profile.get("current_services") or []

    try:
        result = run_full_pipeline(
            counselor_form=counselor_form,
            current_services=current_services,
            studentid=studentid,
            counselorid=counselorid,
            parent_form=parent_form,
        )
    except RoadmapAlreadyExistsError as exc:
        logger.error("Roadmap already exists for studentid=%s: %s", studentid, exc)
        return jsonify({"ok": False, "error": str(exc), "studentid": studentid}), 409
    except PipelineStageError as exc:
        logger.error("Pipeline failed at stage %s: %s", exc.stage_name, exc.original)
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
                "stage": exc.stage_name,
                "studentid": studentid,
            }
        ), 500
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc), "studentid": studentid}), 500

    return jsonify(
        {
            "ok": True,
            "message": "Pipeline completed successfully.",
            "studentid": studentid,
            "counselorid": counselorid,
            **result,
        }
    ), 200


@app.get("/health")
def health():
    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
