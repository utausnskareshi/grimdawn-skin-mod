using System.Globalization;
using System.Resources;
using System.Windows.Markup;

namespace GDSkinStudio.Localization;

/// <summary>
/// 画面の文言（Strings.resx。既定は日本語）。英語は Strings.en.resx（衛星アセンブリ en\GDSkinStudio.resources.dll）で、
/// 設定の表示言語で切り替える（再起動の後に効く）。キーと書式の穴（{0} など）は 2 つの resx で同じにする（tests/test_strings_en.py）。
/// コード: L.T("Key")、書式つき: L.F("Key", 引数...)。XAML: {l:Tr Key}。
/// </summary>
public static class L
{
    private static readonly ResourceManager Rm = new("GDSkinStudio.Localization.Strings", typeof(L).Assembly);

    public static string T(string key) => Rm.GetString(key, CultureInfo.CurrentUICulture) ?? $"[{key}]";

    public static string? TryT(string key) => Rm.GetString(key, CultureInfo.CurrentUICulture);

    public static string F(string key, params object?[] args)
    {
        try { return string.Format(CultureInfo.CurrentCulture, T(key), args); }
        catch (FormatException) { return T(key); }
    }

    /// <summary>設定の値を選べる表示言語（Strings.resx = 日本語、Strings.en.resx = 英語。2026-10-17 追加機能 SF08）にそろえる（空・未知の値・"en-US" などは "ja" か "en" に。既定は日本語）。</summary>
    public static string NormalizeLanguage(string? lang)
    {
        var s = (lang ?? "").Trim().ToLowerInvariant();
        return s == "en" || s.StartsWith("en-", StringComparison.Ordinal) ? "en" : "ja";
    }

    /// <summary>今の表示言語（"ja" / "en"）。バックエンドに渡す（CLIP の注意文を表示言語で出す。2026-10-18 未決定 #2）。</summary>
    public static string Language => NormalizeLanguage(CultureInfo.CurrentUICulture.Name);

    /// <summary>表示言語を設定する（"ja" / "en"）。ほかの値は日本語になる（ui_settings.json を手で書き換えても起動できるように）。</summary>
    public static void SetLanguage(string? lang)
    {
        var c = CultureInfo.GetCultureInfo(NormalizeLanguage(lang));
        CultureInfo.CurrentUICulture = c;
        CultureInfo.DefaultThreadCurrentUICulture = c;
    }
}

[MarkupExtensionReturnType(typeof(string))]
public sealed class TrExtension : MarkupExtension
{
    public TrExtension() { }
    public TrExtension(string key) { Key = key; }

    [ConstructorArgument("key")]
    public string Key { get; set; } = "";

    public override object ProvideValue(IServiceProvider serviceProvider) => L.T(Key);
}
