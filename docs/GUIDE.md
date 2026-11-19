# sts2-harness: full guide

Benchmark recordings (`bench/`), renders and the built site are not part of this repository; paths below such as `bench/<suite>/` are where your own runs are written.

Lets an LLM play **Slay the Spire 2** without screenshots, mouse, or keyboard, records everything it does, and benchmarks models against each other on identical seeded runs.

| Piece | What it is |
|---|---|
| `mod/` — **SpireBridge** | A small C# mod that runs inside the game. It serves the current screen as compact plain text over `127.0.0.1:15527` and executes commands by calling the game's own logic. |
| `agent/spire_agent.py` | A loop on the Claude API. It gives the model three tools (`act`, `info`, `set_notes`), keeps context small, and records every step. |
| `agent/system1.py`, `agent/policy.py` | The text protocol for "System 1" models (one stateless request per decision, commands as plain text) and non-LLM policies (Python or HTTP), with two reference policies in `agent/policies/`. |
| `agent/bench.py` | Benchmark runner: every model plays the same seeds under the same rules. |
| `agent/report.py` | Builds the benchmark report: survival by floor, stage-by-stage success, HP, and a replay per run. |
| `agent/replay.py` | Turns one run recording into a step-through replay page. |

```
 Claude API  <--tools-->  spire_agent.py  <--HTTP text-->  SpireBridge mod (in game)
                               |
                          run recording (.jsonl)  -->  replay.html / report.html
```

## Setup

Requirements: Windows, Slay the Spire 2 (Steam), .NET 9 SDK, Python 3.10+.

```powershell
.\build.ps1                                # builds the mod and copies it to <game>\mods\SpireBridge
python -m pip install -r agent\requirements.txt
```

First launch after installing: the game shows its own prompt asking whether to enable mods. Choose **Yes**; the game closes. Launch it again and the mod loads.

With any mod loaded the game uses a separate `modded` save folder (it copies your normal saves there the first time), so agent runs do not touch your regular progress.

API key: set `ANTHROPIC_API_KEY`, or put `ANTHROPIC_API_KEY=...` in a `.env` file in the repo root (gitignored).

## Running the game

Headless is the default: no window opens and nothing takes focus.

```powershell
.\launch.ps1              # headless (Steam must be running)
.\launch.ps1 -Windowed    # normal window, to watch the agent play live
.\launch.ps1 -Stop        # close the game cleanly
.\launch.ps1 -Instance 2  # an extra headless game on port 15527+2
.\launch.ps1 -Stop -All   # close every instance
```

Extra instances (`-Instance 1` and up) are for running benchmarks in parallel. Each keeps its own copy of the game's user data under `.instances\<n>\` (made from your real folder the first time, which is never written to) and has Steam Cloud switched off, so instances cannot overwrite each other's saves or yours. Each uses about 0.75 GB of RAM and a quarter of one CPU core.

## Playing one run

```powershell
python agent\spire_agent.py --new ironclad --seed MYSEED --model claude-sonnet-5-5 --effort low --max-cost 2
```

Without `--new` it plays whatever run is currently loaded. A recording is written to `runs/<time>-<model>.jsonl`; the command prints how to open its replay.

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `claude-opus-5-5` | Any Claude model id |
| `--effort` | `low` | Thinking effort per decision (`low` ... `max`) |
| `--new CHARACTER` | none | Start a fresh standardized run first (`ironclad`, `silent`, `regent`, `necrobinder`, `defect`, `fool`) |
| `--seed`, `--ascension` | random, `0` | For `--new` |
| `--max-cost` | `5` | Stop once estimated spend reaches this many USD |
| `--max-requests` | `2000` | Stop after this many model requests |
| `--max-floor` | none | Stop once the run is past this floor |

## Watching what happened

**Replay viewer.** Every run is recorded as JSONL: each model request with the state it saw, its reasoning summary, the commands it sent, the game's answer, tokens, cost, latency, and a telemetry snapshot (floor, HP, gold, deck, relics).

