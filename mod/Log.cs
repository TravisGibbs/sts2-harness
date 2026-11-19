using Godot;

namespace SpireBridge;

internal static class Log
{
    private const string Prefix = "[SpireBridge] ";

    public static void Info(string message) => GD.Print(Prefix + message);

    public static void Error(string message) => GD.PrintErr(Prefix + message);
}
