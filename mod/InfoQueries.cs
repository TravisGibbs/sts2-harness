using System.Text;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.HoverTips;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;

namespace SpireBridge;

/// <summary>Read-only lookups the agent asks for only when it needs the detail.</summary>
internal static class InfoQueries
{
    public const string Topics = "run, deck, relics, potions, hand, draw, discard, exhaust, enemies, map, paths, boss, enemy [name], catalog [filters], or the name of any card, relic, potion, status, monster, intent, keyword, enchantment, affliction, event or game term";

    public static string Answer(string query)
    {
        var me = Game.Me;
        var run = Game.Run;
        if (me == null || run == null) return "No run in progress.";

        try
        {
            switch (query.Trim().ToLowerInvariant())
            {
                case "run": return RunSummary();
                case "deck": return "Deck (" + me.Deck.Cards.Count + "):\n" + CardLines(me.Deck.Cards, PileType.Deck);
                case "relics": return Relics();
                case "potions": return PotionList();
                // Outside combat there are no piles, so the words fall through to a
                // name lookup ("info exhaust" then explains the Exhaust keyword).
                // The hand is built the way the combat state is (same condition as
                // Screens.Detect), so its indices are the ones 'play <hand#>' takes.
                case "hand" when Game.InCombat && Game.Combat is { } combat && me.PlayerCombatState is { } pcs:
                    return new CombatScreen(me, combat, pcs).HandText();
                case "draw" when me.PlayerCombatState != null: return Pile("Draw pile (order hidden)", me.PlayerCombatState.DrawPile.Cards, PileType.Draw);
                case "discard" when me.PlayerCombatState != null: return Pile("Discard pile", me.PlayerCombatState.DiscardPile.Cards, PileType.Discard);
                case "exhaust" when me.PlayerCombatState != null: return Pile("Exhaust pile", me.PlayerCombatState.ExhaustPile.Cards, PileType.Exhaust);
                case "enemies": return Enemies();
                case "map": return MapInfo.Describe(run);
                case "paths": return MapInfo.Paths(run);
                case "boss": return Bestiary.Boss();
            }

            // Topics that take arguments: "catalog attack rare", "enemy jaw worm".
            var parts = query.Trim().Split(' ', 2, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
            var rest = parts.Length > 1 ? parts[1] : "";
            switch (parts.Length > 0 ? parts[0].ToLowerInvariant() : "")
            {
                case "catalog":
                    return Catalog.Describe(rest);
                case "enemy":
                    if (Bestiary.Enemy(rest) is { } described) return described;
                    // Not a monster; it may still be a card, relic, potion or status with that word in its name.
                    var general = Lookup(query);
                    return general.StartsWith(NothingNamed, StringComparison.Ordinal)
                        ? $"No monster matches '{rest}'. In combat 'info enemy' lists the enemies in the fight; 'info boss' describes this act's boss."
                        : general;
                default:
                    return Lookup(query);
            }
        }
        catch (Exception e)
        {
            Log.Error($"info '{query}' threw: {e}");
            return $"! info '{query}' failed: {e.GetType().Name}: {e.Message}";
        }
    }

    /// <summary>One or two lines that re-establish context at the start of a room.</summary>
    public static string RunSummary()
    {
        var me = Game.Me!;
        var run = Game.Run!;
        var sb = new StringBuilder();
        var boss = Bestiary.BossName(run);
        sb.Line($"Act {run.CurrentActIndex + 1} of {run.Acts.Count} Floor {run.TotalFloor} | " + (boss.Length > 0 ? $"Boss: {boss} | " : "") +
                $"{Text.Clean(me.Character.Title)} Ascension {run.AscensionLevel} | " +
                $"HP {me.Creature.CurrentHp}/{me.Creature.MaxHp} | Gold {me.Gold} | Max energy {me.MaxEnergy}");
        sb.Line("Deck (" + me.Deck.Cards.Count + "): " + Game.CardCounts(me.Deck.Cards));
        sb.Line("Relics: " + (me.Relics.Count == 0 ? "none" : string.Join(", ", me.Relics.Select(r => Text.Clean(r.Title)))));
        var potions = Potions.Summary(me);
        sb.Append("Potions: " + (potions.Length > 0 ? potions : $"none (0/{me.PotionSlots.Count} slots)"));
        return sb.ToString();
    }

    /// <summary>
    /// Look up anything in the game by name: cards (with their upgraded text),
    /// relics, potions and statuses. This is the agent's reference manual.
    /// </summary>
    private const string NothingNamed = "Nothing named";

    private const string Searchable =
        "cards, relics, potions, statuses, monsters, intents (Attack, Buff, Debuff, DeathBlow, StatusCard, ...), card keywords (Exhaust, Retain, ...), enchantments, afflictions, events and their options, and terms like Block";

    private static void Guard(string what, Action search)
    {
        try { search(); }
        catch (Exception e) { Log.Error($"lookup of {what} failed: {e.Message}"); }
    }

    /// <summary>Intent names as the combat state prints them, with the game's intent tooltip.</summary>
    private static void Intents(Action<string, Func<string>> consider)
    {
        foreach (var type in Enum.GetValues<IntentType>())
        {
            var tip = Bestiary.IntentTip(type);
            string Line()
            {
                var name = type == IntentType.StatusCard ? "StatusCard / AddStatusCards" : type.ToString();
                var title = tip is { Title.Length: > 0 } t && !t.Title.Equals(type.ToString(), StringComparison.OrdinalIgnoreCase) ? $" ({t.Title})" : "";
                var text = tip?.Description ?? (type == IntentType.Hidden ? "what this enemy will do is not shown." : "no description.");
                if (type == IntentType.StatusCard) text += " The number after AddStatusCards is how many cards.";
                if (type == IntentType.DeathBlow) text += " The number after DeathBlow is the damage.";
                return $"Intent {name}{title}: {text}";
            }
            consider(type.ToString(), Line);
            if (type == IntentType.StatusCard) consider("AddStatusCards", Line);
            if (tip is { Title.Length: > 0 } named) consider(named.Title, Line);
        }
    }

    /// <summary>Card keywords, with the tooltip a card shows for them (HoverTipFactory.FromKeyword).</summary>
    private static void Keywords(Action<string, Func<string>> consider)
    {
        foreach (var keyword in Enum.GetValues<CardKeyword>())
        {
            if (keyword == CardKeyword.None) continue;
            if (HoverTipFactory.FromKeyword(keyword) is not HoverTip tip) continue;
            var title = Text.Clean(tip.Title);
            consider(title.Length > 0 ? title : keyword.ToString(), () => $"Keyword {title}: {Bestiary.Cap(Text.Clean(tip.Description))}");
        }
    }

    /// <summary>
    /// Game terms with a tooltip of their own: the StaticHoverTip set (HoverTipFactory.Static),
    /// plus any other entry of that table that needs no variables (Gold, Hit Points...).
    /// </summary>
    private static void StaticTips(Action<string, Func<string>> consider)
    {
        foreach (var kind in Enum.GetValues<StaticHoverTip>())
        {
            // The dynamic variants need numbers; their static twins say the same.
            if (kind is StaticHoverTip.None or StaticHoverTip.SummonDynamic or StaticHoverTip.ReplayDynamic) continue;
            HoverTip tip;
            try
            {
                if (HoverTipFactory.Static(kind) is not HoverTip made) continue;
                tip = made;
            }
            catch
            {
                continue;
            }
            var title = Text.Clean(tip.Title);
            if (title.Length == 0) continue;
            consider(title, () => $"Term {title}: {Bestiary.Cap(Text.Clean(tip.Description))}");
        }

        var table = LocManager.Instance.GetTable("static_hover_tips");
        foreach (var key in table.Keys.ToList())
        {
            if (!key.EndsWith(".title", StringComparison.Ordinal)) continue;
            var descriptionKey = key[..^".title".Length] + ".description";
            if (!table.HasEntry(descriptionKey)) continue;
            string rawTitle, rawDescription;
            try
            {
                rawTitle = table.GetRawText(key);
                rawDescription = table.GetRawText(descriptionKey);
            }
            catch
            {
                continue;
            }
            if (rawTitle.Contains('{') || rawDescription.Contains('{')) continue;
            var title = Text.Clean(rawTitle);
            if (title.Length == 0) continue;
            consider(title, () => $"Term {title}: {Bestiary.Cap(Text.Clean(rawDescription))}");
        }
    }

    private static bool IsTestModel(AbstractModel model) =>
        model.Id.Entry.StartsWith("MOCK", StringComparison.OrdinalIgnoreCase) || model.Id.Entry.StartsWith("DEPRECATED", StringComparison.OrdinalIgnoreCase);

    /// <summary>Enchantments (lasting card modifiers), described at amount 1 from a throwaway copy.</summary>
    private static void Enchantments(Action<string, Func<string>> consider)
    {
        foreach (var enchantment in ModelDb.DebugEnchantments)
        {
            if (enchantment == null || IsTestModel(enchantment)) continue;
            var title = Text.Clean(enchantment.Title);
            consider(title, () =>
            {
                var copy = enchantment.ToMutable();
                copy.Amount = 1;
                copy.RecalculateValues();
                return $"Enchantment {title} (on a card): {Bestiary.Cap(Text.Clean(copy.DynamicDescription))}";
            });
        }
    }

    /// <summary>Afflictions (card debuffs enemies apply), described at amount 1 from a throwaway copy.</summary>
    private static void Afflictions(Action<string, Func<string>> consider)
    {
        foreach (var affliction in ModelDb.DebugAfflictions)
        {
            if (affliction == null || IsTestModel(affliction)) continue;
            var title = Text.Clean(affliction.Title);
            consider(title, () =>
            {
                var copy = affliction.ToMutable();
                copy.Amount = 1;
                return $"Affliction {title} (on a card): {Bestiary.Cap(Text.Clean(copy.DynamicDescription))}";
            });
        }
    }

    /// <summary>Events by name, and the options on their first page by option name ("Overcome").</summary>
    private static void Events(Action<string, Func<string>> consider)
    {
        var events = ModelDb.AllEvents.Concat(ModelDb.AllAncients).Where(e => e != null).Distinct().ToList();
        foreach (var ev in events)
        {
            string title;
            List<(string Title, string Description)> options;
            try
            {
                title = Text.Clean(ev.Title);
                options = EventOptions(ev);
            }
            catch
            {
                continue;
            }
            if (title.Length == 0 || title.StartsWith('<')) continue;
            consider(title, () => Bestiary.Cap(
                $"Event {title}: " + (options.Count == 0 ? "no fixed options." : string.Join("; ", options.Select(o => $"{o.Title} ({o.Description})"))), 400));
            foreach (var option in options)
                consider(option.Title, () => Bestiary.Cap($"Event option {option.Title} (event {title}): {option.Description}", 300));
        }
    }

    /// <summary>
    /// The first-page options of an event, read from its localization table. Raw
    /// text with variables filled from the event's own values where it has them,
    /// so nothing is formatted against a half-built event.
    /// </summary>
    private static List<(string Title, string Description)> EventOptions(EventModel ev)
    {
        var result = new List<(string, string)>();
        var table = LocManager.Instance.GetTable(ev.LocTable);
        var prefix = ev.Id.Entry + ".pages.INITIAL.options.";
        foreach (var key in table.Keys.Where(k => k.StartsWith(prefix, StringComparison.Ordinal) && k.EndsWith(".title", StringComparison.Ordinal)).ToList())
        {
            try
            {
                var option = key[..^".title".Length];
                if (option.EndsWith("_LOCKED", StringComparison.Ordinal)) continue;
                var name = Text.Clean(Fill(table.GetRawText(key), ev));
                var description = table.HasEntry(option + ".description") ? Text.Clean(Fill(table.GetRawText(option + ".description"), ev)) : "";
                if (name.Length > 0) result.Add((name, description));
            }
            catch
            {
                // Skip an option that cannot be read.
            }
        }
        return result;
    }

    /// <summary>"{HpLoss}" -> "3" from the event's values, else "Hp loss"; "{Cards:plural:card|cards}" -> "cards".</summary>
    private static string Fill(string raw, EventModel ev) =>
        System.Text.RegularExpressions.Regex.Replace(raw, @"\{(\w+)(?::([^{}]*))?\}", m =>
        {
            var name = m.Groups[1].Value;
            var format = m.Groups[2].Value;
            string? value = null;
            try
            {
                if (ev.DynamicVars.TryGetValue(name, out var variable))
                {
                    if (variable is StringVar text) value = string.IsNullOrEmpty(text.StringValue) ? null : text.StringValue;
                    else if (variable.BaseValue != 0m) value = variable.BaseValue.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture);
                }
            }
            catch
            {
                // Leave it unfilled.
            }
            if (format.StartsWith("plural:", StringComparison.Ordinal))
            {
                var forms = format["plural:".Length..].Split('|');
                return value == "1" ? forms[0] : forms[^1];
            }
            if (format.Length > 0 && !format.StartsWith("diff", StringComparison.Ordinal)) return value ?? "";
            return value ?? Bestiary.Words(name);
        });

