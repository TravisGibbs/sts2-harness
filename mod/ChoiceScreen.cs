using System.Text;
using Godot;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;

namespace SpireBridge;

/// <summary>One numbered option on a screen.</summary>
internal sealed class Choice(string label, Func<Task<string?>> activate)
{
    public string Label { get; } = label;

    /// <summary>Returns null on success or an error message.</summary>
    public Func<Task<string?>> Activate { get; } = activate;

    /// <summary>Why a choice taken earlier in the same batch cannot be taken again.</summary>
    public const string AlreadyTaken = "that option was already taken earlier in this act call (numbers do not shift within one call)";

    public static Choice Click(string label, NClickableControl control, Func<string?>? precheck = null)
    {
        // A Choice belongs to one rendered screen, which one act call reuses for every
        // index, so this remembers whether the same call already clicked it.
        bool taken = false;
        return new(label, () =>
        {
            bool gone = !GodotObject.IsInstanceValid(control) || !control.IsInsideTree();
            if (taken && (gone || !control.IsEnabled)) return Task.FromResult<string?>(AlreadyTaken);
            if (gone) return Task.FromResult<string?>("that option is no longer on screen");
            if (precheck?.Invoke() is { } blocked) return Task.FromResult<string?>(blocked);
            // ForceClick runs the handler even on a disabled button, so check first.
            if (!control.IsEnabled) return Task.FromResult<string?>("that option is not available right now");
            control.ForceClick();
            taken = true;
            return Task.FromResult<string?>(null);
        });
    }
}

/// <summary>
/// Any screen that boils down to "pick one of these numbered options" plus a
/// few named verbs (proceed, skip, ...). Used for everything outside combat.
/// </summary>
internal sealed class ChoiceScreen(string kind) : Screen
{
    private readonly List<string> _header = [];
    private readonly List<Choice> _choices = [];
    private readonly List<(string Verb, string Usage, Func<string[], Task<string?>> Run)> _verbs = [];
    private readonly List<MegaCrit.Sts2.Core.Models.PotionModel> _potions = Game.Me is { } owner ? Potions.Held(owner) : [];
    private readonly List<MegaCrit.Sts2.Core.HoverTips.IHoverTip> _tips = [];

    /// <summary>Tooltips for things offered on this screen; unexplained terms are appended when rendering.</summary>
    public ChoiceScreen Tips(IEnumerable<MegaCrit.Sts2.Core.HoverTips.IHoverTip> tips)
    {
        try { _tips.AddRange(tips); }
        catch { /* tooltips are optional */ }
        return this;
    }

    public override string Kind => kind;

    public override string Signature =>
        $"{kind}|{string.Join('|', _header)}|{string.Join('|', _choices.Select(c => c.Label))}|{string.Join(',', _verbs.Select(v => v.Verb))}";

    public int ChoiceCount => _choices.Count;

    public ChoiceScreen Head(string line)
    {
        if (!string.IsNullOrWhiteSpace(line)) _header.Add(line);
        return this;
    }

    public ChoiceScreen Add(Choice choice)
    {
        _choices.Add(choice);
        return this;
    }

    public ChoiceScreen Verb(string verb, Func<Task<string?>> run, string? usage = null)
    {
        _verbs.Add((verb, usage ?? verb, _ => run()));
        return this;
    }

    public ChoiceScreen Verb(string verb, string usage, Func<string[], Task<string?>> run)
    {
        _verbs.Add((verb, usage, run));
        return this;
    }

    public ChoiceScreen ClickVerb(string verb, NClickableControl? control, bool requireEnabled = true)
    {
        if (control == null || !GodotObject.IsInstanceValid(control)) return this;
        if (control is Control c && !c.IsVisibleInTree()) return this;
        if (requireEnabled && !control.IsEnabled) return this;
        return Verb(verb, () =>
        {
            if (!GodotObject.IsInstanceValid(control)) return Task.FromResult<string?>("that button is gone");
            control.ForceClick();
            return Task.FromResult<string?>(null);
        });
    }

    /// <summary>
    /// A button that leaves the screen (Proceed, Skip). The exit is animated
    /// and, after a boss, loads a whole new act, so wait for the button to go
    /// away or the map to open instead of reporting the unchanged screen.
    /// </summary>
    public ChoiceScreen LeaveVerb(string verb, NClickableControl? control, bool requireEnabled = true)
    {
        if (control == null || !Shown(control)) return this;
        if (requireEnabled && !control.IsEnabled) return this;
        return Verb(verb, async () =>
        {
            if (!GodotObject.IsInstanceValid(control)) return "that button is gone";
            control.ForceClick();
            await MainThread.WaitUntil(
                () => !Shown(control)
                      || MegaCrit.Sts2.Core.Nodes.Screens.Map.NMapScreen.Instance is { IsOpen: true }
                      || MegaCrit.Sts2.Core.Runs.RunManager.Instance.IsChangingRooms,
                20);
            return null;
        });
    }

