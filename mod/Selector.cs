using System.Reflection;
using HarmonyLib;
using MegaCrit.Sts2.Core.CardSelection;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.CardRewardAlternatives;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.TestSupport;

namespace SpireBridge;

/// <summary>A card-selection prompt the game is blocked on until the agent answers.</summary>
internal sealed class PendingChoice
{
    public required IReadOnlyList<CardModel> Options { get; init; }
    public required int Min { get; init; }
    public required int Max { get; init; }
    public required string Prompt { get; init; }

    /// <summary>True when the pick is for an upgrade, so the upgraded text is worth showing.</summary>
    public bool IsUpgrade { get; init; }
    public TaskCompletionSource<IEnumerable<CardModel>> Tcs { get; } = new();
}

/// <summary>
/// The game routes every "pick card(s)" prompt for the local player (discard,
/// exhaust, upgrade, transform, remove, choose-one-of-three, ...) through
/// <see cref="CardSelectorStack.LocalSelector"/> when one is installed, instead
/// of opening a selection screen. Installing ours turns all of those screens
/// into one uniform prompt the agent answers with "select".
/// </summary>
internal sealed class BridgeSelector : ICardSelector
{
    public static readonly BridgeSelector Instance = new();

    public static PendingChoice? Pending { get; private set; }

    // Set by the Harmony prefix just before the game asks us, so the prompt
    // text ("Choose a card to Exhaust") can be shown to the agent.
    private static string? _nextPrompt;
    private static string? _nextKind;
    private static bool? _nextCanSkip;
    private static DateTime _nextStamp;

    public Task<IEnumerable<CardModel>> GetSelectedCards(IEnumerable<CardModel> options, int minSelect, int maxSelect)
    {
        var list = options.ToList();
        bool fresh = (DateTime.UtcNow - _nextStamp).TotalSeconds < 2;
        string prompt = fresh && !string.IsNullOrEmpty(_nextPrompt) ? _nextPrompt! : Describe(fresh ? _nextKind : null);

        int min = Math.Max(0, minSelect);
        // FromChooseACardScreen always asks for (0, 1); its canSkip flag is the real minimum.
        if (fresh && _nextCanSkip == false && min == 0) min = Math.Min(1, list.Count);
        int max = Math.Min(Math.Max(maxSelect, min), list.Count);

        bool isUpgrade = fresh && _nextKind is "FromDeckForUpgrade" or "FromHandForUpgrade";
        _nextPrompt = null;
        _nextKind = null;
        _nextCanSkip = null;

        var choice = new PendingChoice { Options = list, Min = min, Max = max, Prompt = prompt, IsUpgrade = isUpgrade };
        Pending = choice;
        Log.Info($"card selection pending: {prompt} ({min}-{max} of {list.Count})");
        return choice.Tcs.Task;
    }

    // Card rewards consult the per-player selector stack, never the local
    // selector, so this is not called for us. Returning default means "no pick".
    public CardRewardSelection GetSelectedCardReward(IReadOnlyList<CardCreationResult> options, IReadOnlyList<CardRewardAlternative> alternatives) => default;

    public static void Resolve(IEnumerable<CardModel> picked)
    {
        var choice = Pending;
        Pending = null;
        choice?.Tcs.TrySetResult(picked);
    }

    /// <summary>Drop a prompt that can no longer be answered (run ended underneath it).</summary>
    public static void DropIfStale()
    {
        if (Pending != null && !RunManager.Instance.IsInProgress)
        {
            Pending.Tcs.TrySetResult(Array.Empty<CardModel>());
            Pending = null;
        }
    }

    /// <summary>Install on the current run's selector stack if not already there.</summary>
    public static void EnsureInstalled()
    {
        try
        {
            var stack = RunManager.Instance.CardSelectorStack;
            if (stack != null && stack.LocalSelector == null) stack.UseLocalSelector(Instance);
        }
        catch (Exception e)
        {
            Log.Error("could not install card selector: " + e.Message);
        }
    }

    private static string Describe(string? kind) => kind switch
    {
        "FromDeckForUpgrade" or "FromHandForUpgrade" => "Choose a card to Upgrade",
        "FromDeckForTransformation" => "Choose a card to Transform",
        "FromDeckForRemoval" => "Choose a card to Remove",
        "FromDeckForEnchantment" => "Choose a card to Enchant",
        "FromHandForDiscard" => "Choose a card to Discard",
        _ => "Choose a card",
    };

    // ---- Harmony: capture prompt text on the way into CardSelectCmd ----

    public static void Patch(Harmony harmony)
    {
        var prefix = new HarmonyMethod(typeof(BridgeSelector), nameof(CapturePrompt));
        foreach (var method in typeof(CardSelectCmd).GetMethods(BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly))
        {
            if (!method.Name.StartsWith("From", StringComparison.Ordinal)) continue;
            try { harmony.Patch(method, prefix: prefix); }
            catch (Exception e) { Log.Error($"could not patch CardSelectCmd.{method.Name}: {e.Message}"); }
        }
    }

    private static void CapturePrompt(MethodBase __originalMethod, object[] __args)
    {
        try
        {
            bool fresh = (DateTime.UtcNow - _nextStamp).TotalSeconds < 2;
            // Wrappers (FromHandForDiscard -> FromHand) run this twice; keep the outer, more specific name.
            if (!fresh || _nextKind == null) _nextKind = __originalMethod.Name;
            _nextStamp = DateTime.UtcNow;

            var parameters = __originalMethod.GetParameters();
            for (int i = 0; i < parameters.Length && i < __args.Length; i++)
            {
                if (__args[i] is CardSelectorPrefs prefs)
                {
                    var text = Text.Clean(prefs.Prompt);
                    if (text.Length > 0) _nextPrompt = text;
                }
                else if (parameters[i].Name == "canSkip" && __args[i] is bool canSkip)
                {
                    _nextCanSkip = canSkip;
                }
            }
        }
        catch (Exception e)
        {
            Log.Error("prompt capture failed: " + e.Message);
        }
    }
}
