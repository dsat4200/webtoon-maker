"""Native decode handoffs must not occupy the derived-effect LRU."""
from threading import get_ident

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import ImageObject
from comic_editor.ui import distort_pipeline
from comic_editor.ui.async_projection import ProjectionPending
from comic_editor.ui.source_images import image_for_render
from test_deferred_source_images import blocked_decoder, finish, same_image
from test_effect_job_retention import owner
from test_live_image_prefix_reuse import scene as live_scene
from test_navigator_patterns import canvas, preview


@pytest.mark.parametrize('scope,key,alias', [
    (('source-image-decode', 'source'), ('source-image-decode-preview', 'bytes'), False),
    (('source-image-decode', 'source'), ('ordinary-effect', 'bytes'), True),
    (('ordinary-effect', 'source'), ('source-image-decode-preview', 'bytes'), True),
    (('ordinary-effect', 'source'), ('ordinary-effect', 'bytes'), True),
])
def test_only_native_source_scope_and_key_skip_derived_completion_alias(owner, scope, key, alias):
    jobs = owner._effect_jobs
    image = QImage(16, 16, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('blue'))
    jobs.request(scope, key, lambda _: QImage(image), image.sizeInBytes(), require_exact=True)
    jobs.running[3].result(timeout=5)
    jobs.poll()
    assert (key in owner.results) is alias
    assert jobs.result(scope, key) == image
    assert jobs.completed == 1 and not jobs.discarded


def cold_encoded_image():
    image = QImage(512, 512, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#8048a0c0'))
    raw, buffer = QByteArray(), QBuffer()
    buffer.setBuffer(raw)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, 'PNG')
    buffer.close()
    return bytes(raw)


@pytest.mark.parametrize('resident', [False, True], ids=['oversized-store-handoff', 'store-resident'])
def test_actual_decode_finish_and_adoption_preserve_compact_mesh_prefix(canvas, monkeypatch, resident):
    _obj, warp, _color, _mask = live_scene(canvas)
    warp.modifier_type = 'distort_mesh_warp'
    warp.parameters, warp.points, warp.source_points = {'rows': 2, 'columns': 2}, [], []
    warp.validate()
    warp.points[0] = (.04, .02)
    calls, original = [], distort_pipeline.render_distort_stage
    def mesh(*args, **kwargs):
        calls.append(args[5].modifier_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', mesh)
    before = preview(canvas, False, live=True)
    assert calls == [warp.modifier_id]
    prefix = {key: int(image.cacheKey()) for key, image in canvas._modifier_render_cache.items()
              if key[:1] == ('live-effect-draft-stage',)}
    assert prefix
    canvas._modifier_render_cache_budget = max(1, canvas._modifier_render_cache_bytes)
    raw = cold_encoded_image()
    expected = ImageStore._decode(raw)[0]
    assert expected.sizeInBytes() > canvas._modifier_render_cache_budget
    cold = canvas.chapter.add_object(canvas.chapter.root_page_ids[0],
        ImageObject(x=800, y=100, pixel_width=512, pixel_height=512, visible=False))
    canvas.images.put(cold.object_id, 'cold.png', raw)
    previous_decode = canvas.images._decoded.pop(cold.object_id, None)
    if previous_decode is not None:
        canvas.images.decoded_bytes -= int(previous_decode.sizeInBytes())
    if not resident:
        canvas.images.decoded_budget = 1
    jobs = canvas._effect_jobs
    jobs.retained_budget = 512
    protected = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    protected.fill(QColor('red'))
    jobs.retained_put(('protected-derived-prefix',), ('old-prefix',), protected, shared=True)
    assert jobs._retained_shared
    entered, release, decode_threads = blocked_decoder(monkeypatch)
    previous = canvas._projection_exact, canvas._projection_defer_effects
    canvas._projection_exact = canvas._projection_defer_effects = True
    try:
        with pytest.raises(ProjectionPending) as waiting:
            image_for_render(canvas, cold.object_id)
        assert entered.wait(2)
        release.set()
        finish(canvas)
        # The original frame exceeds both retained/store limits, but the
        # existing exclusive source handoff is still consumable without the
        # unrelated derived-effect alias or a synchronous decoder fallback.
        assert jobs.retained_get(('result', waiting.value.scope), waiting.value.key) is not None
        assert all(int(canvas._modifier_render_cache[key].cacheKey()) == storage
                   for key, storage in prefix.items())
        assert waiting.value.key not in canvas._modifier_render_cache
        same_image(image_for_render(canvas, cold.object_id), expected)
        same_image(image_for_render(canvas, cold.object_id), expected)
        assert (canvas.images.cached_image(cold.object_id) is not None) is resident
        assert decode_threads == [decode_threads[0]] and decode_threads[0] != get_ident()
        assert jobs.submitted == jobs.completed == 1 and not jobs.discarded
        assert jobs.retained_bytes <= max(jobs.retained_budget, int(expected.sizeInBytes()))
    finally:
        release.set()
        canvas._projection_exact, canvas._projection_defer_effects = previous
    assert preview(canvas, False, live=True) == before
    assert calls == [warp.modifier_id]
