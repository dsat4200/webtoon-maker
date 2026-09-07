import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QFileDialog, QMessageBox

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ImageObject, MirrorModifier, OutlineModifier,
    PathContour, PathNode, RasterObject, ShapeStyle, modifier_from_dict,
)
from comic_editor.core.settings import EditorSettings, load_settings, save_settings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.baking import apply_raster_modifiers, rasterize, rasterize_reason
from comic_editor.core.effect_geometry import reflection_transform
from comic_editor.ui.shape_outline import remove_nodes


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=128)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 128, 128))
    page.fill_color = None
    page.border_width = 0
    canvas = CanvasWidget(EditorSettings())
    tiles = TileStore()
    canvas.set_document(chapter, tiles)
    yield canvas, chapter, page
    canvas.deleteLater()


def pixels(image):
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(image.constBits(), dtype=np.uint8).reshape(image.height(), image.bytesPerLine())[:, :image.width()*4].copy()


def render(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def raster(scene):
    canvas, chapter, page = scene
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    canvas.tiles.paint_dab(obj.object_id, QPointF(25, 30), 10, QColor("red"), square=True, antialias=False)
    canvas.set_selection("object", obj.object_id)
    return obj


def test_model_outline_mirror_and_background_migration(scene):
    canvas, chapter, page = scene
    obj = raster(scene)
    modifier = MirrorModifier(axis_start=(64, 0), axis_end=(64, 128), compound_operation="subtract")
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    page.bound.nodes[0].outline_multiplier = 2.25
    page.bound.nodes[0].outline_enabled = False
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.modifiers[modifier.modifier_id].to_dict() == modifier.to_dict()
    assert restored.layers[page.layer_id].bound.nodes[0].outline_multiplier == 2.25
    assert not restored.layers[page.layer_id].bound.nodes[0].outline_enabled
    legacy = chapter.to_dict()
    legacy.update(schema_version=21, background="#FFFFFFFF")
    assert ChapterDocument.from_dict(legacy).background == "#00000000"
    legacy["background"] = "#FF123456"
    assert ChapterDocument.from_dict(legacy).background == "#FF123456"
    legacy.update(schema_version=22, background="#FFFFFFFF")
    assert ChapterDocument.from_dict(legacy).background == "#FFFFFFFF"
    assert PathNode.from_dict({"position": [0, 0]}).outline_multiplier == 1
    assert PathNode.from_dict({"position": [0, 0], "outline_multiplier": 100}).outline_multiplier == 10
    with pytest.raises(ValueError):
        modifier_from_dict({"type": "mirror", "axis_start": [1, 1], "axis_end": [1, 1]})


def test_transparent_partial_render_clears_old_pixels(scene):
    canvas, chapter, page = scene
    page.fill_color = "#80ff0000"
    image = render(canvas)
    assert image.pixelColor(60, 60).alpha() == 128
    page.fill_color = None
    canvas.render_preview(image, QRect(30, 30, 40, 40))
    assert image.pixelColor(60, 60).alpha() == 0
    assert image.pixelColor(90, 90).alpha() == 128


@pytest.mark.parametrize("start,end,point,expected", [
    ((64, 0), (64, 128), (25, 30), (103, 30)),
    ((0, 0), (100, 100), (25, 30), (30, 25)),
    ((0, 20), (100, 20), (25, 30), (25, 10)),
])
def test_reflection_axis(start, end, point, expected):
    actual = reflection_transform(MirrorModifier(axis_start=start, axis_end=end)).map(QPointF(*point))
    assert actual.toTuple() == pytest.approx(expected)


def test_mirror_render_mute_source_reuse_and_bounded_cache(scene):
    canvas, chapter, page = scene
    obj = raster(scene)
    mirror = MirrorModifier(axis_start=(64, 0), axis_end=(64, 128))
    chapter.add_modifier(mirror, [("object", obj.object_id)])
    image = render(canvas)
    assert image.pixelColor(25, 30) == QColor("red")
    assert image.pixelColor(103, 30) == QColor("red")
    assert set(np.unique(pixels(image)[:, 3::4])) <= {0, 255}
    source_keys = set(canvas._modifier_source_cache)
    mirror.axis_start, mirror.axis_end = (60, 0), (60, 128)
    assert render(canvas).pixelColor(95, 30) == QColor("red")
    assert set(canvas._modifier_source_cache) == source_keys
    assert canvas._modifier_render_cache_bytes <= canvas._modifier_render_cache_budget
    mirror.muted = True
    assert render(canvas).pixelColor(95, 30).alpha() == 0


def test_mirror_compound_contributes_when_original_ignored(scene):
    canvas, chapter, page = scene
    parent = chapter.add_layer(page.layer_id, "Compound", BoundGeometry.rectangle(5, 5, 10, 10))
    parent.compound_enabled = True
    child = chapter.add_layer(parent.layer_id, "Source", BoundGeometry.rectangle(20, 20, 10, 10))
    child.compound_operation = "ignore"
    mirror = MirrorModifier(axis_start=(64, 0), axis_end=(64, 128), compound_operation="add")
    chapter.add_modifier(mirror, [("layer", child.layer_id)])
    path = canvas.layer_effective_path(parent.layer_id)
    assert path.contains(QPointF(103, 25))
    assert not path.contains(QPointF(25, 25))
    assert "compound" in rasterize_reason(canvas, "layer", child.layer_id)
    mirror.muted = True
    canvas._compound_path_cache.clear()
    assert not canvas.layer_effective_path(parent.layer_id).contains(QPointF(103, 25))


def test_shape_outline_split_delete_metadata():
    bound = BoundGeometry.path([PathNode(x=10, y=10, outline_multiplier=2, outline_enabled=False), PathNode(x=50, y=10, outline_multiplier=4), PathNode(x=80, y=10)])
    node = CanvasWidget._split_shape_segment(bound, 0, .5)
    assert node.outline_multiplier == 3 and not node.outline_enabled
    bound.nodes.insert(1, node)
    contour = PathContour(bound.nodes, False)
    remove_nodes(contour, {node.node_id})
    assert not contour.nodes[0].outline_enabled
    assert len(contour.nodes) == 3


def test_shape_outline_baseline_and_hidden_edge(scene):
    canvas, chapter, page = scene
    bound = BoundGeometry.rectangle(20, 20, 60, 60)
    bound.primitive = "custom"
    shape = chapter.add_layer(page.layer_id, "Shape", bound, style=ShapeStyle(outline_thickness=4, outline_color="#ff0000"))
    bound.nodes[0].outline_multiplier = 2
    assert render(canvas).pixelColor(35, 22).alpha() > 0
    bound.nodes[0].outline_enabled = False
    assert render(canvas).pixelColor(50, 22).alpha() == 0
    shape.border_width = 0
    assert render(canvas).pixelColor(78, 50).alpha() == 0
    assert bound.nodes[0].outline_multiplier == 2


def test_raster_apply_prefix_retains_muted_shared_and_undo(scene):
    canvas, chapter, page = scene
    obj = raster(scene)
    other = chapter.add_object(page.layer_id, RasterObject())
    muted = OutlineModifier(muted=True, thickness=2)
    mirror = MirrorModifier(axis_start=(64, 0), axis_end=(64, 128))
    later = OutlineModifier(thickness=1)
    chapter.add_modifier(muted, [("object", obj.object_id)])
    chapter.add_modifier(mirror, [("object", obj.object_id), ("object", other.object_id)])
    chapter.add_modifier(later, [("object", obj.object_id)])
    before = pixels(render(canvas))
    apply_raster_modifiers(canvas, mirror.modifier_id)
    assert obj.modifier_ids == [muted.modifier_id, later.modifier_id]
    assert other.modifier_ids == [mirror.modifier_id]
    assert np.max(np.abs(pixels(render(canvas)).astype(int) - before.astype(int))) <= 1
    canvas.command_stack.undo()
    assert canvas.chapter.objects[obj.object_id].modifier_ids == [muted.modifier_id, mirror.modifier_id, later.modifier_id]
    assert np.array_equal(pixels(render(canvas)), before)
    canvas.command_stack.redo()
    assert canvas.chapter.objects[obj.object_id].modifier_ids == [muted.modifier_id, later.modifier_id]


def test_rasterize_image_undo_resources_and_opacity(scene):
    canvas, chapter, page = scene
    obj = raster(scene)
    obj.opacity_locked = False
    obj.opacity = .5
    page.opacity = .6
    mirror = MirrorModifier(axis_start=(64, 0), axis_end=(64, 128))
    chapter.add_modifier(mirror, [("object", obj.object_id)])
    before = pixels(render(canvas))
    rasterize(canvas, "object", obj.object_id)
    assert isinstance(chapter.objects[obj.object_id], ImageObject)
    assert not canvas.tiles.object_tiles(obj.object_id)
    assert np.max(np.abs(pixels(render(canvas)).astype(int) - before.astype(int))) <= 2
    canvas.command_stack.undo()
    assert isinstance(canvas.chapter.objects[obj.object_id], RasterObject)
    assert canvas.images.source(obj.object_id) is None
    assert np.array_equal(pixels(render(canvas)), before)
    canvas.command_stack.redo()
    assert isinstance(canvas.chapter.objects[obj.object_id], ImageObject)


def test_named_export_restart_repeat_cancel_and_failure(qapp, tmp_path, monkeypatch):
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.ui.main_window import MainWindow
    repository = SeriesRepository(tmp_path / "Series")
    series = repository.create("Export")
    repository.create_chapter(series, "Chapter")
    window = MainWindow()
    try:
        assert window.open_series(repository.root)
        destination = tmp_path / "named.png"
        dialogs = []
        def choose(*args):
            dialogs.append(args[2])
            return str(destination), "PNG"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
        window._export_again()
        assert destination.exists() and len(dialogs) == 1
        key = window._export_destination_key()
        assert load_settings().export_destinations[key] == str(destination)
        window._export_again()
        assert len(dialogs) == 1
        saved = destination.read_bytes()
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: ("", ""))
        window._export_as()
        assert destination.read_bytes() == saved
        monkeypatch.setattr(QMessageBox, "critical", lambda *a: None)
        monkeypatch.setattr(window.canvas, "render_preview", lambda *a: (_ for _ in ()).throw(ValueError("test")))
        window._export_again()
        assert destination.read_bytes() == saved
        assert window.settings.export_destinations[key] == str(destination)
    finally:
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.deleteLater()


