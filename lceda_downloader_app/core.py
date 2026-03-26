#!/usr/bin/env python3
"""LCSC/LcEDA 3D model downloader.

Features:
1) Search components by keyword.
2) Download STEP by selected search index.
3) Download OBJ and split embedded MTL content.
4) GUI mode with component image + 3D preview (OBJ mesh).
5) One-click export AD SchLib/PcbLib (via local dotnet bridge).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

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
    from PyQt6.QtCore import QThread, Qt, pyqtSignal
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QCheckBox,
        QFormLayout,
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
    QFormLayout = None
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
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib import cm as mpl_cm
    from matplotlib.figure import Figure
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
except Exception:
    FigureCanvasTkAgg = None
    mpl_cm = None
    Figure = None
    Poly3DCollection = None

try:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
except Exception:
    FigureCanvasQTAgg = None


SEARCH_API = "https://pro.lceda.cn/api/szlcsc/eda/product/list?wd={keyword}"
COMPONENT_API = "https://pro.lceda.cn/api/components/{uuid}?uuid={uuid}"
STEP_API = "https://modules.lceda.cn/qAxj6KHrDKw4blvCG8QJPs7Y/{model_uuid}"
OBJ_API = "https://modules.lceda.cn/3dmodel/{model_uuid}"

# Preview rendering tuning (does not affect exported files).
# Use higher limits to preserve pin-level detail.
PREVIEW_PARSE_MAX_TRIANGLES = 300000
PREVIEW_RENDER_MAX_TRIANGLES = 180000
PREVIEW_EDGE_COLOR = "#2f3f4f"
PREVIEW_EDGE_WIDTH = 0.10
PREVIEW_FACE_ALPHA = 1.0
PREVIEW_PIN_Z_FRACTION = 0.18
PREVIEW_PIN_COLOR = (0.80, 0.80, 0.82, 1.0)


@dataclass
class SearchItem:
    index: int
    display_title: str
    title: str
    manufacturer: str
    model_uuid: str | None
    raw: dict[str, Any]


class LcedaApiError(RuntimeError):
    pass


AD_EXPORT_GUIDE_TEXT = """AD library export guide (EasyEDA):

Important:
- EasyEDA Std API does not directly export .SchLib/.PcbLib.
- This folder contains symbol/footprint source JSON for the selected component.

Recommended workflow:
1) Open EasyEDA Pro.
2) Open/import the exported symbol and footprint source.
3) Use menu: File/Export -> Altium Designer.
4) In Altium Designer, use Design -> Make/Extract Library (if needed)
   to generate/organize SchLib and PcbLib.

Files in this folder:
- *_symbol_easyeda.json : schematic symbol source
- *_footprint_easyeda.json : PCB footprint source
"""

AD_ALTIUM_BUILDER_PROJECT = "EasyedaToAltiumBridge"
AD_ALTIUM_BUILDER_VERSION = "v1"
AD_ALTIUM_BUILDER_CSPROJ_TEXT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <LangVersion>latest</LangVersion>
  </PropertyGroup>

  <ItemGroup>
    <PackageReference Include="OriginalCircuit.AltiumSharp" Version="1.0.2" />
  </ItemGroup>
</Project>
"""

