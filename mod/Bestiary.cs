using System.Reflection;
using System.Text;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.MonsterMoves;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;
using MegaCrit.Sts2.Core.MonsterMoves.MonsterMoveStateMachine;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Runs;

namespace SpireBridge;

/// <summary>
/// What the game's bestiary and a practiced player know about a monster: its
/// HP, every move with its intent, and the order the moves come in. Monsters in
/// the current fight are read live (numbers include their current modifiers);
/// any other monster is described from a throwaway copy built the same way the
/// game's own bestiary screen builds one, so the run is never touched.
/// </summary>
internal static class Bestiary
{
    private const BindingFlags Hidden = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
    private const int MaxFullMatches = 3;

    // ------------------------------------------------------------------ topics

    /// <summary>"The Kin", or "A then B" with two bosses; "" when the act has none set.</summary>
    public static string BossName(RunState run)
    {
        try
        {
            var act = run.Act;
            var name = Text.Clean(act.BossEncounter.Title);
            if (act.HasSecondBoss && act.SecondBossEncounter is { } second) name += " then " + Text.Clean(second.Title);
            return name;
        }
        catch
        {
            return "";
        }
    }

    public static string Boss()
    {
        var run = Game.Run;
        if (run == null) return "No run in progress.";

        ActModel act;
        EncounterModel first;
        try
        {
            act = run.Act;
            first = act.BossEncounter;
        }
        catch
        {
            return "This act has no boss set.";
        }

        var sb = new StringBuilder();
        var notes = new Notes();
        AppendEncounter(sb, $"Act {run.CurrentActIndex + 1} boss", first, notes);
        try
        {
            if (act.HasSecondBoss && act.SecondBossEncounter is { } second)
                AppendEncounter(sb, "Second boss (fought right after the first)", second, notes);
        }
        catch (Exception e)
        {
            Log.Error("second boss lookup failed: " + e.Message);
        }
        return sb.ToString().TrimEnd();
    }

    /// <summary>
    /// Moves of the monsters in the current fight (no name), or of any monster
    /// by name. Null when a name matches nothing, so the caller can try other lookups.
    /// </summary>
    public static string? Enemy(string name)
    {
        name = name.Trim();
        var live = LiveEnemies();
        var sb = new StringBuilder();
        var notes = new Notes();

        if (name.Length == 0)
        {
            if (live.Count == 0) return "Not in combat. 'info enemy <name>' describes any monster; 'info boss' describes this act's boss.";
            AppendLive(sb, live, _ => true, notes);
            AppendTerms(sb, notes);
            return sb.ToString().TrimEnd();
        }

        // Prefer the ones actually in front of the player: their numbers are live.
        bool Named(Creature e) => e.Monster != null && Title(e.Monster).Contains(name, StringComparison.OrdinalIgnoreCase);
        if (live.Any(Named))
        {
            AppendLive(sb, live, Named, notes);
            AppendTerms(sb, notes);
            return sb.ToString().TrimEnd();
        }

        var matches = new List<MonsterModel>();
        try
        {
            foreach (var monster in ModelDb.Monsters)
            {
                string title;
                try { title = Title(monster); }
                catch { continue; }
                if (title.Contains(name, StringComparison.OrdinalIgnoreCase)) matches.Add(monster);
            }
        }
        catch (Exception e)
        {
            Log.Error("monster list failed: " + e.Message);
        }
        if (matches.Count == 0) return null;

        var exact = matches.Where(m => Title(m).Equals(name, StringComparison.OrdinalIgnoreCase)).ToList();
        if (exact.Count > 0) matches = exact;
        matches = matches.GroupBy(Title).Select(g => g.First()).ToList();
        if (matches.Count > MaxFullMatches)
            return $"{matches.Count} monsters match '{name}': {string.Join(", ", matches.Select(Title).OrderBy(t => t))}. Be more specific.";

        foreach (var monster in matches) AppendMonster(sb, monster, null, "", "", notes);
        AppendTerms(sb, notes);
        return sb.ToString().TrimEnd();
    }

    /// <summary>Every monster's display name once, for name lookups; never throws.</summary>
    public static List<string> MonsterTitles()
    {
        var titles = new List<string>();
        try
        {
            foreach (var monster in ModelDb.Monsters)
            {
                try
                {
                    var title = Title(monster);
                    if (title.Length > 0 && !titles.Contains(title)) titles.Add(title);
                }
                catch
                {
                    // Skip a monster whose name cannot be read.
                }
            }
        }
        catch (Exception e)
        {
            Log.Error("monster titles failed: " + e.Message);
        }
        return titles;
    }

    // ------------------------------------------------------------------ terms

    /// <summary>Statuses and intents named by a listing, explained once each after it.</summary>
    private sealed class Notes
    {
        public readonly List<Type> Powers = [];
        public readonly List<IntentType> Intents = [];
        public readonly HashSet<string> Shown = [];

        public void Power(Type type)
        {
            if (!Powers.Contains(type)) Powers.Add(type);
        }

