using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Entities.Potions;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

/// <summary>Potion listing and use; shared by combat and out-of-combat screens.</summary>
internal static class Potions
{
    /// <summary>
    /// The potions currently held, in belt order. Potions are numbered by their
    /// position in this list, not by belt slot: slot numbers have gaps
    /// ("0 Fire, 2 Strength"), and a model reading that asks for potion 1.
    /// </summary>
    public static List<PotionModel> Held(Player me) => me.PotionSlots.OfType<PotionModel>().ToList();

    /// <summary>"[0] Fire Potion, [1] Blood Potion (2/3 slots)", or "" when the belt is empty.</summary>
    public static string Summary(Player me)
    {
        var held = Held(me);
        if (held.Count == 0) return "";
        var parts = held.Select((potion, i) => $"[{i}] {Text.Clean(potion.Title)}");
        return string.Join(", ", parts) + $" ({held.Count}/{me.PotionSlots.Count} slots)";
    }

    public static string Describe(PotionModel potion) =>
        $"{Text.Clean(potion.Title)}: {Text.Safe(() => potion.DynamicDescription.GetFormattedText(), "")}" +
        (potion.Usage == PotionUsage.CombatOnly ? " (combat only)" : "") +
        (potion.TargetType == TargetType.AnyEnemy ? " >enemy" : "");

    private static string? Pick(Player me, string[] args, IReadOnlyList<PotionModel> held, out PotionModel? potion)
    {
        potion = null;
        if (args.Length == 0 || !int.TryParse(args[0], out var index)) return "usage: potion <#> [e#]";
        if (index < 0 || index >= held.Count)
            return held.Count == 0 ? "you have no potions" : $"no potion {index} (you have 0-{held.Count - 1})";
        potion = held[index];
        if (!me.Potions.Contains(potion)) return $"{Text.Clean(potion.Title)} is no longer on your belt";
        return null;
    }

    /// <param name="held">The potion list the agent was shown; indices refer to it.</param>
    public static string? Use(Player me, string[] args, List<Creature>? enemies, IReadOnlyList<PotionModel> held)
    {
        if (Pick(me, args, held, out var potion) is { } err) return err;
        var title = Text.Clean(potion!.Title);
        bool inCombat = CombatManager.Instance.IsInProgress;

        if (!me.CanUseOrRemovePotions || RunManager.Instance.IsPotionUseLocked) return "potions cannot be used right now";
        if (potion.IsQueued) return $"{title} is already being used";
        if (potion.Usage == PotionUsage.Automatic) return $"{title} triggers automatically and cannot be used manually";
        if (potion.Usage == PotionUsage.CombatOnly && !inCombat) return $"{title} can only be used in combat";
        if (!potion.PassesCustomUsabilityCheck) return $"{title} cannot be used right now";
        if (inCombat && !CombatScreen.ReadyForInput(me)) return "not your turn to act";

        Creature? target = null;
        if (potion.TargetType == TargetType.AnyEnemy)
        {
            if (!inCombat || enemies == null) return $"{title} needs an enemy target";
            if (args.Length > 1)
            {
                if (CombatScreen.ResolveEnemy(args[1], enemies, out target) is { } targetErr) return targetErr;
            }
            else
            {
                var alive = enemies.Where(e => e.IsAlive).ToList();
                if (alive.Count != 1) return $"{title} needs a target: potion {args[0]} e<#>";
                target = alive[0];
            }
        }

        if (target != null && !potion.IsValidTarget(target)) return $"{title} cannot target that";
        potion.EnqueueManualUse(target);
        return null;
    }

    public static string? Drop(Player me, string[] args, IReadOnlyList<PotionModel> held)
    {
        if (Pick(me, args, held, out var potion) is { } err) return err.Replace("potion <#> [e#]", "drop <#>");
        if (!me.CanUseOrRemovePotions) return "potions cannot be discarded right now";
        int slot = me.GetPotionSlotIndex(potion!);
        var action = new DiscardPotionGameAction(me, (uint)slot, CombatManager.Instance.IsInProgress);
        RunManager.Instance.ActionQueueSynchronizer.RequestEnqueue(action);
        return null;
    }
}