AD_ALTIUM_BUILDER_PROGRAM_TEXT = r"""using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text.Json;
using OriginalCircuit.AltiumSharp;
using OriginalCircuit.AltiumSharp.BasicTypes;
using OriginalCircuit.AltiumSharp.Records;

internal static class Program
{
    private const double SymbolUnitToMm = 0.254;     // EasyEDA symbol: 1 unit = 10 mil
    private const double FootprintUnitToMm = 0.0254; // EasyEDA footprint: 1 unit = 1 mil

    private sealed class Options
    {
        public string? SymbolPath { get; set; }
        public string? FootprintPath { get; set; }
        public string ComponentName { get; set; } = "component";
        public string SchLibPath { get; set; } = "component.SchLib";
        public string PcbLibPath { get; set; } = "component.PcbLib";
        public bool Force { get; set; }
    }

    private sealed class SymbolPinRaw
    {
        public string Id { get; set; } = "";
        public double X { get; set; }
        public double Y { get; set; }
        public double Length { get; set; }
        public double Rotation { get; set; }
    }

    private sealed class SymbolRectRaw
    {
        public double X1 { get; set; }
        public double Y1 { get; set; }
        public double X2 { get; set; }
        public double Y2 { get; set; }
    }

    private sealed class SymbolPolyRaw
    {
        public List<CoordPoint> Points { get; } = new List<CoordPoint>();
    }

    private sealed class SymbolEllipseRaw
    {
        public double X { get; set; }
        public double Y { get; set; }
        public double Rx { get; set; }
        public double Ry { get; set; }
    }

    private sealed class FootprintPadRaw
    {
        public string Designator { get; set; } = "";
        public double X { get; set; }
        public double Y { get; set; }
        public double Width { get; set; }
        public double Height { get; set; }
        public double Hole { get; set; }
        public double HoleSlot { get; set; }
        public string HoleShape { get; set; } = "ROUND";
        public double Rotation { get; set; }
        public int LayerCode { get; set; }
        public string Shape { get; set; } = "";
    }

    private sealed class FootprintPolyRaw
    {
        public int LayerCode { get; set; }
        public double Width { get; set; }
        public List<CoordPoint> Points { get; } = new List<CoordPoint>();
    }

    private sealed class FootprintCircleRaw
    {
        public int LayerCode { get; set; }
        public double Width { get; set; }
        public double X { get; set; }
        public double Y { get; set; }
        public double Radius { get; set; }
    }

    private sealed class FootprintArcRaw
    {
        public int LayerCode { get; set; }
        public double Width { get; set; }
        public double X { get; set; }
        public double Y { get; set; }
        public double Radius { get; set; }
        public double StartAngle { get; set; }
        public double EndAngle { get; set; }
    }

    private sealed class FootprintRegionRaw
    {
        public int LayerCode { get; set; }
        public List<CoordPoint> Points { get; } = new List<CoordPoint>();
    }

    private sealed class RawPoint
    {
        public double X { get; set; }
        public double Y { get; set; }
    }

    private static int Main(string[] args)
    {
        try
        {
            var options = ParseArgs(args);
            if (options is null)
            {
                PrintUsage();
                return 2;
            }

            if (string.IsNullOrWhiteSpace(options.SymbolPath) && string.IsNullOrWhiteSpace(options.FootprintPath))
            {
                Console.Error.WriteLine("No input specified. Need at least one of --symbol/--footprint.");
                return 2;
            }

            if (!string.IsNullOrWhiteSpace(options.SymbolPath))
            {
                Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(options.SchLibPath)) ?? ".");
                if (!File.Exists(options.SchLibPath) || options.Force)
                {
                    var schLib = BuildSchLibrary(options.ComponentName, options.SymbolPath!);
                    new SchLibWriter().Write(schLib, options.SchLibPath, true);
                }
            }

            if (!string.IsNullOrWhiteSpace(options.FootprintPath))
            {
                Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(options.PcbLibPath)) ?? ".");
                if (!File.Exists(options.PcbLibPath) || options.Force)
                {
                    var pcbLib = BuildPcbLibrary(options.ComponentName, options.FootprintPath!);
                    new PcbLibWriter().Write(pcbLib, options.PcbLibPath, true);
                }
            }

            Console.WriteLine("OK");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"ERROR: {ex.Message}");
            return 1;
        }
    }

    private static Options? ParseArgs(string[] args)
    {
        var options = new Options();
        for (var i = 0; i < args.Length; i++)
        {
            var a = args[i];
            if (a == "--symbol" && i + 1 < args.Length)
            {
                options.SymbolPath = args[++i];
                continue;
            }
            if (a == "--footprint" && i + 1 < args.Length)
            {
                options.FootprintPath = args[++i];
                continue;
            }
            if (a == "--name" && i + 1 < args.Length)
            {
                options.ComponentName = args[++i];
                continue;
            }
            if (a == "--schlib" && i + 1 < args.Length)
            {
                options.SchLibPath = args[++i];
                continue;
            }
            if (a == "--pcblib" && i + 1 < args.Length)
            {
                options.PcbLibPath = args[++i];
                continue;
            }
            if (a == "--force")
            {
                options.Force = true;
                continue;
            }
            return null;
        }
        return options;
    }

    private static void PrintUsage()
    {
        Console.WriteLine(
            "Usage: bridge --name <component> --schlib <out.SchLib> --pcblib <out.PcbLib> " +
            "[--symbol <symbol_easyeda.json>] [--footprint <footprint_easyeda.json>] [--force]"
        );
    }

    private static SchLib BuildSchLibrary(string componentName, string symbolJsonPath)
    {
        var rows = ParseEasyedaRows(symbolJsonPath);
        var pins = new List<SymbolPinRaw>();
        var rects = new List<SymbolRectRaw>();
        var polys = new List<SymbolPolyRaw>();
        var ellipses = new List<SymbolEllipseRaw>();
        var attrByParent = new Dictionary<string, Dictionary<string, string>>(StringComparer.OrdinalIgnoreCase);

        double? boxMinX = null;
        double? boxMaxX = null;
        double? boxMinY = null;
        double? boxMaxY = null;
        double? partBoxMinX = null;
        double? partBoxMaxX = null;
        double? partBoxMinY = null;
        double? partBoxMaxY = null;

        foreach (var row in rows)
        {
            if (!TryGetType(row, out var t))
                continue;

            if (t == "PIN")
            {
                var pin = new SymbolPinRaw
                {
                    Id = GetString(row, 1, ""),
                    X = GetDouble(row, 4, 0),
                    Y = GetDouble(row, 5, 0),
                    Length = GetDouble(row, 6, 20),
                    Rotation = GetDouble(row, 7, 0),
                };
                if (!string.IsNullOrWhiteSpace(pin.Id))
                    pins.Add(pin);

                var angle = NormalizeAngle(pin.Rotation);
                var dx = 0.0;
                var dy = 0.0;
                if (angle < 45.0 || angle >= 315.0)
                    dx = pin.Length;
                else if (angle < 135.0)
                    dy = pin.Length;
                else if (angle < 225.0)
                    dx = -pin.Length;
                else
                    dy = -pin.Length;

                UpdateBounds(ref boxMinX, ref boxMaxX, Math.Min(pin.X, pin.X + dx), Math.Max(pin.X, pin.X + dx));
                UpdateBounds(ref boxMinY, ref boxMaxY, Math.Min(pin.Y, pin.Y + dy), Math.Max(pin.Y, pin.Y + dy));
            }
            else if (t == "PART")
            {
                var partMetaElement = TryGetElement(row, 2);
                if (partMetaElement.HasValue && partMetaElement.Value.ValueKind == JsonValueKind.Object)
                {
                    var partMeta = partMetaElement.Value;
                    if (partMeta.TryGetProperty("BBOX", out var bbox) && bbox.ValueKind == JsonValueKind.Array && bbox.GetArrayLength() >= 4)
                    {
                        var x1 = GetDouble(bbox, 0, 0);
                        var y1 = GetDouble(bbox, 1, 0);
                        var x2 = GetDouble(bbox, 2, 0);
                        var y2 = GetDouble(bbox, 3, 0);
                        partBoxMinX = Math.Min(x1, x2);
                        partBoxMaxX = Math.Max(x1, x2);
                        partBoxMinY = Math.Min(y1, y2);
                        partBoxMaxY = Math.Max(y1, y2);
                        UpdateBounds(ref boxMinX, ref boxMaxX, partBoxMinX.Value, partBoxMaxX.Value);
                        UpdateBounds(ref boxMinY, ref boxMaxY, partBoxMinY.Value, partBoxMaxY.Value);
                    }
                }
            }
            else if (t == "ATTR")
            {
                var parent = GetString(row, 2, "");
                var key = GetString(row, 3, "");
                var value = GetString(row, 4, "");
                if (string.IsNullOrWhiteSpace(parent) || string.IsNullOrWhiteSpace(key))
                    continue;
                if (!attrByParent.TryGetValue(parent, out var map))
                {
                    map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                    attrByParent[parent] = map;
                }
                map[key] = value;
            }
            else if (t == "RECT")
            {
                var x1 = GetDouble(row, 2, 0);
                var y1 = GetDouble(row, 3, 0);
                var x2 = GetDouble(row, 4, 0);
                var y2 = GetDouble(row, 5, 0);
                rects.Add(
                    new SymbolRectRaw
                    {
                        X1 = x1,
                        Y1 = y1,
                        X2 = x2,
                        Y2 = y2,
                    }
                );
                UpdateBounds(ref boxMinX, ref boxMaxX, Math.Min(x1, x2), Math.Max(x1, x2));
                UpdateBounds(ref boxMinY, ref boxMaxY, Math.Min(y1, y2), Math.Max(y1, y2));
            }
            else if (t == "POLY")
            {
                var polyElement = TryGetElement(row, 2);
                if (!polyElement.HasValue)
                    continue;
                var rawPoints = ParsePathRawPoints(polyElement.Value);
                if (rawPoints.Count < 2)
                    continue;

                var poly = new SymbolPolyRaw();
                foreach (var rawPoint in rawPoints)
                {
                    UpdateBounds(ref boxMinX, ref boxMaxX, rawPoint.X, rawPoint.X);
                    UpdateBounds(ref boxMinY, ref boxMaxY, rawPoint.Y, rawPoint.Y);
                    poly.Points.Add(CoordPoint.FromMMs(UnitSymbolToMm(rawPoint.X), UnitSymbolToMm(rawPoint.Y)));
                }
                polys.Add(poly);
            }
            else if (t == "CIRCLE")
            {
                var x = GetDouble(row, 2, 0);
                var y = GetDouble(row, 3, 0);
                var r = Math.Abs(GetDouble(row, 4, 0));
                if (r <= 0.000001)
                    continue;
                ellipses.Add(
                    new SymbolEllipseRaw
                    {
                        X = x,
                        Y = y,
                        Rx = r,
                        Ry = r,
                    }
                );
                UpdateBounds(ref boxMinX, ref boxMaxX, x - r, x + r);
                UpdateBounds(ref boxMinY, ref boxMaxY, y - r, y + r);
            }
            else if (t == "ELLIPSE")
            {
                var x = GetDouble(row, 2, 0);
                var y = GetDouble(row, 3, 0);
                var rx = Math.Abs(GetDouble(row, 4, 0));
                var ry = Math.Abs(GetDouble(row, 5, 0));
                if (rx <= 0.000001 || ry <= 0.000001)
                    continue;
                ellipses.Add(
                    new SymbolEllipseRaw
                    {
                        X = x,
                        Y = y,
                        Rx = rx,
                        Ry = ry,
                    }
                );
                UpdateBounds(ref boxMinX, ref boxMaxX, x - rx, x + rx);
                UpdateBounds(ref boxMinY, ref boxMaxY, y - ry, y + ry);
            }
        }

        var schLib = new SchLib();
        var comp = new SchComponent
        {
            LibReference = componentName,
            ComponentDescription = "Generated from EasyEDA symbol",
        };
        var hasBodyPrimitive = false;

        if (rects.Count > 0)
        {
            foreach (var r in rects)
            {
                var body = new SchRectangle
                {
                    Location = CoordPoint.FromMMs(UnitSymbolToMm(r.X1), UnitSymbolToMm(r.Y1)),
                    Corner = CoordPoint.FromMMs(UnitSymbolToMm(r.X2), UnitSymbolToMm(r.Y2)),
                    LineWidth = LineWidth.Small,
                    Color = System.Drawing.Color.Blue,
                    AreaColor = System.Drawing.Color.Blue,
                    IsSolid = false,
                    Transparent = true,
                };
                comp.Add(body);
                hasBodyPrimitive = true;
            }
        }

        foreach (var p in polys)
        {
            if (p.Points.Count < 2)
                continue;
            for (var i = 0; i + 1 < p.Points.Count; i++)
            {
                var line = new SchLine
                {
                    Location = p.Points[i],
                    Corner = p.Points[i + 1],
                    LineWidth = LineWidth.Small,
                    Color = System.Drawing.Color.Blue,
                };
                comp.Add(line);
                hasBodyPrimitive = true;
            }
        }

        foreach (var e in ellipses)
        {
            const int ellipseSegments = 24;
            var first = default(CoordPoint);
            var prev = default(CoordPoint);
            for (var i = 0; i <= ellipseSegments; i++)
            {
                var a = (2.0 * Math.PI * i) / ellipseSegments;
                var ex = e.X + e.Rx * Math.Cos(a);
                var ey = e.Y + e.Ry * Math.Sin(a);
                var p = CoordPoint.FromMMs(UnitSymbolToMm(ex), UnitSymbolToMm(ey));
                if (i == 0)
                {
                    first = p;
                    prev = p;
                    continue;
                }
                var line = new SchLine
                {
                    Location = prev,
                    Corner = p,
                    LineWidth = LineWidth.Small,
                    Color = System.Drawing.Color.Blue,
                };
                comp.Add(line);
                prev = p;
                hasBodyPrimitive = true;
            }
        }

        if (!hasBodyPrimitive && partBoxMinX.HasValue && partBoxMaxX.HasValue && partBoxMinY.HasValue && partBoxMaxY.HasValue)
        {
            var body = new SchRectangle
            {
                Location = CoordPoint.FromMMs(UnitSymbolToMm(partBoxMinX.Value), UnitSymbolToMm(partBoxMaxY.Value)),
                Corner = CoordPoint.FromMMs(UnitSymbolToMm(partBoxMaxX.Value), UnitSymbolToMm(partBoxMinY.Value)),
                LineWidth = LineWidth.Small,
                Color = System.Drawing.Color.Blue,
                AreaColor = System.Drawing.Color.Blue,
                IsSolid = false,
                Transparent = true,
            };
            comp.Add(body);
            hasBodyPrimitive = true;
        }
        else if (!hasBodyPrimitive && boxMinX.HasValue && boxMaxX.HasValue && boxMinY.HasValue && boxMaxY.HasValue)
        {
            var margin = 6.0;
            var body = new SchRectangle
            {
                Location = CoordPoint.FromMMs(UnitSymbolToMm(boxMinX.Value - margin), UnitSymbolToMm(boxMaxY.Value + margin)),
                Corner = CoordPoint.FromMMs(UnitSymbolToMm(boxMaxX.Value + margin), UnitSymbolToMm(boxMinY.Value - margin)),
                LineWidth = LineWidth.Small,
                Color = System.Drawing.Color.Blue,
                AreaColor = System.Drawing.Color.Blue,
                IsSolid = false,
                Transparent = true,
            };
            comp.Add(body);
        }

        for (var i = 0; i < pins.Count; i++)
        {
            var p = pins[i];
            attrByParent.TryGetValue(p.Id, out var attrs);
            var number = SafeNonEmpty(attrs, "NUMBER") ?? (i + 1).ToString(CultureInfo.InvariantCulture);
            var name = SafeNonEmpty(attrs, "NAME") ?? number;
            var pinFlags = PinConglomerateFlags.DisplayNameVisible | PinConglomerateFlags.DesignatorVisible;
            pinFlags |= RotationToPinFlags(p.Rotation);
            var pin = new SchPin
            {
                Designator = number,
                Name = name,
                Location = CoordPoint.FromMMs(UnitSymbolToMm(p.X), UnitSymbolToMm(p.Y)),
                PinLength = Coord.FromMMs(UnitSymbolToMm(p.Length > 0.000001 ? p.Length : 10.0)),
                Color = System.Drawing.Color.Red,
                AreaColor = System.Drawing.Color.Red,
                Electrical = PinElectricalType.Passive,
                PinConglomerate = pinFlags,
                IsNameVisible = true,
                IsDesignatorVisible = true,
            };
            comp.Add(pin);
        }

        schLib.Add(comp);
        return schLib;
    }

    private static PcbLib BuildPcbLibrary(string componentName, string footprintJsonPath)
    {
        var rows = ParseEasyedaRows(footprintJsonPath);
        var pads = new List<FootprintPadRaw>();
        var overlayPolys = new List<FootprintPolyRaw>();
        var overlayCircles = new List<FootprintCircleRaw>();
        var overlayArcs = new List<FootprintArcRaw>();
        var overlayRegions = new List<FootprintRegionRaw>();
        var fallbackDesignator = 1;

        foreach (var row in rows)
        {
            if (!TryGetType(row, out var t))
                continue;

            if (t == "PAD")
            {
                var layerCode = GetInt(row, 4, 1);
                var designator = GetString(row, 5, "");
                if (string.IsNullOrWhiteSpace(designator))
                {
                    designator = fallbackDesignator.ToString(CultureInfo.InvariantCulture);
                    fallbackDesignator += 1;
                }

                var x = GetDouble(row, 6, 0);
                var y = GetDouble(row, 7, 0);
                // EasyEDA PAD rotation is primarily stored at index 8.
                // Some footprints may leave it empty; in that case fall back to index 14.
                var rotation = GetDouble(row, 8, double.NaN);
                if (double.IsNaN(rotation))
                    rotation = GetDouble(row, 14, 0);

                var hole = GetDouble(row, 9, 0);
                var holeSlot = hole;
                var holeShape = "ROUND";
                var holeElement = TryGetElement(row, 9);
                if (holeElement.HasValue && holeElement.Value.ValueKind == JsonValueKind.Array)
                {
                    var holeArr = holeElement.Value;
                    holeShape = GetString(holeArr, 0, "ROUND");
                    hole = GetDouble(holeArr, 1, 0);
                    holeSlot = GetDouble(holeArr, 2, hole);
                }

                var width = 10.0;
                var height = 10.0;
                var shape = "ROUND";
                var shapeElement = TryGetElement(row, 10);
                if (shapeElement.HasValue && shapeElement.Value.ValueKind == JsonValueKind.Array)
                {
                    var shapeArr = shapeElement.Value;
                    shape = GetString(shapeArr, 0, "ROUND");
                    if (string.Equals(shape, "POLY", StringComparison.OrdinalIgnoreCase))
                    {
                        var polyShapeElement = TryGetElement(shapeArr, 1);
                        if (polyShapeElement.HasValue)
                        {
                            var polyRawPoints = ParsePathRawPoints(polyShapeElement.Value);
                            if (polyRawPoints.Count >= 3)
                            {
                                var minX = polyRawPoints[0].X;
                                var maxX = polyRawPoints[0].X;
                                var minY = polyRawPoints[0].Y;
                                var maxY = polyRawPoints[0].Y;
                                foreach (var pt in polyRawPoints)
                                {
                                    minX = Math.Min(minX, pt.X);
                                    maxX = Math.Max(maxX, pt.X);
                                    minY = Math.Min(minY, pt.Y);
                                    maxY = Math.Max(maxY, pt.Y);
                                }
                                width = Math.Max(maxX - minX, width);
                                height = Math.Max(maxY - minY, height);
                            }
                        }
                    }
                    else
                    {
                        width = GetDouble(shapeArr, 1, width);
                        height = GetDouble(shapeArr, 2, width);
                    }
                }

                if (width <= 0)
                    width = 10.0;
                if (height <= 0)
                    height = width;

                pads.Add(
                    new FootprintPadRaw
                    {
                        Designator = designator,
                        X = x,
                        Y = y,
                        Width = width,
                        Height = height,
                        Hole = Math.Max(hole, 0),
                        HoleSlot = Math.Max(holeSlot, hole),
                        HoleShape = holeShape,
                        Rotation = rotation,
                        LayerCode = layerCode,
                        Shape = shape,
                    }
                );
            }
            else if (t == "POLY")
            {
                var layerCode = GetInt(row, 4, -1);
                if (!IsOverlayLayer(layerCode))
                    continue;

                var stroke = GetDouble(row, 5, 6.0);
                var polyElement = TryGetElement(row, 6);
                if (!polyElement.HasValue)
                    continue;

                if (TryParseCircleShape(polyElement.Value, out var cx, out var cy, out var radius))
                {
                    overlayCircles.Add(
                        new FootprintCircleRaw
                        {
                            LayerCode = layerCode,
                            Width = stroke,
                            X = cx,
                            Y = cy,
                            Radius = radius,
                        }
                    );
                    continue;
                }

                var points = ParseFootprintPathPoints(polyElement.Value);
                if (points.Count < 2)
                    continue;

                var poly = new FootprintPolyRaw
                {
                    LayerCode = layerCode,
                    Width = stroke,
                };
                foreach (var pt in points)
                    poly.Points.Add(pt);
                overlayPolys.Add(poly);
            }
            else if (t == "FILL")
            {
                var layerCode = GetInt(row, 4, -1);
                if (!IsOverlayLayer(layerCode))
                    continue;

                var shapesElement = TryGetElement(row, 7);
                if (!shapesElement.HasValue || shapesElement.Value.ValueKind != JsonValueKind.Array)
                    continue;

                foreach (var shape in shapesElement.Value.EnumerateArray())
                {
                    if (TryParseCircleShape(shape, out var cx, out var cy, out var radius))
                    {
                        var region = new FootprintRegionRaw
                        {
                            LayerCode = layerCode,
                        };
                        const int circleSegments = 32;
                        for (var i = 0; i < circleSegments; i++)
                        {
                            var a = (2.0 * Math.PI * i) / circleSegments;
                            var px = cx + radius * Math.Cos(a);
                            var py = cy + radius * Math.Sin(a);
                            region.Points.Add(CoordPoint.FromMMs(UnitFootprintToMm(px), UnitFootprintToMm(py)));
                        }
                        if (region.Points.Count >= 3)
                            overlayRegions.Add(region);
                        continue;
                    }

                    var points = ParseFootprintPathPoints(shape);
                    if (points.Count < 3)
                        continue;
                    var fillRegion = new FootprintRegionRaw
                    {
                        LayerCode = layerCode,
                    };
                    foreach (var pt in points)
                        fillRegion.Points.Add(pt);
                    if (!fillRegion.Points[0].Equals(fillRegion.Points[fillRegion.Points.Count - 1]))
                        fillRegion.Points.Add(fillRegion.Points[0]);
                    overlayRegions.Add(fillRegion);
                }
            }
            else if (t == "TRACK")
            {
                var layerCode = GetInt(row, 4, -1);
                if (!IsOverlayLayer(layerCode))
                    continue;
                var stroke = GetDouble(row, 5, 6.0);
                var x1 = GetDouble(row, 6, 0);
                var y1 = GetDouble(row, 7, 0);
                var x2 = GetDouble(row, 8, 0);
                var y2 = GetDouble(row, 9, 0);
                var poly = new FootprintPolyRaw
                {
                    LayerCode = layerCode,
                    Width = stroke,
                };
                poly.Points.Add(CoordPoint.FromMMs(UnitFootprintToMm(x1), UnitFootprintToMm(y1)));
                poly.Points.Add(CoordPoint.FromMMs(UnitFootprintToMm(x2), UnitFootprintToMm(y2)));
                overlayPolys.Add(poly);
            }
            else if (t == "RECT")
            {
                var layerCode = GetInt(row, 4, -1);
                if (!IsOverlayLayer(layerCode))
                    continue;
                var stroke = GetDouble(row, 5, 6.0);
                var x1 = GetDouble(row, 6, 0);
                var y1 = GetDouble(row, 7, 0);
                var x2 = GetDouble(row, 8, x1);
                var y2 = GetDouble(row, 9, y1);
                var poly = new FootprintPolyRaw
                {
                    LayerCode = layerCode,
                    Width = stroke,
                };
                poly.Points.Add(CoordPoint.FromMMs(UnitFootprintToMm(x1), UnitFootprintToMm(y1)));
                poly.Points.Add(CoordPoint.FromMMs(UnitFootprintToMm(x2), UnitFootprintToMm(y1)));
                poly.Points.Add(CoordPoint.FromMMs(UnitFootprintToMm(x2), UnitFootprintToMm(y2)));
                poly.Points.Add(CoordPoint.FromMMs(UnitFootprintToMm(x1), UnitFootprintToMm(y2)));
                poly.Points.Add(CoordPoint.FromMMs(UnitFootprintToMm(x1), UnitFootprintToMm(y1)));
                overlayPolys.Add(poly);
            }
            else if (t == "CIRCLE")
            {
                var layerCode = GetInt(row, 4, -1);
                if (!IsOverlayLayer(layerCode))
                    continue;
                var stroke = GetDouble(row, 5, 6.0);
                var x = GetDouble(row, 6, 0);
                var y = GetDouble(row, 7, 0);
                var r = Math.Abs(GetDouble(row, 8, 0));
                if (r <= 0.000001)
                    continue;
                overlayCircles.Add(
                    new FootprintCircleRaw
                    {
                        LayerCode = layerCode,
                        Width = stroke,
                        X = x,
                        Y = y,
                        Radius = r,
                    }
                );
            }
            else if (t == "ARC")
            {
                var layerCode = GetInt(row, 4, -1);
                if (!IsOverlayLayer(layerCode))
                    continue;
                var stroke = GetDouble(row, 5, 6.0);
                var x = GetDouble(row, 6, 0);
                var y = GetDouble(row, 7, 0);
                var r = Math.Abs(GetDouble(row, 8, 0));
                if (r <= 0.000001)
                    continue;
                var start = NormalizeAngle(GetDouble(row, 9, 0));
                var end = NormalizeAngle(GetDouble(row, 10, 0));
                overlayArcs.Add(
                    new FootprintArcRaw
                    {
                        LayerCode = layerCode,
                        Width = stroke,
                        X = x,
                        Y = y,
                        Radius = r,
                        StartAngle = start,
                        EndAngle = end,
                    }
                );
            }
        }

        var pcbLib = new PcbLib();
        var comp = new PcbComponent
        {
            Pattern = componentName,
            Description = "Generated from EasyEDA footprint",
            Height = Coord.FromMMs(1.0),
        };

        foreach (var padRaw in pads)
        {
            var shape = MapPadShape(padRaw.Shape, padRaw.Width, padRaw.Height);
            var holeMm = UnitFootprintToMm(padRaw.Hole);
            var altiumLayer = MapLayer(padRaw.LayerCode, holeMm);
            var template = MapPadTemplate(padRaw.LayerCode, holeMm);
            var sizeMm = CoordPoint.FromMMs(UnitFootprintToMm(padRaw.Width), UnitFootprintToMm(padRaw.Height));
            var pad = new PcbPad(template)
            {
                Designator = padRaw.Designator,
                Location = CoordPoint.FromMMs(UnitFootprintToMm(padRaw.X), UnitFootprintToMm(padRaw.Y)),
                Size = sizeMm,
                SizeTop = sizeMm,
                SizeMiddle = sizeMm,
                SizeBottom = sizeMm,
                Shape = shape,
                ShapeTop = shape,
                ShapeMiddle = shape,
                ShapeBottom = shape,
                HoleSize = Coord.FromMMs(holeMm),
                Rotation = NormalizeAngle(padRaw.Rotation),
                Layer = altiumLayer,
                StackMode = PcbStackMode.Simple,
                IsPlated = true,
            };
            if (holeMm > 0.000001 || padRaw.LayerCode == 12)
            {
                pad.HoleShape = MapPadHoleShape(padRaw.HoleShape);
                var slotMm = UnitFootprintToMm(padRaw.HoleSlot);
                if (pad.HoleShape == PcbPadHoleShape.Slot && slotMm > holeMm + 0.000001)
                    pad.HoleSlotLength = Coord.FromMMs(slotMm);
            }

            comp.Add(pad);
        }

        foreach (var poly in overlayPolys)
        {
            if (poly.Points.Count < 2)
                continue;
            var widthMm = ResolveGraphicWidthMm(poly.Width);
            for (var i = 0; i + 1 < poly.Points.Count; i++)
            {
                var track = new PcbTrack
                {
                    Layer = MapGraphicLayer(poly.LayerCode),
                    Width = Coord.FromMMs(widthMm),
                    Start = poly.Points[i],
                    End = poly.Points[i + 1],
                };
                comp.Add(track);
            }
        }

        foreach (var circle in overlayCircles)
        {
            var radiusMm = UnitFootprintToMm(Math.Abs(circle.Radius));
            if (radiusMm <= 0.000001)
                continue;
            var widthMm = ResolveGraphicWidthMm(circle.Width);
            var arc = new PcbArc
            {
                Layer = MapGraphicLayer(circle.LayerCode),
                Width = Coord.FromMMs(widthMm),
                Location = CoordPoint.FromMMs(UnitFootprintToMm(circle.X), UnitFootprintToMm(circle.Y)),
                Radius = Coord.FromMMs(radiusMm),
                StartAngle = 0.0,
                EndAngle = 360.0,
            };
            comp.Add(arc);
        }

        foreach (var raw in overlayArcs)
        {
            var radiusMm = UnitFootprintToMm(Math.Abs(raw.Radius));
            if (radiusMm <= 0.000001)
                continue;
            var widthMm = ResolveGraphicWidthMm(raw.Width);
            var arc = new PcbArc
            {
                Layer = MapGraphicLayer(raw.LayerCode),
                Width = Coord.FromMMs(widthMm),
                Location = CoordPoint.FromMMs(UnitFootprintToMm(raw.X), UnitFootprintToMm(raw.Y)),
                Radius = Coord.FromMMs(radiusMm),
                StartAngle = raw.StartAngle,
                EndAngle = raw.EndAngle,
            };
            comp.Add(arc);
        }

        foreach (var raw in overlayRegions)
        {
            if (raw.Points.Count < 3)
                continue;
            var region = new PcbRegion
            {
                Layer = MapGraphicLayer(raw.LayerCode),
            };
            foreach (var point in raw.Points)
                region.Outline.Add(point);
            comp.Add(region);
        }

        pcbLib.Add(comp);
        return pcbLib;
    }

    private static List<JsonElement> ParseEasyedaRows(string jsonPath)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(jsonPath));
        if (!doc.RootElement.TryGetProperty("result", out var result))
            throw new InvalidOperationException($"Invalid EasyEDA JSON: {jsonPath}");
        if (!result.TryGetProperty("dataStr", out var dataStrElement))
            throw new InvalidOperationException($"Missing result.dataStr in: {jsonPath}");
        var dataStr = dataStrElement.GetString();
        if (string.IsNullOrWhiteSpace(dataStr))
            throw new InvalidOperationException($"Empty dataStr in: {jsonPath}");

        var rows = new List<JsonElement>();
        var lines = dataStr!.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);
        foreach (var raw in lines)
        {
            var line = raw.Trim();
            if (line.Length == 0 || !line.StartsWith("[", StringComparison.Ordinal))
                continue;
            try
            {
                using var rowDoc = JsonDocument.Parse(line);
                if (rowDoc.RootElement.ValueKind == JsonValueKind.Array)
                    rows.Add(rowDoc.RootElement.Clone());
            }
            catch
            {
                // Ignore malformed rows, continue parsing valid rows.
            }
        }
        return rows;
    }

    private static bool TryGetType(JsonElement row, out string type)
    {
        type = GetString(row, 0, "");
        return !string.IsNullOrWhiteSpace(type);
    }

    private static JsonElement? TryGetElement(JsonElement array, int index)
    {
        if (array.ValueKind != JsonValueKind.Array)
            return null;
        if (index < 0 || index >= array.GetArrayLength())
            return null;
        return array[index];
    }

    private static string GetString(JsonElement array, int index, string fallback)
    {
        var element = TryGetElement(array, index);
        if (!element.HasValue)
            return fallback;
        var e = element.Value;
        return e.ValueKind switch
        {
            JsonValueKind.String => e.GetString() ?? fallback,
            JsonValueKind.Number => e.GetDouble().ToString(CultureInfo.InvariantCulture),
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            _ => fallback,
        };
    }

    private static double GetDouble(JsonElement array, int index, double fallback)
    {
        var element = TryGetElement(array, index);
        if (!element.HasValue)
            return fallback;
        var e = element.Value;
        if (e.ValueKind == JsonValueKind.Number && e.TryGetDouble(out var v))
            return v;
        if (e.ValueKind == JsonValueKind.String)
        {
            var s = e.GetString();
            if (double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed))
                return parsed;
        }
        return fallback;
    }

    private static int GetInt(JsonElement array, int index, int fallback)
    {
        var element = TryGetElement(array, index);
        if (!element.HasValue)
            return fallback;
        var e = element.Value;
        if (e.ValueKind == JsonValueKind.Number && e.TryGetInt32(out var v))
            return v;
        if (e.ValueKind == JsonValueKind.String)
        {
            var s = e.GetString();
            if (int.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed))
                return parsed;
        }
        return fallback;
    }

    private static string? SafeNonEmpty(Dictionary<string, string>? map, string key)
    {
        if (map is null)
            return null;
        if (!map.TryGetValue(key, out var value))
            return null;
        return string.IsNullOrWhiteSpace(value) ? null : value.Trim();
    }

    private static void UpdateBounds(ref double? min, ref double? max, double candidateMin, double candidateMax)
    {
        min = min.HasValue ? Math.Min(min.Value, candidateMin) : candidateMin;
        max = max.HasValue ? Math.Max(max.Value, candidateMax) : candidateMax;
    }

    private static bool TryParseCircleShape(JsonElement shape, out double cx, out double cy, out double radius)
    {
        cx = 0;
        cy = 0;
        radius = 0;
        if (shape.ValueKind != JsonValueKind.Array || shape.GetArrayLength() < 4)
            return false;
        var tag = GetString(shape, 0, "");
        if (!string.Equals(tag, "CIRCLE", StringComparison.OrdinalIgnoreCase))
            return false;
        cx = GetDouble(shape, 1, 0);
        cy = GetDouble(shape, 2, 0);
        radius = Math.Abs(GetDouble(shape, 3, 0));
        return radius > 0.000001;
    }

    private static bool TryGetNumber(JsonElement element, out double value)
    {
        value = 0;
        if (element.ValueKind == JsonValueKind.Number && element.TryGetDouble(out var n))
        {
            value = n;
            return true;
        }
        if (element.ValueKind == JsonValueKind.String)
        {
            var s = element.GetString();
            if (double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed))
            {
                value = parsed;
                return true;
            }
        }
        return false;
    }

    private static void AddRawPoint(List<RawPoint> points, double x, double y)
    {
        if (points.Count > 0)
        {
            var last = points[points.Count - 1];
            if (Math.Abs(last.X - x) < 1e-9 && Math.Abs(last.Y - y) < 1e-9)
                return;
        }
        points.Add(
            new RawPoint
            {
                X = x,
                Y = y,
            }
        );
    }

    private static List<RawPoint> ParsePathRawPoints(JsonElement shape)
    {
        var points = new List<RawPoint>();
        if (shape.ValueKind != JsonValueKind.Array)
            return points;
        if (shape.GetArrayLength() > 0)
        {
            var first = shape[0];
            if (first.ValueKind == JsonValueKind.String)
            {
                var t = first.GetString();
                if (string.Equals(t, "CIRCLE", StringComparison.OrdinalIgnoreCase))
                    return points;
            }
        }

        var len = shape.GetArrayLength();
        var i = 0;
        while (i < len)
        {
            var token = shape[i];
            if (token.ValueKind == JsonValueKind.String)
            {
                var cmd = (token.GetString() ?? "").Trim().ToUpperInvariant();
                i += 1;
                if (cmd == "L")
                {
                    while (i + 1 < len && TryGetNumber(shape[i], out var lx) && TryGetNumber(shape[i + 1], out var ly))
                    {
                        AddRawPoint(points, lx, ly);
                        i += 2;
                    }
                    continue;
                }
                if (cmd == "ARC" || cmd == "A")
                {
                    if (i + 2 < len && TryGetNumber(shape[i], out _) && TryGetNumber(shape[i + 1], out var ex) && TryGetNumber(shape[i + 2], out var ey))
                    {
                        AddRawPoint(points, ex, ey);
                        i += 3;
                        continue;
                    }
                    continue;
                }
                continue;
            }

            if (i + 1 < len && TryGetNumber(shape[i], out var x) && TryGetNumber(shape[i + 1], out var y))
            {
                AddRawPoint(points, x, y);
                i += 2;
                continue;
            }

            i += 1;
        }
        return points;
    }

    private static List<CoordPoint> ParseFootprintPathPoints(JsonElement shape)
    {
        var points = new List<CoordPoint>();
        var rawPoints = ParsePathRawPoints(shape);
        foreach (var rawPoint in rawPoints)
        {
            points.Add(CoordPoint.FromMMs(UnitFootprintToMm(rawPoint.X), UnitFootprintToMm(rawPoint.Y)));
        }
        return points;
    }

    private static bool IsOverlayLayer(int easyLayerCode)
    {
        return easyLayerCode == 3 || easyLayerCode == 4 || easyLayerCode == 49;
    }

    private static Layer MapGraphicLayer(int easyLayerCode)
    {
        return easyLayerCode switch
        {
            1 => Layer.TopLayer,
            2 => Layer.BottomLayer,
            3 => Layer.TopOverlay,
            4 => Layer.BottomOverlay,
            5 => Layer.TopSolder,
            6 => Layer.BottomSolder,
            7 => Layer.TopPaste,
            8 => Layer.BottomPaste,
            11 => Layer.Mechanical1,
            13 => Layer.Mechanical2,
            48 => Layer.Mechanical1,
            49 => Layer.TopOverlay,
            50 => Layer.Mechanical5,
            51 => Layer.Mechanical6,
            12 => Layer.MultiLayer,
            _ => Layer.TopOverlay,
        };
    }

    private static double ResolveGraphicWidthMm(double easyWidthValue)
    {
        var widthMm = UnitFootprintToMm(easyWidthValue);
        if (widthMm > 0.000001)
            return widthMm;
        return 0.05;
    }

    private static PinConglomerateFlags RotationToPinFlags(double rotationDeg)
    {
        // EasyEDA pin rotation reference is opposite to Altium pin direction.
        // Add 180deg to align name/designator placement with EasyEDA visual style.
        var a = NormalizeAngle(rotationDeg + 180.0);
        var quadrant = (int)Math.Round(a / 90.0, MidpointRounding.AwayFromZero) % 4;
        if (quadrant < 0)
            quadrant += 4;
        return quadrant switch
        {
            1 => PinConglomerateFlags.Rotated,
            2 => PinConglomerateFlags.Flipped,
            3 => PinConglomerateFlags.Rotated | PinConglomerateFlags.Flipped,
            _ => PinConglomerateFlags.None,
        };
    }

    private static Layer MapLayer(int easyLayerCode, double holeMm)
    {
        if (easyLayerCode == 12 || holeMm > 0.000001)
            return Layer.MultiLayer; // Through-hole
        return easyLayerCode switch
        {
            1 => Layer.TopLayer,
            2 => Layer.BottomLayer,
            _ => Layer.TopLayer,
        };
    }

    private static PcbPadTemplate MapPadTemplate(int easyLayerCode, double holeMm)
    {
        if (easyLayerCode == 12 || holeMm > 0.000001)
            return PcbPadTemplate.Tht;
        return easyLayerCode == 2 ? PcbPadTemplate.SmtBottom : PcbPadTemplate.SmtTop;
    }

    private static PcbPadHoleShape MapPadHoleShape(string holeShapeName)
    {
        var s = (holeShapeName ?? "").Trim().ToUpperInvariant();
        if (s.Contains("SLOT", StringComparison.Ordinal))
            return PcbPadHoleShape.Slot;
        if (s.Contains("SQUARE", StringComparison.Ordinal) || s.Contains("RECT", StringComparison.Ordinal))
            return PcbPadHoleShape.Square;
        return PcbPadHoleShape.Round;
    }

    private static PcbPadShape MapPadShape(string shapeName, double width, double height)
    {
        var s = (shapeName ?? "").Trim().ToUpperInvariant();
        if (s.Contains("POLY", StringComparison.Ordinal))
            return PcbPadShape.Rectangular;
        if (s.Contains("RECT", StringComparison.Ordinal))
            return PcbPadShape.Rectangular;
        if (s.Contains("OCT", StringComparison.Ordinal))
            return PcbPadShape.Octogonal;
        if (s.Contains("OVAL", StringComparison.Ordinal))
            return PcbPadShape.RoundedRectangle;
        if (Math.Abs(width - height) < 0.000001)
            return PcbPadShape.Round;
        return PcbPadShape.RoundedRectangle;
    }

    private static double UnitSymbolToMm(double value) => value * SymbolUnitToMm;

    private static double UnitFootprintToMm(double value) => value * FootprintUnitToMm;

    private static double NormalizeAngle(double value)
    {
        var a = value % 360.0;
        if (a < 0)
            a += 360.0;
        return a;
    }
}
"""