        public void Intent(IntentType type)
        {
            if (!Intents.Contains(type)) Intents.Add(type);
        }
    }

    private const int MaxTermLength = 200;

    // Intents whose meaning is not obvious from their name.
    private static readonly IntentType[] ExplainedIntents = [IntentType.DeathBlow, IntentType.CardDebuff];

    /// <summary>"Terms:" with the game's own tooltip for every status and unusual intent not yet explained.</summary>
    private static void AppendTerms(StringBuilder sb, Notes notes)
    {
        var lines = new List<string>();
        foreach (var type in notes.Powers)
        {
            try
            {
                if (!notes.Shown.Add("power:" + type.FullName)) continue;
                var text = PowerTip(type);
                lines.Add($"  {ModelTitle(type)}: {(text.Length > 0 ? text : "(no description available)")}");
            }
            catch
            {
                // One unreadable status does not spoil the rest.
            }
        }
        foreach (var intent in notes.Intents.Where(ExplainedIntents.Contains))
        {
            if (!notes.Shown.Add("intent:" + intent)) continue;
            if (IntentTip(intent) is { } tip) lines.Add($"  {intent} (intent): {tip.Description}");
        }
        if (lines.Count == 0) return;
        sb.Line("Terms:");
        foreach (var line in lines) sb.Line(line);
    }

    internal static string Cap(string text, int length = MaxTermLength) =>
        text.Length > length ? text[..length].TrimEnd() + "..." : text;

    /// <summary>A status's general tooltip (the one cards show), markup stripped and capped; "" if unreadable.</summary>
    private static string PowerTip(Type type)
    {
        try
        {
            if (ModelDb.GetByIdOrNull<PowerModel>(ModelDb.GetId(type)) is not { } power) return "";
            return Cap(Text.Clean(power.DumbHoverTip.Description));
        }
        catch
        {
            return "";
        }
    }

    /// <summary>
    /// The tooltip the game shows on an intent icon ("intents" table, keyed by the
    /// intent's prefix as in AbstractIntent.GetHoverTip). Numbers that depend on the
    /// fight are shown as X. Null when the game has no tooltip for it.
    /// </summary>
    internal static (string Title, string Description)? IntentTip(IntentType type)
    {
        string prefix = type switch
        {
            IntentType.Attack => "ATTACK",
            IntentType.Buff => "BUFF",
            IntentType.Debuff => "DEBUFF",
            IntentType.DebuffStrong => "DEBUFF_STRONG",
            IntentType.Defend => "DEFEND",
            IntentType.Escape => "ESCAPE",
            IntentType.Heal => "HEAL",
            IntentType.Hidden => "HIDDEN",
            IntentType.Summon => "SUMMON",
            IntentType.Sleep => "SLEEP",
            IntentType.Stun => "STUN",
            IntentType.StatusCard => "STATUS",
            IntentType.CardDebuff => "CARD_DEBUFF",
            IntentType.DeathBlow => "DEATH_BLOW",
            _ => "UNKNOWN",
        };
        try
        {
            if (!LocString.Exists("intents", prefix + ".description")) return null;
            var description = new LocString("intents", prefix + ".description");
            // Every variable the intent descriptions use, so formatting never fails.
            description.Add("IsMultiplayer", false);
            description.Add("Damage", "X");
            description.Add("Repeat", 1m);
            description.Add("CardCount", 1m);
            var title = LocString.Exists("intents", prefix + ".title") ? Text.Clean(new LocString("intents", prefix + ".title")) : "";
            var text = Text.Clean(description);
            return text.Length > 0 ? (title, Cap(text)) : null;
        }
        catch
        {
            return null;
        }
    }

    // ------------------------------------------------------------------ building blocks

    private static List<Creature> LiveEnemies()
    {
        try
        {
            var combat = Game.Combat;
            if (combat == null || !Game.InCombat) return [];
            // Same filter and order as the combat state, so e0, e1... mean the same enemies.
            return combat.Enemies.Where(e => e.IsAlive).ToList();
        }
        catch
        {
            return [];
        }
    }

    private static void AppendLive(StringBuilder sb, List<Creature> live, Func<Creature, bool> wanted, Notes notes)
    {
        var described = new Dictionary<ModelId, int>();
        for (int i = 0; i < live.Count; i++)
        {
            var enemy = live[i];
            var monster = enemy.Monster;
            if (monster == null || !wanted(enemy)) continue;
            if (described.TryGetValue(monster.Id, out var same))
            {
                // A second copy of the same monster: its move list would repeat word for word.
                var now = Safe(() => MoveName(monster, monster.NextMove.Id), "?");
                sb.Line($"e{i} {Title(monster)} HP {enemy.CurrentHp}/{enemy.MaxHp}: same moves as e{same}; now: {now}");
                continue;
            }
            described[monster.Id] = i;
            AppendMonster(sb, monster, enemy, $"e{i} ", "", notes);
        }
    }

