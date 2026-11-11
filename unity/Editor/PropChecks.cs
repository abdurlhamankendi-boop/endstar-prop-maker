// The Prop SDK window's own export checks, run by PropMaker.Export before it starts an export. PropMaker builds the
// SDK's exporter itself, so it never goes through the window's Export button and skipped every check the button runs:
// a bad prop was found only after about two minutes of building, or never. These are the same classes the window
// uses (looked up by name like every SDK type here), in the window's order: the mesh read/write check, the checks of
// each reference component, the claimed-cell and shader checks, and the component whitelist.
// Not run: the window's texture caps, its "out of date" box (a modal box that would hang the bridge), and its
// PreExport step (prefab root to the origin and a Transform Identifier on every transform): PropMaker puts the
// identifiers on the meshes of a scripted prop itself.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;

static partial class PropMaker
{
    // The shaders the SDK's shader check looks up. It opens a modal "Corruption" box when one of the Endless ones is
    // missing, which would freeze the bridge, so the check is only built when all five are there.
    static readonly string[] EndlessShaderNames =
    {
        "Shader Graphs/Endless_Shader_Character_NoFade", "Shader Graphs/Endless_Shader_NoFade",
        "Shader Graphs/Endless_Shader_Terrain", "Shader Graphs/Endless_Shader_Tree_NoFade", "Shader Graphs/Endless_Hologram",
    };

    // The message of every check the prop fails (empty when it passes). `skipped` says which SDK check could not run
    // (the SDK changed or threw); the export then goes ahead as it did before these checks existed, and says so.
    static List<string> ExportChecks(UnityEngine.Object profile, out string skipped)
    {
        var failures = new List<string>();
        var notes = new List<string>();
        skipped = "";
        var prefab = (GameObject)Get(profile, "Prefab");
        if (prefab == null) return new List<string> { "the profile has no prefab" };
        List<object> validators;
        try
        {
            validators = SdkValidators(profile, prefab, notes);
        }
        catch (Exception e)
        {
            validators = new List<object>();
            notes.Add("SDK checks could not be set up: " + (e.InnerException ?? e).Message);
        }
        // One check that throws (a material slot left empty, say) must not switch off the others.
        foreach (var validator in validators)
        {
            try
            {
                foreach (var result in (IEnumerable)Call(validator, "PassesValidation"))
                {
                    if ((bool)Get(result, "Success")) continue;
                    var source = Get(result, "Source") as UnityEngine.Object;
                    failures.Add((string)Get(result, "Message") + (source != null ? $" [{source.name}]" : ""));
                }
            }
            catch (Exception e)
            {
                notes.Add($"{validator.GetType().Name} could not run: " + (e.InnerException ?? e).Message);
            }
        }
        // Nothing in the SDK checks that a prop can be walked on or hit: no collider on the Default layer means no
        // floor for a character and no wall for a shot, and the game builds its navigation from exactly those.
        var inGame = LayerMask.NameToLayer("Default");
        if (!prefab.GetComponentsInChildren<Collider>().Any(c => c.gameObject.layer == inGame))
            failures.Add("no collider on the Default layer, so nothing could walk on or hit this prop");
        skipped = string.Join("; ", notes);
        return failures;
    }

    // The SDK's checks for this prop, in the window's order. Their argument goes in an object[] of its own: a Unity object turns
    // into a bool by itself, so CreateInstance(type, prefab) picks the (type, nonPublic) form and looks for an empty
    // constructor, which none of them has (3 Nov, the first export after the restart). Each group is set up on its own, so an SDK class that
    // changed or is missing costs only its own check (said in `notes`), not the others.
    static List<object> SdkValidators(UnityEngine.Object profile, GameObject prefab, List<string> notes)
    {
        var validators = new List<object>();
        SetUp(validators, notes, "the mesh read/write check",
            () => new[] { Activator.CreateInstance(SdkType("Runtime.Props.Validations.ReadWriteSetValidator"), new object[] { prefab }) });
        SetUp(validators, notes, "the reference components' checks", () =>
        {
            var found = new List<object>();
            foreach (var reference in prefab.GetComponentsInChildren(SdkType("Endless.Props.ReferenceComponents.ReferenceBase")))
                found.AddRange(((IEnumerable)Call(reference, "GetValidators")).Cast<object>());
            return found;
        });
        SetUp(validators, notes, "the claimed-cell check",
            () => new[] { Activator.CreateInstance(SdkType("EndlessEditor.PropSDK.MinimumCellClaimValidator"), new object[] { profile }) });
        var missing = EndlessShaderNames.Where(n => Shader.Find(n) == null).ToList();
        if (missing.Count > 0)
            notes.Add("shader check skipped, shader not found: " + string.Join(", ", missing));
        else
            SetUp(validators, notes, "the shader check", () =>
            {
                var shaderCheck = SdkType("EndlessEditor.PropSDK.EndlessShaderValidator");
                return prefab.GetComponentsInChildren<MeshRenderer>().Select(r => Activator.CreateInstance(shaderCheck, new object[] { r })).ToList();
            });
        SetUp(validators, notes, "the component check",
            () => new[] { Activator.CreateInstance(SdkType("EndlessEditor.PropSDK.PropPurityValidator"), new object[] { prefab }) });
        return validators;
    }

    static void SetUp(List<object> validators, List<string> notes, string what, Func<IEnumerable<object>> make)
    {
        try
        {
            validators.AddRange(make());
        }
        catch (Exception e)
        {
            notes.Add($"{what} could not be set up: " + (e.InnerException ?? e).Message);
        }
    }
}
