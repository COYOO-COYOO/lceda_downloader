from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import (
    LcedaApiError,
    download_obj,
    download_step,
    export_ad_altium_libs,
    export_ad_sources,
    search_components,
    select_item,
)
from .gui_qt import QApplication, launch_gui_qt
from .gui_tk import launch_gui_tk, tk

def launch_gui(prefer_qt: bool = True) -> int:
    if prefer_qt and QApplication is not None:
        try:
            return launch_gui_qt()
        except Exception as exc:  # noqa: BLE001
            # Fall back to Tkinter only when Qt startup fails.
            if tk is not None:
                print(f"Qt GUI unavailable, fallback to Tkinter: {exc}", file=sys.stderr)
            else:
                raise
    return launch_gui_tk()


def cmd_search(args: argparse.Namespace) -> int:
    items = search_components(args.keyword)
    if not items:
        print("No results.")
        return 0

    count = min(len(items), args.limit)
    print(f"Found {len(items)} result(s), showing first {count}:")
    for item in items[:count]:
        model_flag = "yes" if item.model_uuid else "no"
        print(
            f"[{item.index:>3}] {item.display_title or item.title} | "
            f"Manufacturer: {item.manufacturer or '-'} | 3D model: {model_flag}"
        )
    return 0


def cmd_download_step(args: argparse.Namespace) -> int:
    item = select_item(args.keyword, args.index)
    out = download_step(item, out_dir=Path(args.output), force=args.force)
    print(f"STEP saved: {out}")
    return 0


def cmd_download_obj(args: argparse.Namespace) -> int:
    item = select_item(args.keyword, args.index)
    obj_path, mtl_path = download_obj(item, out_dir=Path(args.output), force=args.force)
    print(f"OBJ saved: {obj_path}")
    print(f"MTL saved: {mtl_path}")
    return 0


def cmd_export_ad(args: argparse.Namespace) -> int:
    item = select_item(args.keyword, args.index)
    if args.source_only:
        result = export_ad_sources(item, out_dir=Path(args.output), force=args.force)
        if "symbol" in result:
            print(f"Symbol source saved: {result['symbol']}")
        if "footprint" in result:
            print(f"Footprint source saved: {result['footprint']}")
        print(f"Guide saved: {result['guide']}")
    else:
        result = export_ad_altium_libs(
            item,
            out_dir=Path(args.output),
            force=args.force,
            dotnet_cmd=args.dotnet,
        )
        if "schlib" in result:
            print(f"SchLib saved: {result['schlib']}")
        if "pcblib" in result:
            print(f"PcbLib saved: {result['pcblib']}")
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    return launch_gui()


def cmd_gui_qt(_args: argparse.Namespace) -> int:
    return launch_gui_qt()


def cmd_gui_tk(_args: argparse.Namespace) -> int:
    return launch_gui_tk()


def run_interactive(show_default_hint: bool = True) -> int:
    if show_default_hint:
        print("No command detected, enter interactive mode.")
    else:
        print("Interactive CLI mode.")
    print("Press Ctrl+C at any time to exit.\n")

    keyword = input("Keyword: ").strip()
    if not keyword:
        print("Empty keyword, exit.")
        return 0

    items = search_components(keyword)
    if not items:
        print("No results.")
        return 0

    count = min(len(items), 20)
    print(f"\nFound {len(items)} result(s), showing first {count}:")
    for item in items[:count]:
        model_flag = "yes" if item.model_uuid else "no"
        print(
            f"[{item.index:>3}] {item.display_title or item.title} | "
            f"Manufacturer: {item.manufacturer or '-'} | 3D model: {model_flag}"
        )

    action = input("\nAction [1=download STEP, 2=download OBJ, 3=exit]: ").strip() or "3"
    if action == "3":
        print("Exit.")
        return 0
    if action not in {"1", "2"}:
        print("Invalid action, exit.")
        return 1

    index_raw = input("Index (default 1): ").strip() or "1"
    try:
        index = int(index_raw)
    except ValueError:
        print("Invalid index.")
        return 1

    item = select_item(keyword, index)
    force = input("Overwrite existing cache? [y/N]: ").strip().lower() == "y"

    if action == "1":
        output = input("STEP output dir (default step): ").strip() or "step"
        out_path = download_step(item, out_dir=Path(output), force=force)
        print(f"STEP saved: {out_path}")
    else:
        output = input("OBJ output dir (default temp): ").strip() or "temp"
        obj_path, mtl_path = download_obj(item, out_dir=Path(output), force=force)
        print(f"OBJ saved: {obj_path}")
        print(f"MTL saved: {mtl_path}")

    return 0