    private static void AppendEncounter(StringBuilder sb, string label, EncounterModel encounter, Notes notes)
    {
        sb.Line($"{label}: {Text.Clean(encounter.Title)}");
        foreach (var (monster, count) in Members(encounter))
            AppendMonster(sb, monster, null, "", count switch { 0 => " (can join the fight)", 1 => "", _ => $" x{count}" }, notes);
        AppendTerms(sb, notes);
    }

    /// <summary>
    /// The monsters of an encounter with how many of each start the fight; count
    /// 0 marks ones that only appear later (summons, later phases).
    /// </summary>
    private static List<(MonsterModel Monster, int Count)> Members(EncounterModel encounter)
    {
        var members = new List<(MonsterModel, int)>();
        bool counted = false;
        try
        {
            // The same steps the bestiary takes to lay out a multi-monster encounter.
            var copy = encounter.CanonicalInstance.ToMutable();
            copy.GenerateMonstersWithSlots(NullRunState.Instance);
            foreach (var group in copy.MonstersWithSlots.Select(m => m.Item1).GroupBy(m => m.Id))
                members.Add((group.First().CanonicalInstance, group.Count()));
            counted = members.Count > 0;
        }
        catch (Exception e)
        {
            Log.Error($"could not lay out encounter {encounter.Id.Entry}: {e.Message}");
            members.Clear();
        }

        try
        {
            foreach (var monster in encounter.AllPossibleMonsters)
            {
                if (monster == null || members.Any(m => m.Item1.Id == monster.Id)) continue;
                members.Add((monster, counted ? 0 : 1));
            }
        }
        catch (Exception e)
        {
            Log.Error($"could not list monsters of {encounter.Id.Entry}: {e.Message}");
        }
        return members;
    }

    /// <summary>A mutable copy with a move state machine, exactly as NBestiaryLayoutDefault.Setup makes one.</summary>
    private static MonsterModel Simulate(MonsterModel monster)
    {
        var copy = monster.CanonicalInstance.ToMutable();
        // Chaotic RNG: nothing here may ever draw from the run's seeded generators.
        copy.Rng = Rng.Chaotic;
        copy.RunRng = new RunRngSet(Rng.Chaotic);
        copy.SetUpForCombat();
        _ = new Creature(copy, CombatSide.Enemy, null) { CombatState = NullCombatState.Instance };
        return copy;
    }

    /// <summary>The monster's display name; its id made readable if the text cannot be loaded.</summary>
    private static string Title(MonsterModel monster)
    {
        var text = Text.Clean(monster.Title);
        return text.Length > 0 && !text.StartsWith('<') ? text : Words(Pascal(monster.Id.Entry));
    }

    private static string Safe(Func<string> text, string fallback)
    {
        try { return text(); }
        catch { return fallback; }
    }

    // ------------------------------------------------------------------ one monster

    private sealed class Move
    {
        public required string Id;
        public required string Name;
        public required MoveState State;
        public string Prefix = "";
        public string Intents = "";
        public bool Attacks;
        public string? NextId;
        public readonly List<AttackIntent> Varying = [];
        public string Forced = "";
    }

