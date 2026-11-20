using System.Windows;
using System.Windows.Media;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;

namespace GDSkinStudio.Views;

/// <summary>
/// Windows のハイコントラストへの対応。Fluent の辞書は、起動時にハイコントラストなら WPF がハイコントラスト用に切り替えるが、
/// Styles.xaml の独自の色（固定の配色）はそのまま残り、カードの中の文字などが読めなくなる。そこで起動時にハイコントラストなら、
/// 独自の色をシステムの色（SystemColors）に置き換える（通常は今の固定の配色のまま）。
/// 起動した後の切り替えには WPF も辞書を差し替えないので、アプリの起動し直しを案内する。
/// </summary>
public static class HighContrast
{
    /// <summary>独自の色のキーと、ハイコントラストのときに使うシステムの色。</summary>
    private static readonly (string Key, Func<Brush> Brush)[] Map =
    [
        // 背景
        ("PageBg", () => SystemColors.WindowBrush), ("CardBg", () => SystemColors.WindowBrush), ("NavBg", () => SystemColors.WindowBrush),
        ("InfoBg", () => SystemColors.WindowBrush), ("WarnBg", () => SystemColors.WindowBrush), ("ErrBg", () => SystemColors.WindowBrush),
        ("ChipBg", () => SystemColors.WindowBrush), ("ThumbBg", () => SystemColors.WindowBrush),
        // 文字・枠（補助の文字も GrayText ではなく WindowText にする。GrayText は使えない項目の色）
        ("CardBorder", () => SystemColors.WindowTextBrush), ("NavFg", () => SystemColors.WindowTextBrush),
        ("NavTitle", () => SystemColors.WindowTextBrush), ("NavMuted", () => SystemColors.WindowTextBrush),
        ("NavStatus", () => SystemColors.WindowTextBrush), ("Muted", () => SystemColors.WindowTextBrush),
        ("LabelFg", () => SystemColors.WindowTextBrush), ("SubtleFg", () => SystemColors.WindowTextBrush),
        ("WarnText", () => SystemColors.WindowTextBrush), ("OkBrush", () => SystemColors.WindowTextBrush),
        ("WarnBrush", () => SystemColors.WindowTextBrush), ("ErrBrush", () => SystemColors.WindowTextBrush),
        // 選択・強調（背景は Highlight、その上の文字は HighlightText）
        ("NavSel", () => SystemColors.HighlightBrush), ("NavHover", () => SystemColors.HighlightBrush), ("Accent", () => SystemColors.HighlightBrush),
        ("NavSelFg", () => SystemColors.HighlightTextBrush), ("NavHoverFg", () => SystemColors.HighlightTextBrush),
        ("AccentFg", () => SystemColors.HighlightTextBrush),
        // ヘルプの検索で見つかった語（選択と同じ色の組み合わせ）
        ("HelpMarkBg", () => SystemColors.HighlightBrush), ("HelpMarkFg", () => SystemColors.HighlightTextBrush),
    ];

    private static bool _startedHighContrast;
    private static bool _notified;

    /// <summary>起動時（App の辞書を読んだ後、最初の窓を作る前）に呼ぶ。</summary>
    public static void Init(Application app)
    {
        _startedHighContrast = SystemParameters.HighContrast;
        if (_startedHighContrast)
        {
            int n = Apply(app.Resources);
            AppLog.Info(L.F("Log_App_HighContrast", n));
        }
        SystemParameters.StaticPropertyChanged += (_, e) =>
        {
            if (e.PropertyName != nameof(SystemParameters.HighContrast) || _notified) return;
            if (SystemParameters.HighContrast == _startedHighContrast) return;
            _notified = true;
            AppLog.Info(L.T("Log_App_HighContrastChanged"));
            // 設定の変更の通知を処理している最中なので、終わってから案内する
            _ = app.Dispatcher.InvokeAsync(() => Dialogs.Info(L.T("App_HighContrastChangedTitle"), L.T("App_HighContrastChanged")));
        };
    }

    /// <summary>
    /// resources（とその中の辞書）にある独自の色を、ハイコントラスト用のシステムの色に置き換える。置き換えた数を返す。
    /// 置き換えは、その色を StaticResource で使う窓・スタイルを作る前に行う（作った後のものには効かない）。
    /// </summary>
    public static int Apply(ResourceDictionary resources)
    {
        int n = 0;
        foreach (var (key, brush) in Map)
        {
            var d = Find(resources, key);
            if (d == null) continue;
            d[key] = brush();
            n++;
        }
        return n;
    }

    /// <summary>
    /// key を定義している辞書（自分の中、無ければ後に読んだ辞書から順に。検索の優先順と同じ）。Styles.xaml の中のスタイルは
    /// 同じ辞書の色を参照するので、上の辞書に足すのではなく、定義している辞書の値を置き換える必要がある。
    /// （ResourceDictionary.Contains は中の辞書まで探すので、自分の中だけを見るには Keys を使う）
    /// </summary>
    private static ResourceDictionary? Find(ResourceDictionary d, string key)
    {
        if (d.Keys.Cast<object>().Contains(key)) return d;
        for (int i = d.MergedDictionaries.Count - 1; i >= 0; i--)
        {
            var f = Find(d.MergedDictionaries[i], key);
            if (f != null) return f;
        }
        return null;
    }
}
