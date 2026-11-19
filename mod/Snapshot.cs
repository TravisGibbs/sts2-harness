using System.Text.Json;
using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

/// <summary>
/// Machine-readable run telemetry (JSON) for recording and benchmarking. The
/// agent never sees this; the harness polls it between steps to log progress
/// without spending tokens.
/// </summary>
internal static class Snapshot
{
    private static readonly JsonSerializerOptions Options = new() { WriteIndented = false };

    public static string Json()
    {
        var data = new Dictionary<string, object?>
        {
            ["mod_version"] = ModEntry.Version,
            ["game_version"] = Meta.GameVersion,
            ["headless"] = Meta.IsHeadless,
        };
        if (Render.Enabled)
        {
            // With Movie Maker on, one frame is written per process frame: this places an event in the video.
            data["render_frame"] = Godot.Engine.GetProcessFrames();
            data["fight_replays"] = Text.Safe(FightReplays.Directory);
        }

        Screen? screen = null;
        try { screen = Screens.Detect(); }
        catch (Exception e) { data["error"] = e.Message; }
        data["screen"] = screen?.Kind ?? "UNKNOWN";

        var run = Game.Run;
        var me = Game.Me;
        data["run"] = run != null && me != null;
        if (run == null || me == null) return JsonSerializer.Serialize(data, Options);

        try
        {
            data["seed"] = run.Rng.StringSeed;
            data["character"] = me.Character.Id.Entry;
            data["ascension"] = run.AscensionLevel;
            data["act"] = run.CurrentActIndex + 1;
            data["act_count"] = run.Acts.Count;
            data["act_floor"] = run.ActFloor;
            data["floor"] = run.TotalFloor;
            data["room_type"] = run.CurrentRoom?.RoomType.ToString();
            data["hp"] = me.Creature.CurrentHp;
            data["max_hp"] = me.Creature.MaxHp;
            data["gold"] = me.Gold;
            data["max_energy"] = me.MaxEnergy;
            data["deck_size"] = me.Deck.Cards.Count;
            data["deck"] = me.Deck.Cards.Select(c => Text.Safe(() => c.Title)).OrderBy(t => t).ToList();
            data["relics"] = me.Relics.Select(r => Text.Clean(r.Title)).ToList();
            data["potions"] = Potions.Held(me).Select(p => Text.Clean(p.Title)).ToList();

            var rm = RunManager.Instance;
            data["run_ended"] = rm.HasRunEnded;
            if (rm.HasRunEnded || screen?.Kind == "GAME_OVER")
            {
                bool? win = null;
                try { win = rm.History?.Win; } catch { /* not written yet */ }
                data["victory"] = win ?? (rm.WinTime > 0 ? true : null);
                data["abandoned"] = rm.IsAbandoned;
            }

            var combat = Game.Combat;
            var pcs = me.PlayerCombatState;
            data["in_combat"] = Game.InCombat;
            if (Game.InCombat && combat != null && pcs != null)
            {
                data["combat"] = new Dictionary<string, object?>
                {
                    ["turn"] = pcs.TurnNumber,
                    ["energy"] = pcs.Energy,
                    ["block"] = me.Creature.Block,
                    ["hand"] = pcs.Hand.Cards.Count,
                    ["draw"] = pcs.DrawPile.Cards.Count,
                    ["discard"] = pcs.DiscardPile.Cards.Count,
                    ["exhaust"] = pcs.ExhaustPile.Cards.Count,
                    ["enemies"] = combat.Enemies.Where(e => e.IsAlive).Select(e => new Dictionary<string, object?>
                    {
                        ["name"] = Text.Safe(() => e.Name),
                        ["hp"] = e.CurrentHp,
                        ["max_hp"] = e.MaxHp,
                        ["block"] = e.Block,
                    }).ToList(),
                };
            }
        }
        catch (Exception e)
        {
            data["error"] = e.GetType().Name + ": " + e.Message;
        }

        return JsonSerializer.Serialize(data, Options);
    }
}
