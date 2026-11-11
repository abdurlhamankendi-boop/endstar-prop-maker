// Endstar command bridge: lets Claude drive this Unity editor through files.
// Claude writes <tool folder>\unity\inbox\<id>.cmd holding one line, "<command> <argument>".
// On its next update the editor claims it by renaming it to <id>.run, reads and deletes that, runs it
// and writes <tool folder>\unity\outbox\<id>.txt. Python cancels by renaming <id>.cmd to
// <id>.cancelled, so of the two renames exactly one wins and a cancelled command never runs.
// Only the commands in Run() exist; there is no way to run arbitrary code through this file.
// Source of truth: <tool folder>\unity\Editor, copied into the SDK project's Assets\Editor\SendToEndless by
// tools\install_bridge.py (or unity_restart) together with bridge-root.txt, which names the tool folder.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using UnityEditor;
using UnityEngine;

[InitializeOnLoad]
static class EndstarBridge
{
    static readonly string Root = ToolFolder();
    const string AssetRoot = "Assets/User Data/SendToEndless";
    static readonly string Inbox = Path.Combine(Root, "unity", "inbox");
    internal static readonly string Outbox = Path.Combine(Root, "unity", "outbox");
    // Where the tools put the claimed-cell lists that setcells reads.
    internal static readonly string CellsFolder = Path.Combine(Root, "unity", "cells");

    // The tool folder (holding unity\inbox and unity\outbox), from bridge-root.txt next to these scripts;
    // without that file, the folder on the PC this was written on.
    static string ToolFolder()
    {
        try
        {
            var file = Path.Combine(Application.dataPath, "Editor", "SendToEndless", "bridge-root.txt");
            if (File.Exists(file))
            {
                var folder = File.ReadAllText(file).Trim();
                if (folder != "") return folder;
            }
        }
        catch (Exception)
        {
        }
        return @"C:\endstar-unity-mcp";
    }
    internal static string ToolRoot => Root;   // for PropMaterials.cs
    static double next;

    // Same colours as PALETTE in art/blender/make_props.py (linear values, as Blender stores them).
    static readonly Dictionary<string, Color> Palette = new Dictionary<string, Color>
    {
        { "signal_red", new Color(0.78f, 0.08f, 0.06f) }, { "white", new Color(0.92f, 0.92f, 0.9f) },
        { "glass", new Color(0.04f, 0.09f, 0.14f) }, { "metal", new Color(0.32f, 0.33f, 0.36f) },
        { "dark", new Color(0.05f, 0.05f, 0.06f) }, { "concrete", new Color(0.55f, 0.55f, 0.53f) },
        { "concrete_dark", new Color(0.38f, 0.38f, 0.37f) }, { "hazard_yellow", new Color(0.95f, 0.72f, 0.05f) },
        { "cone_orange", new Color(0.98f, 0.33f, 0.03f) }, { "sign_green", new Color(0.05f, 0.45f, 0.2f) },
        { "car_blue", new Color(0.1f, 0.32f, 0.7f) }, { "car_teal", new Color(0.05f, 0.55f, 0.5f) },
        { "car_yellow", new Color(0.95f, 0.75f, 0.12f) }, { "tyre", new Color(0.04f, 0.04f, 0.045f) },
        { "light", new Color(1.0f, 0.95f, 0.75f) },
        // Dark stone, plaster, wood and iron tones for scenes lit by lamps. `iron` is nearly black: a part on it shows no glow
        // until a script lights it (Visuals:SetEmissiveColor), and the glow map says which parts.
        { "stone", new Color(0.045f, 0.034f, 0.020f) }, { "stone_dark", new Color(0.022f, 0.016f, 0.010f) },
        { "stone_floor", new Color(0.030f, 0.026f, 0.021f) }, { "plaster", new Color(0.050f, 0.044f, 0.032f) },
        { "wood", new Color(0.117f, 0.048f, 0.018f) }, { "iron", new Color(0.034f, 0.034f, 0.04f) },
        // A lamp's pool of light on a wall: the wall's plaster colour, so where a pool's glow fades out (and in the editor,
        // where no script runs) it is the wall itself; it carries the glow map like iron.
        { "glow_plaster", new Color(0.050f, 0.044f, 0.032f) },
        { "glow_stone", new Color(0.045f, 0.034f, 0.020f) },   // the same for lamps on a stone wall
    };