```powershell
python agent\replay.py runs\<recording>.jsonl --open
```

The page is self-contained. It shows an HP timeline you can click, a floor list, and for each step: what the model saw, its reasoning, what it sent, and what the game answered. Arrow keys step through.

**Live.** Run `.\launch.ps1 -Windowed` to watch the game itself while the agent plays. To read the current screen from outside without disturbing a run, use `GET /state?peek=1` or `GET /snapshot`.

**In the game.** Finished runs also appear in the game's own Compendium → Run History (in the modded profile).

## Benchmarking models

```powershell
python agent\bench.py --suite pilot --models claude-haiku-5-5,claude-sonnet-5-5 --seeds BENCH01,BENCH02,BENCH03
python agent\report.py --suite pilot --open
```

Every model plays every seed. Results go to `bench/<suite>/`: one recording per run, `report.html`, a replay per run, and `summary.json`. The suite is resumable (finished runs are skipped), and the report can be rebuilt while a suite is still playing.

| Flag | Default | Meaning |
|---|---|---|
| `--models` | required | Comma-separated model specs (see below); each may carry its own effort as `@effort` |
| `--seeds` | required | Comma-separated; shared by all models |
| `--character`, `--ascension` | `ironclad`, `0` | The run every model plays |
| `--max-requests` | `600` | Per-run cap on model requests; a run that hits it is "cut short", not a death |
| `--max-floor` | none | Stop each run past this floor (e.g. `17` for an Act 1 sprint) |
| `--attempts` | `1` | Play each (model, seed) this many times, e.g. `10` for a pass@10 on one seed |
| `--parallel` | `1` | Number of game instances to play on at once; the runner starts and stops them |
| `--max-cost` | none | Per-run spend cap in USD |
| `--budget` | none | Stop starting new runs once the suite has spent this many USD (runs in flight finish) |

A pass@10 for two models on one seed, five games at a time:

```powershell
python agent\bench.py --suite pass10 --models claude-haiku-5-5,claude-sonnet-5-5 --seeds BENCH01 --attempts 10 --parallel 5
```

The game rewrites some seed characters (`O` becomes `0`, `I` becomes `1`), so seeds that differ only by those are the same run.

### Resuming safely

Re-running a suite skips completed attempts. Before retrying a failed or partial attempt, the runner moves its original JSONL into `bench/<suite>/archive/`; these files are retained as evidence but excluded from score calculations. Spend limits count both current and archived requests, plus any explicitly documented `cost_adjustments.json`. A resume rejects changed character, ascension, seeds, attempt count, limits, models, protocols, prompts or harness version: use a new suite name for a different experiment.

A cost cap requires known, finite, nonnegative prices. The runner refuses unknown-price models when `--max-cost` or `--budget` is set. Caps are checked between requests, so the last request can exceed a cap; the suite budget stops new runs while runs already in flight finish. Twelve repeated invalid action replies on an unchanged state stop a tool run as `stalled`, with a recorded reason, instead of spending indefinitely. Lookups are guarded too (harness 0.3): an identical repeated `info` or `set_notes` call with no move in between is answered with "you already have this, act now" instead of reaching the game, and after 40 non-move calls (or 20 requests) without a move, further lookups are refused; three more such requests end the run as `stalled` (`lookup_loop`). The thresholds sit above the longest legitimate lookup streak in 143 recorded tool runs (31 calls over 9 requests). Runs before harness 0.3 have no lookup guard: one Qwen3 30B run in `ladder-ironclad-a0` spent its $0.75 cap repeating `info hand` 196 times.

### Models from any provider

A model spec is `[provider:]model[@effort][@protocol]` (protocols: below). Plain `claude-...` names use the Anthropic API (the older `claude-haiku-5-5:low` form still works). Anything that speaks the OpenAI Chat Completions API works through a provider prefix:

| Provider | Example spec | Key |
|---|---|---|
| Anthropic | `claude-opus-5-5@low` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai:gpt-5.1@low` | `OPENAI_API_KEY` |
| OpenRouter | `openrouter:meta-llama/llama-4-maverick` | `OPENROUTER_API_KEY` |
| OpenRouter Decisions | `decisions:typesafe/jev-1.14` | `OPENROUTER_API_KEY` |
| Local (Ollama, vLLM, LM Studio) | `local:qwen3:32b` | none; `SPIRE_LOCAL_BASE_URL` (default Ollama's `http://127.0.0.1:11434/v1`) |

Within the tools protocol, every model gets the same system prompt, function-tool schemas, game text, per-floor context reset and limits; the prompt fingerprint is identical across providers. Text and decision protocols are separate contestants with their own interface and fingerprint. What a provider forces to differ is recorded in each run: provider and endpoint host (never the key), whether a thinking effort was sent, and whether prompt caching applies. Costs are known for Claude models; for others pass `--prices prices.json` (`{"openai:gpt-5.1": [input, output, cache_read, cache_write]}` in USD per million tokens), otherwise cost shows as n/a and `--max-cost`/`--budget` cannot stop those runs. More providers can be added with `SPIRE_PROVIDERS=<json file>`.

`tools\mock_model_server.py` is a scripted stand-in model on the OpenAI API, for testing the harness without any key; it also answers text-protocol requests (no tools) in plain text. Real OpenRouter tool runs for six models were recorded in the author's `openmodels-ironclad-a0` suite (recordings are not included in this repo); the report distinguishes game losses, format stalls and caps.

### System 1 models and non-LLM policies

The default protocol (`tools`) lets a model deliberate: a multi-turn conversation per floor with `act`, `info` and `set_notes`, and thinking at the chosen effort. To benchmark "System 1" players, which map the state to an action in one fast step, a spec can end in `@text` instead:

```powershell
python agent\bench.py --suite s1 --models openrouter:meta-llama/llama-3.1-8b-instruct@text,claude-haiku-5-5@low@text,policy:greedy --seeds BENCH01
```

**Text protocol** (`agent/system1.py`). Each decision is one stateless request: a compact system prompt (the same interface rules and strategy guidance, playbook included, minus everything about tools, lookups and notes), the run summary and the current state. The model answers with commands only (`play 0 e0; play 2; end`); the harness takes the last line(s) that are commands valid on that screen, ignoring code fences, bullets and chatter around them. No tools are sent and thinking is off, or as low as the model allows (Claude: `thinking` disabled, `between_tools` on Sonnet 5.5, adaptive at the given effort where it cannot be turned off; OpenAI-compatible: only the effort the spec names, `@default` sends none). An answer with no usable command, or whose first command the game rejects, is shown back once with the error; a second failure plays `end` in combat and `wait` elsewhere. Decisions, retries and fallbacks are counted in each run and shown in the report. There are no notes and no history: a decision depends only on what the game shows, which is also exactly what a policy sees. Six decisions in a row that only `wait` end the run as stalled.

**Policies** (`agent/policy.py`). `policy:<target>` plays something that is not a language model, always under the text protocol: a trained RL agent, a world-model-based controller, a script. The target is a reference policy name, `module:function`, `path\to\file.py:function`, or an HTTP endpoint (`policy:http://127.0.0.1:9000/act`). A Python policy is `act(state)` or `act(state, context) -> "commands"`, where `context` has the run summary, seed, attempt, the decision number, whether this is a retry, a per-run `random.Random` seeded from (seed, attempt) and a per-run `memory` dict. An HTTP policy is POSTed the state text and answers with the commands as text (or JSON `{"commands": ...}`); headers carry the run id, seed, attempt, step and retry flag. Policies cost $0.

**Reference policies** (`agent/policies/`), floors that any model should beat:

| Policy | What it does |
|---|---|
| `policy:random_legal` | One uniformly random command per decision from the playable, affordable cards, `end`, potions, numbered options and the Commands line's verbs; seeded, so a (seed, attempt) replays exactly. |
| `policy:greedy` | Blocks when unblocked incoming damage exceeds 5, then plays the highest-damage affordable attacks at the weakest enemy, then anything else affordable, then `end`; takes the first card reward, rests below half HP (otherwise upgrades), avoids elites below 70% HP, skips shops. |

