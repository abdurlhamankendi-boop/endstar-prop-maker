// Tile sets for Send to Endless (10 Nov): opens the SDK's own Choose Parent Set window for a tile set profile, from code, so that
// what it does (or what it throws) can be seen. A click on the Terrain SDK window's button did nothing twice. Every SDK type is
// looked up by name, as in PropMaker.cs, so this file compiles even if the SDK changes.
// Used by EndstarBridge: "tileparent <asset path>" opens the window; "tilepick" says what the window handed back.
using System;
using System.Reflection;
using UnityEditor;
using UnityEngine;

static class TilesetMaker
{
    static string lastPick = "nothing picked yet";
    static UnityEngine.Object lastProfile;

    static Type SdkType(string fullName)
    {
        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            var t = asm.GetType(fullName);
            if (t != null) return t;
        }
        return null;
    }

    internal static string Parent(string path)
    {
        var profileType = SdkType("Endless.TerrainCosmetics.TilesetCosmeticProfile");
        var chooser = SdkType("EndlessEditor.TerrainCosmetics.ParentTilesetChooserWindow");
        if (profileType == null || chooser == null) return "ERROR the SDK's tile set classes are not loaded";
        var profile = AssetDatabase.LoadAssetAtPath(path.Trim(), profileType);
        if (profile == null) return "ERROR no tile set profile at " + path;
        var open = chooser.GetMethod("Open", BindingFlags.Public | BindingFlags.Static);
        if (open == null) return "ERROR ParentTilesetChooserWindow.Open not found";
        var actionType = open.GetParameters()[1].ParameterType;
        var argType = actionType.IsGenericType ? actionType.GetGenericArguments()[0] : typeof(object);
        var picked = typeof(TilesetMaker).GetMethod(nameof(Picked), BindingFlags.NonPublic | BindingFlags.Static).MakeGenericMethod(argType);
        lastProfile = profile;
        lastPick = "the window is open; nothing picked yet";
        try
        {
            var window = open.Invoke(null, new object[] { profile, Delegate.CreateDelegate(actionType, picked) });
            return $"opened {window} (the window hands back a {argType.FullName})";
        }
        catch (TargetInvocationException e)
        {
            return "ERROR " + e.InnerException;
        }
    }

    // What the window hands back when a parent is chosen. A tile set profile hands back its id, which is set as the parent of ours
    // through the profile's public InheritedTilesetId.
    static void Picked<TArg>(TArg value)
    {
        lastPick = value == null ? "null" : $"{value.GetType().FullName}: {value}";
        Debug.Log("TILEPARENT picked " + lastPick);
        try
        {
            var idProp = value?.GetType().GetProperty("TilesetId");
            var target = lastProfile?.GetType().GetProperty("InheritedTilesetId");
            if (idProp != null && target != null && lastProfile != null)
            {
                target.SetValue(lastProfile, idProp.GetValue(value));
                EditorUtility.SetDirty(lastProfile);
                AssetDatabase.SaveAssets();
                lastPick += " | parent id set on our profile and saved";
            }
        }
        catch (Exception e)
        {
            lastPick += " | could not set the parent: " + e.Message;
        }
    }

    internal static string Pick() => lastPick;

    // "tileeditor <asset path>" (10 Nov, evening): opens the SDK's own tile set editor ON that profile, through its public
    // Open(TilesetCosmeticProfile). Its Choose Parent Set button then runs the SDK's own callback, which fetches the parent's
    // files: our own callback above only heard the pick, and the chooser then waited on 'Downloading necessary parent
    // information' for good. The button did nothing when the editor was opened from the menu with no profile active.
    internal static string Editor(string path)
    {
        var profileType = SdkType("Endless.TerrainCosmetics.TilesetCosmeticProfile");
        var editorType = SdkType("EndlessEditor.TerrainCosmetics.TilesetCosmeticProfileEditor");
        if (profileType == null || editorType == null) return "ERROR the SDK's tile set classes are not loaded";
        var profile = AssetDatabase.LoadAssetAtPath(path.Trim(), profileType);
        if (profile == null) return "ERROR no tile set profile at " + path;
        var open = editorType.GetMethod("Open", BindingFlags.Public | BindingFlags.Static, null, new[] { profileType }, null);
        if (open == null) return "ERROR TilesetCosmeticProfileEditor.Open(profile) not found";
        try
        {
            open.Invoke(null, new object[] { profile });
        }
        catch (TargetInvocationException e)
        {
            return "ERROR " + e.InnerException;
        }
        var active = editorType.GetField("ActiveProfile", BindingFlags.Public | BindingFlags.Static)?.GetValue(null) as UnityEngine.Object;
        return $"opened the tile set editor; active profile: {(active == null ? "none" : active.name)}";
    }

    // "tileinfo <asset path>": what the profile holds now, read through its public properties: the parent's id, the type,
    // how many tiles, material tables and materials. Proof that a parent was set and its tiles came with it.
    internal static string Info(string path)
    {
        var profileType = SdkType("Endless.TerrainCosmetics.TilesetCosmeticProfile");
        if (profileType == null) return "ERROR the SDK's tile set classes are not loaded";
        var profile = AssetDatabase.LoadAssetAtPath(path.Trim(), profileType);
        if (profile == null) return "ERROR no tile set profile at " + path;
        string Read(string name)
        {
            try
            {
                var value = profileType.GetProperty(name, BindingFlags.Public | BindingFlags.Instance)?.GetValue(profile);
                if (value is Array array) return array.Length + " item(s)";
                if (value is System.Collections.ICollection list) return list.Count + " item(s)";
                return value == null ? "null" : value.ToString();
            }
            catch (Exception e)
            {
                return "could not read: " + (e.InnerException ?? e).Message;
            }
        }
        var sb = new System.Text.StringBuilder($"profile {profile.name}\n");
        foreach (var name in new[] { "DisplayName", "TilesetType", "TilesetId", "InheritedTilesetId", "TileCosmeticInheritance",
                                     "MaterialVariantTablesInheritance", "TileCosmetics", "MaterialVariantWeightTables", "Materials" })
            sb.AppendLine($"  {name}: {Read(name)}");
        return sb.ToString();
    }
}
