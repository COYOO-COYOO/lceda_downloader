from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    from PyQt6.QtCore import QThread, Qt, pyqtSignal
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QCheckBox,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHeaderView,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSplitter,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except Exception:
    QApplication = None
    QThread = None
    Qt = None
    pyqtSignal = None
    QPixmap = None
    QAbstractItemView = None
    QCheckBox = None
    QFrame = None
    QGridLayout = None
    QGroupBox = None
    QHeaderView = None
    QHBoxLayout = None
    QLabel = None
    QLineEdit = None
    QMainWindow = object
    QMessageBox = None
    QPushButton = None
    QSplitter = None
    QTableWidget = None
    QTableWidgetItem = None
    QTextEdit = None
    QVBoxLayout = None
    QWidget = None

try:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    from matplotlib.figure import Figure
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
except Exception:
    FigureCanvasQTAgg = None
    Figure = None
    Poly3DCollection = None

from .core import (
    OBJ_API,
    PREVIEW_EDGE_COLOR,
    PREVIEW_EDGE_WIDTH,
    PREVIEW_FACE_ALPHA,
    PREVIEW_PARSE_MAX_TRIANGLES,
    PREVIEW_RENDER_MAX_TRIANGLES,
    LcedaApiError,
    SearchItem,
    _http_get,
    build_preview_facecolors,
    choose_image_url,
    decimate_triangles_preserve_pins,
    download_obj,
    download_step,
    export_ad_altium_libs,
    get_model_uuid,
    has_symbol_or_footprint,
    parse_obj_mesh,
    search_components,
)

if QThread is not None and pyqtSignal is not None:

    class _FunctionWorker(QThread):
        succeeded = pyqtSignal(object)
        failed = pyqtSignal(str)

        def __init__(self, fn: Any):
            super().__init__()
            self._fn = fn

        def run(self) -> None:
            try:
                result = self._fn()
                self.succeeded.emit(result)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(str(exc))


else:
    _FunctionWorker = None