    private static void AppendMonster(StringBuilder sb, MonsterModel model, Creature? live, string before, string after, Notes notes)
    {
        string name = Safe(() => Title(model), model.Id.Entry);
        try
        {
            var monster = live?.Monster ?? Simulate(model);
            string hp;
            if (live != null) hp = $"HP {live.CurrentHp}/{live.MaxHp}";
            else
            {
                int min = monster.MinInitialHp, max = monster.MaxInitialHp;
                hp = min == max ? $"HP {max}" : $"HP {min}-{max}";
            }
            sb.Line($"{before}{name}{after} {hp}");

            var machine = monster.MoveStateMachine;
            if (machine == null)
            {
                sb.Line("  moves unknown");
                return;
            }

            var targets = live != null ? Game.Combat?.PlayerCreatures : null;
            var moves = new List<Move>();
            foreach (var (id, state) in machine.States)
            {
                if (state is not MoveState moveState) continue;
                var move = new Move { Id = id, Name = MoveName(model, id), State = moveState, Prefix = Pascal(MoveKey(id)) };
                try { move.NextId = moveState.FollowUpState?.Id ?? moveState.FollowUpStateId; }
                catch { /* no fixed follow-up */ }
                move.Intents = IntentsText(moveState, live, targets, out move.Attacks, move.Varying, notes);
                moves.Add(move);
            }

            var values = Values(monster);
            var owners = AssignValues(values, moves);

            // Statuses this monster deals in: from its moves, and the ones it starts with.
            var powers = new List<Type>();
            var effectText = moves.Select(m => Effects(m, values, owners, powers)).ToList();
            var startTypes = StartingPowerTypes(monster);
            var start = string.Join(", ", startTypes.Select(ModelTitle).Distinct());

            // What a "(varies)" amount follows, and which moves only happen when something forces them.
            foreach (var move in moves)
            {
                foreach (var attack in move.Varying)
                {
                    var note = VariesNote(monster, attack, moves, values);
                    int at = move.Intents.IndexOf("(varies)", StringComparison.Ordinal);
                    if (note.Length > 0 && at >= 0) move.Intents = move.Intents[..at] + $"(varies: {note})" + move.Intents[(at + "(varies)".Length)..];
                }
            }
            ForcedMoves(monster, moves, powers.Concat(startTypes).Distinct().ToList());

            // A fixed loop through every move reads best as one line.
            var initialId = (Field(machine, "_initialState") as MonsterState)?.Id;
            var loop = FixedLoop(machine, moves, initialId);
            var branches = new List<string>();

            for (int m = 0; m < moves.Count; m++)
            {
                var move = moves[m];
                var line = new StringBuilder("  ").Append(move.Name).Append(": ").Append(move.Intents);
                var effects = effectText[m];
                if (effects.Length > 0) line.Append(" {").Append(effects).Append('}');
                if (loop == null && !string.IsNullOrEmpty(move.NextId))
                    line.Append(" -> ").Append(Reference(machine, model, move.NextId!, branches));
                if (move.Forced.Length > 0) line.Append(" (").Append(move.Forced).Append(')');
                sb.Line(line.ToString());
            }

            if (loop != null) sb.Line("  Pattern: " + loop);
            else if (live == null && !string.IsNullOrEmpty(initialId))
                sb.Line("  First: " + Reference(machine, model, initialId!, branches));

            // Branch states are shared by several moves, so each is spelled out once.
            // Describing one can reveal another, so the list may grow while it is walked.
            for (int i = 0; i < branches.Count && i < 8; i++)
                sb.Line("  " + BranchLine(machine, model, branches[i], branches));

            if (live != null)
            {
                sb.Line("  Now: " + Safe(() => MoveName(model, monster.NextMove.Id), "?"));
            }
            else
            {
                if (start.Length > 0) sb.Line("  Fight starts with: " + start);
                foreach (var type in startTypes) powers.Add(type);
            }

            var other = values.Where((_, i) => owners[i] == null).Select(v => $"{Words(v.Name)} {v.Text}").Take(12).ToList();
            if (other.Count > 0) sb.Line("  Other values: " + string.Join(", ", other));

            foreach (var type in powers) notes.Power(type);
        }
        catch (Exception e)
        {
            Log.Error($"describing monster {model.Id.Entry} threw: {e}");
            sb.Line($"{before}{name}{after}: could not be described ({e.GetType().Name})");
        }
    }

    // ------------------------------------------------------------------ intents

    private static string IntentsText(MoveState move, Creature? live, IReadOnlyList<Creature>? targets, out bool attacks, List<AttackIntent> varying, Notes notes)
    {
        attacks = false;
        var parts = new List<string>();
        IReadOnlyList<AbstractIntent> intents;
        try { intents = move.Intents; }
        catch { return "?"; }

        foreach (var intent in intents)
        {
            try
            {
                notes.Intent(intent.IntentType);
                switch (intent)
                {
                    case AttackIntent attack:
                        attacks = true;
                        // In a fight, the same call the intent icon uses (the attacker's and the
                        // player's modifiers included). Otherwise the move's base number.
                        int damage = live != null && targets != null
                            ? attack.GetSingleDamage(targets, live)
                            : Math.Max(0, (int)(attack.DamageCalc?.Invoke() ?? 0m));
                        int repeats = attack.Repeats;
                        string word = attack.IntentType == IntentType.DeathBlow ? "DeathBlow" : "Attack";
                        // Outside a fight a move whose damage builds up during the fight computes as 0.
                        string amount = live == null && damage <= 0 ? "(varies)" : damage.ToString();
                        if (amount == "(varies)") varying.Add(attack);
                        parts.Add(repeats > 1 ? $"{word} {amount}x{repeats}" : $"{word} {amount}");
                        break;
                    case StatusIntent status:
                        parts.Add($"AddStatusCards {status.CardCount}");
                        break;
                    default:
                        parts.Add(intent.IntentType.ToString());
                        break;
                }
            }
            catch
            {
                parts.Add(intent is AttackIntent ? "Attack ?" : "?");
            }
        }
        return parts.Count > 0 ? string.Join(" + ", parts) : "no intent";
    }

    // ------------------------------------------------------------------ move names

    private static string MoveKey(string stateId) => stateId.EndsWith("_MOVE", StringComparison.Ordinal) ? stateId[..^5] : stateId;

    /// <summary>The bestiary's name for a move, or the state id made readable.</summary>
    private static string MoveName(MonsterModel monster, string stateId)
    {
        var key = MoveKey(stateId);
        if (MoveTitle(monster, key) is { } title) return title;

        // Variants of one move ("TACKLE2", "THRASH_MOVE_2") have no name of their own in the bestiary.
        var plain = key.Replace("_MOVE", "", StringComparison.Ordinal);
        var stem = plain.TrimEnd('0', '1', '2', '3', '4', '5', '6', '7', '8', '9');
        var number = plain[stem.Length..];
        stem = stem.TrimEnd('_');
        if (number.Length > 0 && stem.Length > 0)
            return (MoveTitle(monster, stem) ?? Words(Pascal(stem))) + " " + number;
        return Words(Pascal(plain));
    }

