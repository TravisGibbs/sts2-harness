using MegaCrit.Sts2.Core.Modding;

namespace SpireBridge;

/// <summary>Entry point the game's mod loader calls once at startup.</summary>
[ModInitializer(nameof(Initialize))]
public static class ModEntry
{
    public const string Version = "0.1.3";

    private static HttpServer? _server;

    public static void Initialize()
    {
        try
        {
            if (!MainThread.TryInit())
            {
                Log.Error("no SceneTree at mod init; bridge disabled");
                return;
            }

            var harmony = new HarmonyLib.Harmony("local.spirebridge");
            BridgeSelector.Patch(harmony);
            NoCloud.Apply(harmony);
            Render.Apply(harmony);
            FightReplays.Apply(harmony);

            int port = HttpServer.DefaultPort;
            var env = System.Environment.GetEnvironmentVariable("SPIREBRIDGE_PORT");
            if (int.TryParse(env, out var p) && p is > 1023 and < 65536) port = p;

            _server = new HttpServer(port);
            _server.Start();
            Log.Info($"v{Version} ready");
        }
        catch (Exception e)
        {
            // Never let the bridge break game startup.
            Log.Error("failed to start: " + e);
        }
    }
}
