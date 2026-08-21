"""Reusable horizontal or vertical ribbon controls.

The editor intentionally keeps the ribbon independent from any document model.
Callers own the page contents and decide when contextual pages are visible.
"""
from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class RibbonGroup(QFrame):
    """A titled column of related ribbon controls."""

    def __init__(
        self, title: str, parent: QWidget | None = None,
        *, orientation: Qt.Orientation = Qt.Orientation.Horizontal,
    ):
        super().__init__(parent)
        self.setObjectName("ribbonGroup")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        vertical = orientation == Qt.Orientation.Vertical
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding
            if vertical else QSizePolicy.Policy.Minimum,
            QSizePolicy.Policy.Minimum
            if vertical else QSizePolicy.Policy.Expanding,
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 2)
        outer.setSpacing(2)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self.collapse_button = QToolButton(self)
        self.collapse_button.setAutoRaise(True)
        self.collapse_button.setFixedSize(16, 16)
        self.collapse_button.setArrowType(Qt.DownArrow)
        self.collapse_button.clicked.connect(self._toggle_collapsed)
        header.addWidget(self.collapse_button)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("ribbonGroupTitle")
        self.title_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.title_label, 1)
        outer.addLayout(header)
        self._collapsed = False

        self.content = QWidget(self)
        self.content.setObjectName("ribbonGroupContent")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(4)
        outer.addWidget(self.content, 1)

    @property
    def title(self) -> str:
        return self.title_label.text()

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)

    def add_widget(
        self, widget: QWidget, stretch: int = 0, alignment: Qt.AlignmentFlag = Qt.Alignment()
    ) -> None:
        self.content_layout.addWidget(widget, stretch, alignment)

    def add_layout(self, layout: QLayout, stretch: int = 0) -> None:
        self.content_layout.addLayout(layout, stretch)

    def add_stretch(self, stretch: int = 1) -> None:
        self.content_layout.addStretch(stretch)

    def _toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        self.content.setVisible(not self._collapsed)
        self.collapse_button.setArrowType(Qt.RightArrow if self._collapsed else Qt.DownArrow)

    def set_collapsed(self, collapsed: bool) -> None:
        if bool(collapsed) != self._collapsed:
            self._toggle_collapsed()


class RibbonPage(QScrollArea):
    """A ribbon page whose groups scroll along the ribbon orientation."""

    def __init__(
        self, key: str, title: str, parent: QWidget | None = None,
        *, orientation: Qt.Orientation = Qt.Orientation.Horizontal,
    ):
        super().__init__(parent)
        self.key = key
        self.title = title
        self.orientation = orientation
        self.setObjectName("ribbonPage")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        vertical = orientation == Qt.Orientation.Vertical
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            if vertical else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if vertical else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        self.groups_container = QWidget(self)
        self.groups_container.setObjectName("ribbonPageGroups")
        self.groups_container.setSizePolicy(
            QSizePolicy.Policy.Expanding
            if vertical else QSizePolicy.Policy.Minimum,
            QSizePolicy.Policy.Minimum
            if vertical else QSizePolicy.Policy.Expanding,
        )
        layout_type = QVBoxLayout if vertical else QHBoxLayout
        self.groups_layout = layout_type(self.groups_container)
        self.groups_layout.setContentsMargins(4, 3, 4, 3)
        self.groups_layout.setSpacing(4)
        self.groups_layout.setSizeConstraint(
            QLayout.SizeConstraint.SetMinimumSize
        )
        self.groups_layout.addStretch(1)
        self.setWidget(self.groups_container)

    def add_group(
        self, title: str, *, minimum_width: int | None = None
    ) -> RibbonGroup:
        group = RibbonGroup(
            title, self.groups_container, orientation=self.orientation
        )
        if (
            minimum_width is not None
            and self.orientation == Qt.Orientation.Horizontal
        ):
            group.setMinimumWidth(minimum_width)
        self.groups_layout.insertWidget(self.groups_layout.count() - 1, group)
        return group

    def insert_group(
        self, index: int, title: str, *, minimum_width: int | None = None
    ) -> RibbonGroup:
        group = RibbonGroup(
            title, self.groups_container, orientation=self.orientation
        )
        if (
            minimum_width is not None
            and self.orientation == Qt.Orientation.Horizontal
        ):
            group.setMinimumWidth(minimum_width)
        maximum = max(0, self.groups_layout.count() - 1)
        self.groups_layout.insertWidget(max(0, min(index, maximum)), group)
        return group

    def groups(self) -> list[RibbonGroup]:
        result: list[RibbonGroup] = []
        for index in range(max(0, self.groups_layout.count() - 1)):
            item = self.groups_layout.itemAt(index)
            widget = item.widget()
            if isinstance(widget, RibbonGroup):
                result.append(widget)
        return result