def test_full_color_dialog_cancel_apply_and_eyedropper(scene, qapp, monkeypatch):
    from comic_editor.ui.main_window import MainWindow
    from comic_editor.ui.color_picker import ColorPickerPopup
    from comic_editor.ui import main_window as main_module
    monkeypatch.setattr(main_module, "save_settings", lambda settings: None)
    window = MainWindow()
    window.canvas.set_document(scene[1], scene[0].tiles)
    window.chapter = scene[1]
    before = window.color_panel.primary_color()
    applied = []
    popup = ColorPickerPopup("#80112233", window)
    popup.colorApplied.connect(applied.append)
    popup.open()
    qapp.processEvents()
    assert [popup.workspace.tabText(i) for i in range(3)] == ["Picker", "Palette", "History"]
    popup.workspace.panel.apply_color("#40123456")
    assert window.color_panel.primary_color() == before
    popup.reject()
    assert applied == [] and window.color_panel.primary_color() == before
    popup.setColor("#80112233")
    popup.open()
    qapp.processEvents()
    tool = window.canvas.tool
    popup._sample()
    assert not popup.isVisible() and window.canvas.tool == ToolKind.EYEDROPPER
    window._eyedropper_preview("#40abcdef")
    assert window.color_panel.primary_color() == before
    window._eyedropper_commit("#40abcdef")
    assert popup.isVisible() and window.canvas.tool == tool
    assert popup.color_argb() == "#40ABCDEF"
    popup.accept()
    assert applied == ["#40ABCDEF"]
    assert window.color_panel.primary_color() == "#40ABCDEF"
    window._dirty = False
    window.deleteLater()


