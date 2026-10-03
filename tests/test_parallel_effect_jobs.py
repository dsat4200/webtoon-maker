from threading import Event

import pytest

from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, current_contract, pixel_scope
from comic_editor.ui.async_projection import ProjectionFailed, ProjectionPending, projection_result_or_pending
from comic_editor.ui.effect_jobs import EffectJobs
from test_effect_job_retention import Owner, image


@pytest.fixture
def owner(qapp):
    canvas = Owner()
    canvas._effect_jobs.executor.shutdown(wait=True)
    canvas._effect_jobs = EffectJobs(canvas, workers=3, budget=300)
    yield canvas
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def finish(jobs):
    for job in jobs.running_jobs:
        job[3].result(timeout=5)
    jobs.poll()


def test_independent_exact_jobs_run_concurrently_with_combined_budget(owner):
    jobs = owner._effect_jobs
    starts, release = [Event() for _ in range(3)], Event()
    def work(index):
        def compute(cancelled):
            starts[index].set()
            assert release.wait(5)
            return image('red')
        return compute
    try:
        for i in range(3):
            assert jobs.request(('tile', i), ('state', i), work(i), 100, require_exact=True)
        assert all(start.wait(1) for start in starts)
        assert jobs.bytes_in_flight == 300
        assert jobs.has_running(('tile', 2), ('state', 2))
        with pytest.raises(ProjectionPending):
            projection_result_or_pending(owner, ('tile', 2), ('state', 2))
        assert jobs.request('next', 'revision', work(0), 100, require_exact=True)
        assert jobs.bytes_in_flight == 300 and jobs.waiting['next'] == 'revision'
    finally:
        release.set()
    finish(jobs)
    assert jobs.completed == 3 and not jobs.running_jobs
    for i in range(3):
        assert jobs.result(('tile', i), ('state', i)) is not None


def test_obsolete_parallel_job_cannot_publish_or_report_failure(owner):
    jobs = owner._effect_jobs
    starts, release = [Event() for _ in range(2)], Event()
    def old(cancelled):
        starts[0].set()
        assert release.wait(5)
        raise RuntimeError('obsolete failure')
    def sibling(cancelled):
        starts[1].set()
        assert release.wait(5)
        return image('blue')
    try:
        jobs.request('active', 'old', old, 50, require_exact=True)
        jobs.request('sibling', 'current', sibling, 50, require_exact=True)
        assert all(start.wait(1) for start in starts)
        jobs.request('active', 'new', lambda c: image('green'), 50, require_exact=True)
        latest = next(job for job in jobs.running_jobs if job[:2] == ('active', 'new'))
        latest[3].result(timeout=5)
        jobs.poll()
        assert jobs.result('active', 'new') == image('green')
    finally:
        release.set()
    # Read the obsolete failed future through poll, rather than raising it here.
    for job in jobs.running_jobs:
        try:
            job[3].result(timeout=5)
        except RuntimeError:
            pass
    jobs.poll()
    assert jobs.discarded == 1
    assert 'active' not in jobs.exact_failures
    assert jobs.result('sibling', 'current') == image('blue')
    assert jobs.result('active', 'new') == image('green')


def test_each_queued_worker_keeps_its_captured_pixel_policy(owner):
    jobs = owner._effect_jobs
    jobs.worker_limit = 1
    release, started = Event(), Event()
    observed = []
    def first(cancelled):
        started.set()
        assert release.wait(5)
        observed.append(current_contract())
        return image()
    try:
        with pixel_scope(FLOAT_PIXELS):
            jobs.request('one', 'float', first, 50)
        assert started.wait(1)
        jobs.request('two', 'legacy', lambda c: (observed.append(current_contract()), image())[1], 50)
        assert current_contract() == LEGACY_PIXELS
    finally:
        release.set()
    finish(jobs)
    finish(jobs)
    assert observed == [FLOAT_PIXELS, LEGACY_PIXELS]


def test_oversized_work_has_exclusive_pool_and_failure_is_terminal(owner):
    jobs = owner._effect_jobs
    release, entered = Event(), Event()
    def large(cancelled):
        entered.set()
        assert release.wait(5)
        return None
    try:
        jobs.request('large', 'revision', large, 400, allow_oversized=True, require_exact=True)
        assert entered.wait(1) and len(jobs.running_jobs) == 1
        jobs.request('small', 'revision', lambda c: image(), 1, require_exact=True)
        assert len(jobs.running_jobs) == 1 and jobs.bytes_in_flight == 400
    finally:
        release.set()
    finish(jobs)
    with pytest.raises(ProjectionFailed):
        jobs.result('large', 'revision')