    static EndstarBridge()
    {
        // Import workers run this constructor too: only the main editor may write ready.txt and take commands.
        if (AssetDatabase.IsAssetImportWorkerProcess()) return;
        Directory.CreateDirectory(Inbox);
        Directory.CreateDirectory(Outbox);
        EditorApplication.update += Poll;
        // Exporter exceptions thrown on Unity's own loop never reach the SDK's failure callback.
        Application.logMessageReceived -= PropMaker.OnLog;
        Application.logMessageReceived += PropMaker.OnLog;
        File.WriteAllText(Path.Combine(Outbox, "ready.txt"), $"{Application.unityVersion} {DateTime.Now:O}");
    }

    static double nextBeat;

    static void Poll()
    {
        if (EditorApplication.timeSinceStartup < next) return;
        next = EditorApplication.timeSinceStartup + 0.5;
        // A heartbeat every 5 s, so a stalled bridge can be told apart from a stalled editor.
        if (EditorApplication.timeSinceStartup >= nextBeat)
        {
            nextBeat = EditorApplication.timeSinceStartup + 5;
            // The log path comes last, so a reader can take everything after "log=".
            File.WriteAllText(Path.Combine(Outbox, "heartbeat.txt"),
                $"{DateTime.Now:O} compiling={EditorApplication.isCompiling} updating={EditorApplication.isUpdating} log={Application.consoleLogPath}");
        }
        // Only compiling pauses the bridge: after an export the asset database once looked busy for good.
        if (EditorApplication.isCompiling) return;
        foreach (var file in Directory.GetFiles(Inbox, "*.cmd").OrderBy(f => f))
        {
            var id = Path.GetFileNameWithoutExtension(file);
            var claimed = Path.Combine(Inbox, id + ".run");
            // Claim first, with one atomic rename. Reading first left a gap in which Python's cancel could
            // succeed and the command still run.
            try
            {
                File.Move(file, claimed);
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                continue;   // Python cancelled it (FileNotFoundException is an IOException), or still holds it
            }
            string line;
            try
            {
                line = File.ReadAllText(claimed).Trim();
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                // Claimed, so Python can no longer cancel it: answer, or the caller waits for nothing.
                File.WriteAllText(Path.Combine(Outbox, id + ".txt"), "ERROR the command file could not be read: " + e.Message);
                TryDelete(claimed);
                return;
            }
            TryDelete(claimed);
            // The id is the send time. An older command was left behind by a caller that gave up
            // (send() waits 180 s), so running it now would run it late and out of order.
            if (DateTime.TryParseExact(id, "yyyyMMdd-HHmmss-fff", CultureInfo.InvariantCulture, DateTimeStyles.None, out var sent)
                && (DateTime.Now - sent).TotalSeconds > 150)
            {
                File.WriteAllText(Path.Combine(Outbox, id + ".txt"), $"ERROR stale command (sent {sent:HH:mm:ss}), not run");
                continue;   // nothing ran, so the next command may go in this tick
            }
            var space = line.IndexOf(' ');
            var cmd = space < 0 ? line : line.Substring(0, space);
            var arg = space < 0 ? "" : line.Substring(space + 1).Trim();
            string result;
            try
            {
                result = Run(cmd, arg);
            }
            catch (Exception e)
            {
                result = "ERROR " + e;
            }
            File.WriteAllText(Path.Combine(Outbox, id + ".txt"), result);
            return;   // one command per tick, so an import or compile in between can finish
        }
    }

    // A .run file left behind is harmless (only *.cmd is read), so failing to delete one must not
    // throw out of Poll on every tick.
    static void TryDelete(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (Exception)
        {
        }
    }

