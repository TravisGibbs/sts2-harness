using Godot;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Screens.CharacterSelect;
using MegaCrit.Sts2.Core.Nodes.Screens.MainMenu;
using MegaCrit.Sts2.Core.Nodes.Screens.Timeline;
using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

/// <summary>Everything outside a run: popups, the main menu, character select.</summary>
internal static class MenuScreens
{
    /// <summary>A modal popup blocks whatever is underneath it, in or out of a run.</summary>
    public static Screen? DetectModal()
    {
        if (NModalContainer.Instance?.OpenModal is not Node modal || !GodotObject.IsInstanceValid(modal)) return null;

        var texts = ChoiceScreen.Find<Label>(modal).Select(l => Text.Clean(l.Text))
            .Concat(ChoiceScreen.Find<RichTextLabel>(modal).Select(l => Text.Clean(l.Text)))
            .Where(t => t.Length > 0)
            .Distinct()
            .Take(3);
        var screen = new ChoiceScreen("POPUP").Head($"{modal.GetType().Name}: {string.Join(" / ", texts)}");

        var popup = modal as NVerticalPopup ?? modal.GetNodeOrNull<NVerticalPopup>("VerticalPopup");
        if (popup != null)
        {
            if (ChoiceScreen.Shown(popup.YesButton)) screen.Add(Choice.Click(ChoiceScreen.Caption(popup.YesButton), popup.YesButton));
            if (ChoiceScreen.Shown(popup.NoButton)) screen.Add(Choice.Click(ChoiceScreen.Caption(popup.NoButton), popup.NoButton));
            if (screen.ChoiceCount > 0) return screen;
        }
        return ChoiceScreen.Generic("POPUP", $"{modal.GetType().Name}: {string.Join(" / ", texts)}", modal);
    }

    public static Screen Detect()
    {
        var game = NGame.Instance;
        if (game == null) return new BusyScreen("game is starting");
        var menu = game.MainMenu;
        if (menu == null) return new BusyScreen(game.LogoAnimation != null ? "intro logo" : "loading");

        var stack = menu.SubmenuStack;
        Node listRoot = menu.GetNodeOrNull<Node>("MainMenuTextButtons") ?? menu;
        string where = "main menu";
        if (stack.SubmenusOpen && stack.Peek() is { } top)
        {
            listRoot = top;
            where = top.GetType().Name;
        }

        bool hasSave = ChoiceScreen.Shown(Button(menu, "ContinueButton"));
        var screen = ChoiceScreen.Generic("MENU", where + (hasSave ? " (a saved run exists)" : ""), listRoot);

        if (stack.SubmenusOpen && stack.Peek() is NTimelineScreen timeline)
            screen.Verb("timeline", () => ClearTimeline(timeline), "timeline (reveal new entries and return to the menu)");

        if (hasSave)
        {
            screen.Verb("continue", () => Continue(menu));
            screen.Verb("abandon", () => Abandon(menu));
        }
        else
        {
            var names = string.Join("|", ModelDb.AllCharacters.Select(c => c.Id.Entry.ToLowerInvariant()));
            screen.Verb("new", $"new <{names}> [ascension] [seed]", StandardRun);
            screen.Verb("menu_new", "menu_new <character> [ascension] [seed] (through the game's own menus, using this profile's unlocks)", NewRun);
        }
        return screen;
    }

    private static NClickableControl? Button(NMainMenu menu, string name) =>
        menu.GetNodeOrNull<NClickableControl>("MainMenuTextButtons/" + name);

    private static async Task<string?> Continue(NMainMenu menu)
    {
        var button = Button(menu, "ContinueButton");
        if (!ChoiceScreen.Shown(button) || !button!.IsEnabled) return "there is no saved run to continue";
        button.ForceClick();
        bool started = await MainThread.WaitUntil(() => RunManager.Instance.IsInProgress || NModalContainer.Instance?.OpenModal != null, 60);
        return started ? null : "the saved run did not load";
    }

    private static async Task<string?> Abandon(NMainMenu menu)
    {
        var button = Button(menu, "AbandonRunButton");
        if (!ChoiceScreen.Shown(button) || !button!.IsEnabled) return "there is no saved run to abandon";
        button.ForceClick();
        if (!await MainThread.WaitUntil(() => NModalContainer.Instance?.OpenModal != null, 5)) return "the confirmation popup did not appear";
        var yes = (NModalContainer.Instance!.OpenModal as Node)?.GetNodeOrNull<NClickableControl>("VerticalPopup/YesButton");
        if (yes == null) return "could not find the confirm button";
        yes.ForceClick();
        await MainThread.WaitUntil(() => NModalContainer.Instance?.OpenModal == null, 5);
        return null;
    }

