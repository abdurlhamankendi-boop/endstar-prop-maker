// The bridge command "propmaterials": gives an imported model Endstar materials made from its OWN materials
// (the prop uploader, tools\prop_uploader). Each material of the model becomes a material on the shader
// "Shader Graphs/Endless_Shader_NoFade" with the model's colour picture in _Albedo and its colour in Albedo_Tint,
// saved under Assets/User Data/SendToEndless/Uploads/<Model>/, and the model is pointed at them.
// The palette materials are not touched, and the palette command "materials" skips a model that has them (HasOwnMaterials).
// A manifest entry with "glow": true also gets its picture as the glow map (_Emissive_Map) and starts dark.
//
//   propmaterials <Model>|<manifest.json>
//
// <Model> is the FBX in Assets/User Data/SendToEndless/Models/<Model>.fbx. The manifest is REPORT.json from
// prepare_model.py, in a folder under <tool folder>\tools\prop_uploader\work (its "materials" list: name, colour as
// Blender's linear RGBA, and a picture path relative to the manifest).
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEngine;

static class PropMaterials
{
    const string ShaderName = "Shader Graphs/Endless_Shader_NoFade";
    const string AlbedoTexture = "_Albedo";
    const string AlbedoTint = "Albedo_Tint";
    const string UploadsRoot = "Assets/User Data/SendToEndless/Uploads";
    const string EmissiveMap = "_Emissive_Map";      // the palette's wall lights use the same pair (EndstarBridge.Materials)
    const string EmissionColor = "_EmissionColor";   // what Visuals:SetEmissiveColor writes

    // True when propmaterials has made this model's materials: the palette command must leave such a model alone.
    public static bool HasOwnMaterials(string model) => AssetDatabase.IsValidFolder($"{UploadsRoot}/{model}/Materials");

    [Serializable]
    class Entry
    {
        public string name;
        public float[] color;
        public string texture;
        public bool glow;      // the picture is the glow map too (_Emissive_Map), starting dark: a script's SetEmissiveColor lights it
        public string emission;   // a glow map alone (the lamps' light_glow.png for iron and glow_*), relative to the manifest, starting dark
    }

    [Serializable]
    class Manifest
    {
        public Entry[] materials;
    }

