using MegaCrit.Sts2.Core.Debug;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Settings;

namespace SpireBridge;

/// <summary>Game-wide settings the bridge reads or adjusts while an agent is attached.</summary>
internal static class Meta
{
    private static bool _prefsApplied;
    private static FastModeType? _originalFastMode;

    public static bool IsHeadless
    {
        get
        {
            try { return Godot.DisplayServer.GetName().Equals("headless", StringComparison.OrdinalIgnoreCase); }
            catch { return false; }
        }
    }

    /// <summary>
    /// Close the game cleanly. In headless mode the game's own quit path would
    /// record the (non-existent) window's size and position into the player's
    /// display settings, so save everything except those and exit directly.
    /// </summary>
    public static void QuitGame()
    {
        var game = MegaCrit.Sts2.Core.Nodes.NGame.Instance;
        if (game == null) return;
        try
        {
            var save = SaveManager.Instance;
            if (_originalFastMode is { } original) save.PrefsSave.FastMode = original;
            // Render mode's window is not the player's either: keep it out of their display settings too.
            if (IsHeadless || Render.Enabled)
            {
                if (save.IsProfileInitialized)
                {
                    save.SavePrefsFile();
                    save.SaveProgressFile();
                    save.SaveProfile();
                }
                game.GetTree().Quit();
                return;
            }
        }
        catch (Exception e)
        {
            Log.Error("clean quit failed, falling back: " + e.Message);
        }
        game.Quit();
    }

    public static string GameVersion
    {
        get
        {
            try { return ReleaseInfoManager.Instance.ReleaseInfo?.Version ?? "unknown"; }
            catch { return "unknown"; }
        }
    }

    /// <summary>
    /// The first time an agent talks to the bridge: speed up animations and
    /// turn off tutorial popups, which would otherwise block on a mouse click.
    /// Both are ordinary game settings the player can change back in Settings.
    /// </summary>
    public static void ApplyAgentPrefsOnce()
    {
        if (_prefsApplied) return;
        _prefsApplied = true;
        try
        {
            var save = SaveManager.Instance;
            _originalFastMode = save.PrefsSave.FastMode;
            if (save.PrefsSave.FastMode is FastModeType.None or FastModeType.Normal)
                save.PrefsSave.FastMode = FastModeType.Fast;
            save.SetFtuesEnabled(false);
            Log.Info("agent attached: fast mode on, tutorials off");
        }
        catch (Exception e)
        {
            Log.Error("could not apply agent prefs: " + e.Message);
        }
    }

    public static string? SetSpeed(string value)
    {
        FastModeType? mode = value.ToLowerInvariant() switch
        {
            "normal" => FastModeType.Normal,
            "fast" => FastModeType.Fast,
            "instant" => FastModeType.Instant,
            _ => null,
        };
        if (mode == null) return "usage: speed normal|fast|instant";
        SaveManager.Instance.PrefsSave.FastMode = mode.Value;
        return null;
    }
}
