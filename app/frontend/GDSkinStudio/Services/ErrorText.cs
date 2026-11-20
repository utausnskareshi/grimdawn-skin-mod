using System.Text.Json.Nodes;
using GDSkinStudio.Localization;

namespace GDSkinStudio.Services;

public sealed record UserError(string Code, string Title, string Message, string Hint, string? Detail);

/// <summary>
/// エラーコード → 利用者が読む文（計画書 12 章）。文はリソースの Error_&lt;code&gt;_Title / _Message / _Hint。
/// INVALID_INPUT などは params.reason ごとの文（Error_INVALID_INPUT_&lt;reason&gt;_Message・_Hint・_Title）があればそちらを使う。
/// </summary>
public static class ErrorText
{
    public static UserError From(Exception e)
    {
        if (e is ApiException a) return From(a.Code, ScreenReason(a), a.Message, a.Detail, a);
        if (e is BackendStartException b)
            return new UserError("BACKEND_UNAVAILABLE", L.T("Error_BACKEND_UNAVAILABLE_Title"),
                L.TryT($"Error_BACKEND_{b.Message}_Message") ?? L.T("Error_BACKEND_UNAVAILABLE_Message"),
                L.T("Error_BACKEND_UNAVAILABLE_Hint"), b.Detail);
        return new UserError("INTERNAL", L.T("Error_INTERNAL_Title"), L.T("Error_INTERNAL_Message"), L.T("Error_INTERNAL_Hint"),
            e.GetType().Name + ": " + e.Message);
    }

    public static UserError From(string code, string? reason, string? technical, string? detail, ApiException? a = null)
    {
        // 見出しも reason ごとの文（Error_<code>_<reason>_Title）があればそちらを使う（GAME_NOT_FOUND の
        // game_tool_missing は「Grim Dawn が見つかりません」にしない。2026-10-17 FG05-R2）
        string title = (reason != null ? L.TryT($"Error_{code}_{reason}_Title") : null)
                       ?? L.TryT($"Error_{code}_Title") ?? L.T("Error_INTERNAL_Title");
        string msg = (reason != null ? L.TryT($"Error_{code}_{reason}_Message") : null)
                     ?? L.TryT($"Error_{code}_Message") ?? L.T("Error_INTERNAL_Message");
        string hint = (reason != null ? L.TryT($"Error_{code}_{reason}_Hint") : null)
                      ?? L.TryT($"Error_{code}_Hint") ?? "";
        if (a != null)
        {
            // 文の中の {problems} などを params で埋める
            foreach (var kv in a.Params)
            {
                var v = AppLog.Redact(a.Param(kv.Key) ?? "");
                msg = msg.Replace("{" + kv.Key + "}", v);
                hint = hint.Replace("{" + kv.Key + "}", v);
            }
            // {field} は params.key（設定の項目の内部名 game_dir など）を画面の欄の名前にしたもの（path_not_absolute で
            // どの欄かを内部名でなく欄の名前で示す。2026-10-27 検証 D7_contracts-02）
            if (a.Param("key") is { } key)
            {
                var field = FieldLabel(key);
                msg = msg.Replace("{field}", field);
                hint = hint.Replace("{field}", field);
            }
        }
        var det = string.Join("\n", new[] { technical, detail }.Where(s => !string.IsNullOrWhiteSpace(s)));
        return new UserError(code, title, msg, hint, AppLog.Redact(det));
    }

    /// <summary>
    /// 文を引く reason。ふつうは params.reason（無ければ ImpliedReason）。INVALID_INPUT の existing_venv_rebuild で、版は同じで
    /// venv\Scripts\python.exe だけが無い（params.python_missing が true で venv_version == required）ときは、画面だけの reason
    /// existing_venv_rebuild_python_missing（「Python 3.11.9 を Python 3.11.9 で作り直す」と言わない。バックエンドの
    /// setup._venv_rebuild_error と同じ条件。python_missing は版が違うときも true になりうるので版の一致も見る。
    /// 2026-11-04 退行の確認 2 回目 RV_backend-01）。
    /// </summary>
    private static string? ScreenReason(ApiException a)
    {
        var reason = a.Param("reason") ?? ImpliedReason(a);
        if (a.Code == "INVALID_INPUT" && reason == "existing_venv_rebuild" && a.Param("python_missing") == "true"
            && a.Param("venv_version") == a.Param("required"))
            return "existing_venv_rebuild_python_missing";
        return reason;
    }

    /// <summary>
    /// reason の無いエラーのうち、params から事情が分かるものの reason。REFS_MISSING で不足は無く壊れたファイル（params.corrupt）だけが
    /// あるときは refs_corrupt（「基準メッシュがありません／まだ準備されていません」と出さない。2026-10-27 検証 D7_contracts-02）。
    /// 今のバックエンドは壊れているときに params.reason refs_corrupt を（開けないときは refs_unreadable を）自分で付けるので、
    /// これは reason を付けずに corrupt だけを返していた以前の版のバックエンドのための推定として残す。
    /// </summary>
    private static string? ImpliedReason(ApiException a)
    {
        static bool NonEmpty(ApiException a, string k) => a.Params.TryGetPropertyValue(k, out var v) && v is JsonArray arr && arr.Count > 0;
        if (a.Code == "REFS_MISSING" && NonEmpty(a, "corrupt") && !NonEmpty(a, "missing")) return "refs_corrupt";
        return null;
    }

    /// <summary>設定の項目の内部名（バックエンドの settings の _PATH_KEYS）→ 設定画面の欄の名前。知らない名前はそのまま。</summary>
    private static string FieldLabel(string key) => key switch
    {
        "game_dir" => L.T("Set_GameDir"),
        "install_target_dir" => L.T("Set_TargetDir"),
        "trellis_root" => L.T("Set_TrellisRoot"),
        _ => key,
    };
}
