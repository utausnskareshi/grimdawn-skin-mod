using System.Text.Json.Nodes;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;

namespace GDSkinStudio.ViewModels;

/// <summary>
/// 起動オプション /basemods の確認の文（バックエンドの launch_option: 今の Steam ユーザーで判定。2026-10-17 機能 SF12）。
/// 本ツールは起動オプションを書き換えない（D-022）。
/// </summary>
public static class LaunchInfo
{
    public static string State(JsonNode? lo) => lo.Str("state") ?? "unknown";

    /// <summary>状態の文（Launch_*）に、判定した Steam ユーザー・他のユーザーの未設定・Steam を終了すると確認できる旨を添える。</summary>
    public static string Describe(JsonNode? lo)
    {
        var state = State(lo);
        var parts = new List<string> { L.T("Launch_" + state) };
        if (lo.Str("scope") == "current_user")
        {
            parts.Add(L.T(lo.Str("detected_by") == "active_user" ? "UD1_LaunchByActive" : "UD1_LaunchByLogin"));
            if ((lo.Long("others_without") ?? 0) > 0) parts.Add(L.F("UD1_LaunchOthers", lo.Long("others_without") ?? 0));
        }
        // Steam の起動中に設定した直後は、Steam を終了するまで設定のファイルに書かれないことがある（Steam が起動していないと
        // 分かっているときは出さない）
        bool steamStopped = lo?["steam_running"] is JsonValue v && v.TryGetValue<bool>(out var running) && !running;
        if (state != "all" && !steamStopped) parts.Add(L.T("UD1_LaunchApply"));
        return string.Join(" ", parts);
    }
}
