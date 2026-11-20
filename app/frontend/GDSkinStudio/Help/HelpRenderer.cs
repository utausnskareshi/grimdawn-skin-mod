using System.Windows;
using System.Windows.Documents;
using System.Windows.Media;

namespace GDSkinStudio.Help;

/// <summary>
/// ヘルプの 1 項目を FlowDocument にする（選んで写せる文。見出し・段落・一覧・注意の枠・コード・項目へのリンク）。
/// 検索の語は背景の色で示し、その Run を marks に返す（画面が最初の 1 つまでスクロールする）。
/// 色は Styles.xaml のキー（ハイコントラストでは HighContrast.cs がシステムの色に置き換える）。
/// </summary>
public static class HelpRenderer
{
    private const double BodySize = 14;
    private static readonly FontFamily Mono = new("Cascadia Mono, Consolas");

    public static FlowDocument Build(HelpSection section, string? query, Action<string> onLink, FrameworkElement host,
                                     out List<TextElement> marks)
    {
        var r = new Ctx(host, query, onLink);
        var doc = new FlowDocument
        {
            FontFamily = TextElement.GetFontFamily(host),
            FontSize = BodySize,
            LineHeight = BodySize * 1.6,
            TextAlignment = TextAlignment.Left,
            PagePadding = new Thickness(22, 18, 22, 24),
            Foreground = r.Brush("LabelFg", Brushes.Black),
            Background = r.Brush("CardBg", Brushes.White),
        };
        var title = new Paragraph { FontSize = 21, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 12) };
        r.AddText(title.Inlines, section.Title, false, false);
        doc.Blocks.Add(title);

