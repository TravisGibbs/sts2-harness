using System.Reflection;
using HarmonyLib;
using MegaCrit.Sts2.Core.Platform.Steam;
using MegaCrit.Sts2.Core.Saves;

namespace SpireBridge;

/// <summary>
/// Keeps an extra game instance off Steam Cloud. Every instance of the game
/// shares one cloud save slot, so several benchmark instances running at once
/// would overwrite each other's (and the player's) cloud saves. With
/// SPIREBRIDGE_NO_CLOUD=1 this instance stops writing to, deleting from and
/// syncing with the cloud; its saves stay in its own local folder only.
/// launch.ps1 sets the variable for instances 1 and up, never for the normal game.
/// </summary>
internal static class NoCloud
{
    private static readonly HashSet<string> Mutating =
    [
        "WriteFile", "WriteFileAsync", "DeleteFile", "RenameFile", "CreateDirectory", "DeleteDirectory",
        "DeleteTemporaryFiles", "ForgetFile", "BeginSaveBatch", "EndSaveBatch", "SetLastModifiedTime",
    ];

    public static bool Enabled =>
        System.Environment.GetEnvironmentVariable("SPIREBRIDGE_NO_CLOUD") is "1" or "true";

    public static void Apply(Harmony harmony)
    {
        if (!Enabled) return;

        var skip = new HarmonyMethod(typeof(NoCloud), nameof(Skip));
        var skipTask = new HarmonyMethod(typeof(NoCloud), nameof(SkipTask));
        int patched = 0;

        const BindingFlags flags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly;
        foreach (var method in typeof(SteamRemoteSaveStore).GetMethods(flags))
        {
            if (!Mutating.Contains(method.Name)) continue;
            if (TryPatch(harmony, method, method.ReturnType == typeof(Task) ? skipTask : method.ReturnType == typeof(void) ? skip : null)) patched++;
        }

        // The pull direction: never replace this instance's local saves with cloud copies.
        foreach (var method in typeof(SaveManager).GetMethods(flags))
        {
            if (method.Name == "SyncCloudToLocal" && method.ReturnType == typeof(Task) && TryPatch(harmony, method, skipTask)) patched++;
        }
        foreach (var method in typeof(CloudSaveStore).GetMethods(flags))
        {
            if (method.Name is "SyncCloudToLocal" or "OverwriteCloudWithLocal" && method.ReturnType == typeof(Task) && TryPatch(harmony, method, skipTask)) patched++;
        }

        Log.Info($"Steam Cloud disabled for this instance ({patched} methods patched)");
    }

    private static bool TryPatch(Harmony harmony, MethodInfo method, HarmonyMethod? prefix)
    {
        if (prefix == null) return false;
        try
        {
            harmony.Patch(method, prefix: prefix);
            return true;
        }
        catch (Exception e)
        {
            Log.Error($"could not patch {method.DeclaringType?.Name}.{method.Name}: {e.Message}");
            return false;
        }
    }

    private static bool Skip() => false;

    private static bool SkipTask(ref Task __result)
    {
        __result = Task.CompletedTask;
        return false;
    }
}
