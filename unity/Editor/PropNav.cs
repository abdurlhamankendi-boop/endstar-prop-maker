// "navtest <Prop name>|x,y,z|x,y,z[|x,y,z ...]": a quick SCREEN of whether a character could walk between points on a prop,
// before any export. It builds a NavMesh in this editor from the prop's own colliders with the game's walking agent,
// then asks whether each point can be reached from the one before. The points are in the prefab's own space, in metres
// (the numbers propinfo prints as "mesh x y z"; the floor of the bottom cell is at y = -0.5), standing on the walkable
// surface: for a stair, the foot of the lowest step and the top of the highest.
//
// It is a screen, not proof. The game bakes its NavMesh from a NavMeshSurface whose overrides are not in any local file,
// together with the terrain, every other prop and a falloff volume, so the voxel size and the other build numbers may
// not be the ones used here; and the game's second step (navigation cells in the claimed cells) can only be checked in
// part (NearClaimedCell). A "NOT CONNECTED" is a reason to look; a "CONNECTED" is not yet a reason to relax.
// Calibrate it before trusting it, and write the results down where the next person will find them:
//   1. A stair of 16 steps of 0.25 m that a walker really climbed in the game: must say CONNECTED.
//   2. A copy of that stair with a 0.3 m riser: must say NOT CONNECTED.
//   3. The same stair at 0.25 m and at 0.27 m: the game's agent climbs 0.27 m, but the NavMesh rounds the climb to
//      its voxel height, so find which side of 0.25 .. 0.27 the screen flips on. If it says CONNECTED at 0.27 m
//      while the game's walker could not climb it, the screen is too generous.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.AI;

static partial class PropMaker
{
    const int MaxNavPoints = 20;

    // The agent the game walks its characters with (agent type 0 in its project settings): radius 0.1 m, height 1.0 m,
    // slope 47 degrees, climb 0.27 m, smallest walkable island 2 m2, voxels of 0.1667 m, tiles of 256 voxels. The
    // numbers are set on a copy of this project's agent 0, so the project's own NavMesh settings are never changed.
    static NavMeshBuildSettings GameAgent()
    {
        var agent = NavMesh.GetSettingsByID(0);
        agent.agentRadius = 0.1f;
        agent.agentHeight = 1.0f;
        agent.agentSlope = 47f;
        agent.agentClimb = 0.27f;
        agent.minRegionArea = 2.0f;
        agent.overrideVoxelSize = true;
        agent.voxelSize = 0.16666667f;
        agent.overrideTileSize = true;
        agent.tileSize = 256;
        agent.buildHeightMesh = false;
        return agent;
    }

    // What the game makes its NavMesh from: the colliders on the Default layer (boxes, spheres, capsules and meshes).
    // Unity's own collector turns them into NavMesh sources, so no shape is worked out here.
    static List<NavMeshBuildSource> NavSources(GameObject root)
    {
        var sources = new List<NavMeshBuildSource>();
        NavMeshBuilder.CollectSources(root.transform, 1 << LayerMask.NameToLayer("Default"), NavMeshCollectGeometry.PhysicsColliders,
                                      0, new List<NavMeshBuildMarkup>(), sources);
        return sources;
    }

    // The game gives a walkable surface navigation cells only inside a claimed cell or directly above or below one.
    // `standing` is a point on the surface; it is nudged up so it counts for the cell the character stands in.
    static bool NearClaimedCell(Vector3 standing, Vector3[] claimed)
    {
        foreach (var step in new[] { 0f, 1f, -1f })
        {
            var q = standing + new Vector3(0, 0.01f + step, 0);
            if (claimed.Any(c => Mathf.Abs(q.x - c.x) <= 0.5f && Mathf.Abs(q.y - c.y) <= 0.5f && Mathf.Abs(q.z - c.z) <= 0.5f))
                return true;
        }
        return false;
    }

    static string ParsePoints(string[] parts, List<Vector3> points)
    {
        for (var i = 1; i < parts.Length; i++)
        {
            var f = parts[i].Split(',');
            var v = new float[3];
            var ok = f.Length == 3;
            for (var k = 0; ok && k < 3; k++)
                ok = float.TryParse(f[k], NumberStyles.Float, CultureInfo.InvariantCulture, out v[k]);
            if (!ok) return $"point {i}: want x,y,z in metres, got '{parts[i]}'";
            points.Add(new Vector3(v[0], v[1], v[2]));
        }
        return null;
    }