        var blocks = section.Blocks;
        for (int i = 0; i < blocks.Count;)
        {
            var b = blocks[i];
            switch (b.Kind)
            {
                case HelpBlockKind.Heading2:
                case HelpBlockKind.Heading3:
                {
                    bool h2 = b.Kind == HelpBlockKind.Heading2;
                    var p = new Paragraph
                    {
                        FontSize = h2 ? 17 : 15, FontWeight = FontWeights.SemiBold,
                        Margin = h2 ? new Thickness(0, 18, 0, 6) : new Thickness(0, 12, 0, 4),
                    };
                    r.AddLine(p.Inlines, b.Lines[0]);
                    doc.Blocks.Add(p);
                    i++;
                    break;
                }
                case HelpBlockKind.Paragraph:
                    doc.Blocks.Add(r.Para(b, new Thickness(0, 0, 0, 10)));
                    i++;
                    break;
                case HelpBlockKind.Note:
                case HelpBlockKind.Warning:
                {
                    bool warn = b.Kind == HelpBlockKind.Warning;
                    var box = new Section
                    {
                        Background = r.Brush(warn ? "WarnBg" : "InfoBg", Brushes.WhiteSmoke),
                        BorderBrush = r.Brush(warn ? "WarnBrush" : "Accent", Brushes.Gray),
                        BorderThickness = new Thickness(3, 0, 0, 0),
                        Padding = new Thickness(12, 8, 12, 8),
                        Margin = new Thickness(0, 2, 0, 12),
                    };
                    box.Blocks.Add(r.Para(b, new Thickness(0)));
                    doc.Blocks.Add(box);
                    i++;
                    break;
                }
                case HelpBlockKind.Code:
                {
                    var p = new Paragraph
                    {
                        FontFamily = Mono, FontSize = 13, LineHeight = 13 * 1.45,
                        Background = r.Brush("ChipBg", Brushes.WhiteSmoke), Padding = new Thickness(12, 8, 12, 8),
                        Margin = new Thickness(0, 2, 0, 12),
                    };
                    for (int k = 0; k < b.Lines.Count; k++)
                    {
                        if (k > 0) p.Inlines.Add(new LineBreak());
                        r.AddText(p.Inlines, b.Lines[k][0].Text, false, false);
                    }
                    doc.Blocks.Add(p);
                    i++;
                    break;
                }
                default:
                {
                    int before = i;
                    var list = r.List(blocks, ref i);
                    if (i > before) doc.Blocks.Add(list);
                    else
                    {
                        // 一覧を始められない項目（読み込みの決まりでは起きない）。止まらないように段落で出す
                        doc.Blocks.Add(r.Para(b, new Thickness(0, 0, 0, 10)));
                        i++;
                    }
                    break;
                }
            }
        }
        marks = r.Marks;
        return doc;
    }

    private sealed class Ctx(FrameworkElement host, string? query, Action<string> onLink)
    {
        public readonly List<TextElement> Marks = [];

        public Brush Brush(string key, Brush fallback) => host.TryFindResource(key) as Brush ?? fallback;

        public Paragraph Para(HelpBlock b, Thickness margin)
        {
            var p = new Paragraph { Margin = margin };
            for (int k = 0; k < b.Lines.Count; k++)
            {
                if (k > 0) p.Inlines.Add(new LineBreak());
                AddLine(p.Inlines, b.Lines[k]);
            }
            return p;
        }

        /// <summary>一覧（続けて並んだ項目。1 段目の種類が変わったら別の一覧。2 段目は直前の項目の中の一覧）。</summary>
        public List List(List<HelpBlock> blocks, ref int i)
        {
            var kind = blocks[i].Kind;
            var list = NewList(kind == HelpBlockKind.Numbered ? TextMarkerStyle.Decimal : TextMarkerStyle.Disc, 0);
            if (kind == HelpBlockKind.Numbered) list.StartIndex = blocks[i].Number;
            ListItem? last = null;
            List? sub = null;
            while (i < blocks.Count)
            {
                var b = blocks[i];
                if (b.Kind is not (HelpBlockKind.Bullet or HelpBlockKind.Numbered)) break;
                if (b.Level == 1)
                {
                    if (b.Kind != kind) break;
                    if (kind == HelpBlockKind.Numbered && last != null && b.Number == 1) break;   // 1 からの新しい一覧
                    last = new ListItem(Para(b, new Thickness(0, 0, 0, 4)));
                    list.ListItems.Add(last);
                    sub = null;
                }
                else
                {
                    if (last == null) break;
                    if (sub == null)
                    {
                        sub = NewList(TextMarkerStyle.Circle, 4);
                        last.Blocks.Add(sub);
                    }
                    sub.ListItems.Add(new ListItem(Para(b, new Thickness(0, 0, 0, 2))));
                }
                i++;
            }
            return list;
        }

        private static List NewList(TextMarkerStyle style, double top) => new()
        {
            MarkerStyle = style, Margin = new Thickness(0, top, 0, 10), Padding = new Thickness(24, 0, 0, 0),
        };

        public void AddLine(InlineCollection into, IReadOnlyList<HelpInline> line)
        {
            // 検索の語は行の全体（太字・コード・リンクの部分をつないだ文。目次の数え方の HelpSection.PlainText と同じ）で探し、
            // 各部分をその範囲で分けて強調する（「`mods` フォルダー」のように部分の境目をまたぐ語は、またいだ部分のそれぞれを強調する。
            // 以前は部分ごとに探したので、目次に数が出ても本文に強調が出なかった）。長さは HelpSearch が返す実際の長さ（全角・半角などで語と違う）
            var found = HelpSearch.Find(string.Concat(line.Select(x => x.Text)), query);
            int offset = 0;
            foreach (var x in line)
            {
                var ranges = Within(found, offset, x.Text.Length);
                offset += x.Text.Length;
                switch (x.Kind)
                {
                    case HelpInlineKind.Text:
                        AddText(into, x.Text, x.Bold, false, ranges);
                        break;
                    case HelpInlineKind.Code:
                        AddText(into, x.Text, x.Bold, true, ranges);
                        break;
                    case HelpInlineKind.Link:
                    {
                        var h = new Hyperlink { Foreground = Brush("Accent", Brushes.Blue) };
                        if (x.Bold) h.FontWeight = FontWeights.SemiBold;
                        AddText(h.Inlines, x.Text, false, false, ranges);
                        var target = x.Target!;
                        h.Click += (_, _) => onLink(target);
                        into.Add(h);
                        break;
                    }
                }
            }
        }

        /// <summary>行の中で見つかった範囲のうち、行の start 文字目から length 文字の部分に重なるところ（部分の先頭からの位置と長さ）。</summary>
        private static List<(int Start, int Length)> Within(List<(int Start, int Length)> found, int start, int length)
        {
            var res = new List<(int Start, int Length)>();
            foreach (var (s, len) in found)
            {
                int a = Math.Max(s, start), b = Math.Min(s + len, start + length);
                if (a < b) res.Add((a - start, b - a));
            }
            return res;
        }

        /// <summary>文を足す（検索の語の部分は強調の Run に分ける）。</summary>
        public void AddText(InlineCollection into, string text, bool bold, bool code) =>
            AddText(into, text, bold, code, HelpSearch.Find(text, query));

        /// <summary>文を足す（ranges の部分（位置の順で重ならない）を強調の Run に分ける）。</summary>
        private void AddText(InlineCollection into, string text, bool bold, bool code, List<(int Start, int Length)> ranges)
        {
            int pos = 0;
            foreach (var (start, len) in ranges)
            {
                if (start > pos) into.Add(Run(text[pos..start], bold, code));
                var m = Run(text.Substring(start, len), bold, code);
                m.Background = Brush("HelpMarkBg", Brushes.Yellow);
                m.Foreground = Brush("HelpMarkFg", Brushes.Black);
                into.Add(m);
                Marks.Add(m);
                pos = start + len;
            }
            if (pos < text.Length) into.Add(Run(text[pos..], bold, code));
        }

        private Run Run(string s, bool bold, bool code)
        {
            var run = new Run(s);
            if (bold) run.FontWeight = FontWeights.SemiBold;
            if (code)
            {
                run.FontFamily = Mono;
                run.Background = Brush("ChipBg", Brushes.WhiteSmoke);
            }
            return run;
        }
    }
}
