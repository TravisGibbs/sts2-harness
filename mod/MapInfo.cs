using System.Text;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

/// <summary>Text rendering of the act map.</summary>
internal static class MapInfo
{
    public static string TypeName(MapPointType type) => type switch
    {
        MapPointType.Monster => "Monster",
        MapPointType.Elite => "Elite",
        MapPointType.Boss => "Boss",
        MapPointType.RestSite => "Rest",
        MapPointType.Shop => "Shop",
        MapPointType.Treasure => "Treasure",
        MapPointType.Unknown => "Unknown",
        MapPointType.Ancient => "Ancient",
        _ => type.ToString(),
    };

    public static string Code(MapPointType type) => type switch
    {
        MapPointType.Monster => "M",
        MapPointType.Elite => "E",
        MapPointType.Boss => "B",
        MapPointType.RestSite => "R",
        MapPointType.Shop => "$",
        MapPointType.Treasure => "T",
        MapPointType.Unknown => "?",
        MapPointType.Ancient => "A",
        _ => "?",
    };

    /// <summary>"Monster/Elite" style summary of where a point leads next.</summary>
    public static string NextTypes(MapPoint point)
    {
        var types = point.Children
            .OrderBy(c => c.coord.col)
            .Select(c => TypeName(c.PointType))
            .Distinct()
            .ToList();
        return types.Count > 0 ? string.Join("/", types) : "";
    }

    /// <summary>Every room still reachable from where the player stands, row by row.</summary>
    public static string Describe(RunState run)
    {
        var map = run.Map;
        if (map == null) return "No map.";

        IEnumerable<MapPoint> frontier;
        var current = run.CurrentMapPoint;
        if (current != null) frontier = current.Children;
        else frontier = new[] { map.StartingMapPoint };

        // Breadth-first over children: only rooms that can still be reached.
        var reachable = new HashSet<MapPoint>();
        var queue = new Queue<MapPoint>(frontier);
        while (queue.Count > 0)
        {
            var point = queue.Dequeue();
            if (!reachable.Add(point)) continue;
            foreach (var child in point.Children) queue.Enqueue(child);
        }

        if (reachable.Count == 0) return "Nothing ahead on this act's map.";

        var sb = new StringBuilder();
        var bossName = Bestiary.BossName(run);
        sb.Line($"Map, act {run.CurrentActIndex + 1}. " + (bossName.Length > 0 ? $"Boss: {bossName}. " : "") + "Reachable rooms by row as column:type>next columns.");
        sb.Line("M monster, E elite, R rest, $ shop, T treasure, ? unknown, A ancient, B boss.");
        if (current != null) sb.Line($"You are at row {current.coord.row} col {current.coord.col}.");
        foreach (var row in reachable.GroupBy(p => p.coord.row).OrderBy(g => g.Key))
        {
            var cells = row.OrderBy(p => p.coord.col).Select(p =>
            {
                var next = string.Join(",", p.Children.Select(c => c.coord.col).OrderBy(c => c).Distinct());
                return $"{p.coord.col}:{Code(p.PointType)}" + (next.Length > 0 ? ">" + next : "");
            });
            sb.Line($"r{row.Key}: " + string.Join("  ", cells));
        }
        return sb.ToString().TrimEnd();
    }

    // ---------------------------------------------------------------- route planning

    /// <summary>The room kinds a route is judged by, in the order they are printed.</summary>
    private static readonly (MapPointType Type, string Name)[] Counted =
    [
        (MapPointType.Elite, "Elite"),
        (MapPointType.RestSite, "Rest"),
        (MapPointType.Shop, "Shop"),
        (MapPointType.Treasure, "Treasure"),
        (MapPointType.Unknown, "Unknown"),
    ];

    private const int Elite = 0, Rest = 1, Shop = 2, Treasure = 3;

    /// <summary>Everything worked out about the routes from one room to the boss.</summary>
    private sealed class Route
    {
        public readonly int[] Min = new int[Counted.Length];
        public readonly int[] Max = new int[Counted.Length];
        /// <summary>Counts along the suggested route.</summary>
        public readonly int[] Best = new int[Counted.Length];
        /// <summary>Moves needed from this room to stand in the boss room.</summary>
        public int Floors;
        public MapPoint? BestNext;
    }

    /// <summary>
    /// What a player works out by tracing the map with a finger: for each room
    /// that can be entered next, how many elites, rests, shops, treasures and
    /// unknowns lie between it and the boss at least and at most, and one good route.
    /// </summary>
    public static string Paths(RunState run)
    {
        var map = run.Map;
        if (map == null || map is NullActMap) return "No map.";

        MapPoint boss;
        try { boss = map.BossMapPoint; }
        catch { return "No map."; }

        var current = run.CurrentMapPoint;
        List<MapPoint> next;
        try
        {
            if (current == null) next = [map.StartingMapPoint];
            else
            {
                // The game's own rule for where the map lets you go next.
                next = MapTravel.GetTravelablePointsFrom(run, current).ToList();
                if (next.Count == 0 && current != boss && current.coord.row == map.GetRowCount() - 1) next = [boss];
            }
        }
        catch (Exception e)
        {
            Log.Error("paths: next rooms failed: " + e.Message);
            next = current?.Children.ToList() ?? [];
        }
        next = next.Where(p => p != null).OrderBy(p => p.coord.col).ToList();

        var sb = new StringBuilder();
        var bossName = Bestiary.BossName(run);
        sb.Append($"Paths, act {run.CurrentActIndex + 1}.");
        if (bossName.Length > 0) sb.Append($" Boss: {bossName}.");
        if (current != null) sb.Append($" You are at row {current.coord.row} col {current.coord.col}.");
        sb.Append('\n');

        if (current == boss || (current != null && current == map.SecondBossMapPoint) || next.Count == 0)
        {
            sb.Append("No rooms left to choose on this act's map.");
            return sb.ToString();
        }

        AppendPaths(sb, next, boss);
        return sb.ToString().TrimEnd();
    }

