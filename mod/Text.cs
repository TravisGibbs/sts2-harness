using System.Text;
using System.Text.RegularExpressions;
using MegaCrit.Sts2.Core.Localization;

namespace SpireBridge;

/// <summary>Turns the game's rich text (BBCode, icon images) into terse plain text.</summary>
internal static partial class Text
{
    // The game draws energy as repeated icons ("gain [icon][icon]") for 1-4, and "5[icon]" above that.
    [GeneratedRegex(@"\[img[^\]]*\][^\[]*?energy_icon[^\[]*?\[/img\]", RegexOptions.IgnoreCase)]
    private static partial Regex EnergyIcon();

    [GeneratedRegex(@"\[img[^\]]*\][^\[]*?star_icon[^\[]*?\[/img\]", RegexOptions.IgnoreCase)]
    private static partial Regex StarIcon();

    [GeneratedRegex(@"\[img[^\]]*\][^\[]*?gold_icon[^\[]*?\[/img\]", RegexOptions.IgnoreCase)]
    private static partial Regex GoldIcon();

    [GeneratedRegex(@"\[img[^\]]*\][^\[]*?\[/img\]", RegexOptions.IgnoreCase)]
    private static partial Regex AnyImage();

    [GeneratedRegex(@"(\d+)?((?:\x01)+)")]
    private static partial Regex EnergyRun();

    [GeneratedRegex(@"(\d+)?((?:\x02)+)")]
    private static partial Regex StarRun();

    // BBCode tags such as [gold], [/gold], [color=#fff], [fly_in offset_x=1 offset_y=40].
    [GeneratedRegex(@"\[/?[a-zA-Z_][a-zA-Z0-9_]*(?:[= ][^\]\[]*)?\]")]
    private static partial Regex Tag();

    [GeneratedRegex(@"\s+")]
    private static partial Regex Whitespace();

    public static string Clean(string? raw)
    {
        if (string.IsNullOrEmpty(raw)) return "";
        var s = EnergyIcon().Replace(raw, "\x01");
        s = StarIcon().Replace(s, "\x02");
        s = GoldIcon().Replace(s, " Gold");
        s = AnyImage().Replace(s, "");
        s = Tag().Replace(s, "");
        s = EnergyRun().Replace(s, m => Counted(m, "Energy"));
        s = StarRun().Replace(s, m => Counted(m, "Star"));
        s = Whitespace().Replace(s, " ");
        return s.Trim();
    }

    private static string Counted(Match m, string unit)
    {
        // "3<icon>" means three; a bare run of icons means as many as there are icons.
        string count = m.Groups[1].Success ? m.Groups[1].Value : m.Groups[2].Length.ToString();
        return $" {count} {unit} ";
    }

    public static string Clean(LocString? loc)
    {
        if (loc == null) return "";
        try { return Clean(loc.GetFormattedText()); }
        catch (Exception e) { return $"<text error: {e.GetType().Name}>"; }
    }

    /// <summary>Evaluate a text getter, never letting a formatting bug break a whole render.</summary>
    public static string Safe(Func<string?> getter, string fallback = "?")
    {
        try { return Clean(getter()) is { Length: > 0 } s ? s : fallback; }
        catch { return fallback; }
    }

    public static StringBuilder Line(this StringBuilder sb, string text) => sb.Append(text).Append('\n');
}
