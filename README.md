# sts2-harness

A benchmark harness that lets LLMs play **Slay the Spire 2** through text, records every decision, and compares models on identical seeded runs.

- `mod/`: **SpireBridge**, a C# mod that serves the current screen as plain text on `127.0.0.1:15527` and runs commands through the game's own logic.
- `agent/`: the agent loop (`spire_agent.py`), benchmark runner (`bench.py`), reports, replays, model providers and reference policies.
- `tools/`: tests, determinism checks, a mock model server, game-footage rendering (`render/`) and the static site builder (`site/`).

Full documentation: [docs/GUIDE.md](docs/GUIDE.md).

## Requirements

- Windows, with Slay the Spire 2 on Steam (built against **v0.113.1**, Early Access)
- .NET 9 SDK
- Python 3.10+ (`pip install -r agent/requirements.txt`)
- For the site and renders only: `pip install markdown pillow`, and `ffmpeg` on `PATH`

**The game is not included.** The mod compiles against the DLLs of your own installed copy. No game files and no decompiled game code are distributed here.

## Build and install the mod

```powershell
.\build.ps1                                   # default Steam library
.\build.ps1 -GameDir 'D:\SteamLibrary\steamapps\common\Slay the Spire 2'
```

This builds `mod/SpireBridge.csproj` and copies it into `<game>\mods\SpireBridge`. To build by hand: `dotnet build mod -c Release -p:GameDir="<game folder>"`. The first launch with the mod shows the game's prompt to enable mods. Choose Yes, then launch again.

## Launch headless

```powershell
.\launch.ps1                 # headless, no window (Steam must be running)
.\launch.ps1 -Instance 2     # extra instance on port 15527+2
.\launch.ps1 -Stop -All      # stop every instance
```

## Run a benchmark

Set `ANTHROPIC_API_KEY` (or `OPENROUTER_API_KEY` and others, see the guide), in the environment or in a gitignored `.env`.

```powershell
python agent\bench.py --suite pilot --models claude-haiku-5-5,claude-sonnet-5-5 --seeds BENCH01,BENCH02 --parallel 2 --max-cost 2
python agent\report.py --suite pilot --open
```

Results go to `bench/<suite>/`. To test without a key, use `policy:greedy` as the model, or `tools\mock_model_server.py`.

## Build the site

```powershell
python tools\site\build_site.py --skip-videos      # into site_build/sts2
python tools\site\build_site.py --publish <folder>  # also mirror the build into a folder
```

The site reads the suites listed in `SUITES` in `tools/site/manifest.py`. Edit that list to match your own `bench/` suites. Game footage comes from `renders/` (`tools/render/`), or from `--renders <dir>`.

## Tests

```powershell
python -m unittest discover -s tools -p "test_*.py"
```