def test_mirror_handle_selection_snapping_and_dirty(scene):
    from PySide6.QtCore import QRectF
    from comic_editor.core.models import GridSettings
    canvas, chapter, page = scene
    obj = raster(scene)
    mirror = MirrorModifier(axis_start=(60, 0), axis_end=(60, 100))
    chapter.add_modifier(mirror, [("object", obj.object_id)])
    canvas.modifier_mode = True
    canvas.active_modifier_id = mirror.modifier_id
    canvas.settings.snap_to_grid = True
    page.grid_override = GridSettings(size=40, divisions=4)
    start = canvas.camera_transform().map(QPointF(60, 50))
    assert canvas._begin_modifier_handle(start)
    assert canvas._move_modifier_handle(canvas.camera_transform().map(QPointF(74, 57)))
    assert mirror.axis_start == (70, 10)
    assert mirror.axis_end == (70, 110)
    assert canvas._finish_modifier_handle()
    expanded = canvas.modifier_expanded_dirty(obj.object_id, QRectF(20, 20, 10, 10))
    assert expanded.contains(QPointF(115, 25))
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[mirror.modifier_id].axis_start == (60, 0)
    canvas.modifier_mode = True
    canvas.active_modifier_id = mirror.modifier_id
    canvas.clear_selection()
    assert canvas.active_modifier_id == ""


