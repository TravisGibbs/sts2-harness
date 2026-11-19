using System.Net;
using System.Text;

namespace SpireBridge;

/// <summary>
/// Minimal loopback-only HTTP server. Plain text in, plain text out: the
/// payloads are written for a language model to read, so there is no JSON
/// envelope to spend tokens on.
///
///   GET  /health          liveness + versions
///   GET  /state           current screen as compact text
///   POST /act             body = one command per line; returns results + new state
///   GET  /info?q=...      on-demand detail (deck, piles, relics, map, card text)
///   GET  /snapshot        JSON telemetry for recording (never shown to the agent)
/// </summary>
internal sealed class HttpServer
{
    public const int DefaultPort = 15527;

    private readonly HttpListener _listener = new();
    private readonly SemaphoreSlim _actLock = new(1, 1);
    private readonly int _port;

    public HttpServer(int port)
    {
        _port = port;
        // 127.0.0.1 only: never reachable from another machine.
        _listener.Prefixes.Add($"http://127.0.0.1:{port}/");
    }

    public void Start()
    {
        _listener.Start();
        _ = Task.Run(AcceptLoop);
        Log.Info($"listening on http://127.0.0.1:{_port}/");
    }

    private async Task AcceptLoop()
    {
        while (_listener.IsListening)
        {
            HttpListenerContext ctx;
            try { ctx = await _listener.GetContextAsync(); }
            catch (Exception e) when (e is HttpListenerException or ObjectDisposedException) { break; }
            _ = Task.Run(() => Handle(ctx));
        }
    }

    private async Task Handle(HttpListenerContext ctx)
    {
        int status = 200;
        string body;
        try
        {
            if (!IsTrusted(ctx.Request))
            {
                status = 403;
                body = "forbidden: requests must come from a local non-browser client";
            }
            else
            {
                (status, body) = await Route(ctx.Request);
            }
        }
        catch (Exception e)
        {
            status = 500;
            body = "error: " + e.GetType().Name + ": " + e.Message;
            Log.Error("request failed: " + e);
        }

        try
        {
            var bytes = Encoding.UTF8.GetBytes(body);
            ctx.Response.StatusCode = status;
            ctx.Response.ContentType = "text/plain; charset=utf-8";
            ctx.Response.ContentLength64 = bytes.Length;
            await ctx.Response.OutputStream.WriteAsync(bytes);
            ctx.Response.Close();
        }
        catch (Exception e)
        {
            Log.Error("failed to write response: " + e.Message);
        }
    }

    /// <summary>
    /// Web pages can reach localhost ports. Refuse anything that looks like a
    /// browser (Origin / Sec-Fetch-Site headers) or a DNS-rebinding attempt
    /// (Host header that is not loopback), so a website cannot drive the game.
    /// </summary>
    private bool IsTrusted(HttpListenerRequest req)
    {
        if (!IPAddress.IsLoopback(req.RemoteEndPoint.Address)) return false;
        if (!string.IsNullOrEmpty(req.Headers["Origin"])) return false;
        var fetchSite = req.Headers["Sec-Fetch-Site"];
        if (!string.IsNullOrEmpty(fetchSite) && fetchSite != "none") return false;
        var host = req.Headers["Host"] ?? "";
        return host == $"127.0.0.1:{_port}" || host == $"localhost:{_port}";
    }

    private async Task<(int, string)> Route(HttpListenerRequest req)
    {
        var path = req.Url?.AbsolutePath.TrimEnd('/') ?? "";
        switch (req.HttpMethod, path)
        {
            case ("GET", "/health"):
                return (200, await MainThread.Run(Bridge.Health));

            case ("GET", "/state"):
                bool peek = req.QueryString["peek"] is "1" or "true";
                return (200, await MainThread.RunAsync(() => Bridge.State(peek)));

            case ("GET", "/snapshot"):
                return (200, await MainThread.Run(Snapshot.Json));

            case ("GET", "/info"):
                var q = req.QueryString["q"] ?? "";
                return (200, await MainThread.Run(() => Bridge.Info(q)));

            case ("POST", "/act"):
                string text;
                using (var reader = new StreamReader(req.InputStream, Encoding.UTF8))
                    text = await reader.ReadToEndAsync();
                // One action batch at a time: actions mutate the game.
                await _actLock.WaitAsync();
                try { return (200, await MainThread.RunAsync(() => Bridge.Act(text))); }
                finally { _actLock.Release(); }

            default:
                return (404, "not found. endpoints: GET /health, GET /state, GET /info?q=, POST /act");
        }
    }
}
