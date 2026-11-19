using System.Text;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.HoverTips;

namespace SpireBridge;

/// <summary>
/// Explains game terms the first time they show up on each floor, using the
/// same tooltips a human sees when hovering a card or status. This is how an
/// agent learns what "Plating", "Sly" or "Chains of Binding" mean without
/// relying on prior knowledge of the game, and it puts every model on the same
/// footing. The per-floor scope matches the agent's per-floor context reset.
/// </summary>
internal static class Glossary
{
    private const int MaxTermsPerRender = 14;
    private const int MaxDescriptionLength = 220;

    private static string _scope = "";
    private static readonly HashSet<string> Seen = [];

    /// <summary>
    /// While set, terms are shown but not recorded as explained. Used for
    /// observer reads, so watching a run never changes what the agent is told.
    /// </summary>
    public static bool Peek { get; set; }

    private static string CurrentScope()
    {
        var run = Game.Run;
        if (run == null) return "";
        try { return $"{run.Rng.StringSeed}|{run.CurrentActIndex}|{run.TotalFloor}"; }
        catch { return "run"; }
    }

    /// <summary>Forget what has been explained (new run, or on request).</summary>
    public static void Reset()
    {
        _scope = "";
        Seen.Clear();
    }

    /// <summary>Append a "Terms:" block for any tip not yet explained on this floor.</summary>
    public static void Append(StringBuilder sb, IEnumerable<IHoverTip> tips)
    {
        var scope = CurrentScope();
        if (scope != _scope)
        {
            _scope = scope;
            Seen.Clear();
        }

        var seen = Peek ? new HashSet<string>(Seen) : Seen;
        var lines = new List<string>();
        try
        {
            foreach (var tip in tips)
            {
                if (lines.Count >= MaxTermsPerRender) break;
                string key;
                string line;
                switch (tip)
                {
                    case HoverTip text:
                        var title = Text.Clean(text.Title);
                        var description = Text.Clean(text.Description);
                        if (title.Length == 0 || description.Length == 0) continue;
                        if (description.Length > MaxDescriptionLength) description = description[..MaxDescriptionLength] + "...";
                        key = title;
                        line = $"{title}: {description}";
                        break;
                    case CardHoverTip card:
                        key = "card:" + Text.Safe(() => card.Card.Title);
                        line = "Card " + Game.CardLine(card.Card, PileType.None);
                        break;
                    default:
                        continue;
                }
                if (seen.Add(key)) lines.Add(line);
            }
        }
        catch (Exception e)
        {
            Log.Error("glossary failed: " + e.Message);
        }

        if (lines.Count == 0) return;
        sb.Line("Terms:");
        foreach (var line in lines) sb.Line("  " + line);
    }

    /// <summary>Tooltips of a set of cards, never throwing.</summary>
    public static IEnumerable<IHoverTip> Of(IEnumerable<MegaCrit.Sts2.Core.Models.CardModel> cards)
    {
        foreach (var card in cards)
        {
            IEnumerable<IHoverTip> tips;
            try { tips = card.HoverTips.ToList(); }
            catch { continue; }
            foreach (var tip in tips) yield return tip;
        }
    }

    /// <summary>Tooltips of every visible status on a creature, never throwing.</summary>
    public static IEnumerable<IHoverTip> Of(MegaCrit.Sts2.Core.Entities.Creatures.Creature creature)
    {
        List<IHoverTip> tips = [];
        try
        {
            foreach (var power in creature.Powers.Where(p => p.IsVisible)) tips.AddRange(power.HoverTips);
        }
        catch { /* partial is fine */ }
        return tips;
    }
}