    private static string Lookup(string query)
    {
        var q = query.Trim();
        foreach (var prefix in new[] { "card ", "relic ", "potion ", "power ", "status ", "monster ", "intent ", "keyword ", "enchantment ", "affliction ", "event " })
        {
            if (q.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) q = q[prefix.Length..].Trim();
        }
        q = q.TrimEnd('+');
        if (q.Length < 2) return $"Unknown info topic '{query}'. Topics: {Topics}";

        var found = new List<(bool Exact, string Line)>();
        void Consider(string name, Func<string> line)
        {
            bool exact = name.Equals(q, StringComparison.OrdinalIgnoreCase);
            if (!exact && !name.Contains(q, StringComparison.OrdinalIgnoreCase)) return;
            try { found.Add((exact, line())); }
            catch { /* skip entries that cannot be described outside a run */ }
        }

        foreach (var card in ModelDb.AllCards)
        {
            var title = Text.Safe(() => card.Title, "");
            Consider(title, () =>
            {
                var line = "Card " + Game.CardLine(card, PileType.None) + $" [{card.Rarity}]";
                return Game.UpgradePreview(card) is { } up ? line + " => upgraded: " + up : line;
            });
        }
        foreach (var relic in ModelDb.AllRelics)
            Consider(Text.Clean(relic.Title), () => $"Relic {Text.Clean(relic.Title)}: {Text.Clean(relic.DynamicDescription)}");
        foreach (var potion in ModelDb.AllPotions)
            Consider(Text.Clean(potion.Title), () => "Potion " + Potions.Describe(potion));
        foreach (var power in ModelDb.AllPowers)
        {
            // The game ships test-only powers ("Mock ...") with no tooltip; never offer those.
            var typeName = power.GetType().Name;
            if (typeName.StartsWith("Mock", StringComparison.Ordinal) || typeName.StartsWith("Test", StringComparison.Ordinal)) continue;
            Consider(Text.Clean(power.Title), () =>
            {
                var description = Text.Clean(power.DumbHoverTip.Description);
                if (description.Length == 0) throw new InvalidOperationException("no tooltip");
                return $"Status {Text.Clean(power.Title)}: {description}";
            });
        }

        // Each further kind is searched on its own so one failing table cannot hide the rest.
        Guard("intents", () => Intents(Consider));
        Guard("keywords", () => Keywords(Consider));
        Guard("terms", () => StaticTips(Consider));
        Guard("enchantments", () => Enchantments(Consider));
        Guard("afflictions", () => Afflictions(Consider));
        Guard("events", () => Events(Consider));

        // Monsters: an exact name gets the full move listing, like 'info enemy <name>'.
        var monsterHint = new List<string>();
        Guard("monsters", () =>
        {
            foreach (var title in Bestiary.MonsterTitles())
            {
                if (title.Equals(q, StringComparison.OrdinalIgnoreCase))
                {
                    if (Bestiary.Enemy(title) is { } listing) found.Add((true, listing));
                }
                else if (title.Contains(q, StringComparison.OrdinalIgnoreCase))
                {
                    monsterHint.Add(title);
                    found.Add((false, $"Monster {title}: 'info enemy {title}' lists its moves"));
                }
            }
        });
        // A single partial match is as good as an exact one.
        if (found.Count == 1 && monsterHint.Count == 1 && Bestiary.Enemy(monsterHint[0]) is { } only)
            found[0] = (true, only);

        if (found.Count == 0) return $"{NothingNamed} '{q}' was found. Names that can be looked up: {Searchable}. Topics: {Topics}";
        // Exact names first; cap partial matches so a vague query stays short.
        var lines = found.OrderByDescending(f => f.Exact).Select(f => f.Line).Distinct().Take(found.Any(f => f.Exact) ? 4 : 8);
        return string.Join("\n", lines);
    }

