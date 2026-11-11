// "setcells <Prop name>|<file name>": gives a prop the claimed cells listed in <tool folder>\unity\cells\<file name>
// instead of the ones the SDK's voxelizer finds. For pieces built on a grid the tools know every wall,
// slab and step cell, so the list is exact: the voxelizer is a heuristic, and its result had to be proved afterwards
// (by a checking script of your own). The file has one cell per line, "x y z" as whole numbers (commas work too), and "#"
// starts a note. The cells are written the way the SDK's own wizard writes them, with the half-cell X/Z offsets off
// (the same as makeprop ...|grid); the prop must already exist (makeprop made its profile and prefab), and the icon is
// made again from the new cells, as FinishCells does. The Claimed Cell manager, the SDK window and the 30 s watch are
// not involved, so the answer comes at once.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using UnityEditor;
using UnityEngine;

static partial class PropMaker
{
    const int MaxCells = 20000;   // far above the biggest piece so far
    const int MaxSpan = 101;      // a level fits a box of 101 cells on each axis, so a bigger prop could never be placed

    // Reads the cells file's lines. Returns the first problem as a sentence (null when all is well); the cells come
    // out in file order, unique, none below the ground cell (the SDK never claims below y = 0), none out of reach.
    static string ParseCells(IEnumerable<string> lines, List<Vector3Int> cells)
    {
        var seen = new HashSet<Vector3Int>();
        var number = 0;
        foreach (var raw in lines)
        {
            number++;
            var line = raw;
            var note = line.IndexOf('#');
            if (note >= 0) line = line.Substring(0, note);
            var parts = line.Split(new[] { ' ', ',', '\t' }, StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length == 0) continue;
            var v = new int[3];
            var whole = parts.Length == 3;
            for (var i = 0; whole && i < 3; i++)
                whole = int.TryParse(parts[i], NumberStyles.Integer, CultureInfo.InvariantCulture, out v[i]);
            if (!whole) return $"line {number}: want three whole numbers x y z, got '{raw.Trim()}'";
            var cell = new Vector3Int(v[0], v[1], v[2]);
            if (cell.y < 0) return $"line {number}: y is below the ground cell: {cell.x} {cell.y} {cell.z}";
            if (!seen.Add(cell)) return $"line {number}: the cell {cell.x} {cell.y} {cell.z} is listed twice";
            cells.Add(cell);
            if (cells.Count > MaxCells) return $"more than {MaxCells} cells";
        }
        if (cells.Count == 0) return "no cells in the file";
        var lo = new Vector3Int(cells.Min(c => c.x), cells.Min(c => c.y), cells.Min(c => c.z));
        var hi = new Vector3Int(cells.Max(c => c.x), cells.Max(c => c.y), cells.Max(c => c.z));
        var span = hi - lo + Vector3Int.one;
        if (span.x > MaxSpan || span.y > MaxSpan || span.z > MaxSpan)
            return $"the cells span {span.x} x {span.y} x {span.z}, more than the {MaxSpan} cells a level fits on one axis";
        return null;
    }

    public static string SetCells(string arg)
    {
        var p = arg.Split('|');
        if (p.Length != 2) return "ERROR usage: setcells <Prop name>|<file name in unity\\cells>";
        string name = p[0], file = p[1];
        if (file == "" || file != Path.GetFileName(file) || file.StartsWith(".")) return "ERROR the cells file is a plain file name in unity\\cells, no folders: " + file;
        CheckExportLock();
        if (pendingCells != null) return $"ERROR cells for {pendingCells} are still being generated";
        if (exporting == name) return $"ERROR {name} is being exported since {exportStarted:HH:mm:ss}; its cells cannot change now";
        var profile = FindProfile(name);
        if (profile == null) return "ERROR no prop profile " + name;
        var path = Path.Combine(EndstarBridge.CellsFolder, file);
        if (!File.Exists(path)) return "ERROR no cells file " + path;
        var cells = new List<Vector3Int>();
        var problem = ParseCells(File.ReadAllLines(path), cells);
        if (problem != null) return $"ERROR {file}: {problem}";

        var item = Get(profile, "Item");
        var was = ((Vector3[])Call(item, "GetLocalCellPositions")).Length;
        var offsetType = SdkType("Endless.Props.Assets.PropLocationOffset");
        var offsets = Array.CreateInstance(offsetType, cells.Count);
        for (var i = 0; i < cells.Count; i++)
        {
            var offset = Activator.CreateInstance(offsetType);
            Set(offset, "Offset", cells[i]);
            offsets.SetValue(offset, i);
        }
        Call(item, "SetPropLocationOffsets", offsets);
        Set(item, "ApplyXOffset", false);
        Set(item, "ApplyZOffset", false);
        // The prop keeps its measured size until told otherwise; the cells just changed.
        PropType.GetField("bounds", BindingFlags.NonPublic | BindingFlags.Instance)?.SetValue(item, default(Vector3Int));

        var now = (Vector3[])Call(item, "GetLocalCellPositions");
        var folder = Path.GetDirectoryName(AssetDatabase.GetAssetPath(profile)).Replace('\\', '/');
        string icon;
        try
        {
            icon = MakeIcon(profile, folder, name, now);
        }
        catch (Exception e)
        {
            icon = "ERROR the icon was not made again: " + (e.InnerException ?? e).Message;   // the cells are still kept
        }
        EditorUtility.SetDirty(profile);
        AssetDatabase.SaveAssets();
        var line = $"set {name} {now.Length} cells (was {was}) from {file}, x offset {Get(item, "ApplyXOffset")}, z offset {Get(item, "ApplyZOffset")}, {icon}";
        Append(Path.Combine(Outbox, "makeprop.log"), $"{DateTime.Now:HH:mm:ss} {line}");
        return "cells " + line;
    }
}
