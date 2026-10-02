"""Search and copy a brush from the user's local Clip Studio Paint library."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from comic_editor.core.csp_library import (
    discover_libraries, import_library_brush, list_library_brushes, resolve_library,
)


class CspLibraryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Import from Clip Studio Paint')
        self.resize(700, 540)
        self.definition = None
        self.entries = []
        self._notes = []
        self._future = None
        self._job = ''
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='csp-library')
        layout = QVBoxLayout(self)
        intro = QLabel('Search your downloaded brush materials and installed sub tools, then choose a brush to import.', self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        location_row = QHBoxLayout()
        self.locations = QComboBox(self)
        self.locations.setMinimumContentsLength(12)
        self.locations.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.locations.setToolTip('Clip Studio Paint library location')
        for library in discover_libraries():
            self.locations.addItem(str(library.common), library)
        self.browse = QPushButton('Choose folder…', self)
        self.browse.setToolTip('Choose CELSYS, CLIPStudioCommon, or Material if your library was moved.')
        self.refresh_button = QPushButton('Refresh', self)
        location_row.addWidget(self.locations, 1)
        location_row.addWidget(self.browse)
        location_row.addWidget(self.refresh_button)
        layout.addLayout(location_row)
        search_row = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText('Search brushes…')
        self.search.setClearButtonEnabled(True)
        self.source = QComboBox(self)
        self.source.addItem('All brushes', '')
        self.source.addItem('Downloaded materials', 'material')
        self.source.addItem('Installed sub tools', 'installed')
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.source)
        layout.addLayout(search_row)
        self.brushes = QListWidget(self)
        self.brushes.setIconSize(QSize(64, 40))
        layout.addWidget(self.brushes, 1)
        self.count = QLabel(self)
        layout.addWidget(self.count)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel, self)
        self.import_button = buttons.addButton('Import brush', QDialogButtonBox.AcceptRole)
        self.import_button.setEnabled(False)
        self.import_button.setDefault(True)
        layout.addWidget(buttons)
        buttons.rejected.connect(self.reject)
        self.import_button.clicked.connect(self._import_selected)
        self.brushes.itemDoubleClicked.connect(self._import_selected)
        self.brushes.currentItemChanged.connect(self._selection_changed)
        self.search.textChanged.connect(self._filter)
        self.source.currentIndexChanged.connect(self._filter)
        self.locations.currentIndexChanged.connect(self._load_library)
        self.browse.clicked.connect(self._choose_folder)
        self.refresh_button.clicked.connect(self._load_library)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._poll)
        self.finished.connect(self._shutdown)
        QTimer.singleShot(0, self._load_library)

    def _set_busy(self, busy):
        self.locations.setEnabled(not busy)
        self.browse.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        self.brushes.setEnabled(not busy)
        self.import_button.setEnabled(not busy and self.selected_entry() is not None)

    def _load_library(self):
        if self._future is not None or self._closed:
            return
        self.entries = []
        self._notes = []
        self.brushes.clear()
        self.count.clear()
        library = self.locations.currentData()
        if library is None:
            self.status.setText('No Clip Studio Paint library was found. Choose your CELSYS or CLIPStudioCommon folder.')
            self.import_button.setEnabled(False)
            return
        self.status.setText('Reading your Clip Studio Paint brush library…')
        self._start('scan', list_library_brushes, library)

    def _choose_folder(self):
        library = self.locations.currentData()
        path = QFileDialog.getExistingDirectory(self, 'Choose Clip Studio Paint library',
                                               str(library.common) if library else '')
        if not path:
            return
        try:
            library = resolve_library(path)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, 'Library not found', str(error))
            return
        index = next((i for i in range(self.locations.count()) if self.locations.itemData(i) == library), -1)
        if index < 0:
            self.locations.addItem(str(library.common), library)
            index = self.locations.count()-1
        if index == self.locations.currentIndex():
            self._load_library()
        else:
            self.locations.setCurrentIndex(index)

    def _start(self, job, function, *args):
        self._job = job
        self._future = self._executor.submit(function, *args)
        self._set_busy(True)
        self._timer.start()

    def _poll(self):
        if self._future is None or not self._future.done():
            return
        future, job = self._future, self._job
        self._future = None
        self._timer.stop()
        self._set_busy(False)
        try:
            result = future.result()
        except Exception as error:
            self.status.setText(f'Brush {"import" if job == "import" else "library scan"} failed: {error}')
            return
        if job == 'import':
            self.definition = result
            self.accept()
            return
        self.entries, self._notes = result
        for index, entry in enumerate(self.entries):
            origin = 'Downloaded' if entry.kind == 'material' else 'Installed'
            item = QListWidgetItem(f'{entry.name}  —  {origin}')
            item.setData(Qt.UserRole, index)
            item.setToolTip(f'{entry.name}\n{origin} in Clip Studio Paint\n{entry.path}')
            if entry.thumbnail is not None and entry.thumbnail.is_file():
                item.setIcon(QIcon(str(entry.thumbnail)))
            self.brushes.addItem(item)
        self._filter()
        self.search.setFocus()

    def _filter(self):
        query = self.search.text().strip().casefold()
        kind = self.source.currentData()
        visible = 0
        for i, entry in enumerate(self.entries):
            show = query in entry.name.casefold() and (not kind or entry.kind == kind)
            self.brushes.item(i).setHidden(not show)
            visible += int(show)
        if self.brushes.currentItem() is not None and self.brushes.currentItem().isHidden():
            self.brushes.setCurrentRow(-1)
        self.count.setText(f'{visible} of {len(self.entries)} brushes')
        self._selection_changed()

    def selected_entry(self):
        item = self.brushes.currentItem()
        if item is None or item.isHidden():
            return None
        return self.entries[item.data(Qt.UserRole)]

    def _selection_changed(self):
        entry = self.selected_entry()
        self.import_button.setEnabled(entry is not None and self._future is None)
        if self._future is not None:
            return
        if not self.entries:
            text = 'No brushes were found here. Choose another library folder or export a brush from CSP and use Import .sut.'
        elif entry is None:
            text = 'Select a brush to import.'
        else:
            text = f'{entry.name} will be copied into your brush presets.'
        self.status.setText('\n'.join([text, *self._notes]))

    def _import_selected(self, *_):
        entry = self.selected_entry()
        if entry is None or self._future is not None:
            return
        self.status.setText(f'Importing {entry.name}…')
        self._start('import', import_library_brush, entry)

    def _shutdown(self):
        self._closed = True
        self._timer.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