class LcedaQtMainWindow(QMainWindow):
    def __init__(self):
        if QApplication is None or Qt is None:
            raise LcedaApiError("PyQt6 is unavailable in this Python environment.")

        super().__init__()
        self.setWindowTitle("LCSC/LcEDA 3D Model Downloader (Qt)")
        self.resize(1180, 760)
        self.setMinimumSize(900, 580)

        self.items: list[SearchItem] = []
        self.action_busy = False
        self.preview_token = 0
        self.image_cache: dict[str, bytes | None] = {}
        self.mesh_cache: dict[str, tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]] | None] = {}
        self._threads: list[Any] = []
        self._current_pixmap: QPixmap | None = None

        self.search_button = None
        self.keyword_edit = None
        self.table = None
        self.step_button = None
        self.obj_button = None
        self.ad_button = None
        self.status_label = None
        self.log_text = None
        self.image_label = None
        self.step_dir_edit = None
        self.obj_dir_edit = None
        self.ad_dir_edit = None
        self.force_check = None
        self.viewer_placeholder = None

        self.figure = None
        self.ax = None
        self.canvas = None

        self._build_ui()
        self._set_image_message("Select a component to preview image.")
        self._clear_mesh("Select a component to preview 3D model.")
        self._refresh_buttons()
        self.log("GUI started (Qt).")

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.setChildrenCollapsible(False)
        root.addWidget(main_splitter, 1)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        search_group = QGroupBox("Search")
        search_layout = QHBoxLayout(search_group)
        search_layout.setContentsMargins(8, 8, 8, 8)
        search_layout.addWidget(QLabel("Keyword"))
        self.keyword_edit = QLineEdit()
        self.search_button = QPushButton("Search")
        search_layout.addWidget(self.keyword_edit, 1)
        search_layout.addWidget(self.search_button)
        left_layout.addWidget(search_group, 0)

        comp_group = QGroupBox("Components")
        comp_layout = QVBoxLayout(comp_group)
        comp_layout.setContentsMargins(6, 6, 6, 6)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Display Name", "Manufacturer", "3D"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(2, 180)
        comp_layout.addWidget(self.table)
        left_layout.addWidget(comp_group, 1)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        preview_splitter = QSplitter(Qt.Orientation.Vertical)
        preview_splitter.setChildrenCollapsible(False)
        right_layout.addWidget(preview_splitter, 1)

        image_group = QGroupBox("Component Image")
        image_group.setMinimumHeight(120)
        image_layout = QVBoxLayout(image_group)
        image_layout.setContentsMargins(6, 6, 6, 6)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background:#eef2f7; color:#334155; border:1px solid #d4dde6;")
        image_layout.addWidget(self.image_label)
        preview_splitter.addWidget(image_group)

        model_group = QGroupBox("3D Preview")
        model_group.setMinimumHeight(180)
        model_layout = QVBoxLayout(model_group)
        model_layout.setContentsMargins(6, 6, 6, 6)
        if Figure is not None and FigureCanvasQTAgg is not None and Poly3DCollection is not None:
            self.figure = Figure(figsize=(5, 4), dpi=100)
            self.ax = self.figure.add_subplot(111, projection="3d")
            self.canvas = FigureCanvasQTAgg(self.figure)
            self.canvas.setMinimumSize(160, 160)
            model_layout.addWidget(self.canvas)
        else:
            self.viewer_placeholder = QLabel("Matplotlib Qt backend unavailable.")
            self.viewer_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            model_layout.addWidget(self.viewer_placeholder)
        preview_splitter.addWidget(model_group)
        preview_splitter.setCollapsible(0, False)
        preview_splitter.setCollapsible(1, False)
        preview_splitter.setStretchFactor(0, 4)
        preview_splitter.setStretchFactor(1, 6)
        preview_splitter.setSizes([250, 400])

        action_row = QFrame()
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(8)
        self.step_button = QPushButton("Download STEP")
        self.obj_button = QPushButton("Download OBJ/MTL")
        self.ad_button = QPushButton("One-click AD Lib")
        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        action_layout.addWidget(self.step_button)
        action_layout.addWidget(self.obj_button)
        action_layout.addWidget(self.ad_button)
        action_layout.addStretch(1)
        action_layout.addWidget(self.status_label)
        right_layout.addWidget(action_row, 0)

        main_splitter.addWidget(left_panel)
        main_splitter.addWidget(right_panel)
        main_splitter.setCollapsible(0, False)
        main_splitter.setCollapsible(1, False)
        main_splitter.setStretchFactor(0, 5)
        main_splitter.setStretchFactor(1, 5)
        main_splitter.setSizes([620, 560])

        settings_group = QGroupBox("Output Settings")
        settings_layout = QGridLayout(settings_group)
        settings_layout.setContentsMargins(8, 8, 8, 8)
        settings_layout.addWidget(QLabel("STEP dir"), 0, 0)
        self.step_dir_edit = QLineEdit("step")
        settings_layout.addWidget(self.step_dir_edit, 0, 1)
        settings_layout.addWidget(QLabel("OBJ dir"), 0, 2)
        self.obj_dir_edit = QLineEdit("temp")
        settings_layout.addWidget(self.obj_dir_edit, 0, 3)
        settings_layout.addWidget(QLabel("AD dir"), 0, 4)
        self.ad_dir_edit = QLineEdit("ad_lib")
        settings_layout.addWidget(self.ad_dir_edit, 0, 5)
        self.force_check = QCheckBox("Overwrite cache")
        settings_layout.addWidget(self.force_check, 0, 6)
        settings_layout.setColumnStretch(1, 1)
        settings_layout.setColumnStretch(3, 1)
        settings_layout.setColumnStretch(5, 1)
        root.addWidget(settings_group, 0)

        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(6, 6, 6, 6)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(120)
        log_layout.addWidget(self.log_text)
        root.addWidget(log_group, 0)

        self.search_button.clicked.connect(self.on_search)
        self.keyword_edit.returnPressed.connect(self.on_search)
        self.table.itemSelectionChanged.connect(self.on_result_selected)
        self.step_button.clicked.connect(self.on_download_step)
        self.obj_button.clicked.connect(self.on_download_obj)
        self.ad_button.clicked.connect(self.on_export_ad)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._refresh_image_pixmap()
        self._draw_canvas_safe()

    def _item_key(self, item: SearchItem) -> str:
        raw_uuid = item.raw.get("uuid")
        if raw_uuid:
            return str(raw_uuid)
        return f"{item.index}:{item.title}:{item.display_title}"

    def _get_selected_item(self) -> SearchItem | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.items):
            return None
        return self.items[row]

    def _refresh_buttons(self) -> None:
        item = self._get_selected_item()
        can_download = (not self.action_busy) and (item is not None) and bool(item.model_uuid)
        can_export_ad = (not self.action_busy) and has_symbol_or_footprint(item)
        self.step_button.setEnabled(can_download)
        self.obj_button.setEnabled(can_download)
        self.ad_button.setEnabled(can_export_ad)
        self.search_button.setEnabled(not self.action_busy)

    def _set_action_busy(self, busy: bool, status_text: str) -> None:
        self.action_busy = busy
        self.status_label.setText(status_text)
        self._refresh_buttons()

    def _show_warning(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def _show_info(self, title: str, message: str) -> None:
        QMessageBox.information(self, title, message)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")

    def _start_worker(self, worker_fn: Any, on_success: Any, on_error: Any) -> None:
        if _FunctionWorker is None:
            on_error("Qt worker unavailable.")
            return
        thread = _FunctionWorker(worker_fn)
        self._threads.append(thread)

        def _cleanup() -> None:
            if thread in self._threads:
                self._threads.remove(thread)
            thread.deleteLater()

        thread.succeeded.connect(on_success)
        thread.failed.connect(on_error)
        thread.finished.connect(_cleanup)
        thread.start()

    def _run_action_task(
        self,
        busy_text: str,
        worker_fn: Any,
        on_success: Any,
        error_title: str,
    ) -> None:
        if self.action_busy:
            return
        self._set_action_busy(True, busy_text)

        def _ok(result: Any) -> None:
            self._set_action_busy(False, "Ready")
            on_success(result)

        def _err(message: str) -> None:
            self._set_action_busy(False, "Ready")
            self.log(f"{error_title}: {message}")
            self._show_error("Error", f"{error_title}\n{message}")

        self._start_worker(worker_fn, _ok, _err)

    def _set_image_message(self, message: str) -> None:
        self._current_pixmap = None
        self.image_label.clear()
        self.image_label.setText(message)

    def _refresh_image_pixmap(self) -> None:
        if self._current_pixmap is None:
            return
        size = self.image_label.size()
        if size.width() < 10 or size.height() < 10:
            return
        scaled = self._current_pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    def _set_image_from_bytes(self, content: bytes) -> None:
        if QPixmap is None:
            self._set_image_message("QPixmap unavailable.")
            return
        pix = QPixmap()
        if not pix.loadFromData(content):
            self._set_image_message("Failed to decode image.")
            return
        self._current_pixmap = pix
        self.image_label.setText("")
        self._refresh_image_pixmap()

    def _clear_mesh(self, message: str) -> None:
        if self.canvas is None or self.ax is None:
            if self.viewer_placeholder is not None:
                self.viewer_placeholder.setText(message)
            return
        self.ax.clear()
        self.ax.set_axis_off()
        self.ax.text2D(0.5, 0.5, message, transform=self.ax.transAxes, ha="center", va="center")
        self._draw_canvas_safe()

    def _draw_canvas_safe(self) -> None:
        if self.canvas is None:
            return

        # Matplotlib 3D may throw when widget size collapses to 0 during rapid resize.
        width = int(self.canvas.width())
        height = int(self.canvas.height())
        if width < 8 or height < 8:
            return

        try:
            self.canvas.draw()
        except ValueError as exc:
            if "box_aspect" in str(exc) and "fig_aspect" in str(exc):
                return
            self.log(f"3D draw warning: {exc}")

    def _render_mesh(
        self,
        vertices: list[tuple[float, float, float]],
        faces: list[tuple[int, int, int]],
    ) -> None:
        if self.canvas is None or self.ax is None or Poly3DCollection is None:
            return
        if not vertices or not faces:
            self._clear_mesh("No mesh data.")
            return

        xs = [v[0] for v in vertices]
        ys = [v[1] for v in vertices]
        zs = [v[2] for v in vertices]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        min_z, max_z = min(zs), max(zs)

        cx = (min_x + max_x) / 2.0
        cy = (min_y + max_y) / 2.0
        cz = (min_z + max_z) / 2.0
        span = max(max_x - min_x, max_y - min_y, max_z - min_z) or 1.0

        normalized = [((x - cx) / span, (y - cy) / span, (z - cz) / span) for x, y, z in vertices]
        faces_to_draw = decimate_triangles_preserve_pins(
            faces,
            normalized,
            PREVIEW_RENDER_MAX_TRIANGLES,
        )

        polygons: list[list[tuple[float, float, float]]] = []
        for a, b, c in faces_to_draw:
            if a < 0 or b < 0 or c < 0:
                continue
            if a >= len(normalized) or b >= len(normalized) or c >= len(normalized):
                continue
            polygons.append([normalized[a], normalized[b], normalized[c]])

        if not polygons:
            self._clear_mesh("No valid mesh faces.")
            return

        facecolors = build_preview_facecolors(polygons)
        self.ax.clear()
        mesh = Poly3DCollection(
            polygons,
            linewidths=PREVIEW_EDGE_WIDTH,
            edgecolors=PREVIEW_EDGE_COLOR,
            facecolors=facecolors if facecolors else "#b9d7ef",
            alpha=PREVIEW_FACE_ALPHA,
            antialiased=True,
        )
        self.ax.add_collection3d(mesh)
        self.ax.set_box_aspect((1, 1, 1))
        self.ax.set_xlim(-0.6, 0.6)
        self.ax.set_ylim(-0.6, 0.6)
        self.ax.set_zlim(-0.6, 0.6)
        self.ax.view_init(elev=24, azim=36)
        self.ax.set_axis_off()
        self._draw_canvas_safe()

    def on_search(self) -> None:
        keyword = self.keyword_edit.text().strip()
        if not keyword:
            self._show_warning("Hint", "Please input a keyword.")
            return

        self.log(f"Searching: {keyword}")

        def _worker() -> list[SearchItem]:
            return search_components(keyword)

        def _on_success(results: list[SearchItem]) -> None:
            self.items = results
            self.table.setRowCount(len(results))
            for i, item in enumerate(results):
                has3d = "Yes" if item.model_uuid else "No"
                title = item.display_title or item.title or "(No title)"
                values = [str(item.index), title, item.manufacturer or "-", has3d]
                for c, val in enumerate(values):
                    cell = QTableWidgetItem(val)
                    if c in (0, 3):
                        cell.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
                    self.table.setItem(i, c, cell)

            self.log(f"Search complete: {len(results)} result(s).")
            if results:
                self.table.selectRow(0)
                self.on_result_selected()
            else:
                self._set_image_message("No image.")
                self._clear_mesh("No model.")
                self._show_info("Result", "No component found.")
            self._refresh_buttons()

        self._run_action_task("Searching...", _worker, _on_success, "Search failed")

    def on_result_selected(self) -> None:
        item = self._get_selected_item()
        self._refresh_buttons()
        if item is None:
            self._set_image_message("No selection.")
            self._clear_mesh("No selection.")
            return

        title = item.display_title or item.title or "(No title)"
        self.log(f"Selected: {title}")
        self._set_image_message("Loading image...")
        if item.model_uuid:
            self._clear_mesh("Loading 3D model...")
        else:
            self._clear_mesh("This component has no 3D model.")

        self.preview_token += 1
        token = self.preview_token
        key = self._item_key(item)

        image_cached = key in self.image_cache
        mesh_cached = key in self.mesh_cache
        if image_cached and (not item.model_uuid or mesh_cached):
            self._apply_preview_data(
                token,
                item,
                {
                    "image_bytes": self.image_cache.get(key),
                    "image_error": None,
                    "mesh": self.mesh_cache.get(key),
                    "mesh_error": None,
                    "has_model": bool(item.model_uuid),
                },
            )
            return

        def _worker() -> dict[str, Any]:
            payload: dict[str, Any] = {
                "image_bytes": None,
                "image_error": None,
                "mesh": None,
                "mesh_error": None,
                "has_model": bool(item.model_uuid),
            }
            image_url = choose_image_url(item)
            if image_url:
                try:
                    payload["image_bytes"] = _http_get(image_url)
                except Exception as exc:  # noqa: BLE001
                    payload["image_error"] = str(exc)

            if item.model_uuid:
                try:
                    model_uuid = get_model_uuid(item)
                    obj_raw = _http_get(OBJ_API.format(model_uuid=quote(model_uuid)))
                    payload["mesh"] = parse_obj_mesh(
                        obj_raw.decode("utf-8", errors="ignore"),
                        max_triangles=PREVIEW_PARSE_MAX_TRIANGLES,
                    )
                except Exception as exc:  # noqa: BLE001
                    payload["mesh_error"] = str(exc)
            return payload

        def _ok(payload: dict[str, Any]) -> None:
            self._apply_preview_data(token, item, payload)

        def _err(message: str) -> None:
            if token != self.preview_token:
                return
            self.log(f"Preview failed: {message}")
            self._set_image_message("Preview failed.")
            self._clear_mesh("Preview failed.")

        self._start_worker(_worker, _ok, _err)

    def _apply_preview_data(self, token: int, item: SearchItem, data: dict[str, Any]) -> None:
        if token != self.preview_token:
            return

        key = self._item_key(item)
        image_bytes = data.get("image_bytes")
        mesh_data = data.get("mesh")
        self.image_cache[key] = image_bytes
        self.mesh_cache[key] = mesh_data

        if image_bytes:
            self._set_image_from_bytes(image_bytes)
        else:
            self._set_image_message("No component image.")
            if data.get("image_error"):
                self.log(f"Image preview failed: {data['image_error']}")

        if mesh_data and data.get("has_model"):
            vertices, faces = mesh_data
            self._render_mesh(vertices, faces)
            self.log(f"3D preview ready: {len(vertices)} vertices, {len(faces)} triangles.")
        else:
            if data.get("has_model"):
                self._clear_mesh("Failed to load 3D model preview.")
                if data.get("mesh_error"):
                    self.log(f"3D preview failed: {data['mesh_error']}")
            else:
                self._clear_mesh("This component has no 3D model.")

    def on_download_step(self) -> None:
        item = self._get_selected_item()
        if item is None:
            self._show_warning("Hint", "Please select a component first.")
            return
        if not item.model_uuid:
            self._show_warning("Hint", "Selected component has no 3D model.")
            return

        out_dir = Path(self.step_dir_edit.text().strip() or "step")
        force = self.force_check.isChecked()
        title = item.display_title or item.title
        self.log(f"Downloading STEP: {title}")

        def _worker() -> Path:
            return download_step(item, out_dir=out_dir, force=force)

        def _on_success(step_path: Path) -> None:
            self.log(f"STEP saved: {step_path}")
            self._show_info("Done", f"STEP saved:\n{step_path}")

        self._run_action_task("Downloading STEP...", _worker, _on_success, "Download STEP failed")

    def on_download_obj(self) -> None:
        item = self._get_selected_item()
        if item is None:
            self._show_warning("Hint", "Please select a component first.")
            return
        if not item.model_uuid:
            self._show_warning("Hint", "Selected component has no 3D model.")
            return

        out_dir = Path(self.obj_dir_edit.text().strip() or "temp")
        force = self.force_check.isChecked()
        title = item.display_title or item.title
        self.log(f"Downloading OBJ/MTL: {title}")

        def _worker() -> tuple[Path, Path]:
            return download_obj(item, out_dir=out_dir, force=force)

        def _on_success(paths: tuple[Path, Path]) -> None:
            obj_path, mtl_path = paths
            self.log(f"OBJ saved: {obj_path}")
            self.log(f"MTL saved: {mtl_path}")
            self._show_info("Done", f"OBJ/MTL saved:\n{obj_path}\n{mtl_path}")

        self._run_action_task("Downloading OBJ/MTL...", _worker, _on_success, "Download OBJ/MTL failed")

    def on_export_ad(self) -> None:
        item = self._get_selected_item()
        if item is None:
            self._show_warning("Hint", "Please select a component first.")
            return
        if not has_symbol_or_footprint(item):
            self._show_warning("Hint", "Selected component has no symbol/footprint uuid.")
            return

        out_dir = Path(self.ad_dir_edit.text().strip() or "ad_lib")
        force = self.force_check.isChecked()
        title = item.display_title or item.title
        self.log(f"Exporting AD SchLib/PcbLib: {title}")

        def _worker() -> dict[str, Path]:
            return export_ad_altium_libs(item, out_dir=out_dir, force=force)

        def _on_success(result: dict[str, Path]) -> None:
            self.log("AD SchLib/PcbLib export done.")
            lines = []
            if "schlib" in result:
                lines.append(f"SchLib: {result['schlib']}")
            if "pcblib" in result:
                lines.append(f"PcbLib: {result['pcblib']}")
            if not lines:
                lines.append("No SchLib/PcbLib generated.")
            self._show_info("Done", "\n".join(lines))

        self._run_action_task(
            "Exporting AD SchLib/PcbLib...",
            _worker,
            _on_success,
            "Export AD SchLib/PcbLib failed",
        )


def launch_gui_qt() -> int:
    if QApplication is None:
        raise LcedaApiError("PyQt6 is unavailable in this Python environment.")

    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv)

    window = LcedaQtMainWindow()
    window.show()

    if owns_app:
        return int(app.exec())
    return 0

