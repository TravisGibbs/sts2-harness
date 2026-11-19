using System.Collections.Concurrent;
using Godot;

namespace SpireBridge;

/// <summary>
/// Marshals work from the HTTP thread onto Godot's main thread. All game state
/// reads and writes must go through here: the game is not thread-safe.
/// </summary>
internal static class MainThread
{
    private static readonly ConcurrentQueue<Action> Queue = new();
    private static int _mainThreadId = -1;
    private static bool _hooked;

    public static bool IsMainThread => System.Environment.CurrentManagedThreadId == _mainThreadId;

    /// <summary>Hook the per-frame pump. Must be called from the main thread.</summary>
    public static bool TryInit()
    {
        if (_hooked) return true;
        if (Engine.GetMainLoop() is not SceneTree tree) return false;
        _mainThreadId = System.Environment.CurrentManagedThreadId;
        tree.ProcessFrame += Pump;
        _hooked = true;
        return true;
    }

    private static void Pump()
    {
        // Bound the work per frame so a burst of requests cannot stall rendering.
        for (int i = 0; i < 32 && Queue.TryDequeue(out var work); i++)
        {
            try { work(); }
            catch (Exception e) { Log.Error("main-thread work threw: " + e); }
        }
    }

    /// <summary>Run a synchronous function on the main thread and await its result.</summary>
    public static Task<T> Run<T>(Func<T> func)
    {
        var tcs = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
        Queue.Enqueue(() =>
        {
            try { tcs.SetResult(func()); }
            catch (Exception e) { tcs.SetException(e); }
        });
        return tcs.Task;
    }

    /// <summary>
    /// Start an async function on the main thread and await its completion. Its
    /// continuations stay on the main thread (Godot's synchronization context).
    /// </summary>
    public static Task<T> RunAsync<T>(Func<Task<T>> func)
    {
        var tcs = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
        Queue.Enqueue(async () =>
        {
            try { tcs.SetResult(await func()); }
            catch (Exception e) { tcs.SetException(e); }
        });
        return tcs.Task;
    }

    /// <summary>Await the next rendered frame. Main thread only.</summary>
    public static async Task NextFrame()
    {
        var tree = (SceneTree)Engine.GetMainLoop();
        await tree.ToSignal(tree, SceneTree.SignalName.ProcessFrame);
    }

    /// <summary>
    /// Poll a condition once per frame until it holds or the timeout elapses.
    /// Main thread only. Returns whether the condition was met.
    /// </summary>
    public static async Task<bool> WaitUntil(Func<bool> condition, double timeoutSeconds)
    {
        var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        while (true)
        {
            bool ok;
            try { ok = condition(); }
            catch { ok = false; }
            if (ok) return true;
            if (DateTime.UtcNow >= deadline) return false;
            await NextFrame();
        }
    }
}