    public override void Render(StringBuilder sb)
    {
        if (_header.Count == 0) sb.Line(kind);
        else
        {
            sb.Line($"{kind}: {_header[0]}");
            foreach (var line in _header.Skip(1)) sb.Line(line);
        }

        for (int i = 0; i < _choices.Count; i++) sb.Line($"{i} {_choices[i].Label}");

        var commands = new List<string>();
        if (_choices.Count > 0) commands.Add("choose <#>");
        commands.AddRange(_verbs.Select(v => v.Usage));
        // Potions are only worth mentioning outside combat when one can be used
        // here, or when a full belt would block picking up another.
        var me = Game.Me;
        if (me != null && me.Potions.Any())
        {
            bool usable = me.Potions.Any(p => p.Usage == MegaCrit.Sts2.Core.Entities.Potions.PotionUsage.AnyTime);
            bool full = !me.HasOpenPotionSlots && kind is "REWARDS" or "SHOP" or "EVENT_SHOP";
            if (usable || full) sb.Line("Potions: " + Potions.Summary(me));
            if (usable) commands.Add("potion <#>");
            if (full) commands.Add("drop <#>");
        }
        Glossary.Append(sb, _tips);
        sb.Line("Commands: " + (commands.Count > 0 ? string.Join(" | ", commands) : "wait"));
    }

    public override async Task<string?> Exec(string verb, string[] args)
    {
        if (verb == "choose")
        {
            if (ParseIndex(args, 0, _choices.Count, out var index) is { } err) return err;
            return await _choices[index].Activate();
        }

        foreach (var v in _verbs)
        {
            if (v.Verb == verb) return await v.Run(args);
        }

        var me = Game.Me;
        if (me != null)
        {
            if (verb == "potion") return Potions.Use(me, args, null, _potions);
            if (verb == "drop") return Potions.Drop(me, args, _potions);
        }

        var valid = new List<string>();
        if (_choices.Count > 0) valid.Add("choose");
        valid.AddRange(_verbs.Select(v => v.Verb));
        return Unknown(verb, valid.Count > 0 ? string.Join(", ", valid) : "wait");
    }

    // ---- helpers shared by screen builders

    public static List<T> Find<T>(Node? root) where T : Node =>
        root != null && GodotObject.IsInstanceValid(root) ? UiHelper.FindAll<T>(root) : [];

    public static bool Shown(Node? node) =>
        node != null && GodotObject.IsInstanceValid(node) && node.IsInsideTree() && (node is not CanvasItem item || item.IsVisibleInTree());

    /// <summary>Best-effort visible caption of a button: its first text-bearing child.</summary>
    public static string Caption(Node node)
    {
        try
        {
            foreach (var label in UiHelper.FindAll<Label>(node))
                if (!string.IsNullOrWhiteSpace(label.Text)) return Text.Clean(label.Text);
            foreach (var rich in UiHelper.FindAll<RichTextLabel>(node))
                if (!string.IsNullOrWhiteSpace(rich.Text)) return Text.Clean(rich.Text);
        }
        catch { /* fall through to the node name */ }
        return node.Name.ToString();
    }

    /// <summary>
    /// Last resort for a screen the bridge has no dedicated support for: list
    /// every visible, enabled button so the agent can still get past it.
    /// </summary>
    public static ChoiceScreen Generic(string kind, string description, Node root)
    {
        var screen = new ChoiceScreen(kind).Head(description);
        foreach (var control in Find<NClickableControl>(root).Where(c => Shown(c) && c.IsEnabled).Take(24))
        {
            var caption = Caption(control);
            var name = control.Name.ToString();
            screen.Add(Choice.Click(caption == name ? name : $"{caption} ({name})", control));
        }
        return screen;
    }
}

/// <summary>Story dialogue with no decision in it; the bridge clicks through it by itself.</summary>
internal sealed class DialogueScreen(NClickableControl hitbox) : Screen
{
    public override string Kind => "BUSY";
    public override bool IsBusy => true;
    public override void Render(StringBuilder sb) => sb.Line("BUSY (dialogue). Send 'wait'.");
    public override Task<string?> Exec(string verb, string[] args) => Task.FromResult<string?>("dialogue is playing; send 'wait'");

    public void Advance()
    {
        if (GodotObject.IsInstanceValid(hitbox) && hitbox.IsEnabled)
            hitbox.EmitSignal(NClickableControl.SignalName.Released, hitbox);
    }
}
