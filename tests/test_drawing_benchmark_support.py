"""Benchmark completion must mean an exact current frame, not a placeholder."""
from types import SimpleNamespace

from drawing_benchmark_support import ExactProgress, canvas_pending


def observe(progress, now=1., *, pending=False, busy=False, failed=False, signature=(1,)):
    return progress.observe(now, frame_pending=pending, jobs_busy=busy, failed=failed,
                            signature=signature, activity="stroke")


def terminal(progress, now=2., *, activity=.5, signature=(1,), busy=False, failed=False):
    return progress.terminal(now, last_activity_at=activity, signature=signature,
                             jobs_busy=busy, failed=failed)


def test_first_paint_and_exact_completion_are_distinct():
    progress = ExactProgress()
    assert not observe(progress, pending=True, busy=True)
    assert progress.first_paint_at == 1. and progress.first_exact_at is None
    assert not terminal(progress)
    assert observe(progress, 3.)
    assert progress.first_exact_at == 3.
    assert terminal(progress, 3.2)


def test_stale_exact_frame_cannot_finish_new_input_or_camera():
    progress = ExactProgress()
    observe(progress)
    assert not terminal(progress, activity=1.1)
    assert not terminal(progress, signature=(2,))
    observe(progress, 1.5, signature=(2,))
    assert terminal(progress, activity=1.1, signature=(2,))


def test_job_completion_needs_a_confirming_paint():
    progress = ExactProgress()
    observe(progress, busy=True)
    assert not terminal(progress, busy=False)
    observe(progress, 1.5)
    assert terminal(progress)


def test_pending_frame_and_terminal_failure_are_never_ready():
    for reason in ({"pending": True}, {"failed": True}):
        progress = ExactProgress()
        observe(progress, **reason)
        assert not terminal(progress)
    progress = ExactProgress()
    observe(progress)
    assert not terminal(progress, failed=True)


def test_changed_dependencies_restart_quiet_completion_interval():
    progress = ExactProgress()
    observe(progress)
    observe(progress, 1.95, signature=(2,))
    assert not terminal(progress, 2., signature=(2,))
    assert terminal(progress, 2.1, signature=(2,))


def test_presented_old_revision_is_pending_even_with_empty_job_queue():
    jobs = SimpleNamespace(exact_failures={}, running=None, pending={}, submitted=1,
                           completed=1, discarded=0)
    canvas = SimpleNamespace(_effect_jobs=jobs, _document_projection=SimpleNamespace(revision=2),
        _projection_async_enabled=True, _projection_presented_revision=1,
        _projection_frame_pending=False)
    assert canvas_pending(canvas)["frame_pending"]
    canvas._projection_presented_revision = 2
    assert not canvas_pending(canvas)["frame_pending"]
    jobs.exact_failures["scope"] = ("key", "failed")
    assert canvas_pending(canvas)["failed"]