    static string Run(string cmd, string arg)
    {
        switch (cmd)
        {
            case "ping": return $"{Application.unityVersion} {Application.dataPath} log={Application.consoleLogPath}";
            case "refresh": AssetDatabase.Refresh(); return "ok";
            case "import": return Import();
            case "shaders": return Shaders(arg);
            case "menus": return Menus(arg);
            case "menu": return Menu(arg);
            case "types": return Types(arg);
            case "materials": return Materials(arg);
            case "propmaterials": return PropMaterials.Run(arg);   // the prop uploader: materials from the model's own (PropMaterials.cs)
            case "prefab": return Prefab(arg);
            case "selection": return SelectAsset(arg);
            case "members": return Members(arg);
            case "makeprop": return PropMaker.Make(arg);
            case "propinfo": return PropMaker.Info(arg);
            case "exportprop": return PropMaker.Export(arg);
            case "exportstate": return PropMaker.ExportState();
            case "exportreport": return PropMaker.Report();
            case "frame": return PropMaker.Frame(arg);
            case "setcells": return PropMaker.SetCells(arg);
            case "navtest": return PropMaker.NavTest(arg);
            case "windows": return Windows();
            case "place": return Place(arg);
            case "tileparent": return TilesetMaker.Parent(arg);   // tiles (10 Nov): the SDK's Choose Parent Set window, from code
            case "tilepick": return TilesetMaker.Pick();
            case "tileeditor": return TilesetMaker.Editor(arg);   // the SDK's own tile set editor, on that profile
            case "tileinfo": return TilesetMaker.Info(arg);
            default: return "ERROR unknown command " + cmd;
        }
    }

    // Copies every FBX from art/export into the project and imports it.
    static string Import()
    {
        var target = Path.Combine(Directory.GetParent(Application.dataPath).FullName, AssetRoot.Replace('/', '\\'), "Models");
        Directory.CreateDirectory(target);
        var copied = new List<string>();
        foreach (var fbx in Directory.GetFiles(Path.Combine(Root, "art", "export"), "*.fbx"))
        {
            File.Copy(fbx, Path.Combine(target, Path.GetFileName(fbx)), true);
            copied.Add(Path.GetFileName(fbx));
        }
        AssetDatabase.Refresh();
        return "imported " + string.Join(", ", copied);
    }

    // Lists shaders whose name contains `filter`, with every property name and type.
    static string Shaders(string filter)
    {
        var sb = new StringBuilder();
        foreach (var guid in AssetDatabase.FindAssets("t:Shader"))
        {
            var path = AssetDatabase.GUIDToAssetPath(guid);
            var shader = AssetDatabase.LoadAssetAtPath<Shader>(path);
            if (shader == null || shader.name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
            sb.AppendLine($"SHADER {shader.name}  ({path})");
            for (var i = 0; i < shader.GetPropertyCount(); i++)
                sb.AppendLine($"  {shader.GetPropertyName(i)}  {shader.GetPropertyType(i)}  \"{shader.GetPropertyDescription(i)}\"");
        }
        return sb.Length == 0 ? "no shader matches " + filter : sb.ToString();
    }

    // Lists editor menu items (from [MenuItem] attributes) whose path starts with `prefix`.
    static string Menus(string prefix)
    {
        var items = new SortedSet<string>();
        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            Type[] types;
            try { types = asm.GetTypes(); } catch (ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null).ToArray(); }
            foreach (var type in types)
            foreach (var method in type.GetMethods(BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic))
            foreach (var attr in method.GetCustomAttributes(typeof(MenuItem), false).Cast<MenuItem>())
                if (attr.menuItem.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    items.Add($"{attr.menuItem}   <- {type.FullName}.{method.Name} [{asm.GetName().Name}]");
        }
        return items.Count == 0 ? "no menu items start with " + prefix : string.Join("\n", items);
    }

    // Runs one menu item. Only the Endstar SDK's own menus, saving, and the editor windows are allowed.
    static string Menu(string path)
    {
        var allowed = new[] { "Endstar SDK/", "File/Save", "Window/", "Assets/Refresh" };
        if (!allowed.Any(a => path.StartsWith(a, StringComparison.Ordinal))) return "ERROR menu not allowed: " + path;
        return EditorApplication.ExecuteMenuItem(path) ? "ran " + path : "ERROR no such menu " + path;
    }

