"""Parameters shared by the three painterly Kuwahara modifiers."""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QDoubleSpinBox, QSpinBox

from comic_editor.core.kuwahara import KUWAHARA_VARIANTS


class KuwaharaControls(QWidget):
    def __init__(self, modifier, owner, card, parent=None):
        super().__init__(parent)
        self.setObjectName("kuwaharaControls")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        def combo(label, attribute, values, tooltip, refresh=False):
            row = QWidget(self)
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(QLabel(label, row))
            control = QComboBox(row)
            control.setObjectName("kuwahara_"+attribute)
            for value, title in values:
                control.addItem(title, value)
            control.setCurrentIndex(max(0, control.findData(getattr(modifier, attribute))))
            control.setToolTip(tooltip)
            def change(_index):
                owner.set_parameter(modifier.modifier_id, attribute, control.currentData(), True)
                if refresh:
                    owner.refresh()
            control.currentIndexChanged.connect(change)
            line.addWidget(control, 1)
            layout.addWidget(row)
            return control

        def numeric(label, attribute, minimum, maximum, tooltip, step=1.):
            row = QWidget(self)
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(QLabel(label, row))
            control = QSpinBox(row) if attribute == "iterations" else QDoubleSpinBox(row)
            control.setObjectName("kuwahara_"+attribute)
            control.setRange(minimum, maximum)
            control.setValue(getattr(modifier, attribute))
            control.setSingleStep(step)
            control.setKeyboardTracking(False)
            control.setToolTip(tooltip)
            control.valueChanged.connect(lambda value: owner.set_parameter(
                modifier.modifier_id, attribute, value, True))
            line.addWidget(control, 1)
            layout.addWidget(row)

        combo("Filter", "variant", list(KUWAHARA_VARIANTS.items()),
              "Original uses four square regions. Papari uses eight smooth circular sectors. "
              "Anisotropic follows the local image direction with elliptical sectors.", True)
        radius = card._slider_row("Size", 0, 64, round(modifier.size), "size", " px")
        radius.setToolTip("Sampling radius in pixels. A size mask changes the actual filter footprint; "
                          "zero keeps the original detail. Use limited gradients in mask edit mode.")
        layout.addWidget(radius)
        layout.addWidget(card._slider_row("Strength", 0, 100, round(modifier.strength), "strength", "%"))
        if modifier.variant != "original":
            numeric("Sharpness", "sharpness", 1., 16., "How strongly the least-variable sector wins; higher values keep sharper edges.")
            numeric("Hardness", "hardness", 1., 100., "Variance sensitivity. Higher values favor flatter regions.")
            numeric("Sector overlap", "overlap", 1., 100., "Blend between neighboring sectors to soften sector boundaries.")
        if modifier.variant == "anisotropic":
            numeric("Anisotropy", "anisotropy", 0., 100., "Elongate filter regions along image features. Zero uses circular regions.")
            numeric("Direction smoothing", "tensor_radius", .25, 8., "Gaussian radius used to smooth the structure tensor, in pixels.", .25)
        numeric("Passes", "iterations", 1, 4, "Repeat the filter for stronger abstraction. Each pass adds processing time.", 1)
        if modifier.variant != "original":
            combo("Sampling", "quality", [("draft", "Draft · 49 samples"),
                  ("balanced", "Balanced · 113 samples"), ("high", "High · 317 samples")],
                  "More samples reduce sparse-sampling artifacts at large sizes and increase processing time. "
                  "This setting is also used for exports.")
        combo("Resolution", "processing_scale", [(100., "100% · Full"), (50., "50% · Half"), (25., "25% · Quarter")],
              "Filter at a reduced resolution to trade fine detail for speed. Size stays in original pixels. "
              "The same resolution is used for preview, baking and export.")
        hint = QLabel("Use the mask button beside Size or Strength, then add a limited linear or radial gradient in mask edit mode.", self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(hint)
