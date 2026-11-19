using Godot;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.Cards;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events;
using MegaCrit.Sts2.Core.Nodes.Events.Custom;
using MegaCrit.Sts2.Core.Nodes.Events.Custom.CrystalSphere;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Relics;
using MegaCrit.Sts2.Core.Nodes.RestSite;
using MegaCrit.Sts2.Core.Nodes.Rewards;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.Capstones;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using MegaCrit.Sts2.Core.Nodes.Screens.TreasureRoomRelic;
using MegaCrit.Sts2.Core.Nodes.Vfx;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

/// <summary>Builds the screen for everything that can happen during a run outside of card play.</summary>
internal static class RunScreens
{
    private static Task<string?> Ok => Task.FromResult<string?>(null);
    private static Task<string?> Fail(string message) => Task.FromResult<string?>(message);

    private static string RelicText(RelicModel relic) =>
        $"{Text.Clean(relic.Title)}: {Text.Safe(() => relic.DynamicDescription.GetFormattedText(), "")}";

    // ------------------------------------------------------------ overlays

    /// <summary>A capstone (deck view, pause menu, settings) can only have been opened by a human.</summary>
    public static Screen? DetectCapstone()
    {
        if (NCapstoneContainer.Instance is not { InUse: true } capstone) return null;
        return new ChoiceScreen("PAUSED")
            .Head("A menu or card view is open over the game.")
            .Verb("resume", () => { capstone.Close(); return Ok; });
    }

    public static Screen? DetectMap(RunState run) =>
        NMapScreen.Instance is { IsOpen: true } map ? Map(run, map) : null;

    public static Screen? DetectOverlay(Player me, RunState run)
    {
        var top = NOverlayStack.Instance?.Peek();
        if (top is not Node node || !GodotObject.IsInstanceValid(node)) return null;

        return top switch
        {
            NRewardsScreen rewards => Rewards(me, rewards),
            NCardRewardSelectionScreen cardReward => CardReward(cardReward),
            NChooseARelicSelection relics => RelicChoice(relics),
            NChooseABundleSelectionScreen bundles => BundleChoice(bundles),
            NCrystalSphereScreen sphere => CrystalSphere(sphere),
            NGameOverScreen gameOver => GameOver(run, gameOver),
            NByrdEvolutionVfx => new BusyScreen("animation"),
            _ => ChoiceScreen.Generic("SCREEN", $"{node.GetType().Name} (no dedicated support; listing its buttons)", node),
        };
    }

    private static string RewardLabel(Reward reward)
    {
        string label = reward switch
        {
            GoldReward gold => $"Gold {gold.Amount}",
            PotionReward { Potion: { } potion } => "Potion: " + Potions.Describe(potion),
            RelicReward { Relic: { } relic } => "Relic: " + RelicText(relic),
            CardReward cards => $"Card reward (pick 1 of {cards.Cards.Count()})",
            _ => Text.Clean(reward.Description),
        };
        if (reward.ParentRewardSet != null) label += " [linked: taking one forfeits the others in its set]";
        return label;
    }

    private static Screen Rewards(Player me, NRewardsScreen screen)
    {
        var result = new ChoiceScreen("REWARDS").Head("take what you want, then proceed");
        foreach (var button in ChoiceScreen.Find<NRewardButton>(screen).Where(b => ChoiceScreen.Shown(b) && b.IsEnabled && b.Reward != null))
        {
            var reward = button.Reward!;
            result.Add(Choice.Click(RewardLabel(reward), button, () =>
                reward is PotionReward && !me.HasOpenPotionSlots ? "your potion belt is full; 'drop <#>' a potion first" : null));
        }
        var proceed = ChoiceScreen.Find<NProceedButton>(screen).FirstOrDefault(ChoiceScreen.Shown);
        result.LeaveVerb("proceed", proceed, requireEnabled: false);
        return result;
    }