    private static string? MoveTitle(MonsterModel monster, string key)
    {
        try
        {
            // The key MonsterModel.GetBestiaryMoveName builds (that method is protected).
            var loc = new LocString("monsters", monster.Id.Entry + ".moves." + key + ".title");
            if (!loc.Exists()) return null;
            var text = Text.Clean(loc.GetFormattedText());
            return text.Length > 0 ? text : null;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>"EYE_LASERS" -> "EyeLasers", the form the monster's own members use.</summary>
    private static string Pascal(string upperSnake)
    {
        var sb = new StringBuilder();
        foreach (var part in upperSnake.Split('_', StringSplitOptions.RemoveEmptyEntries))
            sb.Append(char.ToUpperInvariant(part[0])).Append(part[1..].ToLowerInvariant());
        return sb.ToString();
    }

    /// <summary>"EyeLasers" -> "Eye lasers".</summary>
    internal static string Words(string pascal)
    {
        var sb = new StringBuilder();
        for (int i = 0; i < pascal.Length; i++)
        {
            char c = pascal[i];
            bool boundary = i > 0 && char.IsUpper(c) && !char.IsUpper(pascal[i - 1]);
            if (boundary) sb.Append(' ');
            sb.Append(boundary ? char.ToLowerInvariant(c) : c);
        }
        return sb.ToString();
    }

    // ------------------------------------------------------------------ move order

    private static string Reference(MonsterMoveStateMachine machine, MonsterModel model, string stateId, List<string> branches)
    {
        if (!machine.States.TryGetValue(stateId, out var state)) return stateId;
        if (state is MoveState) return MoveName(model, stateId);
        if (!branches.Contains(stateId)) branches.Add(stateId);
        return $"[{stateId}]";
    }

    private static string BranchLine(MonsterMoveStateMachine machine, MonsterModel model, string stateId, List<string> branches)
    {
        var state = machine.States[stateId];
        try
        {
            switch (state)
            {
                case RandomBranchState random:
                {
                    var weights = new List<float?>();
                    foreach (var option in random.States)
                    {
                        try { weights.Add(option.GetWeight()); }
                        catch { weights.Add(null); }
                    }
                    float total = weights.Sum(w => w ?? 0f);
                    bool percent = total > 0f && weights.All(w => w.HasValue);
                    var parts = new List<string>();
                    for (int i = 0; i < random.States.Count; i++)
                    {
                        var option = random.States[i];
                        var text = Reference(machine, model, option.stateId, branches);
                        if (percent) text += $" {Math.Round(100f * weights[i]!.Value / total)}%";
                        var rules = new List<string>();
                        switch (option.repeatType)
                        {
                            case MoveRepeatType.CannotRepeat: rules.Add("never twice in a row"); break;
                            case MoveRepeatType.UseOnlyOnce: rules.Add("once per fight"); break;
                            case MoveRepeatType.CanRepeatXTimes: rules.Add($"at most {option.maxTimes} in a row"); break;
                        }
                        if (option.cooldown > 0) rules.Add($"cooldown {option.cooldown}");
                        if (rules.Count > 0) text += $" ({string.Join(", ", rules)})";
                        parts.Add(text);
                    }
                    return $"[{stateId}] random: {string.Join(", ", parts)}";
                }
                case ConditionalBranchState:
                {
                    // The options are private; their conditions are code and cannot be shown.
                    var ids = new List<string>();
                    if (Property(state, "States") is System.Collections.IEnumerable options)
                    {
                        foreach (var option in options)
                        {
                            if (option?.GetType().GetField("id", Hidden)?.GetValue(option) is string id)
                                ids.Add(Reference(machine, model, id, branches));
                        }
                    }
                    return ids.Count > 0
                        ? $"[{stateId}] depends on the fight, first that applies: {string.Join(", ", ids)}"
                        : $"[{stateId}] depends on the fight";
                }
                default:
                    return $"[{stateId}] decided by the monster";
            }
        }
        catch
        {
            return $"[{stateId}] unknown";
        }
    }

    /// <summary>
    /// "A > B > C, then repeats from B" when the monster has no branching and the
    /// chain from its first move covers every move; otherwise null.
    /// </summary>
    private static string? FixedLoop(MonsterMoveStateMachine machine, List<Move> moves, string? initialId)
    {
        if (string.IsNullOrEmpty(initialId) || moves.Count == 0) return null;
        if (machine.States.Values.Any(s => s is not MoveState)) return null;

        var order = new List<Move>();
        var id = initialId;
        while (true)
        {
            var move = moves.FirstOrDefault(m => m.Id == id);
            if (move == null) return null;
            int seen = order.IndexOf(move);
            if (seen >= 0)
            {
                if (order.Count != moves.Count) return null;
                var chain = string.Join(" > ", order.Select(m => m.Name));
                if (order.Count == 1) return $"{chain} every turn";
                return seen == order.Count - 1
                    ? $"{chain}, then {move.Name} every turn"
                    : $"{chain}, then repeats from {move.Name}";
            }
            order.Add(move);
            if (string.IsNullOrEmpty(move.NextId)) return null;
            id = move.NextId;
        }
    }

    // ------------------------------------------------------------------ numbers and effects

    private sealed record Number(string Name, string Text);

    /// <summary>
    /// The monster's own read-only numbers (reflection over its non-public
    /// properties): "EbbBlock 33", "CrushStrength 3". These are the amounts its
    /// moves use, which no intent exposes.
    /// </summary>
    private static List<Number> Values(MonsterModel monster)
    {
        var values = new List<Number>();
        try
        {
            for (var type = monster.GetType(); type != null && type != typeof(MonsterModel) && type != typeof(object); type = type.BaseType)
            {
                foreach (var property in type.GetProperties(Hidden | BindingFlags.DeclaredOnly))
                {
                    if (property.GetMethod == null || property.SetMethod != null) continue;
                    if (property.GetIndexParameters().Length > 0) continue;
                    if (property.PropertyType != typeof(int) && property.PropertyType != typeof(decimal)) continue;
                    if (property.Name is "MinInitialHp" or "MaxInitialHp") continue;
                    if (values.Any(v => v.Name == property.Name)) continue;
                    try
                    {
                        var raw = property.GetValue(monster);
                        decimal number = raw is int i ? i : raw is decimal d ? d : 0m;
                        if (number != 0m) values.Add(new Number(property.Name, number.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture)));
                    }
                    catch
                    {
                        // Needs a live fight to compute; leave it out.
                    }
                }
            }
        }
        catch (Exception e)
        {
            Log.Error("monster values failed: " + e.Message);
        }
        return values;
    }

    /// <summary>For each value, the move whose name it starts with (longest wins), or null.</summary>
    private static Move?[] AssignValues(List<Number> values, List<Move> moves)
    {
        var owners = new Move?[values.Count];
        for (int i = 0; i < values.Count; i++)
        {
            var name = values[i].Name;
            owners[i] = moves
                .Where(m => m.Prefix.Length > 0 && name.Length > m.Prefix.Length && name.StartsWith(m.Prefix, StringComparison.Ordinal) && !char.IsLower(name[m.Prefix.Length]))
                .OrderByDescending(m => m.Prefix.Length)
                .FirstOrDefault();
        }
        return owners;
    }

    /// <summary>
    /// What a move does beyond its intent icons: statuses it applies, cards it
    /// adds, monsters it summons (read from the calls its code makes), with the
    /// monster's own amounts where their names line up.
    /// </summary>
    private static string Effects(Move move, List<Number> values, Move?[] owners, List<Type> powers)
    {
        var parts = new List<string>();
        var mine = new List<(string Tail, string Amount)>();
        for (int i = 0; i < values.Count; i++)
        {
            if (owners[i] != move) continue;
            var rest = values[i].Name[move.Prefix.Length..];
            if (rest.Length > 4 && rest.StartsWith("Move", StringComparison.Ordinal) && char.IsUpper(rest[4])) rest = rest[4..];
            // Already shown by the attack intent.
            if (move.Attacks && rest is "Damage" or "Repeat" or "Repeats") continue;
            mine.Add((rest, values[i].Text));
        }

        try
        {
            var perform = Field(move.State, "_onPerform") as Delegate;
            foreach (var (kind, type) in Mentions(perform?.Method))
            {
                var title = ModelTitle(type);
                switch (kind)
                {
                    case 'P':
                        if (!powers.Contains(type)) powers.Add(type);
                        var stem = Stem(type);
                        int at = mine.FindIndex(m => m.Tail.Contains(stem, StringComparison.OrdinalIgnoreCase));
                        if (at >= 0)
                        {
                            title += " " + mine[at].Amount;
                            mine.RemoveAt(at);
                        }
                        parts.Add(title);
                        break;
                    case 'C':
                        parts.Add("card " + title);
                        break;
                    case 'M':
                        parts.Add("summons " + title);
                        break;
                }
            }
        }
        catch (Exception e)
        {
            Log.Error($"move effects of {move.Id} failed: {e.Message}");
        }

        parts.AddRange(mine.Select(m => $"{Words(m.Tail)} {m.Amount}"));
        return string.Join(", ", parts.Distinct());
    }

    private static List<Type> StartingPowerTypes(MonsterModel monster)
    {
        try
        {
            var method = monster.GetType().GetMethod("AfterAddedToRoom", Hidden, Type.EmptyTypes);
            if (method == null || method.DeclaringType == typeof(MonsterModel)) return [];
            return Mentions(method).Where(m => m.Kind == 'P').Select(m => m.Type).Distinct().ToList();
        }
        catch
        {
            return [];
        }
    }

    // ------------------------------------------------------------------ amounts that build up, forced moves

    /// <summary>
    /// What an attack whose damage is only known in a fight follows, read from the
    /// code: the monster property its damage reads, and every place that sets it.
    /// "its Steam Eruption at About to Blow", "starts at 20, +5 after each Pressure Gun".
    /// "" when nothing readable sets it.
    /// </summary>
    private static string VariesNote(MonsterModel monster, AttackIntent attack, List<Move> moves, List<Number> values)
    {
        try
        {
            var type = monster.GetType();
            var read = IlScan.Calls(attack.DamageCalc?.Method, 1).FirstOrDefault(c =>
                c.IsSpecialName && c.Name.StartsWith("get_", StringComparison.Ordinal) &&
                c.DeclaringType != null && c.DeclaringType.IsSubclassOf(typeof(MonsterModel)) && c.DeclaringType.IsAssignableFrom(type));
            if (read == null) return "";
            var property = read.Name[4..];
            string? Value(string name) => values.FirstOrDefault(v => v.Name == name)?.Text;

            var starts = new List<string>();
            var changes = new List<string>();
            foreach (var method in OwnMethods(type))
            {
                if (method.IsSpecialName) continue;
                var calls = IlScan.Calls(method, 1);
                for (int i = 0; i < calls.Count; i++)
                {
                    if (!(calls[i].IsSpecialName && calls[i].Name == "set_" + property)) continue;
                    // What was read just before this assignment (back to the previous one).
                    Type? power = null;
                    string? other = null;
                    bool self = false;
                    for (int j = i - 1; j >= 0 && !calls[j].Name.StartsWith("set_", StringComparison.Ordinal); j--)
                    {
                        var call = calls[j];
                        if (call is MethodInfo { IsGenericMethod: true } generic && call.Name == "GetPowerAmount" &&
                            generic.GetGenericArguments().FirstOrDefault() is { } argument && typeof(PowerModel).IsAssignableFrom(argument))
                            power ??= argument;
                        else if (call.IsSpecialName && call.Name.StartsWith("get_", StringComparison.Ordinal))
                        {
                            var name = call.Name[4..];
                            if (name == property) self = true;
                            else if (other == null && Value(name) != null) other = name;
                        }
                    }
                    var when = moves.FirstOrDefault(m => Same(Perform(m), method))?.Name;
                    if (power != null) changes.Add($"its {ModelTitle(power)}" + (when != null ? $" at {when}" : ""));
                    else if (self && other != null) changes.Add($"+{Value(other)}" + (when != null ? $" after each {when}" : ""));
                    else if (other != null && method.Name == "AfterAddedToRoom") starts.Add($"starts at {Value(other)}");
                }
            }
            return string.Join(", ", starts.Concat(changes).Distinct());
        }
        catch
        {
            return "";
        }
    }

    /// <summary>
    /// Moves the monster is pushed into from outside its normal order
    /// (SetMoveImmediate), with what pushes it: "forced after a death, by Steam Eruption".
    /// </summary>
    private static void ForcedMoves(MonsterModel monster, List<Move> moves, List<Type> powers)
    {
        try
        {
            var type = monster.GetType();
            var own = OwnMethods(type);
            foreach (var method in own)
            {
                var calls = IlScan.Calls(method, 1);
                int at = calls.FindIndex(c => c.Name == "SetMoveImmediate");
                if (at < 0) continue;

                // The state handed over is read just before the call: "AboutToBlowState" -> ABOUT_TO_BLOW_MOVE.
                Move? move = null;
                for (int j = at - 1; j >= 0; j--)
                {
                    var call = calls[j];
                    if (!(call is MethodInfo { IsSpecialName: true } getter && getter.Name.StartsWith("get_", StringComparison.Ordinal) &&
                          typeof(MonsterState).IsAssignableFrom(getter.ReturnType))) continue;
                    // "EnragedState" names ENRAGE_MOVE; the longest matching move name wins.
                    var name = getter.Name[4..];
                    if (name.EndsWith("State", StringComparison.Ordinal)) name = name[..^5];
                    move = moves.Where(m => m.Prefix.Length > 0 && name.StartsWith(m.Prefix, StringComparison.Ordinal))
                        .OrderByDescending(m => m.Prefix.Length).FirstOrDefault();
                    break;
                }
                if (move == null || move.Forced.Length > 0) continue;

                string? why = IsOverride(method) ? Hook(method.Name) : null;
                if (why == null)
                {
                    foreach (var power in powers)
                    {
                        var caller = OwnMethods(power).FirstOrDefault(p => IlScan.Calls(p, 1).Any(c => Same(c, method)));
                        if (caller == null) continue;
                        why = $"{Hook(caller.Name)}, by {ModelTitle(power)}";
                        break;
                    }
                }
                if (why == null)
                {
                    var caller = own.FirstOrDefault(o => !Same(o, method) && IsOverride(o) && IlScan.Calls(o, 1).Any(c => Same(c, method)));
                    if (caller != null) why = Hook(caller.Name);
                }
                if (why != null) move.Forced = "forced " + why;
            }
        }
        catch
        {
            // Annotations are optional.
        }
    }

    /// <summary>"AfterDeath" -> "after a death": when a game hook runs.</summary>
    private static string Hook(string name) => name switch
    {
        "AfterDeath" => "after a death",
        "BeforeDeath" => "before a death",
        "AfterDamageReceived" => "after taking damage",
        "BeforeDamageReceived" => "before taking damage",
        "AfterCurrentHpChanged" => "after an HP change",
        "AfterTurnEnd" => "at the end of a turn",
        "AfterTurnStart" or "AfterSideTurnStart" => "at the start of a turn",
        _ => "by " + Words(name).ToLowerInvariant(),
    };

    private static MethodBase? Perform(Move move) => (Field(move.State, "_onPerform") as Delegate)?.Method;

    private static bool Same(MethodBase? a, MethodBase? b) =>
        a != null && b != null && a.MetadataToken == b.MetadataToken && a.Module == b.Module;

    private static bool IsOverride(MethodBase method) =>
        method is MethodInfo info && info.GetBaseDefinition().DeclaringType != info.DeclaringType;

    /// <summary>Methods a model class declares itself (up to the game's base model class).</summary>
    private static List<MethodInfo> OwnMethods(Type type)
    {
        var methods = new List<MethodInfo>();
        try
        {
            for (var t = type; t != null && t != typeof(MonsterModel) && t != typeof(PowerModel) && t != typeof(AbstractModel) && t != typeof(object); t = t.BaseType)
                methods.AddRange(t.GetMethods(Hidden | BindingFlags.Static | BindingFlags.DeclaredOnly));
        }
        catch
        {
            // Partial is fine.
        }
        return methods;
    }

    /// <summary>
    /// Statuses (P), cards (C) and monsters (M) a method names through the game's
    /// own generic commands: PowerCmd.Apply&lt;T&gt;, CardPileCmd.AddToCombatAndPreview&lt;T&gt;,
    /// CreatureCmd.Add&lt;T&gt;, ModelDb.Power/Card/Monster&lt;T&gt;.
    /// </summary>
    private static List<(char Kind, Type Type)> Mentions(MethodBase? method)
    {
        var found = new List<(char, Type)>();
        foreach (var called in IlScan.Calls(method))
        {
            try
            {
                if (called is not MethodInfo { IsGenericMethod: true } generic) continue;
                var argument = generic.GetGenericArguments().FirstOrDefault();
                if (argument == null || argument.IsGenericParameter) continue;
                var owner = called.DeclaringType?.Name ?? "";
                char kind;
                if (typeof(PowerModel).IsAssignableFrom(argument) && ((owner == "PowerCmd" && called.Name == "Apply") || (owner == "ModelDb" && called.Name == "Power"))) kind = 'P';
                else if (typeof(CardModel).IsAssignableFrom(argument) && (owner == "CardPileCmd" || called.Name == "CreateCard" || (owner == "ModelDb" && called.Name == "Card"))) kind = 'C';
                else if (typeof(MonsterModel).IsAssignableFrom(argument) && ((owner == "CreatureCmd" && called.Name == "Add") || (owner == "ModelDb" && called.Name == "Monster"))) kind = 'M';
                else continue;
                if (!found.Contains((kind, argument))) found.Add((kind, argument));
            }
            catch
            {
                // One unreadable call does not spoil the rest.
            }
        }
        return found;
    }

    /// <summary>"StrengthPower" -> "Strength": the word a monster uses in its own member names.</summary>
    private static string Stem(Type type)
    {
        var name = type.Name;
        foreach (var suffix in new[] { "Power", "Card", "Monster" })
        {
            if (name.Length > suffix.Length && name.EndsWith(suffix, StringComparison.Ordinal)) return name[..^suffix.Length];
        }
        return name;
    }

    private static string ModelTitle(Type type)
    {
        try
        {
            var id = ModelDb.GetId(type);
            string title = "";
            if (typeof(PowerModel).IsAssignableFrom(type)) title = ModelDb.GetByIdOrNull<PowerModel>(id) is { } power ? Text.Clean(power.Title) : "";
            else if (typeof(CardModel).IsAssignableFrom(type)) title = ModelDb.GetByIdOrNull<CardModel>(id) is { } card ? Text.Safe(() => card.Title, "") : "";
            else if (typeof(MonsterModel).IsAssignableFrom(type)) title = ModelDb.GetByIdOrNull<MonsterModel>(id) is { } monster ? Text.Clean(monster.Title) : "";
            if (title.Length > 0 && !title.StartsWith('<')) return title;
        }
        catch
        {
            // Fall back to the type name.
        }
        return Words(Stem(type));
    }

    // ------------------------------------------------------------------ reflection helpers

    private static object? Field(object target, string name)
    {
        try
        {
            for (var type = target.GetType(); type != null; type = type.BaseType)
            {
                if (type.GetField(name, Hidden | BindingFlags.DeclaredOnly) is { } field) return field.GetValue(target);
            }
        }
        catch
        {
            // Treated as absent.
        }
        return null;
    }

    private static object? Property(object target, string name)
    {
        try
        {
            for (var type = target.GetType(); type != null; type = type.BaseType)
            {
                if (type.GetProperty(name, Hidden | BindingFlags.DeclaredOnly) is { } property) return property.GetValue(target);
            }
        }
        catch
        {
            // Treated as absent.
        }
        return null;
    }
}