    public static string Run(string arg)
    {
        var parts = arg.Split('|');
        if (parts.Length != 2) return "ERROR usage: propmaterials <Model>|<manifest.json>";
        var model = parts[0].Trim();
        if (!Regex.IsMatch(model, @"^[A-Za-z0-9_\-]+$")) return "ERROR the model name may only hold letters, digits, _ and -: " + model;
        var manifestPath = Path.GetFullPath(parts[1].Trim());
        var allowed = Path.GetFullPath(Path.Combine(EndstarBridge.ToolRoot, "tools", "prop_uploader", "work")) + Path.DirectorySeparatorChar;
        if (!manifestPath.StartsWith(allowed, StringComparison.OrdinalIgnoreCase)) return "ERROR the manifest must be under " + allowed;
        if (!File.Exists(manifestPath)) return "ERROR no manifest " + manifestPath;
        var shader = Shader.Find(ShaderName);
        if (shader == null) return "ERROR shader not found: " + ShaderName;
        if (shader.FindPropertyIndex(AlbedoTexture) < 0 || shader.FindPropertyIndex(AlbedoTint) < 0)
            return $"ERROR the shader has no {AlbedoTexture} / {AlbedoTint} property (an SDK update? ask the bridge: shaders Endless)";
        var manifest = JsonUtility.FromJson<Manifest>(File.ReadAllText(manifestPath, Encoding.UTF8));
        if (manifest == null || manifest.materials == null || manifest.materials.Length == 0) return "ERROR the manifest lists no materials";

        AssetDatabase.Refresh();
        var modelPath = $"Assets/User Data/SendToEndless/Models/{model}.fbx";
        var importer = AssetImporter.GetAtPath(modelPath) as ModelImporter;
        if (importer == null) return "ERROR no model " + modelPath + " (copy it in first)";

        var root = UploadsRoot;
        EnsureFolder("Assets/User Data/SendToEndless", "Uploads");
        EnsureFolder(root, model);
        var folder = root + "/" + model;
        EnsureFolder(folder, "Materials");
        EnsureFolder(folder, "Textures");
        var sourceDir = Path.GetDirectoryName(manifestPath);
        var made = new Dictionary<string, Material>();
        var report = new StringBuilder();
        foreach (var entry in manifest.materials)
        {
            Texture2D picture = null;
            if (!string.IsNullOrEmpty(entry.texture))
            {
                var source = Path.GetFullPath(Path.Combine(sourceDir, entry.texture));
                if (!source.StartsWith(sourceDir, StringComparison.OrdinalIgnoreCase) || !File.Exists(source))
                    return $"ERROR the picture of material {entry.name} is missing: {source}";
                var assetPath = $"{folder}/Textures/{Path.GetFileName(source)}";
                var target = Path.Combine(Directory.GetParent(Application.dataPath).FullName, assetPath.Replace('/', Path.DirectorySeparatorChar));
                File.Copy(source, target, true);
                AssetDatabase.ImportAsset(assetPath, ImportAssetOptions.ForceUpdate);
                picture = AssetDatabase.LoadAssetAtPath<Texture2D>(assetPath);
                if (picture == null) return "ERROR Unity could not import the picture " + assetPath;
            }
            var matPath = $"{folder}/Materials/{entry.name}.mat";
            var mat = AssetDatabase.LoadAssetAtPath<Material>(matPath);
            if (mat == null)
            {
                mat = new Material(shader);
                AssetDatabase.CreateAsset(mat, matPath);
            }
            mat.shader = shader;
            mat.SetTexture(AlbedoTexture, picture);
            // Blender's colours are linear; SetColor wants sRGB (the same conversion the palette materials use).
            var c = entry.color != null && entry.color.Length >= 3 ? entry.color : new[] { 0.8f, 0.8f, 0.8f };
            mat.SetColor(AlbedoTint, new Color(Mathf.LinearToGammaSpace(c[0]), Mathf.LinearToGammaSpace(c[1]), Mathf.LinearToGammaSpace(c[2]), 1));
            Texture2D glowMap = entry.glow ? picture : null;
            if (!string.IsNullOrEmpty(entry.emission))
            {
                var source = Path.GetFullPath(Path.Combine(sourceDir, entry.emission));
                if (!source.StartsWith(sourceDir, StringComparison.OrdinalIgnoreCase) || !File.Exists(source))
                    return $"ERROR the glow map of material {entry.name} is missing: {source}";
                var assetPath = $"{folder}/Textures/{Path.GetFileName(source)}";
                var target = Path.Combine(Directory.GetParent(Application.dataPath).FullName, assetPath.Replace('/', Path.DirectorySeparatorChar));
                File.Copy(source, target, true);
                AssetDatabase.ImportAsset(assetPath, ImportAssetOptions.ForceUpdate);
                glowMap = AssetDatabase.LoadAssetAtPath<Texture2D>(assetPath);
                if (glowMap == null) return "ERROR Unity could not import the glow map " + assetPath;
            }
            if (glowMap != null)
            {
                if (shader.FindPropertyIndex(EmissiveMap) < 0) return $"ERROR the shader has no {EmissiveMap} property (material {entry.name} asks for a glow)";
                mat.SetTexture(EmissiveMap, glowMap);
                mat.SetColor(EmissionColor, Color.black);
            }
            EditorUtility.SetDirty(mat);
            made[entry.name] = mat;
            report.AppendLine($"material {entry.name}: " + (picture != null ? "picture " + picture.name : "colour only") + (glowMap != null ? ", glow map " + glowMap.name : ""));
        }
        AssetDatabase.SaveAssets();

        // Point the model at them, by the material names the FBX itself carries. The old links are dropped and the model reimported
        // first, so its own materials show again as its parts: a remapped material does not, and a name only an earlier version of
        // the model had (a material an older look used, 11 Nov) must not count as one the new FBX still uses.
        var mapped = importer.GetExternalObjectMap().Keys.Where(k => k.type == typeof(Material)).ToList();
        if (mapped.Count > 0)
        {
            foreach (var id in mapped) importer.RemoveRemap(id);
            importer.SaveAndReimport();
        }
        var used = AssetDatabase.LoadAllAssetsAtPath(modelPath).OfType<Material>().Select(m => m.name).Distinct().ToList();
        var dropped = mapped.Select(k => k.name).Except(used).ToList();
        if (dropped.Count > 0) report.AppendLine("old links dropped (the model no longer has them): " + string.Join(", ", dropped));
        var missing = new List<string>();
        foreach (var name in used)
        {
            if (made.TryGetValue(name, out var mat))
                importer.AddRemap(new AssetImporter.SourceAssetIdentifier(typeof(Material), name), mat);
            else
                missing.Add(name);
        }
        importer.SaveAndReimport();
        report.AppendLine($"{model}.fbx uses: {string.Join(", ", used)}" + (missing.Count > 0 ? $"  MISSING {string.Join(", ", missing)}" : ""));
        if (missing.Count > 0) report.Insert(0, "ERROR the model has materials the manifest does not: " + string.Join(", ", missing) + "\n");
        return report.ToString();
    }

    static void EnsureFolder(string parent, string name)
    {
        if (!AssetDatabase.IsValidFolder(parent + "/" + name)) AssetDatabase.CreateFolder(parent, name);
    }
}
