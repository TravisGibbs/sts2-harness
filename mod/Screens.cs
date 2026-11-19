using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

/// <summary>Works out which screen the player is facing, in priority order.</summary>
internal static class Screens
{
    public static Screen Detect()
    {
        // 1. A card-selection prompt blocks everything else.
        if (BridgeSelector.Pending is { } pending) return new CardSelectScreen(pending);

        // 2. So does a modal popup, in or out of a run.
        if (MenuScreens.DetectModal() is { } modal) return modal;

        var rm = RunManager.Instance;
        if (!rm.IsInProgress) return MenuScreens.Detect();
        if (rm.IsCleaningUp) return new BusyScreen("run is closing");

        var me = Game.Me;
        var run = Game.Run;
        if (me == null || run == null) return new BusyScreen("run is loading");

        // 3. A human-opened menu, then the travel map, then overlay screens (rewards,
        //    card reward, game over, ...). Same order the game uses: a finished
        //    rewards screen stays on the overlay stack underneath the open map.
        if (RunScreens.DetectCapstone() is { } capstone) return capstone;
        if (RunScreens.DetectMap(run) is { } map) return map;
        if (RunScreens.DetectOverlay(me, run) is { } overlay) return overlay;

        if (rm.IsChangingRooms) return new BusyScreen("changing rooms");

        // 4. Combat, the map, then whatever room we are standing in.
        return RunScreens.DetectRoom(me, run);
    }
}
