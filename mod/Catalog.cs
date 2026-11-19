using System.Text;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

/// <summary>
/// The card compendium for this run: every card the character can be offered,
/// so an agent can plan a deck around cards it has not seen yet instead of
/// judging each reward in isolation.
/// </summary>
internal static class Catalog
{
    /// <summary>At or below this many results, each card also shows its upgraded text.</summary>
    private const int UpgradeDetailLimit = 12;

    private static readonly Dictionary<string, CardType> Types = new()
    {
        ["attack"] = CardType.Attack, ["attacks"] = CardType.Attack,
        ["skill"] = CardType.Skill, ["skills"] = CardType.Skill,
        ["power"] = CardType.Power, ["powers"] = CardType.Power,
        ["status"] = CardType.Status, ["statuses"] = CardType.Status,
        ["curse"] = CardType.Curse, ["curses"] = CardType.Curse,
        ["quest"] = CardType.Quest, ["quests"] = CardType.Quest,
    };

    private static readonly Dictionary<string, CardRarity> Rarities = new()
    {
        ["common"] = CardRarity.Common, ["commons"] = CardRarity.Common,
        ["uncommon"] = CardRarity.Uncommon, ["uncommons"] = CardRarity.Uncommon,
        ["rare"] = CardRarity.Rare, ["rares"] = CardRarity.Rare,
        ["basic"] = CardRarity.Basic, ["basics"] = CardRarity.Basic, ["starter"] = CardRarity.Basic,
        ["ancient"] = CardRarity.Ancient, ["ancients"] = CardRarity.Ancient,
        ["event"] = CardRarity.Event, ["events"] = CardRarity.Event,
        ["token"] = CardRarity.Token, ["tokens"] = CardRarity.Token,
    };

    /// <summary>
    /// Filter words that pull in a shared pool which is otherwise left out
    /// (cards a run can contain but that are never offered as a pick).
    /// Value: the pool's CardPoolModel.Title.
    /// </summary>
    private static readonly Dictionary<string, string> ExtraPools = new()
    {
        ["status"] = "status", ["statuses"] = "status",
        ["curse"] = "curse", ["curses"] = "curse",
        ["token"] = "token", ["tokens"] = "token",
        ["event"] = "event", ["events"] = "event", ["ancient"] = "event", ["ancients"] = "event",
        ["quest"] = "quest", ["quests"] = "quest",
    };

    /// <param name="Pool">"own" for the character's pool, otherwise the shared pool's title ("colorless", "status", ...).</param>
    private sealed record Entry(CardModel Card, string Pool, string Line, string Search);

