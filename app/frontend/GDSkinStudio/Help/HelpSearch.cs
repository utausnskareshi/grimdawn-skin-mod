using System.Globalization;

namespace GDSkinStudio.Help;

/// <summary>
/// ヘルプの検索。大文字・小文字、全角・半角、ひらがな・カタカナを区別しない（「mod」で「MOD」、「ろぐ」で「ログ」が見つかる）。
/// 目次の絞り込み（HelpViewModel）と本文の強調（HelpRenderer）で同じ決まりを使う。
/// </summary>
public static class HelpSearch
{
    private const CompareOptions Options = CompareOptions.IgnoreCase | CompareOptions.IgnoreWidth | CompareOptions.IgnoreKanaType;

    /// <summary>text の中の query の位置と長さ（重ならない順に）。query が空白だけなら無し。</summary>
    public static List<(int Start, int Length)> Find(string text, string? query)
    {
        var res = new List<(int, int)>();
        var q = (query ?? "").Trim();
        if (q.Length == 0 || text.Length == 0) return res;
        var ci = CultureInfo.InvariantCulture.CompareInfo;
        int pos = 0;
        while (pos < text.Length)
        {
            int i = ci.IndexOf(text.AsSpan(pos), q.AsSpan(), Options, out int len);
            if (i < 0 || len <= 0) break;
            res.Add((pos + i, len));
            pos += i + len;
        }
        return res;
    }

    public static int Count(string text, string? query) => Find(text, query).Count;
}
