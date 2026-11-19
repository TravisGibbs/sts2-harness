using System.Text;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;
using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

internal sealed class CombatScreen : Screen
{
    private readonly Player _me;
    private readonly CombatState _combat;
    private readonly PlayerCombatState _pcs;

    // Snapshots: the indices the agent sees refer to these lists.
    private readonly List<CardModel> _hand;
    private readonly List<Creature> _enemies;
    private readonly List<PotionModel> _potions;
    private readonly int _turn;

    public CombatScreen(Player me, CombatState combat, PlayerCombatState pcs)
    {
        _me = me;
        _combat = combat;
        _pcs = pcs;
        _hand = pcs.Hand.Cards.ToList();
        _enemies = combat.Enemies.Where(e => e.IsAlive).ToList();
        _potions = Potions.Held(me);
        _turn = pcs.TurnNumber;
    }

    public override string Kind => "COMBAT";

    public override bool IsBusy => !ReadyForInput(_me);

    public override string Signature =>
        $"COMBAT:{_turn}:{_pcs.Energy}:{_hand.Count}:{_pcs.DrawPile.Cards.Count}:{_pcs.DiscardPile.Cards.Count}:" +
        $"{_me.Creature.CurrentHp}:{_me.Creature.Block}:{string.Join(',', _enemies.Select(e => e.CurrentHp + "/" + e.Block))}";

    /// <summary>True when the player may play cards right now.</summary>
    public static bool ReadyForInput(Player me)
    {
        var cm = CombatManager.Instance;
        var pcs = me.PlayerCombatState;
        if (!cm.IsInProgress || pcs == null) return false;
        if (cm.IsOverOrEnding) return false;
        if (pcs.Phase != PlayerTurnPhase.Play) return false;
        if (cm.PlayerActionsDisabled) return false;
        if (cm.IsPlayerReadyToEndTurn(me)) return false;
        if (RunManager.Instance.ActionExecutor is { IsRunning: true }) return false;
        return true;
    }

    public override string? StopReason(Screen current)
    {
        if (current is not CombatScreen now) return null;
        if (now._turn != _turn) return "a new turn started";
        if (now._hand.Any(c => !_hand.Contains(c))) return "new cards entered your hand";
        return null;
    }

    // ---------------------------------------------------------------- render

    public override void Render(StringBuilder sb)
    {
        var run = _me.RunState;
        var self = _me.Creature;
        sb.Append($"COMBAT turn {_turn} | HP {self.CurrentHp}/{self.MaxHp} Block {self.Block} | Energy {_pcs.Energy}/{_pcs.MaxEnergy}");
        if (_pcs.Stars > 0) sb.Append($" | Stars {_pcs.Stars}");
        sb.Append('\n');

        var powers = PowerList(self);
        if (powers.Length > 0) sb.Line("You: " + powers);

        var orbs = _pcs.OrbQueue.Orbs;
        if (_pcs.OrbQueue.Capacity > 0)
            sb.Line($"Orbs {orbs.Count}/{_pcs.OrbQueue.Capacity}: " + (orbs.Count == 0 ? "-" : string.Join(", ", orbs.Select(o => Text.Clean(o.Title)))));

        foreach (var pet in _pcs.Pets.Where(p => p.IsAlive))
        {
            var petPowers = PowerList(pet);
            sb.Line($"Ally {Text.Safe(() => pet.Name)} {pet.CurrentHp}/{pet.MaxHp} Block {pet.Block}" + (petPowers.Length > 0 ? " [" + petPowers + "]" : ""));
        }

        sb.Line("Enemies:");
        for (int i = 0; i < _enemies.Count; i++)
        {
            var e = _enemies[i];
            sb.Append($"e{i} {Text.Safe(() => e.Name)} {e.CurrentHp}/{e.MaxHp}");
            if (e.Block > 0) sb.Append($" Block {e.Block}");
            var ep = PowerList(e);
            if (ep.Length > 0) sb.Append($" [{ep}]");
            sb.Append(" -> ").Append(IntentText(e)).Append('\n');
        }

        if (IncomingText() is { } incoming) sb.Line(incoming);

        sb.Line("Hand:");
        AppendHand(sb);

        sb.Line($"Draw {_pcs.DrawPile.Cards.Count} | Discard {_pcs.DiscardPile.Cards.Count} | Exhaust {_pcs.ExhaustPile.Cards.Count}");
        var potions = Potions.Summary(_me);
        if (potions.Length > 0) sb.Line("Potions: " + potions);

        // Explain any card keyword or status not yet explained on this floor.
        Glossary.Append(sb, Glossary.Of(_hand)
            .Concat(Glossary.Of(self))
            .Concat(_pcs.Pets.SelectMany(p => Glossary.Of(p)))
            .Concat(_enemies.SelectMany(e => Glossary.Of(e))));
        sb.Line(potions.Length > 0
            ? "Commands: play <hand#> [e#] | end | potion <#> [e#] | drop <#>"
            : "Commands: play <hand#> [e#] | end");
    }

