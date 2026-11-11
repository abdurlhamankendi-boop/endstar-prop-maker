// The ids a scripted prop's meshes go by. A Script Reference saved in the game stores the mesh's Transform Identifier
// id as text, and at run time the game looks each id up among the prefab's current identifiers (a missing id is a
// KeyNotFound error on Transforms.<Name>, a repeated id throws). So when PropMaker rebuilds a scripted prop (for
// example one with spinning parts), every mesh must come back with the id it had, or a re-export plus "Apply version" would leave the saved
// script pointing at ids that no longer exist and the spinning parts would stop. The SDK's own export step keeps existing ids
// and only replaces repeated ones; this does the same for a prefab PropMaker empties and fills again.
using System;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Text;
using UnityEngine;

static partial class PropMaker
{
    static Type IdentifierType => SdkType("Endless.Props.TransformIdentifier");

    // A mesh's place in the prefab, e.g. "SpinnerBlades/BladeA": the names from the prefab root down, so two
    // meshes with the same name under different parents stay apart. "." is the root itself.
    static string MeshPath(Transform mesh, Transform root)
    {
        var names = new List<string>();
        for (var t = mesh; t != null && t != root; t = t.parent)
            names.Add(t.name);
        names.Reverse();
        return names.Count == 0 ? "." : string.Join("/", names);
    }

    // {mesh path -> its UniqueId} for every Transform Identifier under the prefab root, as it is now. An identifier
    // that never got an id (empty) is left out: it names nothing a script could use.
    static Dictionary<string, object> ReadMeshIds(Transform root)
    {
        var ids = new Dictionary<string, object>();
        var field = IdentifierType.GetField("UniqueId");
        var isEmpty = field.FieldType.GetProperty("IsEmpty");
        foreach (var identifier in root.GetComponentsInChildren(IdentifierType, true))
        {
            var path = MeshPath(identifier.transform, root);
            var id = field.GetValue(identifier);
            if (isEmpty != null && (bool)isEmpty.GetValue(id)) continue;
            if (!ids.ContainsKey(path)) ids[path] = id;
        }
        return ids;
    }

    // The same id every time for a (prop name, mesh path) pair: the first 16 bytes of an MD5 of "<prop>/<path>".
    static Guid StableGuid(string propName, string path)
    {
        using (var md5 = MD5.Create())
            return new Guid(md5.ComputeHash(Encoding.UTF8.GetBytes(propName + "/" + path)));
    }

    // Gives every mesh of the rebuilt model a Transform Identifier: the id the same mesh path had in `before`, else a
    // stable one made from the prop's name and the path (never a random one, so the next rebuild of this model keeps
    // it too). An id already given to another mesh is never given twice. Returns the line for the make report.
    static string StampMeshIds(string propName, Transform root, Renderer[] renderers, Dictionary<string, object> before)
    {
        var field = IdentifierType.GetField("UniqueId");
        var given = new HashSet<string>();
        int kept = 0, made = 0, fresh = 0;
        var seen = new HashSet<string>();
        foreach (var renderer in renderers)
        {
            var path = MeshPath(renderer.transform, root);
            var identifier = renderer.gameObject.AddComponent(IdentifierType);
            seen.Add(path);
            object id;
            if (before.TryGetValue(path, out id) && given.Add(id.ToString()))
            {
                field.SetValue(identifier, id);
                kept++;
                continue;
            }
            id = MakeGuid(field.FieldType, StableGuid(propName, path).ToString());
            if (given.Add(id.ToString()))
            {
                field.SetValue(identifier, id);
                made++;
                continue;
            }
            IdentifierType.GetMethod("GenerateNewId").Invoke(identifier, null);   // two meshes on one path: cannot happen, but never twice
            fresh++;
        }
        var gone = 0;
        foreach (var path in before.Keys)
            if (!seen.Contains(path)) gone++;
        return $"{renderers.Length} parts can be scripted ({kept} kept their old id, {made} new, {fresh} random)" +
               (gone > 0 ? $"; WARNING {gone} old ids belong to meshes this model no longer has, so a saved script that used them will fail" : "");
    }
}