    private static Screen CardReward(NCardRewardSelectionScreen screen)
    {
        var result = new ChoiceScreen("CARD_REWARD").Head("add one card to your deck, or skip");
        result.Tips(Glossary.Of(ChoiceScreen.Find<NCardHolder>(screen).Where(h => h.CardModel != null).Select(h => h.CardModel!)));
        foreach (var holder in ChoiceScreen.Find<NCardHolder>(screen).Where(h => ChoiceScreen.Shown(h) && h.CardModel != null))
        {
            var card = holder.CardModel!;
            result.Add(new Choice(Game.CardLine(card, PileType.None), () =>
            {
                if (!GodotObject.IsInstanceValid(holder)) return Fail("that card is no longer offered");
                holder.EmitSignal(NCardHolder.SignalName.Pressed, holder);
                return Ok;
            }));
        }

        // Alternatives are Skip, Reroll, and relic-granted options; expose each by its caption.
        foreach (var alt in ChoiceScreen.Find<NCardRewardAlternativeButton>(screen).Where(ChoiceScreen.Shown))
        {
            var caption = ChoiceScreen.Caption(alt);
            var verb = caption.ToLowerInvariant().Split(' ', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault() ?? "alt";
            result.ClickVerb(verb, alt, requireEnabled: false);
        }
        return result;
    }

    private static Screen RelicChoice(NChooseARelicSelection screen)
    {
        var result = new ChoiceScreen("RELIC_SELECT").Head("choose a relic");
        foreach (var holder in ChoiceScreen.Find<NRelicBasicHolder>(screen).Where(ChoiceScreen.Shown))
            result.Add(Choice.Click(RelicText(holder.Relic.Model), holder));
        result.ClickVerb("skip", screen.GetNodeOrNull<NClickableControl>("SkipButton"), requireEnabled: false);
        return result;
    }

    private static Screen BundleChoice(NChooseABundleSelectionScreen screen)
    {
        var result = new ChoiceScreen("BUNDLE_SELECT").Head("choose one bundle of cards");
        foreach (var bundle in ChoiceScreen.Find<NCardBundle>(screen).Where(ChoiceScreen.Shown))
        {
            var label = string.Join(" + ", bundle.Bundle.Select(c => Game.CardLine(c, PileType.None)));
            result.Add(new Choice(label, async () =>
            {
                if (!GodotObject.IsInstanceValid(bundle)) return "that bundle is gone";
                bundle.Hitbox.ForceClick();
                // Selecting opens a preview; the pick only counts once it is confirmed.
                NConfirmButton? confirm = null;
                bool ready = await MainThread.WaitUntil(() =>
                {
                    confirm = ChoiceScreen.Find<NConfirmButton>(screen).FirstOrDefault(b => ChoiceScreen.Shown(b) && b.IsEnabled);
                    return confirm != null;
                }, 5);
                if (!ready || confirm == null) return "the confirm button never became available";
                confirm.ForceClick();
                return null;
            }));
        }
        result.ClickVerb("skip", screen.GetNodeOrNull<NClickableControl>("%SkipButton"));
        return result;
    }

    private static Screen CrystalSphere(NCrystalSphereScreen screen)
    {
        var cells = ChoiceScreen.Find<NCrystalSphereCell>(screen).Where(c => ChoiceScreen.Shown(c) && c.Entity != null).ToList();
        var hidden = cells.Where(c => c.Entity.IsHidden).ToList();
        string left = screen.GetNodeOrNull<Node>("%DivinationsLeft") is { } label ? ChoiceScreen.Caption(label) : "?";

        var result = new ChoiceScreen("CRYSTAL_SPHERE")
            .Head($"fog-covered grid hiding rewards (and one curse). Divinations left: {left}.")
            .Head("Each reveal uses one divination. Tool 'big' clears a 3x3 area, 'small' clears one cell.")
            .Head("Hidden cells (x,y): " + (hidden.Count > 0 ? string.Join(" ", hidden.Select(c => $"{c.Entity.X},{c.Entity.Y}")) : "none"));

        result.Verb("reveal", "reveal <x> <y>", args =>
        {
            if (args.Length < 2 || !int.TryParse(args[0], out var x) || !int.TryParse(args[1], out var y)) return Fail("usage: reveal <x> <y>");
            var cell = hidden.FirstOrDefault(c => c.Entity.X == x && c.Entity.Y == y);
            if (cell == null || !GodotObject.IsInstanceValid(cell)) return Fail($"no hidden cell at {x},{y}");
            cell.EmitSignal(NClickableControl.SignalName.Released, cell);
            return Ok;
        });
        result.Verb("tool", "tool big|small", args =>
        {
            string? path = args.FirstOrDefault()?.ToLowerInvariant() switch
            {
                "big" => "%BigDivinationButton",
                "small" => "%SmallDivinationButton",
                _ => null,
            };
            if (path == null) return Fail("usage: tool big|small");
            if (screen.GetNodeOrNull<NClickableControl>(path) is not { } button) return Fail("that tool is not available");
            button.ForceClick();
            return Ok;
        });
        result.LeaveVerb("proceed", screen.GetNodeOrNull<NClickableControl>("%ProceedButton"));
        return result;
    }

    private static Screen GameOver(RunState run, NGameOverScreen screen)
    {
        bool? win = null;
        try { win = RunManager.Instance.History?.Win; } catch { /* history not written yet */ }
        string outcome = win == true ? "VICTORY" : win == false ? "DEFEAT" : "run ended";

        var result = new ChoiceScreen("GAME_OVER").Head($"{outcome} on floor {run.TotalFloor} (act {run.CurrentActIndex + 1}).");
        var next = screen.GetNodeOrNull<NClickableControl>("%ContinueButton");
        var menu = screen.GetNodeOrNull<NClickableControl>("%MainMenuButton");
        if (ChoiceScreen.Shown(menu) && menu!.IsEnabled) result.ClickVerb("continue", menu);
        else if (ChoiceScreen.Shown(next) && next!.IsEnabled) result.ClickVerb("continue", next);
        // Neither button is live while the intro or the score summary animates.
        else return new BusyScreen($"game over ({outcome}); the summary is still playing");
        return result;
    }

    // ------------------------------------------------------------ rooms

    public static Screen DetectRoom(Player me, RunState run)
    {
        // Combat first: events can embed a fight while their room type stays Event.
        if (Game.InCombat && Game.Combat is { } combat && me.PlayerCombatState is { } pcs)
            return new CombatScreen(me, combat, pcs);

        var roomType = run.CurrentRoom?.RoomType ?? RoomType.Unassigned;
        switch (roomType)
        {
            case RoomType.Monster or RoomType.Elite or RoomType.Boss:
                return new BusyScreen("combat is starting or resolving");
            case RoomType.Event:
                return Event(me);
            case RoomType.RestSite:
                return RestSite();
            case RoomType.Shop:
                return NMerchantRoom.Instance is { } shop
                    ? Shop("SHOP", shop.Inventory, shop.ProceedButton, shop.OpenInventory)
                    : new BusyScreen("shop is loading");
            case RoomType.Treasure:
                return Treasure();
            default:
                return new BusyScreen($"between rooms ({roomType})");
        }
    }

    private static Screen? Map(RunState run, NMapScreen map)
    {
        // Mid-journey the other nodes still look clickable; they are not.
        if (map.IsTraveling) return new BusyScreen("travelling");

        var points = ChoiceScreen.Find<NMapPoint>(map)
            .Where(p => ChoiceScreen.Shown(p) && p.IsEnabled && p.Point != null)
            .OrderBy(p => p.Point.coord.col)
            .ToList();
        if (points.Count == 0)
        {
            // Open without anywhere to go: either still animating, or a human is just looking at it.
            return map.IsTravelEnabled || map.IsTraveling ? new BusyScreen("map is updating") : null;
        }

        var result = new ChoiceScreen("MAP").Head($"act {run.CurrentActIndex + 1}, choose your next room ('info map' shows everything ahead)");
        foreach (var node in points)
        {
            var point = node.Point;
            var next = MapInfo.NextTypes(point);
            var label = $"{MapInfo.TypeName(point.PointType)} (col {point.coord.col})" + (next.Length > 0 ? $", then {next}" : "");
            result.Add(new Choice(label, async () =>
            {
                if (!GodotObject.IsInstanceValid(node) || !node.IsEnabled || map.IsTraveling) return "that room cannot be travelled to right now";
                map.OnMapPointSelectedLocally(node);
                // The move is queued as a game action; wait for it to actually begin
                // so the caller does not mistake the unchanged map for a settled state.
                bool began = await MainThread.WaitUntil(
                    () => map.IsTraveling || !map.IsOpen || RunManager.Instance.IsChangingRooms || !node.IsEnabled, 5);
                return began ? null : "the game did not start travelling";
            }));
        }
        return result;
    }

    private static Screen Event(Player me)
    {
        var room = NEventRoom.Instance;
        if (room == null) return new BusyScreen("event is loading");

        if (room.CustomEventNode is NFakeMerchant fake)
        {
            var proceed = ChoiceScreen.Find<NProceedButton>(fake).FirstOrDefault();
            return Shop("EVENT_SHOP", fake.Inventory, proceed, () => fake.Inventory.Open());
        }

        var layout = room.Layout;
        if (layout == null) return new BusyScreen("event is loading");

        var buttons = layout.OptionButtons.Where(b => ChoiceScreen.Shown(b) && b.Option != null).ToList();

        // Ancient events open with story dialogue; click through it rather than asking the agent.
        if (layout is NAncientEventLayout && !buttons.Any(b => b.IsEnabled && !b.Option.IsLocked))
        {
            if (layout.GetNodeOrNull<NClickableControl>("%DialogueHitbox") is { } hitbox && ChoiceScreen.Shown(hitbox) && hitbox.IsEnabled)
                return new DialogueScreen(hitbox);
        }
        if (buttons.Count == 0 || !buttons.Any(b => b.IsEnabled)) return new BusyScreen("event is updating");

        string title = Text.Clean(layout.GetNodeOrNull<Label>("%Title")?.Text);
        string body = Text.Clean(layout.GetNodeOrNull<RichTextLabel>("%EventDescription")?.Text);
        var result = new ChoiceScreen("EVENT").Head(title.Length > 0 ? title : "event");
        if (body.Length > 0) result.Head(body);

        foreach (var button in buttons)
        {
            var option = button.Option;
            string name = Text.Clean(option.Title);
            string description = Text.Clean(option.Description);
            string label = name.Length > 0 ? (description.Length > 0 ? $"{name}: {description}" : name) : ChoiceScreen.Caption(button);
            // The option text usually already says what its relic does; only add
            // the relic's own text when the option has no description.
            if (option.Relic is { } relic && description.Length == 0) label += $" [relic {RelicText(relic)}]";
            if (option.IsLocked) label += " [locked]";
            else
            {
                try
                {
                    if (option.WillKillPlayer?.Invoke(me) == true) label += " [this would kill you]";
                }
                catch { /* advisory only */ }
            }
            result.Add(Choice.Click(label, button, () => option.IsLocked ? "that option is locked" : null));
        }
        return result;
    }

    private static Screen RestSite()
    {
        var room = NRestSiteRoom.Instance;
        if (room == null) return new BusyScreen("rest site is loading");

        var result = new ChoiceScreen("REST").Head("rest site");
        // The room disables every button while a chosen option is resolving.
        foreach (var button in ChoiceScreen.Find<NRestSiteButton>(room).Where(b => ChoiceScreen.Shown(b) && b.IsEnabled && b.Option != null))
        {
            var option = button.Option;
            string label = $"{Text.Clean(option.Title)}: {Text.Clean(option.Description)}" + (option.IsEnabled ? "" : " [unavailable]");
            result.Add(Choice.Click(label, button, () => option.IsEnabled ? null : "that option is unavailable"));
        }
        result.LeaveVerb("proceed", room.ProceedButton);
        if (result.ChoiceCount == 0 && !room.ProceedButton.IsEnabled) return new BusyScreen("rest site is updating");
        return result;
    }

    private static Screen Treasure()
    {
        var room = NRun.Instance?.TreasureRoom;
        if (room == null) return new BusyScreen("treasure room is loading");

        var result = new ChoiceScreen("TREASURE");
        var holders = ChoiceScreen.Find<NTreasureRoomRelicHolder>(room).Where(h => ChoiceScreen.Shown(h) && h.IsEnabled && h.Relic?.Model != null).ToList();
        var chest = room.GetNodeOrNull<NClickableControl>("%Chest") ?? room.GetNodeOrNull<NClickableControl>("Chest");

        if (holders.Count > 0)
        {
            result.Head("the chest is open; take a relic");
            foreach (var holder in holders) result.Add(Choice.Click(RelicText(holder.Relic.Model), holder));
        }
        else if (chest != null && ChoiceScreen.Shown(chest) && chest.IsEnabled)
        {
            result.Head("a closed chest");
            result.ClickVerb("open", chest);
        }
        else
        {
            result.Head("the chest is empty");
        }

        result.LeaveVerb("proceed", room.ProceedButton);
        if (result.ChoiceCount == 0 && !room.ProceedButton.IsEnabled && !(chest != null && chest.IsEnabled))
            return new BusyScreen("treasure room is updating");
        return result;
    }

    // ------------------------------------------------------------ shop

    private static string EntryLabel(MerchantEntry entry)
    {
        string what = entry switch
        {
            MerchantCardEntry { CreationResult.Card: { } card } c => "Card: " + Game.CardLine(card, PileType.None) + (c.IsOnSale ? " (on sale)" : ""),
            MerchantRelicEntry { Model: { } relic } => "Relic: " + RelicText(relic),
            MerchantPotionEntry { Model: { } potion } => "Potion: " + Potions.Describe(potion),
            MerchantCardRemovalEntry => "Remove a card from your deck",
            BlitzMerchantEntry blitz => $"{Text.Clean(blitz.Title)}: {Text.Clean(blitz.Description)}",
            _ => entry.GetType().Name,
        };
        return $"{what} - {entry.Cost}g" + (entry.EnoughGold ? "" : " [too expensive]");
    }

    private static Screen Shop(string kind, NMerchantInventory? inventoryNode, NClickableControl? proceed, Action openInventory)
    {
        var inventory = inventoryNode?.Inventory;
        if (inventoryNode == null || inventory == null) return new BusyScreen("shop is loading");
        var me = inventory.Player;

        var result = new ChoiceScreen(kind).Head($"you have {me.Gold} gold");
        result.Tips(Glossary.Of(inventory.AllEntries.OfType<MerchantCardEntry>()
            .Where(e => e.IsStocked && e.CreationResult?.Card != null).Select(e => e.CreationResult!.Card)));
        foreach (var entry in inventory.AllEntries.Where(e => e.IsStocked))
        {
            result.Add(new Choice(EntryLabel(entry), async () =>
            {
                if (!entry.IsStocked) return "that item is sold out";
                if (!entry.EnoughGold) return $"not enough gold ({me.Gold} of {entry.Cost})";
                if (entry is MerchantPotionEntry && !me.HasOpenPotionSlots) return "your potion belt is full; 'drop <#>' a potion first";

                if (!inventoryNode.IsOpen)
                {
                    openInventory();
                    await MainThread.WaitUntil(() => inventoryNode.IsOpen, 3);
                }
                // Some purchases (card removal) only finish after a follow-up choice, so do not await this.
                Observe(RunManager.Instance.MerchantSynchronizer.PurchaseLocalEntry(entry));
                return null;
            }));
        }

        result.Verb("proceed", async () =>
        {
            if (inventoryNode.IsOpen)
            {
                inventoryNode.Close();
                await MainThread.WaitUntil(() => !inventoryNode.IsOpen, 3);
            }
            if (proceed == null || !GodotObject.IsInstanceValid(proceed)) return "no way to leave was found";
            await MainThread.WaitUntil(() => proceed.IsEnabled, 3);
            proceed.ForceClick();
            return null;
        }, "proceed (leave the shop)");
        return result;
    }

    private static void Observe(Task task) =>
        task.ContinueWith(t => Log.Error("purchase failed: " + t.Exception?.GetBaseException().Message), TaskContinuationOptions.OnlyOnFaulted);
}
