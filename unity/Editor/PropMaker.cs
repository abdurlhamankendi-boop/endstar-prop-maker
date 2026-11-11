// Builds Endstar props in code through the Endstar SDK's own public classes (PropProfile, Prop,
// LocationOffsetManager, IconSnapshotter, PropExporterV2), so no clicking is needed in the Prop SDK
// window. Every SDK type is looked up by name, so this file compiles even if the SDK changes; a missing
// piece shows up as an error message from the bridge instead.
// Used by EndstarBridge: makeprop, propinfo, exportprop, exportstate, exportreport, frame, setcells, navtest.
// One class in several files (all copied into the SDK project together, see install_bridge in
// tools\endstar_unity_mcp.py): PropMeshIds.cs (ids of a scripted prop's meshes), PropChecks.cs (the SDK's export
// checks), PropCells.cs (setcells) and PropNav.cs (navtest).
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

static partial class PropMaker
{
    const string ModelFolder = "Assets/User Data/SendToEndless/Models";
    const float AnchorBox = 0.25f;   // metres: the "anchor" collider mode's one box
    static string Outbox => EndstarBridge.Outbox;
    static object lastExporter;   // kept alive while an export runs

    // The export lock: two exports at once break each other's uploads, so only one may run.
    // Static fields, so a domain reload clears it too. Every release appends one of exactly five lines
    // to the export log, which Python reads as the verdict: "lock released: uploaded", "... the exporter
    // reported a failure", "... exporter exception", "... 3 minutes after the last stage" or
    // "... 20 minutes after the start".
    static string exporting;      // the prop being exported, null when idle
    static string exportLog;
    static UnityEngine.Object exportProfile;
    static DateTime exportStarted, lastStage;
    static int stagesTotal = -1, stagesDone, exportToken;
    static bool exportWasLocal;
    static double nextExportCheck;

    // The bundle folder an exporter exception names, e.g. ...\AssetBundles\Example Car\... (same rule
    // as exporter_exception in tools\endstar_unity_mcp.py).
    static readonly Regex BundleFolder = new Regex(@"AssetBundles[\\/]([^\\/""]+)[\\/]");

    // The prop whose claimed cells the voxelizer is still making (null when none). The manager holds
    // one active prop, so a second generation would clash with it.
    static string pendingCells;
    static UnityEngine.Object pendingProfile;
    static string pendingFolder;
    static DateTime pendingStarted;
    static MethodInfo voxelizingCheck;
    static bool sawVoxelizing, busySinceCheck;
    static double nextCellCheck;
    static int cellChecks, stableChecks, lastCellCount;