**Fairness.** The protocol is recorded in every run (`run_start.protocol`), text-protocol prompts have their own fingerprint (the tool protocol's is unchanged), recordings carry the protocol in their file name (`<model>__<effort>__text__<seed>__a01.jsonl`), and every report, comparison and leaderboard row keys on it and labels it (`haiku-5-5 · low · text protocol`), so the same model under the two protocols is two contestants and never pooled.

### Jev and other typed decision models

`decisions:typesafe/jev-1.14` calls OpenRouter's **Decisions API** directly, with the same OpenRouter key. It is different from `openrouter:typesafe/jev-router`, which routes requests to generative models. The direct Jev model returns a typed choice and probabilities, not generated prose.

```powershell
python agent\bench.py --suite jev-ironclad-a0 --models decisions:typesafe/jev-1.14 --seeds BENCH01,BENCH02 --parallel 2 --max-requests 600 --max-cost 0.10 --budget 0.20
```

The adapter builds a deterministic menu of up to 128 single-command candidates from the visible state. This is a bounded approximation: it can omit legal actions or include choices the game rejects. It adds no hidden game state, rollout or oracle. Each recording retains the actual menu, selected command, confidence, probability distribution, served model version and API-reported cost. Decisions have their own protocol label and prompt fingerprint; they are not pooled with text or tool-call players. Python baseline policies remain a separate zero-cost category.

Jev's known input price is a fallback estimate; live Decisions requests use `usage.cost` for accounting and caps. Other decision-model IDs can use the same adapter when they support Choice requests, with an explicit prices file for budget preflight. Unsupported IDs and unknown prices are not silently substituted. Jev Router's dynamic `-1` catalog price is rejected as an invalid estimate.

References checked November 16, 2026: [OpenRouter Jev guide](https://openrouter.ai/docs/guides/community/jev), [Decisions API schema](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request), [TypeSafe model reference](https://docs.typesafe.ai/models).

To see whether a change helped, compare suites in one report (same models, different prompt or harness):

```powershell
python agent\report.py --compare pass10-ironclad-a0,pass10-ironclad-a0-v2 --labels "playbook v1,playbook v2"
```

It writes `bench\_compare\<name>\report.html`. Each model keeps its colour; the earlier suite is dashed. A "What changed between suites" card lists exactly what differed (prompt fingerprint, commit, limits) and, per stage, each suite's result side by side with a 95% range for the difference, so a gap that is within luck is labelled as such.

### Everything in one place

```powershell
python agent\index.py --open
```

builds `bench\index.html`: a leaderboard across all suites (separate rows for model, effort, protocol, prompt and recorded game/harness conditions), a chart of how far each gets, every suite and comparison with links, and the latest runs with their replays.

### What makes it fair

- **The run depends only on (character, ascension, seed).** The game's menu lets profile progress leak into a run: encounters and bosses the profile has not met are forced first, undiscovered acts are preferred, and the card, relic and potion pools grow as things are unlocked. The bridge's `new` command bypasses that by starting the run directly with unlocks pinned to "everything", which is what the game's own Daily mode does. `tools\determinism_check.py` verifies it on one game (five characters, five seeds, up to 43 states each, all identical), and `tools\cross_instance_check.py` across parallel games (five instances, 80 states, identical at every step).
- **Same information within each protocol.** The prompt, tools and context policy are identical within a protocol and their fingerprint is recorded in each run. Different protocols are explicitly separated. Game terms are explained from the game's own tooltips rather than left to what a model happens to remember.
- **Caps are not deaths.** A run stopped by the request cap is counted for the floors it played and then leaves the pool (Kaplan-Meier), so a slow or chatty model is not scored as a dead one.
- Seeds fix what the game offers, not what happens after two models choose differently. That divergence is the thing being measured.

### What the report shows

- **Pass@k per stage**: the chance that at least one of k attempts cleared each act, or won. No model has to win for this to be informative.
- **Chance of still being alive after each floor**, per model, with act boundaries marked.
- **Stage by stage success**: share of runs that cleared each act, and that won.
- **Health left after each floor**: median across surviving runs, with individual runs behind it.
- **Where each run ended**, one mark per run, linking to its replay.
- A scorecard and a full table (cost per run, requests per floor, seconds per request).

## How the agent knows the game

It is never given a rulebook, so everything comes from the game itself:

- **Its own state.** Every state is rendered live from the game's models: HP, block, energy, statuses, enemy intents with damage numbers, and each hand card's text with numbers already adjusted for current modifiers.
- **Terms.** The first time a keyword or status appears on a floor, the state appends the game's own tooltip for it (`Terms: Plating: ...`). This is how it learns mechanics that are new in this game.
- **Lookups.** `info <name>` returns the exact text of any card (with its upgraded version), relic, potion, status, monster, intent (Debuff, DeathBlow, ...), card keyword, enchantment, affliction, event or game term like Block. `info deck|relics|potions|draw|discard|exhaust|enemies|map` cover the current run.
- **Upgrades.** When choosing a card to upgrade, each option shows what it becomes.
- **Card catalog.** `info catalog [filters]` lists every card the character can be offered (about 140 for Ironclad), filterable by type, rarity, `colorless`, or any word.
- **Bosses and enemies.** `info boss` gives the act boss's monsters, HP, moves, pattern and the statuses each move applies, with each status explained from the game's own tooltip; `info enemy [name]` does the same for any monster. The run summary and map name the boss from floor 1.
- **Route planning.** `info paths` gives, for each room that can be entered next, the fewest and most elites, rests, shops, treasures and unknowns on any route to the boss, plus a suggested route.
- **Incoming damage.** Combat states show `Incoming: 14 (9 after Block)`, the attack damage aimed at the player this turn.
- **Strategy.** A system prompt gives general deckbuilder advice and the habits that matter (look up the boss, keep notes). For Ironclad it is followed by a playbook (`agent/playbooks/ironclad.md`): what each of the twelve bosses demands, which card combinations to build around, pathing and rest-site priorities. Its claims were checked against the game's code and the mistakes seen in recorded runs. Disable it with `--no-playbook` or `SPIRE_NO_PLAYBOOK=1`. The playbook is part of the prompt fingerprint.

All of this is the same for every model. A run played with these lookups matches one played without them (`tools\tools_check.py`), so reading does not change the seeded run.

Its memory is reset every floor; only the run summary, its own notes (`set_notes`), and the current screen carry over.

## Why it is cheap to run

- **Text, not pixels.** A full combat state is roughly 150-300 tokens.
- **Batching.** One `act` call can carry a whole turn (`play 0 e0; play 2; end`). Indices refer to the state the model last saw, so they stay valid as cards leave the hand. The batch stops early only when something unplannable happens (new cards drawn, a card-selection prompt, a screen change).
- **The reply is the next state.** Every `act` returns the resulting screen, so there is never a separate "look" call.
- **Detail on demand.** Deck lists, relic text, pile contents and the full map are behind `info`, not in every state.
- **Context resets per floor**, and the stable system prompt and tools are prompt-cached.

Measured on 2026-11-15 with `claude-sonnet-5-5` at `--effort low`: one Ironclad run from floor 1 to a loss at the Act 3 boss on floor 48 took 325 requests and about $0.69, roughly 7 requests and $0.014 per floor. (That was before the Terms block was added, which costs a little more input per floor.)

## Bridge API

Plain text in both directions, except `/snapshot`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Mod and game version, whether a run is in progress |
| `GET /state` | The current screen. `?peek=1` reads it without side effects |
| `POST /act` | Body: commands separated by `;` or newlines. Returns any `!` error lines, then the new state |
| `GET /info?q=<topic>` | `run`, `deck`, `relics`, `potions`, `hand` (mod 0.1.2+), `draw`, `discard`, `exhaust`, `enemies`, `map`, `paths`, `boss`, `enemy [name]`, `catalog [filters]`, or any card/relic/potion/status/monster/keyword name |
| `GET /snapshot` | JSON telemetry for recording; never shown to the model |

Every state ends with a `Commands:` line listing what is valid there.

| Screen | Commands |
|---|---|
| `COMBAT` | `play <hand#> [e#]`, `end`, `potion <#> [e#]`, `drop <#>` |
| `CARD_SELECT` | `select <#> [#...]`, `select none` |
| `MAP`, `EVENT`, `REST`, `SHOP`, `TREASURE`, `REWARDS`, `CARD_REWARD`, `RELIC_SELECT`, `BUNDLE_SELECT`, `POPUP` | `choose <#>`, plus `proceed` / `skip` / `open` where shown |
| `MENU` | `new <character> [ascension] [seed]`, `continue`, `abandon`, `timeline`, `menu_new ...` |
| `GAME_OVER` | `continue` |
| any | `wait`, `info <topic>`, `abandon` (give up the current run), `speed normal\|fast\|instant`, `quit` |

`new` is the standardized start described above. `menu_new` goes through the game's own menus and uses the profile's unlocks.

Example combat state:

```
COMBAT turn 2 | HP 61/80 Block 0 | Energy 3/3
You: Strength 2
Enemies:
e0 Jaw Worm 30/42 Block 6 [Vulnerable 2] -> Attack 11
Hand:
0 Strike (1) Attack: Deal 12 damage. >enemy
1 Defend (1) Skill: Gain 5 Block.
2 Bash+ (2) Attack: Deal 18 damage. Apply 3 Vulnerable. >enemy
Draw 5 | Discard 3 | Exhaust 0
Potions: [0] Fire Potion (1/3 slots)
Terms:
  Vulnerable: Vulnerable creatures take 50% more damage from Attacks.
Commands: play <hand#> [e#] | end | potion <#> [e#] | drop <#>
```

## How the mod drives the game

- **Cards, potions, end turn** go through the same calls the UI makes (`CardModel.TryManualPlay`, `PotionModel.EnqueueManualUse`, `EndPlayerTurnAction`), so costs, targeting rules and hooks all apply.
- **Card-selection prompts** (discard, exhaust, upgrade, transform, remove, choose one of three) are captured through the game's `CardSelectorStack.LocalSelector` hook instead of opening a selection screen, so they all surface as one uniform `CARD_SELECT` prompt.
- **Buttons** (map nodes, rewards, event options, rest site, shop, proceed) are activated with the game's `ForceClick()`, which runs the button's handler directly. No input events are synthesized.
- All game access is marshalled onto the main thread; the HTTP listener only accepts loopback, non-browser requests.

While an agent is attached the mod switches the game to Fast mode (restored on a clean stop) and turns tutorial popups off.

## Limits

- Singleplayer only. Built against game v0.113.1; Early Access patches can rename internals and need a rebuild or fixes.
- OpenRouter has been exercised with six real models. The direct OpenAI and local-provider paths remain tested against mocks only.
- The text protocol has been exercised live with Mistral Nemo, Qwen3 235B and DeepSeek V4 Flash; direct Decisions API support has been exercised with Jev 1.14. These small samples establish integration, not model rankings. DeepSeek `@low@text` frequently exhausted the 1,024-token response cap; `@none@text` returned zero reasoning tokens and no truncated replies in two runs.
- A full run takes roughly 7-20 minutes; `--parallel` runs several at once.
- Only Ironclad has a playbook so far.
- While the bridge is attached to a run, card-selection screens are answered through the bridge and do not appear on screen, so do not mix agent and hand play mid-run.
- The game saves on entering a room. Stopping it mid-room and continuing later replays that room from its start.
- `decompiled/` is never committed: game code is reference-only.
