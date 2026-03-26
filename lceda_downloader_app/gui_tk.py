from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk
except Exception:
    tk = None
    messagebox = None
    scrolledtext = None
    ttk = None

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
except Exception:
    FigureCanvasTkAgg = None
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

class LcedaGuiApp:
    def __init__(self, root: Any):
        if tk is None or ttk is None or scrolledtext is None:
            raise LcedaApiError("Tkinter is unavailable in this Python environment.")

        self.root = root
        self.root.title("LCSC/LcEDA 3D Model Downloader")
        self.root.geometry("1180x760")
        self.root.minsize(980, 640)

        self.items: list[SearchItem] = []
        self.action_busy = False
        self.preview_token = 0

        self.keyword_var = tk.StringVar(value="")
        self.step_dir_var = tk.StringVar(value="step")
        self.obj_dir_var = tk.StringVar(value="temp")
        self.ad_dir_var = tk.StringVar(value="ad_lib")
        self.force_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")

        self.search_button = None
        self.step_button = None
        self.obj_button = None
        self.ad_button = None
        self.tree = None
        self.log_box = None
        self.image_label = None

        self.figure = None
        self.ax = None
        self.canvas = None
        self.viewer_placeholder = None

        self._image_ref = None
        self.image_cache: dict[str, bytes | None] = {}
        self.mesh_cache: dict[str, tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]] | None] = {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lceda-tk")
        self._preview_future: Future[Any] | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_image_message("Select a component to preview image.")
        self._clear_mesh("Select a component to preview 3D model.")
        self._refresh_buttons()
        self.log("GUI started.")

    def _build_ui(self) -> None:
        root_frame = ttk.Frame(self.root, padding=10)
        root_frame.pack(fill="both", expand=True)
        root_frame.rowconfigure(0, weight=1)
        root_frame.columnconfigure(0, weight=1)

        main_frame = ttk.Frame(root_frame)
        main_frame.grid(row=0, column=0, sticky="nsew")
        main_frame.columnconfigure(0, weight=2)
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=0)

        left_panel = ttk.Frame(main_frame)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left_panel.rowconfigure(1, weight=1)
        left_panel.columnconfigure(0, weight=1)

        search_frame = ttk.LabelFrame(left_panel, text="Search")
        search_frame.grid(row=0, column=0, sticky="ew")
        search_frame.columnconfigure(1, weight=1)

        ttk.Label(search_frame, text="Keyword").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        keyword_entry = ttk.Entry(search_frame, textvariable=self.keyword_var)
        keyword_entry.grid(row=0, column=1, padx=(0, 6), pady=8, sticky="ew")
        keyword_entry.bind("<Return>", lambda _e: self.on_search())

        self.search_button = ttk.Button(search_frame, text="Search", command=self.on_search)
        self.search_button.grid(row=0, column=2, padx=(0, 8), pady=8)

        list_frame = ttk.LabelFrame(left_panel, text="Components")
        list_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        columns = ("idx", "title", "manufacturer", "has3d")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("idx", text="#")
        self.tree.heading("title", text="Display Name")
        self.tree.heading("manufacturer", text="Manufacturer")
        self.tree.heading("has3d", text="3D")

        self.tree.column("idx", width=56, minwidth=50, anchor="center")
        self.tree.column("title", width=510, minwidth=220, anchor="w")
        self.tree.column("manufacturer", width=180, minwidth=120, anchor="w")
        self.tree.column("has3d", width=70, minwidth=60, anchor="center")

        ybar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self.on_result_selected)

        right_panel = ttk.Frame(main_frame)
        right_panel.grid(row=0, column=1, sticky="nsew")
        right_panel.columnconfigure(0, weight=1)
        right_panel.rowconfigure(0, weight=1, minsize=240)
        right_panel.rowconfigure(1, weight=2, minsize=260)
        right_panel.rowconfigure(2, weight=0, minsize=48)

        image_frame = ttk.LabelFrame(right_panel, text="Component Image", height=260)
        image_frame.grid(row=0, column=0, sticky="nsew")
        image_frame.columnconfigure(0, weight=1)
        image_frame.rowconfigure(0, weight=1)
        image_frame.grid_propagate(False)

        self.image_label = tk.Label(
            image_frame,
            text="",
            bg="#eef2f7",
            fg="#334155",
            anchor="center",
            justify="center",
            bd=0,
            relief="flat",
        )
        self.image_label.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        preview_frame = ttk.LabelFrame(right_panel, text="3D Preview")
        preview_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 8))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        preview_frame.grid_propagate(False)

        if Figure is not None and FigureCanvasTkAgg is not None and Poly3DCollection is not None:
            self.figure = Figure(figsize=(5, 4), dpi=100)
            self.ax = self.figure.add_subplot(111, projection="3d")
            self.canvas = FigureCanvasTkAgg(self.figure, master=preview_frame)
            self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        else:
            self.viewer_placeholder = ttk.Label(
                preview_frame,
                text="Matplotlib unavailable.\nInstall matplotlib to enable 3D preview.",
                anchor="center",
                justify="center",
            )
            self.viewer_placeholder.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        action_frame = ttk.Frame(right_panel, height=44)
        action_frame.grid(row=2, column=0, sticky="nsew")
        action_frame.grid_propagate(False)
        action_frame.rowconfigure(0, weight=1)
        action_frame.columnconfigure(3, weight=1)

        self.step_button = ttk.Button(action_frame, text="Download STEP", command=self.on_download_step)
        self.step_button.grid(row=0, column=0, padx=(0, 6), pady=(4, 4))
        self.obj_button = ttk.Button(action_frame, text="Download OBJ/MTL", command=self.on_download_obj)
        self.obj_button.grid(row=0, column=1, padx=(0, 6), pady=(4, 4))
        self.ad_button = ttk.Button(action_frame, text="One-click AD Lib", command=self.on_export_ad)
        self.ad_button.grid(row=0, column=2, padx=(0, 6), pady=(4, 4))
        ttk.Label(action_frame, textvariable=self.status_var).grid(row=0, column=3, sticky="e", pady=(4, 4))

        config_frame = ttk.LabelFrame(main_frame, text="Output Settings")
        config_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        config_frame.columnconfigure(1, weight=1)
        config_frame.columnconfigure(3, weight=1)
        config_frame.columnconfigure(5, weight=1)

        ttk.Label(config_frame, text="STEP dir").grid(row=0, column=0, padx=(8, 6), pady=8, sticky="w")
        ttk.Entry(config_frame, textvariable=self.step_dir_var).grid(
            row=0, column=1, padx=(0, 12), pady=8, sticky="ew"
        )
        ttk.Label(config_frame, text="OBJ dir").grid(row=0, column=2, padx=(0, 6), pady=8, sticky="w")
        ttk.Entry(config_frame, textvariable=self.obj_dir_var).grid(
            row=0, column=3, padx=(0, 12), pady=8, sticky="ew"
        )
        ttk.Label(config_frame, text="AD dir").grid(row=0, column=4, padx=(0, 6), pady=8, sticky="w")
        ttk.Entry(config_frame, textvariable=self.ad_dir_var).grid(
            row=0, column=5, padx=(0, 12), pady=8, sticky="ew"
        )
        ttk.Checkbutton(config_frame, text="Overwrite cache", variable=self.force_var).grid(
            row=0, column=6, padx=(0, 8), pady=8, sticky="w"
        )

        log_frame = ttk.LabelFrame(root_frame, text="Log")
        log_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.log_box = scrolledtext.ScrolledText(log_frame, height=8)
        self.log_box.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_box.configure(state="disabled")

    def _item_key(self, item: SearchItem) -> str:
        raw_uuid = item.raw.get("uuid")
        if raw_uuid:
            return str(raw_uuid)
        return f"{item.index}:{item.title}:{item.display_title}"

    def _get_selected_item(self) -> SearchItem | None:
        if self.tree is None:
            return None
        selected = self.tree.selection()
        if not selected:
            return None
        try:
            idx = int(selected[0])
        except ValueError:
            return None
        if idx < 0 or idx >= len(self.items):
            return None
        return self.items[idx]

    def _refresh_buttons(self) -> None:
        item = self._get_selected_item()
        can_download = (not self.action_busy) and (item is not None) and bool(item.model_uuid)
        can_export_ad = (not self.action_busy) and has_symbol_or_footprint(item)
        state = "normal" if can_download else "disabled"
        ad_state = "normal" if can_export_ad else "disabled"
        if self.step_button is not None:
            self.step_button.configure(state=state)
        if self.obj_button is not None:
            self.obj_button.configure(state=state)
        if self.ad_button is not None:
            self.ad_button.configure(state=ad_state)
        if self.search_button is not None:
            self.search_button.configure(state="disabled" if self.action_busy else "normal")

    def _set_action_busy(self, busy: bool, status_text: str) -> None:
        self.action_busy = busy
        self.status_var.set(status_text)
        self._refresh_buttons()

    def log(self, message: str) -> None:
        if self.log_box is None:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{timestamp}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _show_warning(self, title: str, message: str) -> None:
        if messagebox is not None:
            messagebox.showwarning(title, message)

    def _show_error(self, title: str, message: str) -> None:
        if messagebox is not None:
            messagebox.showerror(title, message)

    def _show_info(self, title: str, message: str) -> None:
        if messagebox is not None:
            messagebox.showinfo(title, message)

    def _safe_after(self, callback: Any) -> None:
        try:
            self.root.after(0, callback)
        except Exception:
            return

    def _submit_background(
        self,
        worker: Any,
        on_success: Any | None = None,
        on_error: Any | None = None,
    ) -> Future[Any]:
        future = self._executor.submit(worker)

        def _done(done_future: Future[Any]) -> None:
            try:
                result = done_future.result()
            except CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                if on_error is not None:
                    self._safe_after(lambda exc=exc: on_error(exc))
                return
            if on_success is not None:
                self._safe_after(lambda result=result: on_success(result))

        future.add_done_callback(_done)
        return future

    def _on_close(self) -> None:
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._executor.shutdown(wait=False)
        self.root.destroy()

    def _run_action_task(
        self,
        busy_text: str,
        worker: Any,
        on_success: Any,
        error_title: str,
    ) -> None:
        if self.action_busy:
            return
        self._set_action_busy(True, busy_text)
        self._submit_background(
            worker,
            on_success=lambda result: self._on_action_done(on_success, result),
            on_error=lambda exc: self._on_action_error(error_title, exc),
        )

    def _on_action_done(self, on_success: Any, result: Any) -> None:
        self._set_action_busy(False, "Ready")
        on_success(result)

    def _on_action_error(self, title: str, exc: Exception) -> None:
        self._set_action_busy(False, "Ready")
        self.log(f"{title}: {exc}")
        self._show_error("Error", f"{title}\n{exc}")

    def _set_image_message(self, message: str) -> None:
        if self.image_label is None:
            return
        self.image_label.configure(image="", text=message)
        self._image_ref = None

    def _set_image_from_bytes(self, content: bytes) -> None:
        if self.image_label is None:
            return
        if Image is None or ImageTk is None:
            self._set_image_message("Pillow unavailable. Install pillow to display images.")
            return

        try:
            img = Image.open(BytesIO(content))
            resampling = getattr(Image, "Resampling", Image)
            self.root.update_idletasks()
            target_w = max(220, self.image_label.winfo_width() - 12)
            target_h = max(140, self.image_label.winfo_height() - 12)
            img.thumbnail((target_w, target_h), resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Image decode failed: {exc}")
            self._set_image_message("Failed to decode image.")
            return

        self._image_ref = photo
        self.image_label.configure(image=photo, text="")

    def _clear_mesh(self, message: str) -> None:
        if self.canvas is None or self.ax is None:
            if self.viewer_placeholder is not None:
                self.viewer_placeholder.configure(text=message)
            return

        self.ax.clear()
        self.ax.set_axis_off()
        self.ax.text2D(0.5, 0.5, message, transform=self.ax.transAxes, ha="center", va="center")
        self.canvas.draw_idle()

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
        span = max(max_x - min_x, max_y - min_y, max_z - min_z)
        if span <= 0:
            span = 1.0

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
        self.canvas.draw_idle()

    def on_search(self) -> None:
        keyword = self.keyword_var.get().strip()
        if not keyword:
            self._show_warning("Hint", "Please input a keyword.")
            return

        self.log(f"Searching: {keyword}")

        def _worker() -> list[SearchItem]:
            return search_components(keyword)

        def _on_success(results: list[SearchItem]) -> None:
            self.items = results
            assert self.tree is not None
            for iid in self.tree.get_children():
                self.tree.delete(iid)

            for i, item in enumerate(results):
                has3d = "Yes" if item.model_uuid else "No"
                title = item.display_title or item.title or "(No title)"
                self.tree.insert("", "end", iid=str(i), values=(item.index, title, item.manufacturer or "-", has3d))

            self.log(f"Search complete: {len(results)} result(s).")
            if results:
                self.tree.selection_set("0")
                self.tree.focus("0")
                self.on_result_selected()
            else:
                self._set_image_message("No image.")
                self._clear_mesh("No model.")
                self._show_info("Result", "No component found.")
            self._refresh_buttons()

        self._run_action_task("Searching...", _worker, _on_success, "Search failed")

    def on_result_selected(self, _event: Any = None) -> None:
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

        cached_image = self.image_cache.get(key)
        cached_mesh = self.mesh_cache.get(key)
        if cached_image is not None or cached_mesh is not None:
            self._apply_preview_data(
                token,
                item,
                {
                    "image_bytes": cached_image,
                    "image_error": None,
                    "mesh": cached_mesh,
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

        if self._preview_future is not None and not self._preview_future.done():
            self._preview_future.cancel()

        def _on_preview_success(result: dict[str, Any]) -> None:
            self._apply_preview_data(token, item, result)

        def _on_preview_error(exc: Exception) -> None:
            if token != self.preview_token:
                return
            self.log(f"Preview failed: {exc}")
            self._set_image_message("Preview failed.")
            self._clear_mesh("Preview failed.")

        self._preview_future = self._submit_background(
            _worker,
            on_success=_on_preview_success,
            on_error=_on_preview_error,
        )

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
            image_error = data.get("image_error")
            if image_error:
                self.log(f"Image preview failed: {image_error}")

        if mesh_data and data.get("has_model"):
            vertices, faces = mesh_data
            self._render_mesh(vertices, faces)
            self.log(f"3D preview ready: {len(vertices)} vertices, {len(faces)} triangles.")
        else:
            if data.get("has_model"):
                self._clear_mesh("Failed to load 3D model preview.")
                mesh_error = data.get("mesh_error")
                if mesh_error:
                    self.log(f"3D preview failed: {mesh_error}")
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

        out_dir = Path(self.step_dir_var.get().strip() or "step")
        force = self.force_var.get()
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

        out_dir = Path(self.obj_dir_var.get().strip() or "temp")
        force = self.force_var.get()
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

        out_dir = Path(self.ad_dir_var.get().strip() or "ad_lib")
        force = self.force_var.get()
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


def launch_gui_tk() -> int:
    if tk is None:
        raise LcedaApiError("Tkinter is unavailable. Please install a Python build with Tk support.")
    root = tk.Tk()
    LcedaGuiApp(root)
    root.mainloop()
    return 0

