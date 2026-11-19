using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Nodes;

namespace SpireBridge;

/// <summary>
/// Render mode, for filming runs with Godot's Movie Maker (SPIREBRIDGE_RENDER=1, set by
/// `launch.ps1 -Render`). The game then always runs in a small window instead of the player's
/// saved display settings (often fullscreen at desktop size), with vsync and the frame cap off
/// so frames are produced as fast as the GPU allows. launch.ps1 starts such an instance on a
/// separate Windows desktop that is never shown, so the window can't appear or take focus.
/// The size comes from SPIREBRIDGE_RENDER_SIZE ("1280x720" by default).
/// </summary>
internal static class Render
{
    public static bool Enabled =>
        System.Environment.GetEnvironmentVariable("SPIREBRIDGE_RENDER") is "1" or "true";

    public static Vector2I Size
    {
        get
        {
            var text = System.Environment.GetEnvironmentVariable("SPIREBRIDGE_RENDER_SIZE") ?? "";
            var parts = text.Split('x', 'X');
            if (parts.Length == 2 && int.TryParse(parts[0], out var w) && int.TryParse(parts[1], out var h)
                && w is >= 320 and <= 3840 && h is >= 180 and <= 2160)
                return new Vector2I(w, h);
            return new Vector2I(1280, 720);
        }
    }

    public static void Apply(Harmony harmony)
    {
        if (!Enabled) return;
        var target = AccessTools.Method(typeof(NGame), "InitializeGraphicsPreferences");
        if (target == null)
        {
            Log.Error("render mode: NGame.InitializeGraphicsPreferences not found; window left as the game sets it");
            return;
        }
        harmony.Patch(target, postfix: new HarmonyMethod(typeof(Render), nameof(AfterGraphicsPreferences)));
        Log.Info($"render mode on: window {Size.X}x{Size.Y}");
    }

    /// <summary>Runs after the game applied the saved display settings; replaces what matters for filming.</summary>
    private static void AfterGraphicsPreferences()
    {
        try
        {
            DisplayServer.WindowSetMode(DisplayServer.WindowMode.Windowed);
            DisplayServer.WindowSetSize(Size);
            DisplayServer.WindowSetPosition(new Vector2I(0, 0));
            DisplayServer.WindowSetVsyncMode(DisplayServer.VSyncMode.Disabled);
            Engine.MaxFps = 0;
            Log.Info($"render mode: window now {DisplayServer.WindowGetSize()}");
        }
        catch (Exception e)
        {
            Log.Error("render mode: could not set the window: " + e.Message);
        }
    }
}