def test_outline_handle_gesture_and_reset(scene):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent
    canvas, chapter, page = scene
    shape = chapter.add_layer(page.layer_id, "Shape", BoundGeometry.rectangle(20, 20, 60, 60))
    shape.bound.primitive = "custom"
    canvas.set_selection("layer", shape.layer_id)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    node = shape.bound.nodes[0]
    canvas._selected_shape_node_id = node.node_id
    handle = canvas._shape_gizmo_positions(shape.bound, node)["outline_width"]
    assert canvas._begin_shape_edit(handle)
    assert canvas._active_shape_control == "outline_width"
    direction = handle - QPointF(*node.position)
    canvas._update_shape_edit(handle + direction / 7)
    assert node.outline_multiplier == pytest.approx(2)
    point = canvas.camera_transform().map(canvas._shape_gizmo_positions(shape.bound, node)["outline_width"])
    event = QMouseEvent(QEvent.MouseButtonDblClick, point, point, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    canvas.mouseDoubleClickEvent(event)
    assert node.outline_multiplier == 1


def test_apply_transformed_raster_extends_pixels_without_changing_mapping(scene):
    canvas, chapter, page = scene
    obj = raster(scene)
    obj.transform_frame = (0, 0, 128, 128)
    obj.transform_quad = [(10, 5), (138, 5), (138, 133), (10, 133)]
    mirror = MirrorModifier(axis_start=(64, 0), axis_end=(64, 128))
    chapter.add_modifier(mirror, [("object", obj.object_id)])
    before = pixels(render(canvas))
    frame, quad = obj.transform_frame, list(obj.transform_quad)
    apply_raster_modifiers(canvas, mirror.modifier_id)
    assert obj.transform_frame == frame and obj.transform_quad == quad
    assert np.array_equal(pixels(render(canvas)), before)


def test_modifier_card_selection_controls_and_apply_visibility(scene, qapp):
    from PySide6.QtTest import QTest
    from comic_editor.ui.modifier_controls import ModifierControls, ModifierTitleBar
    canvas, chapter, page = scene
    obj = raster(scene)
    canvas.modifier_mode = True
    controls = ModifierControls(canvas)
    controls.add_modifier("mirror")
    mid = canvas.active_modifier_id
    assert isinstance(chapter.modifiers[mid], MirrorModifier)
    card = controls._cards[mid]
    assert "#65bcff" in card.styleSheet()
    assert card.apply_button.isEnabled()
    controls.show()
    qapp.processEvents()
    QTest.mouseClick(card.findChild(ModifierTitleBar), Qt.LeftButton)
    assert canvas.active_modifier_id == ""
    controls.toggle_modifier(mid)
    assert canvas.active_modifier_id == mid
    controls.set_parameter(mid, "muted", True, True)
    assert canvas.active_modifier_id == mid
    assert not controls._cards[mid].apply_button.isEnabled()
    assert canvas._active_mirror_modifier() is None
    controls.remove_modifier(mid)
    assert canvas.active_modifier_id == ""
    controls.deleteLater()


def test_mirror_asset_copy_axis_and_effect_bounds(scene):
    from comic_editor.core.assets import extract_asset, instantiate_asset
    canvas, chapter, page = scene
    obj = raster(scene)
    mirror = MirrorModifier(axis_start=(64, 0), axis_end=(64, 128))
    chapter.add_modifier(mirror, [("object", obj.object_id)])
    manifest, tiles = extract_asset(chapter, canvas.tiles, "object", obj.object_id, "Mirror")
    assert manifest.visual_bounds[2] >= 80
    copied = next(m for m in manifest.document.modifiers.values() if isinstance(m, MirrorModifier))
    assert copied.modifier_id != mirror.modifier_id
    bx, by, bw, bh = manifest.visual_bounds
    _kind, identifier, _ids = instantiate_asset(manifest, tiles, chapter, canvas.tiles, page.layer_id, 64, 60)
    placed = chapter.modifiers[chapter.objects[identifier].modifier_ids[0]]
    assert placed.axis_start == pytest.approx((copied.axis_start[0] + 64 - bx - bw / 2, copied.axis_start[1] + 60 - by - bh / 2))


def test_mirror_on_compound_contributor_reflects_child_pixels(scene):
    canvas, chapter, page = scene
    parent = chapter.add_layer(page.layer_id, "Compound", BoundGeometry.rectangle(5, 5, 5, 5))
    parent.compound_enabled = True
    child = chapter.add_layer(parent.layer_id, "Source", BoundGeometry.rectangle(20, 20, 10, 10))
    obj = chapter.add_object(child.layer_id, RasterObject())
    canvas.tiles.paint_dab(obj.object_id, QPointF(25, 25), 8, QColor("red"), square=True, antialias=False)
    mirror = MirrorModifier(axis_start=(64, 0), axis_end=(64, 128), compound_operation="add")
    chapter.add_modifier(mirror, [("layer", child.layer_id)])
    image = render(canvas)
    assert image.pixelColor(25, 25) == QColor("red")
    assert image.pixelColor(103, 25) == QColor("red")


def test_compound_outline_keeps_hidden_surviving_source_edge(scene):
    canvas, chapter, page = scene
    parent = chapter.add_layer(page.layer_id, "Compound", BoundGeometry.rectangle(20, 20, 60, 60), style=ShapeStyle(outline_thickness=3))
    parent.compound_enabled = True
    parent.bound.primitive = "custom"
    parent.bound.nodes[0].outline_enabled = False
    image = render(canvas)
    assert image.pixelColor(50, 20).alpha() == 0
    assert image.pixelColor(20, 50).alpha() > 0


def test_shape_rasterize_subtree_one_undo_and_atomic_failure(scene, monkeypatch):
    from comic_editor.ui import baking
    canvas, chapter, page = scene
    shape = chapter.add_layer(page.layer_id, "Shape", BoundGeometry.rectangle(20, 20, 60, 60), style=ShapeStyle(primary_color="#8000ff00", outline_thickness=3))
    obj = chapter.add_object(shape.layer_id, RasterObject())
    canvas.tiles.paint_dab(obj.object_id, QPointF(30, 30), 6, QColor("red"))
    canvas.set_selection("layer", shape.layer_id)
    before = pixels(render(canvas))
    rasterize(canvas, "layer", shape.layer_id)
    assert obj.object_id not in chapter.objects and shape.layer_id not in chapter.layers
    assert np.max(np.abs(pixels(render(canvas)).astype(int) - before.astype(int))) <= 2
    canvas.command_stack.undo()
    assert obj.object_id in canvas.chapter.objects and shape.layer_id in canvas.chapter.layers
    state = canvas.chapter.to_dict()
    monkeypatch.setattr(baking, "empty_image", lambda *_: (_ for _ in ()).throw(MemoryError("test")))
    with pytest.raises(MemoryError):
        rasterize(canvas, "layer", shape.layer_id)
    assert canvas.chapter.to_dict() == state
    assert np.array_equal(pixels(render(canvas)), before)


def test_distant_mirror_uses_small_source_and_clipped_output(scene):
    canvas, chapter, page = scene
    obj = raster(scene)
    obj.opacity_locked = False
    obj.opacity = .5
    mirror = MirrorModifier(axis_start=(500000, 0), axis_end=(500000, 128))
    chapter.add_modifier(mirror, [("object", obj.object_id)])
    for offset in range(5):
        mirror.axis_start = (500000 + offset, 0)
        mirror.axis_end = (500000 + offset, 128)
        image = render(canvas)
        assert 125 <= image.pixelColor(25, 30).alpha() <= 129
    assert len(canvas._modifier_source_cache) == 1
    assert max(image.sizeInBytes() for image in canvas._modifier_source_cache.values()) < 1024 * 1024
    assert max(image.sizeInBytes() for image in canvas._modifier_render_cache.values()) < 1024 * 1024