    /// <summary>The listing itself. Depends only on the map graph, not on the run.</summary>
    public static void AppendPaths(StringBuilder sb, List<MapPoint> next, MapPoint boss)
    {
        var memo = new Dictionary<MapPoint, Route>();

        // A single way forward is not a choice; show the choice that follows it instead.
        var lead = new List<MapPoint>();
        while (next.Count == 1 && next[0] != boss && next[0].Children.Count > 0 && lead.Count < 4)
        {
            lead.Add(next[0]);
            next = next[0].Children.OrderBy(p => p.coord.col).ToList();
        }
        if (lead.Count > 0)
            sb.Line($"Only one way on: {string.Join(" ", lead.Select(Step))}. The choices after it:");

        if (next.Count == 1 && next[0] == boss)
        {
            sb.Line("The boss is next.");
            return;
        }

        sb.Line("Per next room, min-max rooms of each kind over every route from it to the boss (itself included):");
        foreach (var point in next)
        {
            var route = Solve(point, boss, memo, []);
            var counts = Counted.Select((c, i) => $"{c.Name} {(route.Min[i] == route.Max[i] ? route.Min[i].ToString() : $"{route.Min[i]}-{route.Max[i]}")}");
            sb.Line($"{TypeName(point.PointType)} (col {point.coord.col}): boss in {route.Floors} floor{(route.Floors == 1 ? "" : "s")} | {string.Join(", ", counts)}");
        }

        sb.Line("Suggested route from each (most Rest, then fewest Elite), as type+column per floor:");
        foreach (var point in next)
        {
            var route = Solve(point, boss, memo, []);
            var steps = new List<string>();
            var at = point;
            for (int guard = 0; at != null && guard < 64; guard++)
            {
                steps.Add(Step(at));
                at = at == boss ? null : memo.TryGetValue(at, out var r) ? r.BestNext : null;
            }
            var summary = $"Rest {route.Best[Rest]}, Elite {route.Best[Elite]}";
            if (route.Best[Shop] > 0) summary += $", Shop {route.Best[Shop]}";
            if (route.Best[Treasure] > 0) summary += $", Treasure {route.Best[Treasure]}";
            sb.Line($"col {point.coord.col}: {string.Join(" ", steps)} = {summary}");
        }
        sb.Line("M monster, E elite, R rest, $ shop, T treasure, ? unknown, A ancient, B boss.");
    }

    private static string Step(MapPoint point) =>
        point.PointType == MapPointType.Boss ? "B" : Code(point.PointType) + point.coord.col;

    /// <summary>Depth-first over the map's forward edges with memoisation; the map is a DAG a few rows deep.</summary>
    private static Route Solve(MapPoint point, MapPoint boss, Dictionary<MapPoint, Route> memo, HashSet<MapPoint> inProgress)
    {
        if (memo.TryGetValue(point, out var known)) return known;

        var route = new Route();
        for (int i = 0; i < Counted.Length; i++)
        {
            int own = point.PointType == Counted[i].Type ? 1 : 0;
            route.Min[i] = route.Max[i] = route.Best[i] = own;
        }

        // The boss ends a route (a second boss, if any, is not a choice). The
        // in-progress set keeps a malformed map with a cycle from recursing forever.
        if (point != boss && inProgress.Add(point))
        {
            Route? best = null;
            int[]? min = null, max = null;
            int floors = int.MaxValue;
            foreach (var child in point.Children.OrderBy(c => c.coord.col))
            {
                if (child == null || inProgress.Contains(child)) continue;
                var sub = Solve(child, boss, memo, inProgress);
                min = min == null ? (int[])sub.Min.Clone() : min.Zip(sub.Min, Math.Min).ToArray();
                max = max == null ? (int[])sub.Max.Clone() : max.Zip(sub.Max, Math.Max).ToArray();
                floors = Math.Min(floors, sub.Floors + 1);
                if (best == null || Better(sub.Best, best.Best))
                {
                    best = sub;
                    route.BestNext = child;
                }
            }
            if (best != null && min != null && max != null)
            {
                for (int i = 0; i < Counted.Length; i++)
                {
                    route.Min[i] += min[i];
                    route.Max[i] += max[i];
                    route.Best[i] += best.Best[i];
                }
                route.Floors = floors;
            }
            inProgress.Remove(point);
        }

        memo[point] = route;
        return route;
    }

    /// <summary>More rests first, then fewer elites, then more shops, then more treasure.</summary>
    private static bool Better(int[] a, int[] b)
    {
        if (a[Rest] != b[Rest]) return a[Rest] > b[Rest];
        if (a[Elite] != b[Elite]) return a[Elite] < b[Elite];
        if (a[Shop] != b[Shop]) return a[Shop] > b[Shop];
        return a[Treasure] > b[Treasure];
    }
}