class RibbonWidget(QWidget):
    """Tabbed ribbon with stable keys and optional contextual pages."""

    pageChanged = Signal(str)
    pageVisibilityChanged = Signal(str, bool)

    def __init__(
        self, parent: QWidget | None = None,
        *, orientation: Qt.Orientation = Qt.Orientation.Horizontal,
    ):
        super().__init__(parent)
        self.setObjectName("ribbon")
        self.orientation = orientation
        self.setProperty(
            "orientation",
            "vertical"
            if orientation == Qt.Orientation.Vertical else "horizontal",
        )
        self._pages: list[RibbonPage] = []
        self._visible: dict[str, bool] = {}
        self._tab_keys: list[str] = []

        vertical = orientation == Qt.Orientation.Vertical
        layout_type = QHBoxLayout if vertical else QVBoxLayout
        layout = layout_type(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tab_bar = QTabBar(self)
        self.tab_bar.setObjectName("ribbonTabs")
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setUsesScrollButtons(True)
        if vertical:
            self.tab_bar.setShape(QTabBar.Shape.RoundedEast)
        self.tab_bar.currentChanged.connect(self._tab_changed)

        self.pages_stack = QStackedWidget(self)
        self.pages_stack.setObjectName("ribbonPageStack")
        if vertical:
            layout.addWidget(self.pages_stack, 1)
            layout.addWidget(self.tab_bar)
        else:
            layout.addWidget(self.tab_bar)
            layout.addWidget(self.pages_stack, 1)
    def add_page(
        self, key: str, title: str, *, visible: bool = True
    ) -> RibbonPage:
        if not key or self.page(key) is not None:
            raise ValueError(f"Ribbon page key must be unique: {key!r}")
        page = RibbonPage(
            key, title, self.pages_stack, orientation=self.orientation
        )
        self._pages.append(page)
        self._visible[key] = bool(visible)
        self.pages_stack.addWidget(page)
        self._rebuild_tabs(preferred_key=self.current_key())
        return page

    def remove_page(self, key: str) -> RibbonPage | None:
        page = self.page(key)
        if page is None:
            return None
        current = self.current_key()
        self._pages.remove(page)
        self._visible.pop(key, None)
        self.pages_stack.removeWidget(page)
        page.setParent(None)
        self._rebuild_tabs(
            preferred_key=current if current != key else None
        )
        return page

    def page(self, key: str) -> RibbonPage | None:
        return next((page for page in self._pages if page.key == key), None)

    def page_keys(self, *, visible_only: bool = False) -> list[str]:
        return [
            page.key
            for page in self._pages
            if not visible_only or self._visible.get(page.key, False)
        ]

    def set_page_visible(self, key: str, visible: bool) -> None:
        if self.page(key) is None:
            raise KeyError(key)
        visible = bool(visible)
        if self._visible.get(key) == visible:
            return
        current = self.current_key()
        self._visible[key] = visible
        self._rebuild_tabs(
            preferred_key=current if current != key or visible else None
        )
        self.pageVisibilityChanged.emit(key, visible)

    def is_page_visible(self, key: str) -> bool:
        if self.page(key) is None:
            raise KeyError(key)
        return self._visible.get(key, False)

    def current_key(self) -> str | None:
        index = self.tab_bar.currentIndex()
        if 0 <= index < len(self._tab_keys):
            return self._tab_keys[index]
        return None

    def select_page(self, key: str) -> bool:
        if key not in self._tab_keys:
            return False
        self.tab_bar.setCurrentIndex(self._tab_keys.index(key))
        return True

    def set_visible_pages(self, keys: Iterable[str]) -> None:
        requested = set(keys)
        unknown = requested.difference(self.page_keys())
        if unknown:
            raise KeyError(next(iter(unknown)))
        old = dict(self._visible)
        current = self.current_key()
        for page in self._pages:
            self._visible[page.key] = page.key in requested
        self._rebuild_tabs(
            preferred_key=current if current in requested else None
        )
        for page in self._pages:
            if old.get(page.key) != self._visible[page.key]:
                self.pageVisibilityChanged.emit(
                    page.key, self._visible[page.key]
                )

    def _rebuild_tabs(self, preferred_key: str | None) -> None:
        self.tab_bar.blockSignals(True)
        while self.tab_bar.count():
            self.tab_bar.removeTab(0)
        self._tab_keys = []
        for page in self._pages:
            if not self._visible.get(page.key, False):
                continue
            self._tab_keys.append(page.key)
            self.tab_bar.addTab(page.title)
        self.tab_bar.blockSignals(False)

        if not self._tab_keys:
            self.pages_stack.setCurrentIndex(-1)
            return
        selected = (
            preferred_key
            if preferred_key in self._tab_keys
            else self._tab_keys[0]
        )
        index = self._tab_keys.index(selected)
        self.tab_bar.setCurrentIndex(index)
        self._show_page(selected, emit=False)

    def _tab_changed(self, index: int) -> None:
        if not 0 <= index < len(self._tab_keys):
            return
        self._show_page(self._tab_keys[index], emit=True)

    def _show_page(self, key: str, *, emit: bool) -> None:
        page = self.page(key)
        if page is None:
            return
        self.pages_stack.setCurrentWidget(page)
        if emit:
            self.pageChanged.emit(key)


class VerticalTabWidget(QWidget):
    """Compact inspector tab host using the vertical ribbon appearance."""

    currentChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ribbon")
        self.setProperty("orientation", "vertical")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.pages_stack = QStackedWidget(self)
        self.pages_stack.setObjectName("ribbonPageStack")
        self.tab_bar = QTabBar(self)
        self.tab_bar.setObjectName("ribbonTabs")
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setUsesScrollButtons(True)
        self.tab_bar.setShape(QTabBar.Shape.RoundedEast)
        layout.addWidget(self.pages_stack, 1)
        layout.addWidget(self.tab_bar)
        self.tab_bar.currentChanged.connect(self._set_index)

    def addTab(self, page: QWidget, title: str) -> int:  # noqa: N802
        index = self.pages_stack.addWidget(page)
        self.tab_bar.addTab(str(title))
        if self.tab_bar.currentIndex() < 0:
            self.tab_bar.setCurrentIndex(index)
        return index

    def indexOf(self, page: QWidget) -> int:  # noqa: N802
        return self.pages_stack.indexOf(page)

    def setCurrentWidget(self, page: QWidget) -> None:  # noqa: N802
        index = self.pages_stack.indexOf(page)
        if index >= 0:
            self.tab_bar.setCurrentIndex(index)

    def currentWidget(self) -> QWidget | None:  # noqa: N802
        return self.pages_stack.currentWidget()

    def currentIndex(self) -> int:  # noqa: N802
        return self.tab_bar.currentIndex()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        self.tab_bar.setCurrentIndex(index)

    def _set_index(self, index: int) -> None:
        if 0 <= index < self.pages_stack.count():
            self.pages_stack.setCurrentIndex(index)
        self.currentChanged.emit(index)
