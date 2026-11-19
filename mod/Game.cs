using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

/// <summary>Null-safe accessors for the live run. Main thread only.</summary>
internal static class Game
{
    public static RunState? Run
    {
        get
        {
            try { return RunManager.Instance.DebugOnlyGetState(); }
            catch { return null; }
        }
    }

    public static Player? Me => Run is { } run ? LocalContext.GetMe(run) : null;

    public static CombatState? Combat
    {
        get
        {
            try { return CombatManager.Instance.DebugOnlyGetState(); }
            catch { return null; }
        }
    }

    public static bool InCombat => CombatManager.Instance.IsInProgress;

    /// <summary>"3", "X", or "-" for cards with no cost (statuses, curses).</summary>
    public static string CostText(CardModel card)
    {
        string energy;
        if (card.EnergyCost.CostsX) energy = "X";
        else
        {
            int cost = card.EnergyCost.GetWithModifiers(CostModifiers.All);
            energy = cost < 0 ? "-" : cost.ToString();
        }

        int stars;
        try { stars = card.HasStarCostX ? -2 : card.GetStarCostWithModifiers(); }
        catch { stars = -1; }
        if (stars == -2) return energy + "+X*";
        return stars > 0 ? $"{energy}+{stars}*" : energy;
    }

    /// <summary>One line describing a card: "Bash+ (2) Attack: Deal 10 damage. Apply 3 Vulnerable."</summary>
    public static string CardLine(CardModel card, PileType pile, MegaCrit.Sts2.Core.Entities.Creatures.Creature? target = null)
    {
        var title = Text.Safe(() => card.Title);
        if (pile == PileType.Hand)
        {
            // Same refresh the card UI does before drawing its text: recompute
            // damage/block numbers for current powers (and for the target, if any).
            try { card.UpdateDynamicVarPreview(CardPreviewMode.Normal, target, card.DynamicVars); }
            catch { /* fall back to whatever values the card already holds */ }
        }
        var desc = Text.Safe(() => card.GetDescriptionForPile(pile, target), "");
        var line = $"{title} ({CostText(card)}) {card.Type}";
        return desc.Length > 0 ? $"{line}: {desc}" : line;
    }

    /// <summary>
    /// What a card would read after one more upgrade, e.g. "(2) Deal 10 damage.
    /// Apply 3 Vulnerable." Built from a throwaway copy, so the real card is untouched.
    /// </summary>
    public static string? UpgradePreview(CardModel card)
    {
        try
        {
            if (!card.IsUpgradable) return null;
            var copy = ModelDb.GetById<CardModel>(card.Id).ToMutable();
            for (int i = 0; i <= card.CurrentUpgradeLevel; i++)
            {
                copy.UpgradeInternal();
                copy.FinalizeUpgradeInternal();
            }
            var description = Text.Safe(() => copy.GetDescriptionForPile(PileType.None), "");
            return $"({CostText(copy)}) {description}";
        }
        catch
        {
            return null;
        }
    }

    /// <summary>Compact "3x Strike, 2x Defend+, Bash" summary, order-independent.</summary>
    public static string CardCounts(IEnumerable<CardModel> cards)
    {
        var groups = cards
            .GroupBy(c => Text.Safe(() => c.Title))
            .OrderByDescending(g => g.Count())
            .ThenBy(g => g.Key)
            .Select(g => g.Count() > 1 ? $"{g.Count()}x {g.Key}" : g.Key);
        var joined = string.Join(", ", groups);
        return joined.Length > 0 ? joined : "(empty)";
    }
}
