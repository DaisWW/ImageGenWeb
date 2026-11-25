from collections import Counter

from flask import abort, jsonify, request, send_file, url_for
from flask_login import current_user

from ..extensions import db
from ..models import GenerationJob, ImageTestRun, ImageTestSuite, WorkerState
from ..serializers import _iso
from ..services.generations import ACTIVE_GENERATION_STATUSES
from ..services.image_tests import MAX_TEST_IMAGES, MAX_TEST_QUESTIONS
from ..worker_health import worker_heartbeat_is_fresh
from . import web
from .shared import (
    accessible_item,
    admin_required,
    channels,
    image_extension,
    json_body,
    services,
    storage,
)

_HISTORY_PAGE_SIZE = 20


@web.get("/api/admin/image-tests")
@admin_required
def image_tests_catalog():
    registry = channels()
    return jsonify(
        suites=[_suite_dict(suite) for suite in services().image_tests.suites()],
        channels=[channel.public_dict() for channel in registry.list(include_disabled=False)],
        max_images=MAX_TEST_IMAGES,
        max_questions=MAX_TEST_QUESTIONS,
        max_prompt_characters=services().settings.runtime().max_prompt_characters,
        history_retention_days=registry.queue.history_retention_days,
    )


@web.post("/api/admin/image-tests/suites")
@admin_required
def create_image_test_suite():
    suite = services().image_tests.save_suite(current_user.id, json_body())
    return jsonify(suite=_suite_dict(suite)), 201


@web.put("/api/admin/image-tests/suites/<suite_id>")
@admin_required
def update_image_test_suite(suite_id: str):
    suite = services().image_tests.save_suite(current_user.id, json_body(), suite_id)
    return jsonify(suite=_suite_dict(suite))


@web.delete("/api/admin/image-tests/suites/<suite_id>")
@admin_required
def delete_image_test_suite(suite_id: str):
    services().image_tests.delete_suite(current_user.id, suite_id, json_body())
    return jsonify(ok=True)


@web.get("/api/admin/image-tests/runs")
@admin_required
def image_test_runs():
    offset = min(100000, max(0, request.args.get("offset", 0, type=int)))
    rows, total = services().image_tests.list_runs(_HISTORY_PAGE_SIZE, offset)
    return jsonify(
        runs=[_run_summary(run, counts) for run, counts in rows],
        total=total,
        limit=_HISTORY_PAGE_SIZE,
    )


@web.post("/api/admin/image-tests/runs")
@admin_required
def start_image_test_run():
    run = services().image_tests.start(current_user.id, json_body())
    return jsonify(run=_run_detail(run)), 202


@web.get("/api/admin/image-tests/runs/<run_id>")
@admin_required
def image_test_run(run_id: str):
    include_questions = request.args.get("progress") != "1"
    run = services().image_tests.get_run(run_id, include_questions=include_questions)
    return jsonify(run=_run_detail(run, include_questions=include_questions))


@web.post("/api/admin/image-tests/runs/<run_id>/cancel")
@admin_required
def cancel_image_test_run(run_id: str):
    run = services().image_tests.cancel(current_user.id, run_id)
    return jsonify(run=_run_detail(run))


@web.post("/api/admin/image-tests/runs/<run_id>/retry")
@admin_required
def retry_image_test_run(run_id: str):
    run = services().image_tests.start(current_user.id, json_body(), retry_of=run_id)
    return jsonify(run=_run_detail(run)), 202


@web.put("/api/admin/image-tests/runs/<run_id>/results/<job_id>")
@admin_required
def review_image_test_result(run_id: str, job_id: str):
    services().image_tests.review(run_id, job_id, json_body().get("verdict"))
    return jsonify(ok=True)


@web.get("/media/admin/image-tests/<item_id>/<variant>")
@admin_required
def image_test_output(item_id: str, variant: str):
    if variant not in {"image", "thumbnail"}:
        abort(404)
    item = accessible_item(item_id, image_test=True)
    thumbnail = variant == "thumbnail" and item.thumbnail_path
    path = item.thumbnail_path if thumbnail else item.output_path
    if not path:
        abort(404)
    try:
        file_path = storage().read(path)
    except FileNotFoundError:
        abort(404)
    return send_file(
        file_path,
        mimetype="image/webp" if thumbnail else item.output_mime_type,
        as_attachment=variant == "image" and request.args.get("download") == "1",
        download_name=f"image_test_{item.id}.{image_extension(item.output_mime_type)}",
        conditional=True,
    )


def _suite_dict(suite: ImageTestSuite) -> dict:
    return {
        "id": suite.id,
        "name": suite.name,
        "questions": suite.questions,
        "revision": suite.revision,
        "builtin": suite.id == "default",
    }


def _run_summary(run: ImageTestRun, counts: dict[str, int]) -> dict:
    finished = run.total_images - sum(
        counts.get(status, 0) for status in ACTIVE_GENERATION_STATUSES
    )
    return {
        "id": run.id,
        "suite_name": run.suite_name,
        "retry_of": run.retry_of,
        "created_at": _iso(run.created_at),
        "total": run.total_images,
        "finished": finished,
        "active": finished < run.total_images,
        "expired": run.total_images - sum(counts.values()),
        "counts": counts,
        "estimated_rmb": format(run.estimated_rmb, ".4f"),
    }


def _run_detail(run: ImageTestRun, *, include_questions: bool = True) -> dict:
    jobs = services().image_tests.jobs(run.id)
    payload = _run_summary(run, Counter(job.status for job in jobs))
    if include_questions:
        payload["questions"] = run.questions
    payload["results"] = [_result(job) for job in jobs]
    if payload["active"]:
        worker = db.session.get(WorkerState, 1)
        payload["worker_ready"] = bool(
            worker
            and worker.worker_id
            and worker_heartbeat_is_fresh(
                worker.heartbeat_at, services().settings.runtime().worker_heartbeat_seconds
            )
        )
    return payload


def _result(job: GenerationJob) -> dict:
    item = job.items[0]
    return {
        "id": job.id,
        "question_index": job.image_test_question_index,
        "channel_id": job.channel_id,
        "channel_label": job.channel_label,
        "model": job.model,
        "status": item.status,
        "elapsed_seconds": float(item.elapsed_seconds)
        if item.elapsed_seconds is not None
        else None,
        "error": item.error_message,
        "error_code": item.error_code,
        "upstream_status": item.upstream_status,
        "width": item.output_width,
        "height": item.output_height,
        "format": job.output_format,
        "verdict": job.image_test_verdict or "",
        "image_url": (
            url_for("web.image_test_output", item_id=item.id, variant="image")
            if item.output_path
            else None
        ),
        "thumbnail_url": (
            url_for("web.image_test_output", item_id=item.id, variant="thumbnail")
            if item.output_path
            else None
        ),
    }