    /// <summary>
    /// One line per hand card, "index card-line [>enemy] [unplayable: why]": the
    /// indices 'play &lt;hand#&gt;' takes. Shared by the combat state and 'info hand'.
    /// </summary>
    public void AppendHand(StringBuilder sb)
    {
        // With exactly one enemy, show numbers against it (Vulnerable, etc. included).
        var hittable = _combat.HittableEnemies.ToList();
        var soleEnemy = hittable.Count == 1 ? hittable[0] : null;

        for (int i = 0; i < _hand.Count; i++)
        {
            var card = _hand[i];
            var previewTarget = card.TargetType == TargetType.AnyEnemy ? soleEnemy : null;
            sb.Append(i).Append(' ').Append(Game.CardLine(card, PileType.Hand, previewTarget));
            if (card.TargetType == TargetType.AnyEnemy) sb.Append(" >enemy");
            if (!card.CanPlay(out var reason, out var preventer)) sb.Append(" [unplayable: ").Append(ReasonText(reason, preventer)).Append(']');
            sb.Append('\n');
        }
    }

    /// <summary>The answer to 'info hand': the hand exactly as the combat state lists it.</summary>
    public string HandText()
    {
        if (_hand.Count == 0) return "Hand (0):\n(empty)";
        var sb = new StringBuilder();
        sb.Append($"Hand ({_hand.Count}):\n");
        AppendHand(sb);
        return sb.ToString().TrimEnd();
    }

    private static string PowerList(Creature c)
    {
        try
        {
            return string.Join(", ", c.Powers.Where(p => p.IsVisible).Select(p =>
            {
                var title = Text.Clean(p.Title);
                return p.DisplayAmount != 0 ? $"{title} {p.DisplayAmount}" : title;
            }));
        }
        catch { return ""; }
    }

    /// <summary>
    /// "Incoming: 14 (9 after Block)": the attack damage every enemy intends this
    /// turn, added up, and what is left after the player's current Block. Null
    /// when nobody intends to attack. The numbers are the ones the intents show
    /// (AttackIntent.GetTotalDamage), which already run the game's damage hooks
    /// for both sides: the enemy's Strength and Weak, and the player's Vulnerable,
    /// Intangible and the like. So nothing is applied on top here except Block.
    /// </summary>
    private string? IncomingText()
    {
        try
        {
            var targets = _combat.PlayerCreatures;
            int total = 0;
            bool any = false;
            foreach (var enemy in _enemies)
            {
                var monster = enemy.Monster;
                if (monster == null || !enemy.IsAlive) continue;
                foreach (var intent in monster.NextMove.Intents)
                {
                    if (intent is not AttackIntent attack) continue;
                    any = true;
                    total += attack.GetTotalDamage(targets, enemy);
                }
            }
            if (!any) return null;
            return $"Incoming: {total} ({Math.Max(0, total - _me.Creature.Block)} after Block)";
        }
        catch
        {
            return null;
        }
    }

