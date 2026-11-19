using System.Text;
using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

/// <summary>
/// The surface the HTTP server calls. Every method here runs on the main thread.
/// </summary>
internal static class Bridge
{
    private const double SettleTimeoutSeconds = 45;
    private const int StableFramesRequired = 6;
    private const double StableMilliseconds = 350;

    public static string Health()
    {
        var run = Game.Run;
        return $"ok spirebridge {ModEntry.Version} game {Meta.GameVersion} run={(run != null ? "yes" : "no")}";
    }

    /// <param name="peek">
    /// An observer's read: touches nothing. It does not wait, click through
    /// dialogue, attach the agent hooks, or use up term explanations.
    /// </param>
    public static async Task<string> State(bool peek = false)
    {
        var sb = new StringBuilder();
        if (peek)
        {
            Glossary.Peek = true;
            try { RenderScreen(sb, Screens.Detect()); }
            finally { Glossary.Peek = false; }
            return sb.ToString();
        }

        Attach();
        var screen = Screens.Detect();
        if (screen.IsBusy)
        {
            // Give animations and dialogue a moment rather than reporting BUSY straight away.
            await Settle(15);
            screen = Screens.Detect();
        }
        RenderScreen(sb, screen);
        return sb.ToString();
    }

    public static string Info(string query)
    {
        Attach();
        return InfoQueries.Answer(query);
    }

    /// <summary>
    /// Execute a batch of commands (one per line, or separated by ';') against
    /// the current screen, then return any errors plus the resulting state.
    /// </summary>
    public static async Task<string> Act(string text)
    {
        Attach();
        var commands = text
            .Split(['\n', ';'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Where(c => c.Length > 0)
            .ToList();

        var notes = new StringBuilder();
        if (commands.Count == 0) notes.Line("! no commands given");

        // Indices in the batch refer to what the agent last saw: the screen as it is now.
        var screen = Screens.Detect();
        if (screen.IsBusy && commands.Count > 0)
        {
            await Settle();
            screen = Screens.Detect();
        }

        for (int i = 0; i < commands.Count; i++)
        {
            var parts = commands[i].Split(' ', StringSplitOptions.RemoveEmptyEntries);
            var verb = parts[0].ToLowerInvariant();
            var args = parts.Skip(1).ToArray();

            if (verb == "abandon" && RunManager.Instance.IsInProgress)
            {
                // Give up the current run; the game then shows its game-over screen.
                RunManager.Instance.Abandon();
                await Settle();
                continue;
            }

            if (verb == "info")
            {
                // A lookup changes nothing, so it can ride along in a batch.
                notes.Line(InfoQueries.Answer(string.Join(' ', args)));
                continue;
            }

            if (verb == "quit")
            {
                // Exit a few frames from now so this response still gets delivered.
                _ = QuitSoon();
                return "ok: game is closing";
            }

            // Commands without an index (proceed, end, open, skip) do not depend on
            // the snapshot, so later ones run against the screen as it is now.
            // 'choose 0; proceed' at a chest works even though proceed only
            // appears once the relic has been taken.
            var target = screen;
            if (i > 0 && args.Length == 0)
            {
                var live = Screens.Detect();
                if (live.Kind == screen.Kind) target = live;
            }

            string? error;
            try
            {
                error = verb switch
                {
                    "wait" => null,
                    "speed" => Meta.SetSpeed(args.FirstOrDefault() ?? ""),
                    _ => await target.Exec(verb, args),
                };
            }
            catch (Exception e)
            {
                Log.Error($"command '{commands[i]}' threw: {e}");
                error = $"internal error: {e.GetType().Name}: {e.Message}";
            }

            if (error != null)
            {
                notes.Line($"! {commands[i]}: {error}");
                if (i + 1 < commands.Count) notes.Line($"! skipped {commands.Count - i - 1} later command(s)");
                break;
            }

            await Settle();

            if (i + 1 >= commands.Count) break;
            var now = Screens.Detect();

            // Picking a card reward drops back to the rewards list the agent has
            // already seen. Let argument-free commands (proceed) carry on there;
            // anything with an index would be pointing at a list that has shifted.
            if (screen.Kind == "CARD_REWARD" && now.Kind == "REWARDS"
                && commands.Skip(i + 1).All(c => !c.Contains(' ')))
            {
                screen = now;
                continue;
            }

            string? stop = now.Kind != screen.Kind ? $"screen changed to {now.Kind}" : screen.StopReason(now);
            if (stop != null)
            {
                notes.Line($"! stopped after '{commands[i]}' ({stop}); skipped {commands.Count - i - 1} later command(s)");
                break;
            }
        }

        RenderScreen(notes, Screens.Detect());
        return notes.ToString();
    }

    private static async Task QuitSoon()
    {
        for (int i = 0; i < 20; i++) await MainThread.NextFrame();
        Meta.QuitGame();
    }

    private static void RenderScreen(StringBuilder sb, Screen screen)
    {
        try
        {
            screen.Render(sb);
        }
        catch (Exception e)
        {
            Log.Error($"render of {screen.Kind} threw: {e}");
            sb.Line($"! could not render {screen.Kind}: {e.GetType().Name}: {e.Message}");
        }
    }

    /// <summary>Per-request housekeeping while an agent is driving the game.</summary>
    private static void Attach()
    {
        BridgeSelector.DropIfStale();
        if (RunManager.Instance.IsInProgress) BridgeSelector.EnsureInstalled();
        Meta.ApplyAgentPrefsOnce();
    }

    /// <summary>
    /// Wait until the game stops changing: not mid-transition, and the screen
    /// fingerprint identical for a run of consecutive frames.
    /// </summary>
    public static async Task Settle(double timeoutSeconds = SettleTimeoutSeconds)
    {
        var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        string? last = null;
        int stable = 0;
        // Frame rate varies a lot (uncapped when headless), so stability is
        // judged on wall-clock time as well as a minimum number of frames.
        var stableSince = DateTime.UtcNow;

        // Queued game actions start on a later frame; never sample on the same one.
        await MainThread.NextFrame();
        await MainThread.NextFrame();

        int frame = 0;
        // Game time the signature has been stable. Headless, game time is wall time; while filming,
        // Movie Maker gives every frame a fixed step whatever it took to draw, so the two drift apart.
        // Requiring the stable time in both keeps a filmed replay settling where the original did.
        double stableGameSeconds = 0;
        while (DateTime.UtcNow < deadline)
        {
            string? signature = null;
            try
            {
                var screen = Screens.Detect();
                if (!screen.IsBusy) signature = screen.Signature;
                // Story dialogue has no decision in it: click through, a few times a second.
                else if (screen is DialogueScreen dialogue && frame % 15 == 0) dialogue.Advance();
            }
            catch (Exception e)
            {
                Log.Error("detect failed while settling: " + e.Message);
            }

            if (signature != null && signature == last)
            {
                stable++;
                stableGameSeconds += FrameSeconds();
            }
            else
            {
                stable = 0;
                stableGameSeconds = 0;
                stableSince = DateTime.UtcNow;
            }
            last = signature;

            if (stable >= StableFramesRequired && (DateTime.UtcNow - stableSince).TotalMilliseconds >= StableMilliseconds
                && (!Render.Enabled || stableGameSeconds * 1000 >= StableMilliseconds)) return;
            frame++;
            await MainThread.NextFrame();
        }
        Log.Info("settle timed out; returning current state");
    }

    private static double FrameSeconds()
    {
        try { return MegaCrit.Sts2.Core.Nodes.NGame.Instance?.GetProcessDeltaTime() ?? 0; }
        catch { return 0; }
    }
}