    /// <summary>
    /// Click through everything the Timeline wants acknowledged (its first-visit
    /// tutorial, each newly obtained entry, and the unlock screens they queue),
    /// then leave. With no window, nobody else can do this.
    /// </summary>
    private static async Task<string?> ClearTimeline(NTimelineScreen timeline)
    {
        var deadline = DateTime.UtcNow.AddSeconds(240);
        while (DateTime.UtcNow < deadline)
        {
            if (!GodotObject.IsInstanceValid(timeline) || !timeline.IsVisibleInTree()) return null;

            NClickableControl? target;
            bool leaving = false;
            if (ChoiceScreen.Find<NTimelineTutorial>(timeline).FirstOrDefault(ChoiceScreen.Shown) is { } tutorial)
                target = tutorial.GetNodeOrNull<NClickableControl>("%AcknowledgeButton");
            else if (timeline.CurrentUnlockScreen is { } unlock && GodotObject.IsInstanceValid(unlock))
                target = unlock.GetNodeOrNull<NClickableControl>("ConfirmButton");
            else if (timeline.GetNodeOrNull<Control>("%EpochInspectScreen") is { Visible: true } inspect)
                target = inspect.GetNodeOrNull<NClickableControl>("%CloseButton");
            else if (ChoiceScreen.Find<NEpochSlot>(timeline).FirstOrDefault(s => ChoiceScreen.Shown(s) && s.State == EpochSlotState.Obtained) is { } slot)
                target = slot;
            else
            {
                target = timeline.GetNodeOrNull<NClickableControl>("BackButton");
                leaving = true;
            }

            if (target != null && ChoiceScreen.Shown(target) && target.IsEnabled)
            {
                target.ForceClick();
                if (leaving)
                {
                    await MainThread.WaitUntil(() => !GodotObject.IsInstanceValid(timeline) || !timeline.IsVisibleInTree(), 10);
                    return null;
                }
                // Let the animation this starts get going before looking again.
                for (int i = 0; i < 30; i++) await MainThread.NextFrame();
            }
            else
            {
                await MainThread.NextFrame();
            }
        }
        return "timed out clicking through the Timeline";
    }

    /// <summary>
    /// Start a run that depends only on (character, ascension, seed), never on
    /// this profile's progress. The menu path lets progress leak in: unseen
    /// encounters and bosses are forced first, undiscovered acts are preferred,
    /// and the card, relic and potion pools grow as the player unlocks things.
    /// Pinning the unlock state to "everything" (what the game's Daily mode does)
    /// removes all of that, so the same seed is the same run for every model.
    /// </summary>
    private static async Task<string?> StandardRun(string[] args)
    {
        if (args.Length == 0) return "usage: new <character> [ascension] [seed]";
        int ascension = 0;
        if (args.Length > 1 && (!int.TryParse(args[1], out ascension) || ascension < 0 || ascension > 20)) return "ascension must be a number from 0 to 20";

        var game = NGame.Instance;
        if (game?.MainMenu == null) return "not at the main menu";
        if (MegaCrit.Sts2.Core.Saves.SaveManager.Instance.HasRunSave) return "a saved run exists; 'continue' it or 'abandon' it first";

        var character = ModelDb.AllCharacters.FirstOrDefault(c => c.Id.Entry.Equals(args[0], StringComparison.OrdinalIgnoreCase));
        if (character == null)
            return $"unknown character '{args[0]}'. Available: {string.Join(", ", ModelDb.AllCharacters.Select(c => c.Id.Entry.ToLowerInvariant()))}";

        string seed = args.Length > 2
            ? MegaCrit.Sts2.Core.Helpers.SeedHelper.CanonicalizeSeed(args[2])
            : MegaCrit.Sts2.Core.Helpers.SeedHelper.GetRandomSeed();

        var unlocks = MegaCrit.Sts2.Core.Unlocks.UnlockState.all;
        FightReplays.Clear();

        // Same draw the game makes when embarking, minus its "prefer an act this profile has not discovered" rule.
        var rng = new MegaCrit.Sts2.Core.Random.Rng(MegaCrit.Sts2.Core.Helpers.StringHelper.GetDeterministicHashCode(seed), "act_selection");
        var acts = new List<ActModel>();
        foreach (var options in ModelDb.ActsByIndex)
        {
            var open = options.Where(a => a.IsUnlocked(unlocks)).ToList();
            acts.Add(rng.NextItem(open) ?? options[0]);
        }

        Glossary.Reset();
        var player = MegaCrit.Sts2.Core.Entities.Players.Player.CreateForNewRun(character, unlocks, 1uL);
        game.ReactionContainer.InitializeNetworking(new MegaCrit.Sts2.Core.Multiplayer.NetSingleplayerGameService());
        await game.StartNewSingleplayerRun(player, shouldSave: true, acts, Array.Empty<ModifierModel>(), seed, GameMode.Standard, isCustomGame: false, ascension);
        bool started = await MainThread.WaitUntil(() => RunManager.Instance.IsInProgress && Game.Run?.CurrentRoom != null, 60);
        return started ? null : "the run did not start";
    }