def _http_get(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 lceda-step-tool",
            "Accept-Encoding": "gzip",
        },
    )
    with urlopen(request, timeout=35) as response:
        content = response.read()
        encoding = response.headers.get("Content-Encoding", "").lower()
        if "gzip" in encoding:
            import gzip

            return gzip.decompress(content)
        return content


def _http_get_json(url: str) -> dict[str, Any]:
    try:
        payload = _http_get(url)
    except Exception as exc:  # noqa: BLE001
        raise LcedaApiError(f"Request failed: {url}") from exc

    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise LcedaApiError(f"Invalid JSON response from: {url}") from exc


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return cleaned or "component"


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    value = str(url).strip()
    if not value:
        return None
    if value.startswith("//"):
        return "https:" + value
    return value


def search_components(keyword: str) -> list[SearchItem]:
    data = _http_get_json(SEARCH_API.format(keyword=quote(keyword)))
    raw_results = data.get("result")
    if not isinstance(raw_results, list):
        return []

    items: list[SearchItem] = []
    for idx, raw in enumerate(raw_results, start=1):
        attrs = raw.get("attributes") or {}
        items.append(
            SearchItem(
                index=idx,
                display_title=str(raw.get("display_title") or ""),
                title=str(raw.get("title") or ""),
                manufacturer=str(attrs.get("Manufacturer") or ""),
                model_uuid=attrs.get("3D Model"),
                raw=raw,
            )
        )
    return items