    private string IntentText(Creature enemy)
    {
        try
        {
            var monster = enemy.Monster;
            if (monster == null) return "?";
            var targets = _combat.PlayerCreatures;
            var parts = monster.NextMove.Intents.Select(intent => intent switch
            {
                AttackIntent a when a.Repeats > 1 => $"Attack {a.GetSingleDamage(targets, enemy)}x{a.Repeats}",
                AttackIntent a => (a.IntentType == IntentType.DeathBlow ? "DeathBlow " : "Attack ") + a.GetSingleDamage(targets, enemy),
                StatusIntent s => $"AddStatusCards {s.CardCount}",
                _ => intent.IntentType.ToString(),
            }).ToList();
            return parts.Count > 0 ? string.Join(" + ", parts) : "None";
        }
        catch { return "?"; }
    }

    private static string ReasonText(UnplayableReason reason, AbstractModel? preventer)
    {
        if (reason.HasFlag(UnplayableReason.EnergyCostTooHigh)) return "not enough energy";
        if (reason.HasFlag(UnplayableReason.StarCostTooHigh)) return "not enough stars";
        if (reason.HasFlag(UnplayableReason.HasUnplayableKeyword)) return "Unplayable";
        if (reason.HasFlag(UnplayableReason.NoLivingAllies)) return "no allies";
        // Something in play forbids it (a power, relic or another card): name it.
        string by = preventer switch
        {
            PowerModel power => Text.Clean(power.Title),
            RelicModel relic => Text.Clean(relic.Title),
            CardModel card => Text.Safe(() => card.Title, ""),
            null => "",
            _ => preventer.GetType().Name,
        };
        return by.Length > 0 ? $"blocked by {by}" : "blocked by an effect";
    }

    // ---------------------------------------------------------------- commands

    public override async Task<string?> Exec(string verb, string[] args)
    {
        switch (verb)
        {
            case "play": return Play(args);
            case "end": return EndTurn();
            case "potion": return Potions.Use(_me, args, _enemies, _potions);
            case "drop": return Potions.Drop(_me, args, _potions);
            default:
                await Task.CompletedTask;
                return Unknown(verb, "play, end, potion, drop");
        }
    }

    private string? Play(string[] args)
    {
        if (!ReadyForInput(_me)) return "not your turn to act";
        if (ParseIndex(args, 0, _hand.Count, out var index, "hand card") is { } err) return err;
        var card = _hand[index];
        var title = Text.Safe(() => card.Title);
        if (!_pcs.Hand.Cards.Contains(card)) return $"{title} is no longer in your hand";
        if (!card.CanPlay(out var reason, out var preventer)) return $"cannot play {title}: {ReasonText(reason, preventer)}";

        Creature? target = null;
        if (card.TargetType == TargetType.AnyEnemy)
        {
            if (args.Length > 1)
            {
                if (ResolveEnemy(args[1], _enemies, out target) is { } targetErr) return targetErr;
            }
            else
            {
                var hittable = _combat.HittableEnemies.ToList();
                if (hittable.Count != 1) return $"{title} needs a target: play {index} e<#>";
                target = hittable[0];
            }
        }
        else if (card.TargetType == TargetType.AnyAlly)
        {
            target = _combat.PlayerCreatures.FirstOrDefault(c => c.IsAlive && c != _me.Creature);
        }

        return card.TryManualPlay(target) ? null : $"the game rejected playing {title}" + (target != null ? " on that target" : "");
    }

    private string? EndTurn()
    {
        if (!ReadyForInput(_me)) return "not your turn to act";
        RunManager.Instance.ActionQueueSynchronizer.RequestEnqueue(new EndPlayerTurnAction(_me, _pcs.TurnNumber));
        return null;
    }

    public static string? ResolveEnemy(string token, List<Creature> enemies, out Creature? target)
    {
        target = null;
        var t = token.TrimStart('e', 'E');
        if (!int.TryParse(t, out var i)) return $"'{token}' is not an enemy (use e0, e1, ...)";
        if (i < 0 || i >= enemies.Count) return $"enemy e{i} does not exist";
        if (!enemies[i].IsAlive) return $"enemy e{i} is already dead";
        target = enemies[i];
        return null;
    }
}
