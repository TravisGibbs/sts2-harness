using HarmonyLib;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;

namespace SpireBridge;

/// <summary>
/// Keeps every fight's native replay. The game records each combat as a CombatReplay (.mcr: the
/// run state on entering the room plus every action and checksum) but writes it to one file,
/// replays/latest.mcr, which the next fight overwrites. This also writes a copy per floor to
/// replays/spirebridge/floor_NN.mcr in the profile folder. The folder is emptied when the
/// bridge starts a new run ('new'), so it always holds the current run's fights; the harness
/// copies it next to the run's recording when the run ends.
/// </summary>
internal static class FightReplays
{
    public const string Folder = "replays/spirebridge";

    public static void Apply(Harmony harmony)
    {
        var target = AccessTools.Method(typeof(RunManager), nameof(RunManager.WriteReplay));
        if (target == null)
        {
            Log.Error("fight replays: RunManager.WriteReplay not found; per-floor replays off");
            return;
        }
        harmony.Patch(target, prefix: new HarmonyMethod(typeof(FightReplays), nameof(BeforeWriteReplay)));
    }

    /// <summary>Absolute path of this profile's per-floor replay folder.</summary>
    public static string Directory() =>
        Godot.ProjectSettings.GlobalizePath(SaveManager.Instance.GetProfileScopedPath(Folder));

    /// <summary>Empty the folder: called when the bridge starts a new run.</summary>
    public static void Clear()
    {
        try
        {
            var dir = Directory();
            if (!System.IO.Directory.Exists(dir)) return;
            foreach (var f in System.IO.Directory.GetFiles(dir, "*.mcr")) File.Delete(f);
        }
        catch (Exception e)
        {
            Log.Error("fight replays: could not clear the folder: " + e.Message);
        }
    }

    private static void BeforeWriteReplay(RunManager __instance)
    {
        try
        {
            var writer = __instance.CombatReplayWriter;
            var run = Game.Run;
            if (writer == null || !writer.IsEnabled || !writer.IsRecordingReplay || run == null) return;
            var path = SaveManager.Instance.GetProfileScopedPath($"{Folder}/floor_{run.TotalFloor:00}.mcr");
            writer.WriteReplay(path, stopRecording: false);
        }
        catch (Exception e)
        {
            // Never let this get in the way of the game's own replay or of ending the fight.
            Log.Error("fight replays: could not write the floor copy: " + e.Message);
        }
    }
}