def get_model_uuid(item: SearchItem) -> str:
    if not item.model_uuid:
        raise LcedaApiError("Selected component has no 3D model UUID.")

    detail = _http_get_json(COMPONENT_API.format(uuid=item.model_uuid))
    code = detail.get("code")
    if code == 0:
        result = detail.get("result") or {}
        model_uuid = result.get("3d_model_uuid")
        if model_uuid:
            return str(model_uuid)
    return str(item.model_uuid)


def choose_step_filename(item: SearchItem) -> str:
    footprint = (item.raw.get("footprint") or {}).get("display_title")
    base = str(footprint or item.display_title or item.title or "component")
    return sanitize_filename(base) + ".step"


def choose_obj_basename(item: SearchItem) -> str:
    base = item.title or item.display_title or "component"
    return sanitize_filename(base)


def choose_image_url(item: SearchItem) -> str | None:
    images = item.raw.get("images")
    if isinstance(images, list) and images:
        first = normalize_url(str(images[0]))
        if first:
            return first
    creator = item.raw.get("creator") or {}
    return normalize_url(creator.get("avatar"))

def download_step(item: SearchItem, out_dir: Path, force: bool) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / choose_step_filename(item)
    if out_file.exists() and not force:
        return out_file

    model_uuid = get_model_uuid(item)
    content = _http_get(STEP_API.format(model_uuid=quote(model_uuid)))
    out_file.write_bytes(content)
    return out_file


