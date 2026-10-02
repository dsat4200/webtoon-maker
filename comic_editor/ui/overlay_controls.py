"""Texture and solid-color overlay controls."""
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QTimer, QSignalBlocker
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget, QDialog

from comic_editor.core.models import LayerNode, TextureModifier, OVERLAY_BLEND_MODES
from comic_editor.core.texture_library import import_texture
from comic_editor.ui.texture_picker import TextureCombo


class OverlayControls(QWidget):
    def __init__(self, modifier, owner, parent=None):
        super().__init__(parent)
        self.modifier, self.owner = modifier, owner
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel("Blend mode", self))
        self.blend = QComboBox(self)
        self.blend.setObjectName("overlayBlendMode")
        for mode, label in OVERLAY_BLEND_MODES:
            self.blend.addItem(label, mode)
        self.blend.setCurrentIndex(self.blend.findData(modifier.blend_mode))
        self.blend.currentIndexChanged.connect(lambda: owner.set_parameter(
            modifier.modifier_id, "blend_mode", self.blend.currentData(), True))
        layout.addWidget(self.blend)
        if isinstance(modifier, TextureModifier):
            self.texture = TextureCombo(lambda: owner.canvas.settings.texture_directory,
                                        modifier.texture_name, modifier.texture_category, self)
            self.texture.textureSelected.connect(self._import)
            layout.addWidget(self.texture)
            layout.addWidget(QLabel("Texture transform", self))
            self.transform_mode = QComboBox(self)
            self.transform_mode.setObjectName("textureTransformMode")
            self.transform_mode.addItem("Uniform", "uniform")
            self.transform_mode.addItem("Free", "free")
            self.transform_mode.setCurrentIndex(self.transform_mode.findData(modifier.transform_mode))
            self.transform_mode.currentIndexChanged.connect(lambda: owner.set_parameter(
                modifier.modifier_id, "transform_mode", self.transform_mode.currentData(), True))
            owner.canvas.interactionFinished.connect(self._sync_transform_mode)
            layout.addWidget(self.transform_mode)
            self.fit = QPushButton("Fit to layer", self)
            self.fit.setObjectName("textureFitToLayer")
            self.fit.clicked.connect(lambda: owner.set_parameter(modifier.modifier_id, "texture_quad",
                owner.canvas._texture_default_quad(modifier), True))
            layout.addWidget(self.fit)
            hint = QLabel("Select this modifier for texture handles. Drag inside to move; outside a corner to rotate.", self)
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.folder = QPushButton("Library folder…", self)
            self.folder.setToolTip("Set the texture library folder in Settings → Paths")
            self.folder.clicked.connect(self._paths)
            layout.addWidget(self.folder)
            self.message = QLabel(self)
            self.message.setWordWrap(True)
            layout.addWidget(self.message)
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="texture-import")
            pool = self._pool
            self.destroyed.connect(lambda: pool.shutdown(wait=False, cancel_futures=True))
            self._future = None
            self._timer = QTimer(self)
            self._timer.setInterval(40)
            self._timer.timeout.connect(self._finish_import)
        else:
            self.color = QPushButton(self)
            self.color.setObjectName("overlayColor")
            self._set_color(modifier.color)
            self.color.clicked.connect(self._choose_color)
            layout.addWidget(QLabel("Color", self))
            layout.addWidget(self.color)
        self.outline = QCheckBox("Apply to outline", self)
        self.outline.setObjectName("overlayApplyToOutline")
        self.outline.setChecked(modifier.apply_to_outline)
        self.outline.toggled.connect(lambda checked: owner.set_parameter(
            modifier.modifier_id, "apply_to_outline", checked, True))
        self.outline.setVisible(any(isinstance(owner.canvas.chapter.modifier_target(*ref), LayerNode)
                                   for ref in owner.canvas.chapter.modifier_target_ids(modifier.modifier_id)))
        layout.addWidget(self.outline)

    def _paths(self):
        from comic_editor.ui.settings_dialog import SettingsDialog
        from comic_editor.core.settings import save_settings
        dialog = SettingsDialog(self.owner.canvas.settings, parent=self)
        dialog.tabs.setCurrentWidget(dialog.paths_tab)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply_user_settings(self.owner.canvas.settings)
            save_settings(self.owner.canvas.settings)
            self.owner.canvas.refresh_grid_settings()

    def _import(self, path, category):
        if self._future is not None:
            return
        self._chapter = self.owner.canvas.chapter
        self._path, self._category = path, category
        self.texture.setEnabled(False)
        self.message.setText("Loading texture…")
        self._future = self._pool.submit(import_texture, path)
        self._timer.start()

    def _finish_import(self):
        if self._future is None or not self._future.done():
            return
        self._timer.stop()
        future, self._future = self._future, None
        self.texture.setEnabled(True)
        self.message.clear()
        try:
            data = future.result()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Texture", f"Could not load {self._path.name}:\n{error}")
            return
        if self.owner.canvas.chapter is not self._chapter or self._chapter.modifiers.get(self.modifier.modifier_id) is not self.modifier:
            return
        before = self._chapter.to_dict()
        self.modifier.texture_data = data
        self.modifier.texture_name = self._path.name
        self.modifier.texture_category = self._category
        if self.modifier.texture_quad is None:
            self.modifier.texture_quad = self.owner.canvas._texture_default_quad(self.modifier)
        self.owner._changed()
        self.owner._push(before, "Choose texture")
        self.texture.texture_name, self.texture.category = self._path.name, self._category
        self.texture.setItemText(0, self._path.name)

    def _set_color(self, value):
        color = QColor(value)
        self.color.setText(value)
        self.color.setStyleSheet(f"background-color: {color.name()}; color: {'#111111' if color.lightnessF() > .6 else '#ffffff'};")

    def _sync_transform_mode(self):
        blocker = QSignalBlocker(self.transform_mode)
        self.transform_mode.setCurrentIndex(self.transform_mode.findData(self.modifier.transform_mode))

    def _choose_color(self):
        from comic_editor.ui.color_picker import choose_color
        def apply(value):
            self._set_color(value)
            self.owner.set_parameter(self.modifier.modifier_id, "color", value, True)
        self._popup = choose_color(self, self.modifier.color, apply, "Overlay color")