def cmd_interactive(_args: argparse.Namespace) -> int:
    return run_interactive(show_default_hint=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search LCSC components and download STEP/OBJ 3D models."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gui_parser = sub.add_parser("gui", help="Launch graphical interface.")
    gui_parser.set_defaults(func=cmd_gui)

    gui_qt_parser = sub.add_parser("gui-qt", help="Launch PyQt6 interface.")
    gui_qt_parser.set_defaults(func=cmd_gui_qt)

    gui_tk_parser = sub.add_parser("gui-tk", help="Launch Tkinter interface.")
    gui_tk_parser.set_defaults(func=cmd_gui_tk)

    search_parser = sub.add_parser("search", help="Search components by keyword.")
    search_parser.add_argument("keyword", help="Search keyword, e.g. C8755 or TYPE-C.")
    search_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum result rows to print (default: 20).",
    )
    search_parser.set_defaults(func=cmd_search)

    step_parser = sub.add_parser(
        "download-step",
        help="Search by keyword and download STEP by result index.",
    )
    step_parser.add_argument("keyword", help="Search keyword.")
    step_parser.add_argument(
        "--index",
        type=int,
        default=1,
        help="1-based result index from search list (default: 1).",
    )
    step_parser.add_argument(
        "--output",
        default="step",
        help="Directory to store STEP files (default: ./step).",
    )
    step_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing cached file.",
    )
    step_parser.set_defaults(func=cmd_download_step)

    obj_parser = sub.add_parser(
        "download-obj",
        help="Search by keyword and download OBJ/MTL by result index.",
    )
    obj_parser.add_argument("keyword", help="Search keyword.")
    obj_parser.add_argument(
        "--index",
        type=int,
        default=1,
        help="1-based result index from search list (default: 1).",
    )
    obj_parser.add_argument(
        "--output",
        default="temp",
        help="Directory to store OBJ/MTL files (default: ./temp).",
    )
    obj_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing cached files.",
    )
    obj_parser.set_defaults(func=cmd_download_obj)

    ad_parser = sub.add_parser(
        "export-ad",
        help="One-click export AD .SchLib/.PcbLib (plus EasyEDA source JSON).",
    )
    ad_parser.add_argument("keyword", help="Search keyword.")
    ad_parser.add_argument(
        "--index",
        type=int,
        default=1,
        help="1-based result index from search list (default: 1).",
    )
    ad_parser.add_argument(
        "--output",
        default="ad_lib",
        help="Directory to store AD output files (default: ./ad_lib).",
    )
    ad_parser.add_argument(
        "--source-only",
        action="store_true",
        help="Only export EasyEDA symbol/footprint source JSON, skip SchLib/PcbLib generation.",
    )
    ad_parser.add_argument(
        "--dotnet",
        default="dotnet",
        help="Dotnet command used to build/run the Altium exporter (default: dotnet).",
    )
    ad_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing exported files.",
    )
    ad_parser.set_defaults(func=cmd_export_ad)

    interactive_parser = sub.add_parser(
        "interactive",
        help="Run in interactive CLI mode.",
    )
    interactive_parser.set_defaults(func=cmd_interactive)

    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        try:
            return launch_gui()
        except LcedaApiError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LcedaApiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