    private static string CardLines(IEnumerable<CardModel> cards, PileType pile)
    {
        var lines = cards
            .Select(c => Game.CardLine(c, pile))
            .GroupBy(l => l)
            .OrderBy(g => g.Key)
            .Select(g => g.Count() > 1 ? $"{g.Count()}x {g.Key}" : g.Key);
        var text = string.Join("\n", lines);
        return text.Length > 0 ? text : "(empty)";
    }

    private static string Pile(string label, IReadOnlyList<CardModel>? cards, PileType pile)
    {
        if (cards == null) return "Not in combat.";
        // Grouped and sorted, so the true draw order is never revealed.
        return $"{label} ({cards.Count}):\n" + CardLines(cards, pile);
    }

    private static string Relics()
    {
        var me = Game.Me!;
        if (me.Relics.Count == 0) return "No relics.";
        var sb = new StringBuilder();
        foreach (var relic in me.Relics)
        {
            sb.Append(Text.Clean(relic.Title));
            if (relic.ShowCounter) sb.Append($" [{relic.DisplayAmount}]");
            sb.Append(": ").Append(Text.Safe(() => relic.DynamicDescription.GetFormattedText(), "")).Append('\n');
        }
        return sb.ToString().TrimEnd();
    }

    private static string PotionList()
    {
        var me = Game.Me!;
        var held = Potions.Held(me);
        if (held.Count == 0) return $"No potions (0/{me.PotionSlots.Count} slots).";
        var sb = new StringBuilder();
        for (int i = 0; i < held.Count; i++) sb.Line($"{i} {Potions.Describe(held[i])}");
        return sb.ToString().TrimEnd();
    }

    private static string Enemies()
    {
        var combat = Game.Combat;
        var me = Game.Me!;
        if (combat == null || !Game.InCombat) return "Not in combat.";
        var sb = new StringBuilder();
        AppendPowers(sb, "You", me.Creature);
        int i = 0;
        foreach (var enemy in combat.Enemies.Where(e => e.IsAlive))
        {
            AppendPowers(sb, $"e{i} {Text.Safe(() => enemy.Name)}", enemy);
            i++;
        }
        return sb.ToString().TrimEnd();
    }

    private static void AppendPowers(StringBuilder sb, string label, Creature creature)
    {
        var powers = creature.Powers.Where(p => p.IsVisible).ToList();
        if (powers.Count == 0)
        {
            sb.Line($"{label}: no statuses");
            return;
        }
        sb.Line($"{label}:");
        foreach (var power in powers)
        {
            string description = "";
            try
            {
                description = power.HoverTips.OfType<HoverTip>().Select(t => Text.Clean(t.Description)).FirstOrDefault() ?? "";
            }
            catch { /* description is optional */ }
            sb.Line($"  {Text.Clean(power.Title)} {power.DisplayAmount}: {description}");
        }
    }
}
