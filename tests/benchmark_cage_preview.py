"""Isolate the actual cage preview kernel with saved Pocket Boyfriend sources.

--baseline runs the preserved preceding method against the same immutable
inputs. This measures cage preparation and sampling, not a complete scene frame.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import time
from types import MethodType

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project-copy', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--baseline', type=Path)
parser.add_argument('--repeats', type=int, default=3)
parser.add_argument('--native-scene', action='store_true', help='Render the ordinary whole-scene native preview demand.')
parser.add_argument('--raster-id', help='Limit to one of the saved benchmark drawings.')
args = parser.parse_args()
project, output = args.project_copy.resolve(), args.output.resolve()
assert project.is_relative_to(ROOT/'.artifacts') and (project/'series.json').is_file()
assert output.is_relative_to(ROOT/'.artifacts') and not output.exists()
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'windows' if os.name == 'nt' else 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.render import scene_kernels
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.live_canvas_preview import LiveCanvasPreviewPolicy
from comic_editor.render.pixels import pixel_scope
from comic_editor.render.service import DocumentRenderService, RenderQuality, RenderRequest
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def manifest():
    return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in project.rglob('*') if path.is_file()}


app = QApplication.instance() or QApplication([])
before = manifest()
repository = SeriesRepository(project)
baseline = None
if args.baseline:
    namespace = dict(scene_kernels.__dict__)
    exec(args.baseline.read_text(encoding='utf8'), namespace)
    baseline = namespace['_cage_object_preview']
rows = []
for identifier in ((args.raster_id,) if args.raster_id else
                   ('49a55205999a431da9e5c95cb06c8bba', '0f93e13ec31046dfa9b4bf83101fb22a')):
    for repeat in range(args.repeats):
        chapter, tiles, images = repository.load_chapter('0a72f08009294aa0a3d14e6a38e22bbb', include_images=True)
        obj = chapter.objects[identifier]
        canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', snap_to_grid=False))
        canvas.resize(955, 927)
        canvas.set_document(chapter, tiles, images)
        started = time.perf_counter()
        canvas.set_selection('object', identifier)
        selection_ms = (time.perf_counter()-started)*1000
        bounds = QRectF(*obj.interaction_rect)
        # Exercise the real rectangle and lasso path creation before the cage.
        selection_times = {}
        for kind in (ToolKind.DRAW_SELECT_RECT, ToolKind.DRAW_SELECT_LASSO):
            canvas.set_tool(kind)
            started = time.perf_counter()
            corners = [bounds.topLeft(), bounds.topRight(), bounds.bottomRight(), bounds.bottomLeft()]
            points = [canvas._raster_world_point(obj, point) for point in corners]
            assert canvas._begin_drawing_selection(points[0], canvas.document_to_widget(points[0]), test_transform=False)
            for point in points[1:]:
                assert canvas._continue_drawing_selection(point, canvas.document_to_widget(point))
            assert canvas._finish_drawing_selection()
            selection_times[kind.value] = (time.perf_counter()-started)*1000
        assert canvas.set_tool(ToolKind.CAGE_TRANSFORM)
        deadline = time.monotonic()+20
        while canvas._cage_session is None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.001)
        assert canvas._cage_session is not None, canvas._cage_commit_error
        cage = canvas._cage_session['grid']
        cage.points[5] = (cage.points[5][0]+30, cage.points[5][1]-15)
        canvas._cage_changed()
        document = canvas._render_document_state()
        assert document.live_preview
        capture = canvas._scene_snapshot_compiler.capture(canvas, document)
        while not capture.advance(.004):
            pass
        assert not capture.stale
        backend = DetachedSceneBackend(capture.result)
        scene = backend.scene
        scene._live_canvas_preview_policy = LiveCanvasPreviewPolicy(document.identity, document.revision, 11, lambda: False)
        if baseline is not None:
            scene._cage_object_preview = MethodType(baseline, scene)
        original = scene._render_object_content
        source_times = []
        def source(*arguments, **keywords):
            started = time.perf_counter()
            result = original(*arguments, **keywords)
            source_times.append((time.perf_counter()-started)*1000)
            return result
        scene._render_object_content = source
        surface = QImage(224, 224, QImage.Format_ARGB32_Premultiplied)
        surface.fill(Qt.transparent)
        painter = QPainter(surface)
        scale = 224/max(bounds.width(), bounds.height())
        painter.scale(scale, scale)
        painter.translate(-bounds.x()-obj.x, -bounds.y()-obj.y)
        row = dict(name=obj.name, identifier=identifier, repeat=repeat, selection_ms=selection_ms,
                   drawing_selection_ms=selection_times, source_frame=list(bounds.getRect()))
        started = time.perf_counter()
        try:
            with pixel_scope(document.pixel_contract, environment=capture.result.pixel_environment):
                if args.native_scene:
                    backend.native_preview, backend.artwork_scale = True, 1.
                    backend.live_canvas_preview_policy = scene._live_canvas_preview_policy
                    service = DocumentRenderService(backend)
                    service.projection.revision = document.revision
                    world = scene.object_world_rect(identifier)
                    request = RenderRequest((world.left(), world.top(), 512., 384.), 1., (512, 384),
                        ('detached-preview', 11), document.revision, quality=RenderQuality.INTERACTIVE)
                    result = service.render_region(document, request)
                    row['render_status'], row['exact'] = result.status.value, result.exact
                    assert not result.image.isNull()
                else:
                    assert scene._cage_object_preview(painter, chapter.objects[identifier], 1., bounds)
            row['error'] = None
        except ValueError as error:
            row['error'] = str(error)
        finally:
            row['preview_ms'] = (time.perf_counter()-started)*1000
            row['source_ms'] = sum(source_times)
            row['source_dimensions'] = [image.size().toTuple() for image, _bounds in scene._cage_session['sources'].values()]
            row['cached_output_dimensions'] = [image.size().toTuple() for image in scene._modifier_render_cache.values()]
            painter.end()
            output.parent.mkdir(parents=True, exist_ok=True)
            assert surface.save(str(output.with_name(f'{output.stem}-{obj.name.replace(" ", "-")}-{repeat}.png')))
            backend.close()
            canvas.finish_cage(False)
            canvas._scene_consumers.shutdown()
            canvas.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
        rows.append(row)
assert manifest() == before
report = dict(rows=rows, project_files=len(before), input_files_unchanged=True,
              baseline=bool(baseline), native_scene=args.native_scene, platform=os.environ['QT_QPA_PLATFORM'])
output.write_text(json.dumps(report, indent=2), encoding='utf8')
print(json.dumps(report, indent=2))