    // Lists public types (and their public members) in assemblies whose name contains `filter`.
    static string Types(string filter)
    {
        var sb = new StringBuilder();
        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies()
                     .Where(a => a.GetName().Name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) >= 0))
        {
            sb.AppendLine($"ASSEMBLY {asm.GetName().Name}");
            Type[] types;
            try { types = asm.GetExportedTypes(); } catch (Exception) { continue; }
            foreach (var type in types.OrderBy(t => t.FullName))
            {
                sb.AppendLine($"  {type.FullName}");
                foreach (var m in type.GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly))
                    sb.AppendLine($"    static {m.Name}({string.Join(", ", m.GetParameters().Select(p => p.ParameterType.Name))})");
            }
        }
        return sb.Length == 0 ? "no assembly matches " + filter : sb.ToString();
    }

    // "materials <shader name>|<colour property>[|<emission property>[|<glow texture>]]": makes one material per
    // palette colour on the given shader and points every imported model at them. The glow texture goes on iron.
    static string Materials(string arg)
    {
        var parts = arg.Split('|');
        if (parts.Length < 2) return "ERROR usage: materials <shader>|<colour property>[|<emission property>]";
        var shader = Shader.Find(parts[0]);
        if (shader == null) return "ERROR shader not found: " + parts[0];
        var folder = AssetRoot + "/Materials";
        if (!AssetDatabase.IsValidFolder(folder)) AssetDatabase.CreateFolder(AssetRoot, "Materials");
        var made = new Dictionary<string, Material>();
        foreach (var pair in Palette)
        {
            var path = $"{folder}/{pair.Key}.mat";
            var mat = AssetDatabase.LoadAssetAtPath<Material>(path);
            if (mat == null)
            {
                mat = new Material(shader);
                AssetDatabase.CreateAsset(mat, path);
            }
            mat.shader = shader;
            // Blender stores linear colours; SetColor expects sRGB, so convert to keep the same look.
            var c = pair.Value;
            var srgb = new Color(Mathf.LinearToGammaSpace(c.r), Mathf.LinearToGammaSpace(c.g), Mathf.LinearToGammaSpace(c.b), 1);
            mat.SetColor(parts[1], srgb);
            if (parts.Length > 2 && pair.Key == "light") mat.SetColor(parts[2], srgb * 2f);
            // The wall lights' materials: a glow map so the flame, wall wash and floor pool fade, scaled by the
            // emission colour a script sets (4 Nov). They start with no emission, so iron's other users do not change.
            if (parts.Length > 3 && (pair.Key == "iron" || pair.Key.StartsWith("glow_")))
            {
                AssetDatabase.ImportAsset(parts[3]);   // copied in just before this command
                var glow = AssetDatabase.LoadAssetAtPath<Texture2D>(parts[3]);
                if (glow == null) return "ERROR no texture " + parts[3];
                mat.SetTexture("_Emissive_Map", glow);
                mat.SetColor(parts[2], Color.black);
            }
            EditorUtility.SetDirty(mat);
            made[pair.Key] = mat;
        }
        AssetDatabase.SaveAssets();
        var report = new StringBuilder();
        foreach (var guid in AssetDatabase.FindAssets("t:Model", new[] { AssetRoot + "/Models" }))
        {
            var path = AssetDatabase.GUIDToAssetPath(guid);
            var importer = (ModelImporter)AssetImporter.GetAtPath(path);
            // A model whose pictures propmaterials put on (an upload with its own pictures) keeps them: this run would clear its
            // mapping and report its picture materials MISSING (11 Nov).
            if (PropMaterials.HasOwnMaterials(Path.GetFileNameWithoutExtension(path)))
            {
                report.AppendLine($"{Path.GetFileName(path)}: kept (its own picture materials)");
                continue;
            }
            // A model whose materials are already mapped lists none of its own, so the mapped names count too;
            // without them every run cleared the mapping of each model the run before had mapped.
            var mapped = importer.GetExternalObjectMap().Keys.Where(k => k.type == typeof(Material)).ToList();
            var used = AssetDatabase.LoadAllAssetsAtPath(path).OfType<Material>().Select(m => m.name)
                .Concat(mapped.Select(k => k.name)).Distinct().ToList();
            foreach (var id in mapped)
                importer.RemoveRemap(id);
            var missing = new List<string>();
            foreach (var name in used)
            {
                if (made.TryGetValue(name, out var mat))
                    importer.AddRemap(new AssetImporter.SourceAssetIdentifier(typeof(Material), name), mat);
                else
                    missing.Add(name);
            }
            importer.SaveAndReimport();
            report.AppendLine($"{Path.GetFileName(path)}: {string.Join(", ", used)}" + (missing.Count > 0 ? $"  MISSING {string.Join(", ", missing)}" : ""));
        }
        return report.ToString();
    }

    // "prefab <Model>": a prefab of the model with one Box Collider fitted around all its meshes.
    static string Prefab(string model)
    {
        var modelPath = $"{AssetRoot}/Models/{model}.fbx";
        var source = AssetDatabase.LoadAssetAtPath<GameObject>(modelPath);
        if (source == null) return "ERROR no model " + modelPath;
        var folder = AssetRoot + "/Prefabs";
        if (!AssetDatabase.IsValidFolder(folder)) AssetDatabase.CreateFolder(AssetRoot, "Prefabs");
        var instance = (GameObject)PrefabUtility.InstantiatePrefab(source);
        try
        {
            var renderers = instance.GetComponentsInChildren<Renderer>();
            var bounds = renderers[0].bounds;
            foreach (var r in renderers) bounds.Encapsulate(r.bounds);
            var box = instance.AddComponent<BoxCollider>();
            box.center = instance.transform.InverseTransformPoint(bounds.center);
            box.size = bounds.size;
            var path = $"{folder}/{model}.prefab";
            PrefabUtility.SaveAsPrefabAsset(instance, path);
            return $"{path}  bounds centre {bounds.center} size {bounds.size}";
        }
        finally
        {
            UnityEngine.Object.DestroyImmediate(instance);
        }
    }

    // "members <full type name>": the public fields, properties, methods and events of one type,
    // with their signatures (the public API only).
    static string Members(string typeName)
    {
        var type = AppDomain.CurrentDomain.GetAssemblies().Select(a => a.GetType(typeName)).FirstOrDefault(t => t != null);
        if (type == null) return "ERROR no type " + typeName;
        const BindingFlags flags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;
        var sb = new StringBuilder($"TYPE {type.FullName} : {type.BaseType?.FullName}\n");
        foreach (var f in type.GetFields(flags)) sb.AppendLine($"  field {(f.IsStatic ? "static " : "")}{f.FieldType.Name} {f.Name}");
        foreach (var p in type.GetProperties(flags)) sb.AppendLine($"  prop {p.PropertyType.Name} {p.Name} {(p.CanRead ? "get" : "")}{(p.CanWrite ? "/set" : "")}");
        foreach (var e in type.GetEvents(flags)) sb.AppendLine($"  event {e.EventHandlerType?.Name} {e.Name}");
        foreach (var c in type.GetConstructors()) sb.AppendLine($"  ctor ({string.Join(", ", c.GetParameters().Select(p => p.ParameterType.Name + " " + p.Name))})");
        foreach (var m in type.GetMethods(flags).Where(m => !m.IsSpecialName))
            sb.AppendLine($"  method {(m.IsStatic ? "static " : "")}{m.ReturnType.Name} {m.Name}({string.Join(", ", m.GetParameters().Select(p => p.ParameterType.Name + " " + p.Name))})");
        return sb.ToString();
    }

    // Lists every open editor window: type, title, whether it is docked, and its screen rectangle.
    static string Windows()
    {
        var sb = new StringBuilder();
        foreach (var w in Resources.FindObjectsOfTypeAll<EditorWindow>())
            sb.AppendLine($"{w.GetType().Name}  \"{w.titleContent.text}\"  docked={w.docked}  {w.position}");
        return sb.ToString();
    }

    // "place <window type>|x|y|width|height": moves a floating editor window (in editor points).
    static string Place(string arg)
    {
        var p = arg.Split('|');
        if (p.Length != 5) return "ERROR usage: place <type>|x|y|w|h";
        var window = Resources.FindObjectsOfTypeAll<EditorWindow>().FirstOrDefault(w => w.GetType().Name == p[0]);
        if (window == null) return "ERROR no open window of type " + p[0];
        window.position = new Rect(float.Parse(p[1]), float.Parse(p[2]), float.Parse(p[3]), float.Parse(p[4]));
        window.Show();
        window.Focus();
        return $"{p[0]} now at {window.position} docked={window.docked}";
    }

    // "selection <asset path>": selects and pings an asset, so the next screen step starts from it.
    static string SelectAsset(string path)
    {
        var asset = AssetDatabase.LoadMainAssetAtPath(path);
        if (asset == null) return "ERROR no asset " + path;
        Selection.activeObject = asset;
        EditorGUIUtility.PingObject(asset);
        return "selected " + path;
    }
}
