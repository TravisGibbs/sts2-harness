using System.Text;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Models;

namespace SpireBridge;

/// <summary>The game is blocked on a "pick card(s)" prompt captured by <see cref="BridgeSelector"/>.</summary>
internal sealed class CardSelectScreen(PendingChoice choice) : Screen
{
    public override string Kind => "CARD_SELECT";

    public override string Signature => $"CARD_SELECT:{choice.Options.Count}:{choice.GetHashCode()}";

    public override void Render(StringBuilder sb)
    {
        string count = choice.Min == choice.Max ? $"exactly {choice.Min}" : $"{choice.Min} to {choice.Max}";
        sb.Line($"CARD_SELECT: {choice.Prompt} (pick {count})");
        // Decks hold many identical cards; spell each distinct card out once.
        var firstSeen = new Dictionary<string, int>();
        for (int i = 0; i < choice.Options.Count; i++)
        {
            var card = choice.Options[i];
            var pile = card.Pile?.Type ?? PileType.None;
            var line = Game.CardLine(card, pile);
            if (choice.IsUpgrade && Game.UpgradePreview(card) is { } upgraded) line += " => " + upgraded;
            if (pile is PileType.Draw or PileType.Discard or PileType.Exhaust) line += $" [{pile.ToString().ToLowerInvariant()}]";

            if (firstSeen.TryGetValue(line, out var first))
                sb.Line($"{i} {Text.Safe(() => card.Title)} (same as {first})");
            else
            {
                firstSeen[line] = i;
                sb.Line($"{i} {line}");
            }
        }
        sb.Line(choice.Min == 0 ? "Commands: select <#> [#...] | select none" : "Commands: select <#> [#...]");
    }

    public override Task<string?> Exec(string verb, string[] args)
    {
        // 'choose' is what every other numbered list uses; accept it here too.
        if (verb is not ("select" or "choose")) return Task.FromResult<string?>(Unknown(verb, "select"));
        if (BridgeSelector.Pending != choice) return Task.FromResult<string?>("that selection is no longer open");

        var picked = new List<CardModel>();
        if (!(args.Length == 1 && args[0] is "none" or "skip"))
        {
            for (int i = 0; i < args.Length; i++)
            {
                if (ParseIndex(args, i, choice.Options.Count, out var index, "card") is { } err) return Task.FromResult<string?>(err);
                var card = choice.Options[index];
                if (picked.Contains(card)) return Task.FromResult<string?>($"card {index} listed twice");
                picked.Add(card);
            }
        }

        if (picked.Count < choice.Min || picked.Count > choice.Max)
        {
            string need = choice.Min == choice.Max ? $"exactly {choice.Min}" : $"{choice.Min} to {choice.Max}";
            return Task.FromResult<string?>($"pick {need} card(s); you picked {picked.Count}");
        }

        BridgeSelector.Resolve(picked);
        return Task.FromResult<string?>(null);
    }
}