    public static string Describe(string filterText)
    {
        var me = Game.Me;
        var run = Game.Run;
        if (me == null || run == null) return "No run in progress.";

        var words = filterText
            .Split([' ', ',', '\t'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(w => w.ToLowerInvariant())
            .Distinct()
            .ToList();

        // ---- which cards are in scope
        var character = Text.Clean(me.Character.Title);
        var cards = new List<(CardModel Card, string Pool)>();
        var seen = new HashSet<ModelId>();
        void AddPool(CardPoolModel? pool, string name)
        {
            if (pool == null) return;
            IEnumerable<CardModel> all;
            try
            {
                // The same call card rewards, the merchant and card-generating effects use.
                all = pool.GetUnlockedCards(me.UnlockState, ((IRunState)run).CardMultiplayerConstraint).ToList();
            }
            catch (Exception e)
            {
                Log.Error($"catalog: unlocked cards of {pool.Id.Entry} failed ({e.Message}); listing the whole pool");
                try { all = pool.AllCards.ToList(); }
                catch { return; }
            }
            foreach (var card in all)
            {
                try
                {
                    if (card == null || !card.IsAllowedInRun(run) || !seen.Add(card.Id)) continue;
                    cards.Add((card, name));
                }
                catch
                {
                    // Skip a card that cannot be inspected.
                }
            }
        }

        try { AddPool(me.Character.CardPool, "own"); }
        catch (Exception e) { Log.Error("catalog: character pool failed: " + e.Message); }

        var shared = new List<CardPoolModel>();
        try { shared = ModelDb.AllSharedCardPools.Where(p => p is not null && p.GetType().Name != "DeprecatedCardPool").ToList(); }
        catch (Exception e) { Log.Error("catalog: shared pools failed: " + e.Message); }

        AddPool(shared.FirstOrDefault(p => PoolTitle(p) == "colorless"), "colorless");
        foreach (var title in words.Where(ExtraPools.ContainsKey).Select(w => ExtraPools[w]).Distinct())
            AddPool(shared.FirstOrDefault(p => PoolTitle(p) == title), title);

        // ---- describe each once, then filter
        var entries = new List<Entry>();
        foreach (var (card, pool) in cards)
        {
            try
            {
                var line = Game.CardLine(card, PileType.None);
                entries.Add(new Entry(card, pool, line, line.ToLowerInvariant()));
            }
            catch
            {
                // One card that cannot be rendered must not blank the listing.
            }
        }

        bool unfiltered = words.Count == 0;
        IEnumerable<Entry> filtered = entries;
        foreach (var word in words)
        {
            var w = word;
            bool isType = Types.TryGetValue(w, out var type);
            bool isRarity = Rarities.TryGetValue(w, out var rarity);
            if (w == "colorless") filtered = filtered.Where(e => e.Pool == "colorless");
            else if (isType || isRarity)
                filtered = filtered.Where(e => (isType && (e.Card.Type == type || e.Card.Rarity.ToString() == type.ToString())) || (isRarity && e.Card.Rarity == rarity));
            else filtered = filtered.Where(e => e.Search.Contains(w));
        }

        // Cards that only ever arrive as a side effect stay out unless a filter word asked for them.
        bool Wanted(CardRarity r) => words.Any(w => Rarities.TryGetValue(w, out var asked) && asked == r)
                                     || words.Any(w => Types.TryGetValue(w, out var askedType) && askedType.ToString() == r.ToString());
        filtered = filtered.Where(e => e.Card.Rarity is not (CardRarity.Status or CardRarity.Curse or CardRarity.Token or CardRarity.Event or CardRarity.Quest or CardRarity.None)
                                       || Wanted(e.Card.Rarity));

        var result = filtered.ToList();
        var sb = new StringBuilder();
        if (unfiltered)
        {
            int colorless = result.Count(e => e.Pool == "colorless");
            sb.Line($"Catalog: {result.Count} cards {character} can be offered ({result.Count - colorless} {character}, {colorless} colorless). " +
                    "Narrow with 'info catalog <words>': attack|skill|power, common|uncommon|rare|basic, colorless, or any word from a card's name or text; all must match.");
        }
        else
        {
            var filter = string.Join(' ', words);
            if (result.Count == 0)
                return $"Catalog [{filter}]: no cards match. Filters: attack|skill|power, common|uncommon|rare|basic, colorless, or any word from a card's name or text; all must match. " +
                       "Status, curse and token cards are searched only when one of those words is a filter.";
            sb.Line($"Catalog [{filter}]: {result.Count} card{(result.Count == 1 ? "" : "s")}" +
                    (result.Count <= UpgradeDetailLimit ? ", with upgrades." : "."));
        }

        bool detail = !unfiltered && result.Count <= UpgradeDetailLimit;
        foreach (var group in result.GroupBy(e => GroupName(e)).OrderBy(g => GroupOrder(g.First())))
        {
            sb.Line($"{group.Key} ({group.Count()}):");
            foreach (var entry in group.OrderBy(e => (int)e.Card.Type).ThenBy(e => e.Line, StringComparer.OrdinalIgnoreCase))
            {
                var line = entry.Line;
                if (detail && Game.UpgradePreview(entry.Card) is { } upgraded) line += " => upgraded: " + upgraded;
                sb.Line(line);
            }
        }
        return sb.ToString().TrimEnd();
    }

    private static string PoolTitle(CardPoolModel pool)
    {
        try { return pool.Title.ToLowerInvariant(); }
        catch { return ""; }
    }

    private static string GroupName(Entry e)
    {
        var rarity = e.Card.Rarity switch
        {
            CardRarity.Basic => "Basic (starting deck)",
            CardRarity.Ancient => "Ancient (only from Ancients)",
            var r => r.ToString(),
        };
        return e.Pool == "colorless" ? "Colorless " + rarity : rarity;
    }

    private static int GroupOrder(Entry e)
    {
        int rarity = e.Card.Rarity switch
        {
            CardRarity.Common => 0,
            CardRarity.Uncommon => 1,
            CardRarity.Rare => 2,
            CardRarity.Basic => 3,
            CardRarity.Ancient => 4,
            _ => 5 + (int)e.Card.Rarity,
        };
        return (e.Pool == "own" ? 0 : 100) + rarity;
    }
}