    static Type SdkType(string fullName)
    {
        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            var t = asm.GetType(fullName);
            if (t != null) return t;
        }
        throw new Exception("SDK type not found: " + fullName);
    }

    static Type ProfileType => SdkType("EndlessEditor.PropSDK.PropProfile");
    static Type PropType => SdkType("Endless.Props.Assets.Prop");

    // The signed-in creator's props folder, e.g. Assets/User Data/<username>/Props.
    static string PropsFolder()
    {
        foreach (var dir in AssetDatabase.GetSubFolders("Assets/User Data"))
            if (AssetDatabase.IsValidFolder(dir + "/Props")) return dir + "/Props";
        throw new Exception("No Assets/User Data/<user>/Props folder yet: make one prop in the Prop SDK window first.");
    }

    static UnityEngine.Object FindProfile(string name)
    {
        foreach (var guid in AssetDatabase.FindAssets("t:" + ProfileType.Name, new[] { PropsFolder() }))
        {
            var profile = AssetDatabase.LoadAssetAtPath(AssetDatabase.GUIDToAssetPath(guid), ProfileType);
            if (profile != null && profile.name == name) return profile;
        }
        return null;
    }

    static object Get(object target, string member)
    {
        var t = target.GetType();
        var p = t.GetProperty(member);
        if (p != null) return p.GetValue(target);
        var f = t.GetField(member);
        if (f != null) return f.GetValue(target);
        throw new Exception($"{t.Name} has no {member}");
    }

    static void Set(object target, string member, object value)
    {
        var t = target.GetType();
        var p = t.GetProperty(member);
        if (p != null && p.CanWrite) { p.SetValue(target, value); return; }
        var f = t.GetField(member);
        if (f != null) { f.SetValue(target, value); return; }
        throw new Exception($"{t.Name} has no settable {member}");
    }

    static object Call(object target, string method, params object[] args)
    {
        var t = target as Type ?? target.GetType();
        var m = t.GetMethods().FirstOrDefault(x => x.Name == method && x.GetParameters().Length == args.Length);
        if (m == null) throw new Exception($"{t.Name} has no {method}({args.Length} args)");
        return m.Invoke(target is Type ? null : target, args);
    }

    // An Action<T> of whatever T the SDK wants, which writes one line about what it receives to a
    // log file and then runs `after` (if given) with it.
    static Delegate Logger(Type actionType, string logPath, Func<object, string> text, Action<object> after = null)
    {
        var arg = actionType.GetGenericArguments()[0];
        var make = typeof(PropMaker).GetMethod(nameof(MakeLogger), BindingFlags.NonPublic | BindingFlags.Static).MakeGenericMethod(arg);
        return (Delegate)make.Invoke(null, new object[] { logPath, text, after });
    }

    static Action<T> MakeLogger<T>(string logPath, Func<object, string> text, Action<object> after)
    {
        return x =>
        {
            // These run inside the SDK's own stage, failure and offsets callbacks: nothing may throw back into them.
            try
            {
                Append(logPath, $"{DateTime.Now:HH:mm:ss} {text(x)}");
                after?.Invoke(x);
            }
            catch (Exception)
            {
            }
        };
    }

    // A log line that never throws: a log held open by another reader must not break the work being logged.
    static void Append(string path, string line)
    {
        try
        {
            File.AppendAllText(path, line + "\n");
        }
        catch (Exception)
        {
        }
    }

    static int CountOf(object x)
    {
        if (x is int n) return n;
        if (x is ICollection c) return c.Count;
        if (x is IEnumerable list) return list.Cast<object>().Count();
        return -1;
    }

    static string Describe(object x)
    {
        if (x == null) return "(null)";
        if (x is string s) return s;
        if (x is IEnumerable list) return "[" + string.Join(", ", list.Cast<object>().Select(o => o?.ToString())) + "]";
        return x.ToString();
    }

    static UnityEngine.Object PropSdkWindow()
    {
        var sdk = Resources.FindObjectsOfTypeAll(SdkType("EndlessEditor.PropSDK.PropSDK")).FirstOrDefault();
        if (sdk == null)
        {
            EditorApplication.ExecuteMenuItem("Endstar SDK/Prop SDK");
            sdk = Resources.FindObjectsOfTypeAll(SdkType("EndlessEditor.PropSDK.PropSDK")).FirstOrDefault();
        }
        return sdk ?? throw new Exception("Prop SDK window could not be opened");
    }

    // "makeprop <Model>|<Prop name>|<description>|<base type id>[|<lift>[|<component ids>[|each[|grid]]]]"
    // "each" gives every mesh its own Box Collider (walls made of separate slabs); otherwise one box surrounds the model.
    // "grid": the model is built on the cell grid and moved half a cell when it is an even number of cells wide
    // (the shift a grid-built piece's builder applies), so the SDK's half-cell X/Z offsets must stay off. Rebuilt the
    // SDK's own way, a grid-built prop's claimed cells came out half a cell off or cut short; with this, each grid cell
    // is one claimed cell.
    // The models' pivots are on the ground, but a prop's origin is the middle of its bottom cell
    // (the SDK guide models props inside a 1 m cube centred on the origin), so the model is lowered
    // by half a cell unless another lift is given.
    public static string Make(string arg)
    {
        if (pendingCells != null) return $"ERROR cells for {pendingCells} are still being generated";
        var p = arg.Split('|');
        if (p.Length < 4) return "ERROR usage: makeprop <Model>|<Prop name>|<description>|<base type id>[|<lift>]";
        string model = p[0], name = p[1], description = p[2], baseType = p[3];
        // The exporter is packing this prop's prefab, icon and cells: building it again now would change them under it.
        CheckExportLock();
        if (exporting == name) return $"ERROR {name} is being exported since {exportStarted:HH:mm:ss}; it cannot be built again now";
        var lift = p.Length > 4 && p[4] != "" ? float.Parse(p[4], System.Globalization.CultureInfo.InvariantCulture) : -0.5f;
        var componentIds = p.Length > 5 ? p[5].Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries) : new string[0];
        var colliderPerMesh = p.Length > 6 && p[6] == "each";
        // "anchor": one small box in the anchor cell and none on the meshes, for a prop of wall lights whose glowing floor
        // pools must not block a corridor; the box keeps the export check's collider rule (4 Nov, the light props).
        var colliderAtAnchor = p.Length > 6 && p[6] == "anchor";
        var onGrid = p.Length > 7 && p[7] == "grid";
        var report = new StringBuilder();
        var modelAsset = AssetDatabase.LoadAssetAtPath<GameObject>($"{ModelFolder}/{model}.fbx");
        if (modelAsset == null) return "ERROR no model " + model;
        StageUtility.GoToMainStage();   // never edit a prefab that is open in prefab mode

        // 1. The profile and its prop record: reuse one made in the window, or make a new one like it.
        var profile = FindProfile(name);
        string folder;
        if (profile == null)
        {
            folder = PropsFolder() + "/" + name;
            if (!AssetDatabase.IsValidFolder(folder)) AssetDatabase.CreateFolder(PropsFolder(), name);
            var root = new GameObject(name);
            var prefab = PrefabUtility.SaveAsPrefabAsset(root, $"{folder}/{name}.prefab");
            UnityEngine.Object.DestroyImmediate(root);
            profile = ScriptableObject.CreateInstance(ProfileType);
            profile.name = name;
            var prop = Activator.CreateInstance(PropType);
            Set(prop, "Name", name);
            Set(prop, "AssetID", Guid.NewGuid().ToString());
            Set(prop, "Description", description);
            // Stamped the way the SDK's own "New Prop" does it, whatever other profiles exist: left at the default
            // "0.0.0", the SDK window and the game treat the prop as an old one and move all its claimed cells by
            // half its footprint. The cell list starts empty, not null.
            Set(prop, "InternalVersion", CurrentInternalVersion());
            Set(prop, "AssetType", "prop");
            Call(prop, "SetPropLocationOffsets", Array.CreateInstance(SdkType("Endless.Props.Assets.PropLocationOffset"), 0));
            Set(profile, "Item", prop);
            Set(profile, "Prefab", prefab);
            Set(profile, "IsOnlyLocal", true);
            AssetDatabase.CreateAsset(profile, $"{folder}/{name}.asset");
            report.AppendLine("made new profile " + folder);
        }
        else
        {
            folder = Path.GetDirectoryName(AssetDatabase.GetAssetPath(profile)).Replace('\\', '/');
            report.AppendLine("reusing profile " + folder);
        }
        var item = Get(profile, "Item");
        // A profile that still says "0.0.0" gets the current version now: its cells are made again below.
        var stamped = Get(item, "InternalVersion") as string;
        if (string.IsNullOrEmpty(stamped) || stamped == "0.0.0")
        {
            Set(item, "InternalVersion", CurrentInternalVersion());
            report.AppendLine($"InternalVersion was '{stamped}', now {Get(item, "InternalVersion")}");
        }

        // 2. The prefab: the model as the only child, with one Box Collider around all its meshes.
        var prefabPath = AssetDatabase.GetAssetPath((UnityEngine.Object)Get(profile, "Prefab"));
        var contents = PrefabUtility.LoadPrefabContents(prefabPath);
        try
        {
            // What the old prefab's meshes were called by a script, read before the prefab is emptied (only a scripted
            // prop has ids, so a static one never touches the identifier type).
            var idsBefore = componentIds.Length > 0 ? ReadMeshIds(contents.transform) : new Dictionary<string, object>();
            for (var i = contents.transform.childCount - 1; i >= 0; i--)
                UnityEngine.Object.DestroyImmediate(contents.transform.GetChild(i).gameObject);
            var instance = (GameObject)PrefabUtility.InstantiatePrefab(modelAsset, contents.transform);
            instance.transform.localPosition = new Vector3(0, lift, 0);
            instance.transform.localRotation = Quaternion.identity;
            var renderers = instance.GetComponentsInChildren<Renderer>();
            var bounds = renderers[0].bounds;
            foreach (var r in renderers) bounds.Encapsulate(r.bounds);
            if (colliderAtAnchor)
            {
                var box = contents.AddComponent<BoxCollider>();   // the prop's origin is the middle of its anchor cell
                box.center = Vector3.zero;
                box.size = Vector3.one * AnchorBox;
            }
            else if (colliderPerMesh)
            {
                // A Box Collider added to a mesh object fits that mesh by itself.
                foreach (var r in renderers) r.gameObject.AddComponent<BoxCollider>();
            }
            else
            {
                var box = instance.AddComponent<BoxCollider>();
                box.center = instance.transform.InverseTransformPoint(bounds.center);
                box.size = bounds.size;
            }
            // The game's Script Reference window only lists meshes with a Transform Identifier, and Visuals calls
            // take that id, so every mesh of a scripted prop gets one. A script saved in the game stores these ids,
            // so a rebuilt prop keeps each mesh's old id (see PropMeshIds.cs).
            if (componentIds.Length > 0)
                report.AppendLine(StampMeshIds(name, contents.transform, renderers, idsBefore));
            PrefabUtility.SaveAsPrefabAsset(contents, prefabPath);
            report.AppendLine($"prefab {prefabPath}: {model} lifted {lift}, bounds {bounds.size}, " +
                              (colliderAtAnchor ? "one small collider at the anchor" :
                               colliderPerMesh ? $"{renderers.Length} colliders" : "one collider"));
        }
        finally
        {
            PrefabUtility.UnloadPrefabContents(contents);
        }

        // 3. Base type and components (e.g. Visuals, so a script can drive spinning parts).
        var setIds = PropType.GetMethod("SetComponentIds");
        var guidType = setIds.GetParameters()[0].ParameterType;
        var listType = setIds.GetParameters()[1].ParameterType;
        var components = (IList)Activator.CreateInstance(listType);
        var componentType = listType.GetGenericArguments()[0];   // the SDK keeps these as plain strings
        foreach (var id in componentIds)
            components.Add(componentType == typeof(string) ? id.Trim() : MakeGuid(componentType, id.Trim()));
        Call(item, "SetComponentIds", MakeGuid(guidType, baseType), components);
        report.AppendLine($"base type {Get(item, "BaseTypeId")}, components {Describe(Get(item, "ComponentIds"))}");

        // 4. Claimed cells, through the same manager the Claimed Cell Wizard uses, with the prop
        // opened the way clicking it in the Prop SDK sidebar opens it.
        EditorUtility.SetDirty(profile);
        AssetDatabase.SaveAssets();
        try
        {
            Call(SdkType("EndlessEditor.PropSDK.PropSDK"), "Open", profile);
        }
        catch (Exception e)
        {
            report.AppendLine("note: could not open the prop in the SDK window: " + (e.InnerException?.Message ?? e.Message));
        }
        var managerType = SdkType("EndlessEditor.PropSDK.LocationOffsets.LocationOffsetManager");
        var manager = managerType.GetProperty("Instance").GetValue(null);
        if (manager == null)
        {
            Call(managerType, "Initialize", PropSdkWindow());
            manager = managerType.GetProperty("Instance").GetValue(null);
        }
        Call(manager, "SetActiveProp", profile, true);
        Call(manager, "ClearOffsets");
        if (onGrid)
        {
            // The wizard's "Offset Axes" boxes, unticked before the bounds are measured, and the bounds measured
            // without working the offsets out again (unticking afterwards gave other, wrong cells).
            Set(manager, "ApplyXOffset", false);
            Set(manager, "ApplyZOffset", false);
        }
        Call(manager, "CalculateBounds", !onGrid);
        voxelizingCheck = SdkType("EndlessEditor.PropSDK.LocationOffsets.LocationOffsetGenerator")
            .GetMethod("IsVoxelizationInProgress", BindingFlags.Public | BindingFlags.Static);
        var log = Path.Combine(Outbox, "makeprop.log");
        Append(log, $"{DateTime.Now:HH:mm:ss} offsets requested for {name}");
        var gen = managerType.GetMethod("GenerateOffsets");
        // The SDK keeps a single completion callback and may call it much later, after another
        // makeprop, so its line names no prop.
        gen.Invoke(manager, new object[] { false, Logger(gen.GetParameters()[1].ParameterType, log, x => $"offsets done: {Describe(x)} cells") });
        report.AppendLine($"bounds {Get(manager, "GetBoundsSizeInt")}, x offset {Get(item, "ApplyXOffset")}, z offset {Get(item, "ApplyZOffset")}");

        // 5. The voxelizer runs over the next editor updates, so the icon, the save and the ready
        // marker wait in WatchCells until the cells exist.
        StartCellWatch(name, profile, folder);
        report.Append("cells pending");
        return report.ToString();
    }

    static string ReadyPath(string name) => Path.Combine(Outbox, "makeprop-" + name.Replace(' ', '_') + ".ready");

    static void StartCellWatch(string name, UnityEngine.Object profile, string folder)
    {
        pendingCells = name;
        pendingProfile = profile;
        pendingFolder = folder;
        pendingStarted = DateTime.Now;
        cellChecks = 0;
        stableChecks = 0;
        lastCellCount = -1;
        sawVoxelizing = busySinceCheck = Voxelizing();
        nextCellCheck = EditorApplication.timeSinceStartup + 0.5;
        try
        {
            File.Delete(ReadyPath(name));   // a marker from an earlier makeprop must not look like this one
        }
        catch (Exception)
        {
        }
        EditorApplication.update -= WatchCells;
        EditorApplication.update += WatchCells;
    }

    static bool Voxelizing()
    {
        try
        {
            return voxelizingCheck != null && (bool)voxelizingCheck.Invoke(null, null);
        }
        catch (Exception)
        {
            return false;
        }
    }

    // Runs on every editor update while cells are pending. The voxelizer's busy flag is sampled on every
    // update (a short run can start and end between two checks); the cell count is compared every 0.5 s.
    static void WatchCells()
    {
        if (pendingCells == null)
        {
            EditorApplication.update -= WatchCells;
            return;
        }
        var voxelizing = Voxelizing();
        sawVoxelizing |= voxelizing;
        busySinceCheck |= voxelizing;
        if (EditorApplication.timeSinceStartup < nextCellCheck) return;
        nextCellCheck = EditorApplication.timeSinceStartup + 0.5;
        cellChecks++;
        int count;
        try
        {
            count = ((Vector3[])Call(Get(pendingProfile, "Item"), "GetLocalCellPositions")).Length;
        }
        catch (Exception)
        {
            FinishCells("the cells could not be read");
            return;
        }
        // A check only counts as stable when the voxelizer was idle for the whole half second before it,
        // so the 2 stable checks both fall after generation ended (cells may land a frame after the flag clears).
        stableChecks = count == lastCellCount && !busySinceCheck ? stableChecks + 1 : 0;
        busySinceCheck = false;
        lastCellCount = count;
        // Zero cells is only final once the voxelizer was seen running and has stopped; before that it
        // may simply not have started yet.
        if (!voxelizing && stableChecks >= 2 && (count > 0 || sawVoxelizing))
            FinishCells("generated");
        // The cap also needs 20 checks, so an editor that slept through the 30 s is not cut short.
        else if ((DateTime.Now - pendingStarted).TotalSeconds >= 30 && cellChecks >= 20)
            FinishCells("30 s cap");
    }

    // Icon with the real cells, save, then the ready marker "ready <name> <n> cells" (or "ERROR ...").
    static void FinishCells(string why)
    {
        var name = pendingCells;
        var profile = pendingProfile;
        var folder = pendingFolder;
        pendingCells = null;
        pendingProfile = null;
        EditorApplication.update -= WatchCells;
        var log = Path.Combine(Outbox, "makeprop.log");
        string line;
        try
        {
            var item = Get(profile, "Item");
            var cells = (Vector3[])Call(item, "GetLocalCellPositions");
            var icon = MakeIcon(profile, folder, name, cells);
            EditorUtility.SetDirty(profile);
            AssetDatabase.SaveAssets();
            ShowInSidebar(profile, log);
            line = $"ready {name} {cells.Length} cells";
            Append(log, $"{DateTime.Now:HH:mm:ss} {line} ({why}), x offset {Get(item, "ApplyXOffset")}, z offset {Get(item, "ApplyZOffset")}, {icon}");
        }
        catch (Exception e)
        {
            line = $"ERROR finishing {name}: {(e.InnerException ?? e).Message}";
            Append(log, $"{DateTime.Now:HH:mm:ss} {line}");
        }
        // Written whole and then renamed, so a waiting reader never sees half a line.
        var ready = ReadyPath(name);
        try
        {
            var tmp = ready + ".tmp";
            File.WriteAllText(tmp, line);
            if (File.Exists(ready)) File.Delete(ready);
            File.Move(tmp, ready);
        }
        catch (Exception e)
        {
            Append(log, $"{DateTime.Now:HH:mm:ss} could not write {ready}: {e.Message}");
        }
    }

    // The icon, from the SDK's own snapshotter, saved as a sprite next to the profile.
    static string MakeIcon(UnityEngine.Object profile, string folder, string name, Vector3[] cells)
    {
        var snap = SdkType("EndlessEditor.PropSDK.IconSnapshotter").GetMethod("SnapshotIcon");
        var cellListType = snap.GetParameters()[1].ParameterType;
        var cellList = (IList)Activator.CreateInstance(cellListType);
        var cellType = cellListType.GetGenericArguments()[0];
        foreach (var c in cells)
            cellList.Add(cellType == typeof(Vector3Int) ? (object)Vector3Int.RoundToInt(c) : c);
        var texture = (Texture)snap.Invoke(null, new object[] { Get(profile, "Prefab"), cellList });
        var iconPath = $"{folder}/{name} Icon.png";
        File.WriteAllBytes(Path.Combine(Directory.GetParent(Application.dataPath).FullName, iconPath), ReadablePng(texture));
        AssetDatabase.ImportAsset(iconPath);
        var importer = (TextureImporter)AssetImporter.GetAtPath(iconPath);
        importer.textureType = TextureImporterType.Sprite;
        importer.spriteImportMode = SpriteImportMode.Single;
        importer.alphaIsTransparency = true;
        // The exporter re-encodes the icon to PNG while uploading, which only works on a readable,
        // uncompressed texture.
        importer.isReadable = true;
        importer.textureCompression = TextureImporterCompression.Uncompressed;
        importer.mipmapEnabled = false;
        importer.SaveAndReimport();
        Set(profile, "Icon", AssetDatabase.LoadAssetAtPath<Sprite>(iconPath));
        return $"icon {iconPath} {texture.width}x{texture.height}";
    }

    static void ShowInSidebar(UnityEngine.Object profile, string log)
    {
        try
        {
            var sdk = PropSdkWindow();
            Call(sdk, "InsertIntoSidebarWithEditCheck", profile);
            Set(sdk, "SelectedAsset", profile);
        }
        catch (Exception e)
        {
            Append(log, $"{DateTime.Now:HH:mm:ss} note: sidebar not updated: " + (e.InnerException?.Message ?? e.Message));
        }
    }

    // The InternalVersion the SDK stamps on a new prop (Prop.INTERNAL_VERSION, 1.0.0 in SDK 1.0.27). Read from the
    // SDK so a newer one is followed, with 1.0.0 when it cannot be read as three whole numbers.
    static string CurrentInternalVersion()
    {
        var text = PropType.GetField("INTERNAL_VERSION", BindingFlags.Public | BindingFlags.Static)?.GetValue(null)?.ToString();
        return text != null && Regex.IsMatch(text, @"^\d+\.\d+\.\d+$") ? text : "1.0.0";
    }

    static object MakeGuid(Type guidType, string text)
    {
        var guid = Guid.Parse(text);
        var byGuid = guidType.GetConstructor(new[] { typeof(Guid) });
        if (byGuid != null) return byGuid.Invoke(new object[] { guid });
        var byString = guidType.GetConstructor(new[] { typeof(string) });
        if (byString != null) return byString.Invoke(new object[] { text });
        var implicitOp = guidType.GetMethods(BindingFlags.Public | BindingFlags.Static)
            .FirstOrDefault(m => m.Name == "op_Implicit" && m.ReturnType == guidType && m.GetParameters()[0].ParameterType == typeof(Guid));
        if (implicitOp != null) return implicitOp.Invoke(null, new object[] { guid });
        throw new Exception("cannot make a " + guidType.Name + " from " + text);
    }

    static byte[] ReadablePng(Texture texture)
    {
        var rt = RenderTexture.GetTemporary(texture.width, texture.height, 0, RenderTextureFormat.ARGB32, RenderTextureReadWrite.sRGB);
        Graphics.Blit(texture, rt);
        var previous = RenderTexture.active;
        RenderTexture.active = rt;
        var copy = new Texture2D(texture.width, texture.height, TextureFormat.RGBA32, false);
        copy.ReadPixels(new Rect(0, 0, texture.width, texture.height), 0, 0);
        copy.Apply();
        RenderTexture.active = previous;
        RenderTexture.ReleaseTemporary(rt);
        var png = copy.EncodeToPNG();
        UnityEngine.Object.DestroyImmediate(copy);
        return png;
    }

    // "propinfo <Prop name>": what the profile holds now.
    public static string Info(string name)
    {
        var profile = FindProfile(name);
        if (profile == null) return "ERROR no prop profile " + name;
        var item = Get(profile, "Item");
        var cells = (Vector3[])Call(item, "GetLocalCellPositions");
        var sb = new StringBuilder();
        sb.AppendLine($"profile {AssetDatabase.GetAssetPath(profile)}");
        sb.AppendLine($"name {Get(item, "Name")}  id {Get(item, "AssetID")}  version '{Get(item, "AssetVersion")}'");
        sb.AppendLine($"base type {Get(item, "BaseTypeId")}  components {Describe(Get(item, "ComponentIds"))}");
        sb.AppendLine($"cells {cells.Length}: {string.Join(" ", cells.Take(40))}");
        sb.AppendLine($"x offset {Get(item, "ApplyXOffset")}  z offset {Get(item, "ApplyZOffset")}");
        var prefabAsset = (GameObject)Get(profile, "Prefab");
        sb.AppendLine($"prefab {AssetDatabase.GetAssetPath(prefabAsset)}");
        var temp = (GameObject)PrefabUtility.InstantiatePrefab(prefabAsset);
        try
        {
            var renderers = temp.GetComponentsInChildren<Renderer>();
            if (renderers.Length > 0)
            {
                var b = renderers[0].bounds;
                foreach (var r in renderers) b.Encapsulate(r.bounds);
                sb.AppendLine($"mesh x[{b.min.x:F2}, {b.max.x:F2}] y[{b.min.y:F2}, {b.max.y:F2}] z[{b.min.z:F2}, {b.max.z:F2}]");
            }
        }
        finally
        {
            UnityEngine.Object.DestroyImmediate(temp);
        }
        var icon = (UnityEngine.Object)Get(profile, "Icon");
        sb.AppendLine($"icon {(icon == null ? "none" : AssetDatabase.GetAssetPath(icon))}  local only {Get(profile, "IsOnlyLocal")}");
        return sb.ToString();
    }

    // "exportprop <Prop name>": runs the SDK's own exporter; stages and failures go to export-<name>.log.
    // Refused while another export holds the lock or while this prop's cells are still being made.
    public static string Export(string name)
    {
        CheckExportLock();
        if (exporting != null) return $"ERROR export of {exporting} still running since {exportStarted:HH:mm:ss}";
        if (pendingCells == name) return $"ERROR cells for {name} are still being generated";
        var profile = FindProfile(name);
        if (profile == null) return "ERROR no prop profile " + name;
        // The checks the Prop SDK window's Export button runs, before a lock is taken or a log is started: a bad
        // prop is stopped here, not after two minutes of building.
        var problems = ExportChecks(profile, out var skipped);
        if (problems.Count > 0) return "ERROR validation failed, nothing exported: " + string.Join("; ", problems);
        StageUtility.GoToMainStage();
        var exporterType = SdkType("EndlessEditor.PropSDK.PropExporterV2");
        var ctor = exporterType.GetConstructors()[0];
        var ps = ctor.GetParameters();
        var wasLocal = (bool)Get(profile, "IsOnlyLocal");
        var log = Path.Combine(Outbox, "export-" + name.Replace(' ', '_') + ".log");
        File.WriteAllText(log, $"{DateTime.Now:HH:mm:ss} export of {name} started\n");

        // Take the lock before starting, so a failure reported inside Export() already releases it.
        // The token keeps a late callback of an earlier export from touching this one.
        var token = ++exportToken;
        exporting = name;
        exportLog = log;
        exportProfile = profile;
        exportStarted = lastStage = DateTime.Now;
        stagesTotal = -1;
        stagesDone = 0;
        exportWasLocal = wasLocal;
        nextExportCheck = 0;
        EditorApplication.update -= WatchExport;
        EditorApplication.update += WatchExport;
        try
        {
            lastExporter = ctor.Invoke(new object[]
            {
                profile,
                Logger(ps[1].ParameterType, log, x => "stages " + Describe(x), x =>
                {
                    if (token != exportToken) return;
                    stagesTotal = CountOf(x);
                    lastStage = DateTime.Now;
                }),
                Logger(ps[2].ParameterType, log, x => "stage done " + Describe(x), x =>
                {
                    if (token != exportToken) return;
                    stagesDone++;
                    lastStage = DateTime.Now;
                    // The SDK marks its last stage ("Upload Completed") only after its own error check, and before the
                    // blocking "Export Success!" box, so every stage done is the one sure sign of an upload, first
                    // export or re-export alike. The SDK's log lines for it ("Prop Asset Created", "Prop Update
                    // Complete.") are written before that check and appear when the upload then fails.
                    if (stagesTotal > 0 && stagesDone >= stagesTotal) ReleaseExport("uploaded");
                }),
                Logger(ps[3].ParameterType, log, x => "FAILED " + Describe(x), x =>
                {
                    if (token == exportToken) ReleaseExport("the exporter reported a failure");
                }),
            });
            Call(lastExporter, "Export");
        }
        catch (Exception)
        {
            // The exporter threw before it got going; the bridge answers with the exception itself.
            if (token == exportToken) ReleaseExport("exporter exception");
            throw;
        }
        return "export started, log " + log + (skipped == "" ? "" : "\nnote: " + skipped);
    }

    // "exportstate": which export holds the lock, else whose cells are still being made, else "idle".
    // unity_restart refuses on anything but "idle", so neither an upload nor a .ready marker is cut off.
    public static string ExportState()
    {
        CheckExportLock();
        if (exporting != null)
        {
            var total = stagesTotal < 0 ? "?" : stagesTotal.ToString();
            return $"exporting {exporting} since {exportStarted:HH:mm:ss}, stages {stagesDone}/{total}";
        }
        if (pendingCells != null) return "cells pending for " + pendingCells;
        return "idle";
    }

    static void WatchExport()
    {
        if (exporting == null)
        {
            EditorApplication.update -= WatchExport;
            return;
        }
        if (EditorApplication.timeSinceStartup < nextExportCheck) return;
        nextExportCheck = EditorApplication.timeSinceStartup + 1;
        CheckExportLock();
    }

    // "uploaded" normally comes from the last stage itself (the stage-done callback in Export releases the lock when
    // every stage is done: the SDK marks its last stage only after its error check, for a first export and a
    // re-export alike). This check is the fallback for a last-stage callback that never ran: once every stage is done,
    // a prop that was local only at the start and has stopped being local only is uploaded, else the lock goes 3
    // minutes after the last stage; and always 20 minutes after the start. Nothing before the last stage says "uploaded".
    static void CheckExportLock()
    {
        if (exporting == null) return;
        var now = DateTime.Now;
        if (now - exportStarted > TimeSpan.FromMinutes(20))
        {
            ReleaseExport("20 minutes after the start");
            return;
        }
        if (stagesTotal < 0 || stagesDone < stagesTotal) return;
        bool uploaded;
        try
        {
            uploaded = NoLongerLocal();
        }
        catch (Exception)
        {
            uploaded = false;
        }
        if (uploaded) ReleaseExport("uploaded");
        else if (now - lastStage > TimeSpan.FromMinutes(3)) ReleaseExport("3 minutes after the last stage");
    }

    // Only for a prop that was local only at the start: one uploaded before is not local only already.
    static bool NoLongerLocal()
    {
        if (!exportWasLocal) return false;
        var profile = exportProfile != null ? exportProfile : FindProfile(exporting);
        return profile != null && !(bool)Get(profile, "IsOnlyLocal");
    }

    static void ReleaseExport(string why)
    {
        if (exporting == null) return;
        Append(exportLog, $"{DateTime.Now:HH:mm:ss} lock released: {why}");
        exporting = null;
        exportProfile = null;
        EditorApplication.update -= WatchExport;
    }

    // Subscribed once by the bridge (main-thread messages only, where the SDK's upload logs). It ends an export
    // for one reason only: an exporter exception thrown on Unity's own loop (as in UploadExports), which never
    // reaches the SDK's failure callback. It never says "uploaded": the SDK logs "Prop Asset Created" and "Prop
    // Update Complete." BEFORE its error check, so a log line cannot tell a good upload from a failed one, and it
    // names no prop. Only the last stage (in Export) and the fallback in CheckExportLock say "uploaded".
    public static void OnLog(string condition, string stackTrace, LogType type)
    {
        if (exporting == null || condition == null) return;
        // Anything thrown here would be logged and come straight back into this handler.
        try
        {
            if (type != LogType.Exception) return;
            var text = condition + "\n" + stackTrace;
            if (text.IndexOf("PropExporterV2", StringComparison.Ordinal) < 0 && text.IndexOf("AssetExporter", StringComparison.Ordinal) < 0) return;
            // An exception about another prop's bundle is an earlier upload that was still running when its
            // lock timed out, not this export: it must neither fail nor unlock this one.
            var bundle = BundleFolder.Match(condition);
            if (bundle.Success && !string.Equals(bundle.Groups[1].Value, exporting, StringComparison.OrdinalIgnoreCase)) return;
            Append(exportLog, $"{DateTime.Now:HH:mm:ss} FAILED (Unity exception) {condition.Split('\n')[0].Trim()}");
            ReleaseExport("exporter exception");
        }
        catch (Exception)
        {
        }
    }

    // "frame <Prop name>|top|front": opens the prop the way the Prop SDK sidebar does (which turns on
    // the claimed-cell boxes) and points the Scene view straight down at it, or at its front.
    public static string Frame(string arg)
    {
        var p = arg.Split('|');
        var profile = FindProfile(p[0]);
        if (profile == null) return "ERROR no prop profile " + p[0];
        Call(SdkType("EndlessEditor.PropSDK.PropSDK"), "Open", profile);
        var stage = UnityEditor.SceneManagement.PrefabStageUtility.GetCurrentPrefabStage();
        if (stage == null) return "ERROR the prop did not open in prefab mode";
        Selection.activeGameObject = stage.prefabContentsRoot;
        var view = SceneView.lastActiveSceneView;
        if (view == null) return "ERROR no Scene view";
        var top = p.Length < 2 || p[1] != "front";
        view.orthographic = true;
        view.rotation = top ? Quaternion.Euler(90, 0, 0) : Quaternion.Euler(0, 180, 0);
        view.FrameSelected();
        view.Repaint();
        return $"framed {p[0]} from the {(top ? "top" : "front")}";
    }

    // "exportreport": the exporter's own report text.
    public static string Report()
    {
        if (lastExporter == null) return "no export has run since the last reload";
        return Get(lastExporter, "ReportBuilder")?.ToString() ?? "(empty report)";
    }
}
