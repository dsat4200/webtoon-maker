from __future__ import annotations

import threading
import time

from PySide6.QtGui import QColor, QImage

from comic_editor.three_d.render_service import RenderQuality, RenderRequest, RenderService
from comic_editor.three_d.renderer.scene import SceneData


def _request(frame: str = "frame", *, scene: SceneData | None = None) -> RenderRequest:
    return RenderRequest("chapter", frame, 1, 1, "cache", (64, 48), RenderQuality.FINAL, scene=scene)


class _FakeBackend:
    started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []

    def __init__(self) -> None:
        type(self).started.clear()
        type(self).release_first.clear()
        type(self).calls.clear()

    def render(self, scene: SceneData, size: tuple[int, int], options: object) -> QImage:
        type(self).calls.append(scene.scene_id)
        type(self).started.set()
        if scene.scene_id == "first":
            type(self).release_first.wait(2.0)
        image = QImage(size[0], size[1], QImage.Format.Format_RGBA8888_Premultiplied)
        image.fill(QColor(10, 20, 30, 128))
        return image

    def release(self) -> None:
        pass


def test_service_keeps_only_latest_pending_request_and_drops_stale_result() -> None:
    service = RenderService(backend_factory=_FakeBackend)
    try:
        first = service.submit(_request("first", scene=SceneData(scene_id="first")))
        assert _FakeBackend.started.wait(2.0)
        second = service.submit(_request("second", scene=SceneData(scene_id="second")))
        third = service.submit(_request("third", scene=SceneData(scene_id="third")))
        assert first < second < third
        _FakeBackend.release_first.set()
        assert service.wait_idle(3.0)
        result = service.latest_result()
        assert result is not None
        assert result.generation_id == third
        assert result.request.frame_id == "third"
        assert _FakeBackend.calls == ["first", "third"]
        assert result.image.format() == QImage.Format.Format_RGBA8888_Premultiplied
    finally:
        _FakeBackend.release_first.set()
        service.shutdown()


def test_service_returns_no_gl_placeholder_without_losing_target_size() -> None:
    def unavailable() -> _FakeBackend:
        raise RuntimeError("test context unavailable")

    service = RenderService(backend_factory=unavailable)
    try:
        generation = service.submit(_request(scene=SceneData()))
        assert service.wait_idle(3.0)
        result = service.latest_result()
        assert result is not None and result.generation_id == generation
        assert not result.available
        assert result.error and "unavailable" in result.error
        assert result.image.size().toTuple() == (64, 48)
        assert result.image.format() == QImage.Format.Format_RGBA8888_Premultiplied
    finally:
        service.shutdown()


def test_cancel_suppresses_inflight_result() -> None:
    service = RenderService(backend_factory=_FakeBackend)
    try:
        service.submit(_request("first", scene=SceneData(scene_id="first")))
        assert _FakeBackend.started.wait(2.0)
        service.cancel()
        _FakeBackend.release_first.set()
        assert service.wait_idle(3.0)
        assert service.latest_result() is None
    finally:
        _FakeBackend.release_first.set()
        service.shutdown()


def test_context_failure_is_recreated_on_the_next_submission() -> None:
    class Backend:
        def __init__(self, should_fail: bool) -> None:
            self.should_fail = should_fail

        def render(self, scene: SceneData, size: tuple[int, int], options: object) -> QImage:
            if self.should_fail:
                raise RuntimeError("simulated context loss")
            image = QImage(size[0], size[1], QImage.Format.Format_RGBA8888_Premultiplied)
            image.fill(QColor(20, 40, 60, 255))
            return image

        def release(self) -> None:
            pass

    calls = 0

    def factory() -> Backend:
        nonlocal calls
        calls += 1
        return Backend(calls == 1)

    service = RenderService(backend_factory=factory)
    try:
        first = service.submit(_request("first", scene=SceneData()))
        assert service.wait_idle(3.0)
        assert service.latest_result() is not None
        assert service.latest_result().generation_id == first
        assert not service.latest_result().available
        second = service.submit(_request("second", scene=SceneData()))
        assert service.wait_idle(3.0)
        result = service.latest_result()
        assert result is not None and result.generation_id == second
        assert result.available and result.error is None
        assert service.available
        assert calls == 2
    finally:
        service.shutdown()