def split_obj_and_mtl(content_text: str) -> tuple[str, str]:
    lines = content_text.splitlines()
    mtl_lines: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("newmtl"):
            mtl_lines.append(line)
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                token = next_line.strip().split(" ", 1)[0] if next_line.strip() else ""
                if token in {"newmtl", "v", "vt", "vn", "f", "o", "g", "s", "usemtl", "mtllib"}:
                    break
                mtl_lines.append(next_line)
                j += 1
        i += 1

    obj_text = "\n".join(lines).strip() + "\n"
    mtl_text = "\n".join(mtl_lines).strip() + "\n" if mtl_lines else ""
    return obj_text, mtl_text


def parse_obj_mesh(
    content_text: str,
    max_triangles: int = PREVIEW_PARSE_MAX_TRIANGLES,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []

    for line in content_text.splitlines():
        if line.startswith("v "):
            parts = line.strip().split()
            if len(parts) >= 4:
                try:
                    vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    continue
            continue

        if not line.startswith("f "):
            continue

        parts = line.strip().split()[1:]
        face_indices: list[int] = []
        for token in parts:
            raw_idx = token.split("/", 1)[0]
            if not raw_idx:
                continue
            try:
                idx = int(raw_idx)
            except ValueError:
                continue

            if idx < 0:
                idx = len(vertices) + idx
            else:
                idx = idx - 1
            face_indices.append(idx)

        if len(face_indices) < 3:
            continue

        base = face_indices[0]
        for i in range(1, len(face_indices) - 1):
            triangles.append((base, face_indices[i], face_indices[i + 1]))
            if len(triangles) >= max_triangles:
                return vertices, triangles

    return vertices, triangles


def decimate_triangles(
    triangles: list[tuple[int, int, int]],
    max_triangles: int = PREVIEW_RENDER_MAX_TRIANGLES,
) -> list[tuple[int, int, int]]:
    if len(triangles) <= max_triangles or max_triangles <= 0:
        return triangles

    step = max(1, len(triangles) // max_triangles)
    reduced = triangles[::step]
    if len(reduced) > max_triangles:
        reduced = reduced[:max_triangles]
    return reduced


def decimate_triangles_preserve_pins(
    triangles: list[tuple[int, int, int]],
    vertices: list[tuple[float, float, float]],
    max_triangles: int = PREVIEW_RENDER_MAX_TRIANGLES,
) -> list[tuple[int, int, int]]:
    if len(triangles) <= max_triangles or max_triangles <= 0:
        return triangles
    if not vertices:
        return decimate_triangles(triangles, max_triangles)

    z_values = [v[2] for v in vertices]
    z_min = min(z_values)
    z_max = max(z_values)
    if abs(z_max - z_min) < 1e-12:
        return decimate_triangles(triangles, max_triangles)

    pin_z_threshold = z_min + PREVIEW_PIN_Z_FRACTION * (z_max - z_min)

    pin_faces: list[tuple[int, int, int]] = []
    other_faces: list[tuple[int, int, int]] = []
    for face in triangles:
        a, b, c = face
        if a < 0 or b < 0 or c < 0:
            continue
        if a >= len(vertices) or b >= len(vertices) or c >= len(vertices):
            continue
        z_center = (vertices[a][2] + vertices[b][2] + vertices[c][2]) / 3.0
        if z_center <= pin_z_threshold:
            pin_faces.append(face)
        else:
            other_faces.append(face)

    if len(pin_faces) >= max_triangles:
        return pin_faces[:max_triangles]

    remain = max_triangles - len(pin_faces)
    if remain <= 0:
        return pin_faces
    if len(other_faces) <= remain:
        return pin_faces + other_faces

    step = max(1, len(other_faces) // remain)
    sampled = other_faces[::step]
    if len(sampled) > remain:
        sampled = sampled[:remain]
    return pin_faces + sampled


def build_preview_facecolors(
    polygons: list[list[tuple[float, float, float]]],
) -> list[Any]:
    if not polygons:
        return []
    if mpl_cm is None:
        return ["#b9d7ef"] * len(polygons)

    z_values = [
        (triangle[0][2] + triangle[1][2] + triangle[2][2]) / 3.0
        for triangle in polygons
    ]
    z_min = min(z_values)
    z_max = max(z_values)
    if abs(z_max - z_min) < 1e-12:
        return ["#b9d7ef"] * len(polygons)

    cmap = mpl_cm.get_cmap("YlGnBu")
    pin_z_threshold = z_min + PREVIEW_PIN_Z_FRACTION * (z_max - z_min)
    colors: list[Any] = []
    for z in z_values:
        if z <= pin_z_threshold:
            colors.append(PREVIEW_PIN_COLOR)
            continue
        t = (z - z_min) / (z_max - z_min)
        colors.append(cmap(0.18 + 0.72 * t))
    return colors


def download_obj(item: SearchItem, out_dir: Path, force: bool) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base_name = choose_obj_basename(item)
    obj_file = out_dir / f"{base_name}.obj"
    mtl_file = out_dir / f"{base_name}.mtl"

    if obj_file.exists() and mtl_file.exists() and not force:
        return obj_file, mtl_file

    model_uuid = get_model_uuid(item)
    content = _http_get(OBJ_API.format(model_uuid=quote(model_uuid)))
    text = content.decode("utf-8", errors="ignore")
    obj_text, mtl_text = split_obj_and_mtl(text)

    obj_with_header = f"mtllib {base_name}.mtl\n{obj_text}"
    obj_file.write_text(obj_with_header, encoding="utf-8")
    mtl_file.write_text(mtl_text, encoding="utf-8")
    return obj_file, mtl_file


def select_item(keyword: str, index: int) -> SearchItem:
    items = search_components(keyword)
    if not items:
        raise LcedaApiError(f"No results found for keyword: {keyword}")
    if index < 1 or index > len(items):
        raise LcedaApiError(
            f"Invalid index {index}. Valid range: 1..{len(items)} for keyword '{keyword}'."
        )
    return items[index - 1]


def get_symbol_uuid(item: SearchItem) -> str | None:
    symbol = item.raw.get("symbol") or {}
    symbol_uuid = symbol.get("uuid")
    if symbol_uuid:
        return str(symbol_uuid)

    attrs = item.raw.get("attributes") or {}
    symbol_attr = attrs.get("Symbol")
    if symbol_attr:
        return str(symbol_attr)
    return None


def get_footprint_uuid(item: SearchItem) -> str | None:
    footprint = item.raw.get("footprint") or {}
    footprint_uuid = footprint.get("uuid")
    if footprint_uuid:
        return str(footprint_uuid)

    attrs = item.raw.get("attributes") or {}
    footprint_attr = attrs.get("Footprint")
    if footprint_attr:
        return str(footprint_attr)
    return None


def has_symbol_or_footprint(item: SearchItem | None) -> bool:
    if item is None:
        return False
    return bool(get_symbol_uuid(item) or get_footprint_uuid(item))


def export_ad_sources(
    item: SearchItem,
    out_dir: Path,
    force: bool,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    base = sanitize_filename(item.display_title or item.title or "component")
    exported: dict[str, Path] = {}

    symbol_uuid = get_symbol_uuid(item)
    footprint_uuid = get_footprint_uuid(item)
    if not symbol_uuid and not footprint_uuid:
        raise LcedaApiError("Selected component has no symbol/footprint uuid.")

    if symbol_uuid:
        symbol_data = _http_get_json(COMPONENT_API.format(uuid=symbol_uuid))
        symbol_file = out_dir / f"{base}_symbol_easyeda.json"
        if force or not symbol_file.exists():
            symbol_file.write_text(
                json.dumps(symbol_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        exported["symbol"] = symbol_file

    if footprint_uuid:
        footprint_data = _http_get_json(COMPONENT_API.format(uuid=footprint_uuid))
        footprint_file = out_dir / f"{base}_footprint_easyeda.json"
        if force or not footprint_file.exists():
            footprint_file.write_text(
                json.dumps(footprint_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        exported["footprint"] = footprint_file

    guide_file = out_dir / f"{base}_ad_export_guide.txt"
    if force or not guide_file.exists():
        guide_file.write_text(AD_EXPORT_GUIDE_TEXT, encoding="utf-8")
    exported["guide"] = guide_file
    return exported


def _write_text_if_changed(path: Path, content: str) -> bool:
    current = None
    if path.exists():
        try:
            current = path.read_text(encoding="utf-8")
        except Exception:
            current = None
    if current == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _build_dotnet_env(builder_root: Path, force_local_appdata: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    dotnet_home = str(builder_root / ".dotnet_home")
    nuget_packages = str(builder_root / ".nuget_packages")
    env["DOTNET_CLI_HOME"] = dotnet_home
    env.setdefault("DOTNET_ROLL_FORWARD", "LatestMajor")
    env["NUGET_PACKAGES"] = nuget_packages
    env["NUGET_HTTP_CACHE_PATH"] = str(builder_root / ".nuget_http_cache")
    env["NUGET_PLUGINS_CACHE_PATH"] = str(builder_root / ".nuget_plugins_cache")
    if force_local_appdata:
        appdata = str(builder_root / ".appdata")
        if os.name == "nt":
            env["APPDATA"] = appdata
        else:
            env["HOME"] = appdata
        Path(appdata).mkdir(parents=True, exist_ok=True)
    Path(dotnet_home).mkdir(parents=True, exist_ok=True)
    Path(nuget_packages).mkdir(parents=True, exist_ok=True)
    Path(env["NUGET_HTTP_CACHE_PATH"]).mkdir(parents=True, exist_ok=True)
    Path(env["NUGET_PLUGINS_CACHE_PATH"]).mkdir(parents=True, exist_ok=True)
    return env


def _run_subprocess(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout,
        check=False,
    )


def _ensure_ad_altium_builder(dotnet_cmd: str = "dotnet") -> Path:
    builder_root = Path(".ad_altium_builder").resolve()
    project_dir = builder_root / AD_ALTIUM_BUILDER_PROJECT
    project_dir.mkdir(parents=True, exist_ok=True)

    csproj_path = project_dir / f"{AD_ALTIUM_BUILDER_PROJECT}.csproj"
    program_path = project_dir / "Program.cs"
    version_path = project_dir / ".builder_version"
    nuget_config_path = builder_root / "nuget.config"

    changed = False
    changed |= _write_text_if_changed(csproj_path, AD_ALTIUM_BUILDER_CSPROJ_TEXT)
    changed |= _write_text_if_changed(program_path, AD_ALTIUM_BUILDER_PROGRAM_TEXT)
    changed |= _write_text_if_changed(version_path, AD_ALTIUM_BUILDER_VERSION + "\n")
    changed |= _write_text_if_changed(
        nuget_config_path,
        """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear />
    <add key="nuget" value="https://api.nuget.org/v3/index.json" />
  </packageSources>
</configuration>
""",
    )

    dll_path = project_dir / "bin" / "Release" / "net8.0" / f"{AD_ALTIUM_BUILDER_PROJECT}.dll"
    if dll_path.exists() and not changed:
        return dll_path

    env = _build_dotnet_env(builder_root)

    restore_cmd = [dotnet_cmd, "restore", str(csproj_path), "--nologo", "--configfile", str(nuget_config_path)]
    restore = _run_subprocess(
        restore_cmd,
        cwd=project_dir,
        env=env,
        timeout=1200,
    )
    if restore.returncode != 0:
        restore_text = (restore.stdout or "") + "\n" + (restore.stderr or "")
        if "NuGet.Config" in restore_text and "Access to the path" in restore_text:
            env = _build_dotnet_env(builder_root, force_local_appdata=True)
            restore = _run_subprocess(
                restore_cmd,
                cwd=project_dir,
                env=env,
                timeout=1200,
            )

    if restore.returncode != 0:
        details = "\n".join(
            [
                "Dotnet restore failed while preparing Altium exporter.",
                restore.stdout.strip(),
                restore.stderr.strip(),
                "Hint: ensure dotnet can access https://api.nuget.org and try again, "
                "or use '--source-only' to export EasyEDA JSON only.",
            ]
        ).strip()
        raise LcedaApiError(details)

    build = _run_subprocess(
        [
            dotnet_cmd,
            "build",
            str(csproj_path),
            "-c",
            "Release",
            "--nologo",
            "-v",
            "minimal",
            "--no-restore",
        ],
        cwd=project_dir,
        env=env,
        timeout=1200,
    )
    if build.returncode != 0:
        details = "\n".join(
            [
                "Dotnet build failed while preparing Altium exporter.",
                build.stdout.strip(),
                build.stderr.strip(),
            ]
        ).strip()
        raise LcedaApiError(details)

    if not dll_path.exists():
        raise LcedaApiError(
            "Altium exporter build finished but DLL was not found. "
            "Please check dotnet SDK installation."
        )
    return dll_path


def export_ad_altium_libs(
    item: SearchItem,
    out_dir: Path,
    force: bool,
    dotnet_cmd: str = "dotnet",
) -> dict[str, Path]:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    symbol_uuid = get_symbol_uuid(item)
    footprint_uuid = get_footprint_uuid(item)
    if not symbol_uuid and not footprint_uuid:
        raise LcedaApiError("Selected component has no symbol/footprint source for Altium conversion.")

    base = sanitize_filename(item.display_title or item.title or "component")
    schlib_path = (out_dir / f"{base}.SchLib").resolve()
    pcblib_path = (out_dir / f"{base}.PcbLib").resolve()

    sch_ok = (symbol_uuid is None) or schlib_path.exists()
    pcb_ok = (footprint_uuid is None) or pcblib_path.exists()
    if sch_ok and pcb_ok and not force:
        cached: dict[str, Path] = {}
        if symbol_uuid is not None:
            cached["schlib"] = schlib_path
        if footprint_uuid is not None:
            cached["pcblib"] = pcblib_path
        return cached

    dll_path = _ensure_ad_altium_builder(dotnet_cmd=dotnet_cmd)
    builder_root = Path(".ad_altium_builder").resolve()
    env = _build_dotnet_env(builder_root)

    with tempfile.TemporaryDirectory(prefix="easyeda_src_", dir=str(builder_root)) as temp_dir:
        temp_root = Path(temp_dir)
        symbol_path: Path | None = None
        footprint_path: Path | None = None

        if symbol_uuid is not None:
            symbol_data = _http_get_json(COMPONENT_API.format(uuid=symbol_uuid))
            symbol_path = temp_root / f"{base}_symbol_easyeda.json"
            symbol_path.write_text(json.dumps(symbol_data, ensure_ascii=False, indent=2), encoding="utf-8")

        if footprint_uuid is not None:
            footprint_data = _http_get_json(COMPONENT_API.format(uuid=footprint_uuid))
            footprint_path = temp_root / f"{base}_footprint_easyeda.json"
            footprint_path.write_text(json.dumps(footprint_data, ensure_ascii=False, indent=2), encoding="utf-8")

        cmd = [
            dotnet_cmd,
            str(dll_path),
            "--name",
            base,
            "--schlib",
            str(schlib_path),
            "--pcblib",
            str(pcblib_path),
        ]
        if symbol_path is not None:
            cmd.extend(["--symbol", str(symbol_path)])
        if footprint_path is not None:
            cmd.extend(["--footprint", str(footprint_path)])
        if force:
            cmd.append("--force")

        run = _run_subprocess(cmd, cwd=dll_path.parent, env=env, timeout=1200)
        if run.returncode != 0:
            details = "\n".join(
                [
                    "Altium library conversion failed.",
                    run.stdout.strip(),
                    run.stderr.strip(),
                ]
            ).strip()
            raise LcedaApiError(details)

    exported: dict[str, Path] = {}
    if symbol_uuid is not None:
        if not schlib_path.exists():
            raise LcedaApiError(f"Altium conversion succeeded but SchLib not found: {schlib_path}")
        exported["schlib"] = schlib_path
    if footprint_uuid is not None:
        if not pcblib_path.exists():
            raise LcedaApiError(f"Altium conversion succeeded but PcbLib not found: {pcblib_path}")
        exported["pcblib"] = pcblib_path

    stale_sources = [
        out_dir / f"{base}_symbol_easyeda.json",
        out_dir / f"{base}_footprint_easyeda.json",
        out_dir / f"{base}_ad_export_guide.txt",
    ]
    for stale in stale_sources:
        try:
            if stale.exists():
                stale.unlink()
        except Exception:
            pass
    return exported

