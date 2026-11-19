using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;

namespace SpireBridge;

/// <summary>
/// Lists the methods a method calls, by reading its IL. A monster's move is a
/// private method whose effects (which status it applies, which card it adds)
/// are not exposed as data anywhere, but the calls it makes name them:
/// PowerCmd.Apply&lt;StrengthPower&gt; and so on. Reads only; nothing is executed.
/// Depends on nothing from the game, so it can be exercised outside it.
/// </summary>
internal static class IlScan
{
    private static readonly OpCode[] OneByte = new OpCode[256];
    private static readonly OpCode[] TwoByte = new OpCode[256];
    private static readonly bool[] KnownOne = new bool[256];
    private static readonly bool[] KnownTwo = new bool[256];

    private const BindingFlags Any = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

    static IlScan()
    {
        foreach (var field in typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static))
        {
            if (field.GetValue(null) is not OpCode op) continue;
            ushort value = unchecked((ushort)op.Value);
            if (op.Size == 1)
            {
                OneByte[value & 0xFF] = op;
                KnownOne[value & 0xFF] = true;
            }
            else if (value >> 8 == 0xFE)
            {
                TwoByte[value & 0xFF] = op;
                KnownTwo[value & 0xFF] = true;
            }
        }
    }

    /// <summary>
    /// Every method called (or taken as a delegate) by <paramref name="method"/>,
    /// following calls into the same class (helpers, lambdas, async bodies) up to
    /// <paramref name="depth"/> levels. Never throws; returns what it could read.
    /// </summary>
    public static List<MethodBase> Calls(MethodBase? method, int depth = 3)
    {
        var found = new List<MethodBase>();
        if (method == null) return found;
        try
        {
            var owner = Outermost(method.DeclaringType);
            Collect(method, owner, depth, found, []);
        }
        catch
        {
            // Partial results are still useful.
        }
        return found;
    }

    private static Type? Outermost(Type? type)
    {
        while (type?.DeclaringType != null) type = type.DeclaringType;
        return type;
    }

    private static void Collect(MethodBase method, Type? owner, int depth, List<MethodBase> found, HashSet<MethodBase> visited)
    {
        if (depth <= 0 || !visited.Add(method)) return;

        // An async or iterator method is only a stub; its code lives in MoveNext of a generated class.
        var body = method;
        try
        {
            if (method.GetCustomAttribute<StateMachineAttribute>() is { StateMachineType: { } machine } &&
                machine.GetMethod("MoveNext", Any) is { } moveNext)
            {
                body = moveNext;
            }
        }
        catch
        {
            // Read the stub itself.
        }

        byte[]? il;
        try { il = body.GetMethodBody()?.GetILAsByteArray(); }
        catch { return; }
        if (il == null) return;

        Type[]? typeArgs = null;
        Type[]? methodArgs = null;
        try
        {
            if (body.DeclaringType is { IsGenericType: true } generic) typeArgs = generic.GetGenericArguments();
            if (body is MethodInfo { IsGenericMethod: true } genericMethod) methodArgs = genericMethod.GetGenericArguments();
        }
        catch
        {
            // Resolve without generic context.
        }

        int i = 0;
        while (i < il.Length)
        {
            OpCode op;
            byte first = il[i++];
            if (first == 0xFE)
            {
                if (i >= il.Length || !KnownTwo[il[i]]) return;
                op = TwoByte[il[i++]];
            }
            else
            {
                if (!KnownOne[first]) return;
                op = OneByte[first];
            }

            switch (op.OperandType)
            {
                case OperandType.InlineNone:
                    break;
                case OperandType.ShortInlineBrTarget:
                case OperandType.ShortInlineI:
                case OperandType.ShortInlineVar:
                    i += 1;
                    break;
                case OperandType.InlineVar:
                    i += 2;
                    break;
                case OperandType.InlineI8:
                case OperandType.InlineR:
                    i += 8;
                    break;
                case OperandType.InlineSwitch:
                    if (i + 4 > il.Length) return;
                    i += 4 + 4 * (int)BitConverter.ToUInt32(il, i);
                    break;
                case OperandType.InlineMethod:
                    if (i + 4 > il.Length) return;
                    int token = BitConverter.ToInt32(il, i);
                    i += 4;
                    MethodBase? called = null;
                    try { called = body.Module.ResolveMethod(token, typeArgs, methodArgs); }
                    catch { /* unresolvable in this context; skip */ }
                    if (called == null) break;
                    found.Add(called);
                    // Follow helpers, lambdas and nested async bodies of the same class, but not
                    // property getters (they only compute numbers).
                    if (owner != null && !called.IsSpecialName && Outermost(called.DeclaringType) == owner)
                        Collect(called, owner, depth - 1, found, visited);
                    break;
                default:
                    // Branch targets, fields, strings, types, tokens, signatures, int32 and float32: 4 bytes.
                    i += 4;
                    break;
            }
        }
    }
}