    public static string NavTest(string arg)
    {
        var parts = arg.Split('|');
        if (parts.Length < 3) return "ERROR usage: navtest <Prop name>|x,y,z|x,y,z[|x,y,z ...]";
        if (parts.Length - 1 > MaxNavPoints) return $"ERROR at most {MaxNavPoints} points";
        // The claimed-cell rule reads the prop's cells, which are half made while the voxelizer runs, and an export is
        // packing the prop: a screen can wait for either.
        CheckExportLock();
        if (pendingCells != null) return $"ERROR cells for {pendingCells} are still being generated";
        if (exporting != null) return $"ERROR export of {exporting} still running since {exportStarted:HH:mm:ss}";
        var profile = FindProfile(parts[0]);
        if (profile == null) return "ERROR no prop profile " + parts[0];
        var points = new List<Vector3>();
        var problem = ParsePoints(parts, points);
        if (problem != null) return "ERROR " + problem;

        StageUtility.GoToMainStage();
        var claimed = (Vector3[])Call(Get(profile, "Item"), "GetLocalCellPositions");
        var report = new StringBuilder();
        report.AppendLine($"NAVTEST {parts[0]}: a SCREEN, NOT PROOF. The game builds its NavMesh with settings this editor cannot see, so check the result in the game.");
        var instance = (GameObject)PrefabUtility.InstantiatePrefab((GameObject)Get(profile, "Prefab"));
        var added = default(NavMeshDataInstance);
        NavMeshData data = null;
        try
        {
            var agent = GameAgent();
            var sources = NavSources(instance);
            if (sources.Count == 0) return "ERROR the prop has no collider on the Default layer, so the game would build no NavMesh from it";
            var renderers = instance.GetComponentsInChildren<Renderer>();
            var bounds = renderers.Length > 0 ? renderers[0].bounds : new Bounds(Vector3.zero, Vector3.one);
            foreach (var r in renderers) bounds.Encapsulate(r.bounds);
            bounds.Expand(4f);   // 2 m of room on every side
            report.AppendLine($"agent: radius {agent.agentRadius} height {agent.agentHeight} slope {agent.agentSlope} climb {agent.agentClimb} " +
                              $"min island {agent.minRegionArea} m2, voxel {agent.voxelSize:F4}, tile {agent.tileSize}");
            report.AppendLine($"sources: {sources.Count} colliders on the Default layer");
            var started = DateTime.Now;
            data = NavMeshBuilder.BuildNavMeshData(agent, sources, bounds, Vector3.zero, Quaternion.identity);
            added = NavMesh.AddNavMeshData(data);
            var triangles = NavMesh.CalculateTriangulation().indices.Length / 3;
            report.AppendLine($"navmesh: {triangles} triangles, built in {(DateTime.Now - started).TotalMilliseconds:F0} ms");
            if (triangles == 0) report.AppendLine("WARNING the NavMesh is empty: nothing here is walkable for this agent");

            var filter = new NavMeshQueryFilter { agentTypeID = 0, areaMask = NavMesh.AllAreas };
            var onMesh = new List<Vector3?>();
            for (var i = 0; i < points.Count; i++)
            {
                // The game's own rule: a destination more than 0.5 m from walkable ground is silently ignored.
                if (NavMesh.SamplePosition(points[i], out var hit, 0.5f, filter))
                {
                    onMesh.Add(hit.position);
                    report.AppendLine($"point {i + 1} {points[i]}: on the NavMesh at {hit.position} ({Vector3.Distance(points[i], hit.position):F2} m away); " +
                                      $"claimed-cell rule {(NearClaimedCell(hit.position, claimed) ? "met" : "NOT MET: no claimed cell here, above or below")}");
                }
                else
                {
                    onMesh.Add(null);
                    report.AppendLine($"point {i + 1} {points[i]}: NOT on the NavMesh (nothing walkable within 0.5 m)");
                }
            }
            for (var i = 0; i + 1 < points.Count; i++)
            {
                if (onMesh[i] == null || onMesh[i + 1] == null)
                {
                    report.AppendLine($"{i + 1} -> {i + 2}: NOT CONNECTED (a point is not on the NavMesh)");
                    continue;
                }
                var path = new NavMeshPath();
                NavMesh.CalculatePath(onMesh[i].Value, onMesh[i + 1].Value, filter, path);
                var length = 0f;
                for (var c = 1; c < path.corners.Length; c++) length += Vector3.Distance(path.corners[c - 1], path.corners[c]);
                report.AppendLine(path.status == NavMeshPathStatus.PathComplete
                    ? $"{i + 1} -> {i + 2}: CONNECTED, {length:F1} m along the path, {path.corners.Length} corners"
                    : $"{i + 1} -> {i + 2}: NOT CONNECTED ({path.status})");
            }
        }
        finally
        {
            if (added.valid) NavMesh.RemoveNavMeshData(added);
            if (data != null) UnityEngine.Object.DestroyImmediate(data);
            UnityEngine.Object.DestroyImmediate(instance);
        }
        report.AppendLine("SCREEN ONLY, NOT PROOF. Calibrate before trusting it: (1) a stair a walker really climbed in the game must say CONNECTED, " +
                          "(2) a copy with a 0.3 m riser must say NOT CONNECTED, (3) at 0.25 m and 0.27 m find where it flips, because the climb is rounded to the voxel height.");
        return report.ToString();
    }
}
