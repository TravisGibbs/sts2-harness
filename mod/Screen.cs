using System.Text;

namespace SpireBridge;

/// <summary>
/// A snapshot of what the player is being asked to do right now. A screen is
/// built once, rendered for the agent, and then commands are executed against
/// that same snapshot, so the indices the agent saw stay valid for a whole
/// batch even while the underlying lists shift.
/// </summary>
internal abstract class Screen
{
    /// <summary>Short uppercase tag, e.g. COMBAT, MAP, REWARDS.</summary>
    public abstract string Kind { get; }

    /// <summary>Cheap fingerprint used to tell when the game has stopped changing.</summary>
    public virtual string Signature => Kind;

    /// <summary>True while the game is mid-transition and nothing can be done yet.</summary>
    public virtual bool IsBusy => false;

    public abstract void Render(StringBuilder sb);

    /// <summary>
    /// Run one command. Returns null on success, or an error message for the
    /// agent. Must not wait for the game to settle; the caller does that.
    /// </summary>
    public abstract Task<string?> Exec(string verb, string[] args);

    /// <summary>
    /// After a command settled and the screen kind is unchanged: may the rest
    /// of the batch still run against this snapshot? A non-null return is the
    /// reason to stop (e.g. new cards were drawn).
    /// </summary>
    public virtual string? StopReason(Screen current) => null;

    protected static string? ParseIndex(string[] args, int position, int count, out int index, string what = "option")
    {
        index = -1;
        if (args.Length <= position) return $"missing {what} index";
        var token = args[position].TrimStart('#');
        if (!int.TryParse(token, out index)) return $"'{args[position]}' is not a number";
        if (index < 0 || index >= count) return count == 0 ? $"there are no {what}s here" : $"{what} {index} out of range (0-{count - 1})";
        return null;
    }

    protected string Unknown(string verb, string valid) => $"'{verb}' is not valid on {Kind}. Valid: {valid}";
}

/// <summary>The game is animating or loading; there is nothing to act on yet.</summary>
internal sealed class BusyScreen(string why) : Screen
{
    public override string Kind => "BUSY";
    public override bool IsBusy => true;
    public override string Signature => "BUSY:" + why;
    public override void Render(StringBuilder sb) => sb.Line($"BUSY ({why}). Send 'wait' to wait for the game.");
    public override Task<string?> Exec(string verb, string[] args) =>
        Task.FromResult<string?>("game is busy (" + why + "); send 'wait'");
}