    private static async Task<string?> NewRun(string[] args)
    {
        if (args.Length == 0) return "usage: new <character> [ascension] [seed]";
        int ascension = 0;
        if (args.Length > 1 && !int.TryParse(args[1], out ascension)) return "ascension must be a number";
        string? seed = args.Length > 2 ? args[2] : null;

        var game = NGame.Instance;
        var menu = game?.MainMenu;
        if (game == null || menu == null) return "not at the main menu";
        if (ChoiceScreen.Shown(Button(menu, "ContinueButton"))) return "a saved run exists; 'continue' it or 'abandon' it first";

        var stack = menu.SubmenuStack;

        // After a run the game may land on the Timeline, and it locks Singleplayer
        // until every newly discovered entry has been revealed there.
        if (stack.SubmenusOpen && stack.Peek() is NTimelineScreen open)
        {
            if (await ClearTimeline(open) is { } timelineError) return timelineError;
        }
        if (!stack.SubmenusOpen)
        {
            var singleplayer = Button(menu, "SingleplayerButton");
            if (!ChoiceScreen.Shown(singleplayer)) return "the Singleplayer button is not available";
            if (!singleplayer!.IsEnabled)
            {
                var timelineButton = Button(menu, "TimelineButton");
                if (!ChoiceScreen.Shown(timelineButton) || !timelineButton!.IsEnabled) return "Singleplayer is locked and the Timeline is not reachable";
                timelineButton.ForceClick();
                if (!await MainThread.WaitUntil(() => stack.SubmenusOpen && stack.Peek() is NTimelineScreen, 10)) return "the Timeline did not open";
                if (await ClearTimeline((NTimelineScreen)stack.Peek()!) is { } timelineError) return timelineError;
                if (!await MainThread.WaitUntil(() => !stack.SubmenusOpen && singleplayer.IsEnabled, 10))
                    return "Singleplayer is still locked after viewing the Timeline";
            }
            singleplayer.ForceClick();
            await MainThread.WaitUntil(() => stack.SubmenusOpen, 5);
        }

        if (stack.Peek() is NSingleplayerSubmenu submenu)
        {
            var standard = submenu.GetNodeOrNull<NClickableControl>("%StandardButton");
            if (standard == null || !standard.IsEnabled) return "the Standard run button is not available";
            standard.ForceClick();
            await MainThread.WaitUntil(() => stack.Peek() is NCharacterSelectScreen, 5);
        }

        if (stack.Peek() is not NCharacterSelectScreen select) return "could not reach the character select screen";
        await MainThread.WaitUntil(() => ChoiceScreen.Find<NCharacterSelectButton>(select).Count > 0, 5);

        var buttons = ChoiceScreen.Find<NCharacterSelectButton>(select).Where(b => !b.IsRandom && b.Character != null).ToList();
        string want = args[0].ToLowerInvariant();
        var match = buttons.FirstOrDefault(b => b.Character.Id.Entry.Equals(want, StringComparison.OrdinalIgnoreCase))
                    ?? buttons.FirstOrDefault(b => Text.Clean(b.Character.Title).ToLowerInvariant().Contains(want));
        if (match == null)
            return $"unknown character '{args[0]}'. Available: {string.Join(", ", buttons.Where(b => !b.IsLocked).Select(b => b.Character.Id.Entry.ToLowerInvariant()))}";
        if (match.IsLocked) return $"{Text.Clean(match.Character.Title)} is not unlocked yet";

        match.Select();
        for (int i = 0; i < 10; i++) await MainThread.NextFrame();

        int max = select.Lobby?.MaxAscension ?? 0;
        if (ascension > max) return $"ascension {ascension} is not unlocked for this character (max {max})";
        select.GetNodeOrNull<NAscensionPanel>("%AscensionPanel")?.SetAscensionLevel(ascension);

        var embark = select.GetNodeOrNull<NClickableControl>("ConfirmButton");
        if (embark == null) return "could not find the Embark button";
        if (!await MainThread.WaitUntil(() => embark.IsEnabled, 15)) return "the Embark button never became available";

        // The screen resets the seed override when it initializes, so set it only now.
        if (seed != null) game.DebugSeedOverride = seed;
        embark.ForceClick();
        bool started = await MainThread.WaitUntil(() => RunManager.Instance.IsInProgress && Game.Run?.CurrentRoom != null, 60);
        if (seed != null) game.DebugSeedOverride = null;
        return started ? null : "the run did not start";
    }
}
