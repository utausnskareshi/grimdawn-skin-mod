using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace GDSkinStudio.Help;

// ヘルプの文（Help\help_ja.md・help_en.md。EXE に埋め込む）を読む。WPF に依らない（E2ERunner も同じファイルを取り込んで、
// 画面の表示とファイルの中身を比べる）。書き方は docs/app_architecture.md の「ヘルプの画面」の節。決まりに合わない行は
// HelpFormatException（行番号つき）にする（tests/test_help_content.py が同じ決まりでファイルを確かめる）。

/// <summary>行の中の文字の種類。</summary>
public enum HelpInlineKind { Text, Code, Link }

/// <summary>行の中の文字。Text は Bold で太字、Code は等幅（ファイル名・コマンド）、Link はほかの項目への移動（Target は項目の id）。</summary>
public sealed record HelpInline(HelpInlineKind Kind, string Text, bool Bold = false, string? Target = null);

/// <summary>ブロックの種類。Bullet・Numbered は Level 1・2、Numbered は Number（1 から）。</summary>
public enum HelpBlockKind { Heading2, Heading3, Paragraph, Bullet, Numbered, Note, Warning, Code }

/// <summary>1 つのブロック。Lines は行ごとの文字（段落・注意の枠は複数行のことがある。コードは各行が Text 1 つ）。</summary>
public sealed class HelpBlock(HelpBlockKind kind, int level = 0, int number = 0)
{
    public HelpBlockKind Kind { get; } = kind;
    public int Level { get; } = level;
    public int Number { get; } = number;
    public List<IReadOnlyList<HelpInline>> Lines { get; } = [];
}

/// <summary>目次の 1 項目（「# 見出し {#id}」から次の「# 」の前まで）。</summary>
public sealed class HelpSection(string id, string title)
{
    public string Id { get; } = id;
    public string Title { get; } = title;
    public List<HelpBlock> Blocks { get; } = [];

    private string? _plain;
    /// <summary>検索用の文（見出しと、記号を除いた本文）。</summary>
    public string PlainText => _plain ??= BuildPlain();

    private string BuildPlain()
    {
        var sb = new StringBuilder(Title).Append('\n');
        foreach (var b in Blocks)
            foreach (var line in b.Lines)
            {
                foreach (var i in line) sb.Append(i.Text);
                sb.Append('\n');
            }
        return sb.ToString();
    }
}

public sealed class HelpFormatException(int line, string message) : Exception(message)
{
    /// <summary>1 から数えた行番号。</summary>
    public int Line { get; } = line;
}

public sealed partial class HelpDocument
{
    public IReadOnlyList<HelpSection> Sections { get; }

    private HelpDocument(List<HelpSection> sections) { Sections = sections; }

    public HelpSection? Find(string id) => Sections.FirstOrDefault(s => s.Id == id);

    /// <summary>EXE に埋め込んだヘルプ（lang は "ja" か "en"）。</summary>
    public static HelpDocument Load(string lang)
    {
        var name = ResourceName(lang);
        using var s = typeof(HelpDocument).Assembly.GetManifestResourceStream(name)
            ?? throw new FileNotFoundException(name);
        using var r = new StreamReader(s, new UTF8Encoding(false), detectEncodingFromByteOrderMarks: false);
        return Parse(r.ReadToEnd());
    }

    /// <summary>埋め込みの名前（GDSkinStudio.csproj の LogicalName と同じ）。</summary>
    public static string ResourceName(string lang) => "GDSkinStudio.Help.help_" + (lang == "en" ? "en" : "ja") + ".md";

    [GeneratedRegex(@"^# (?<title>\S.*?) \{#(?<id>[a-z][a-z0-9-]*)\}$")]
    private static partial Regex SectionRe();

    [GeneratedRegex(@"^(?<n>[1-9][0-9]*)\. (?<text>\S.*)$")]
    private static partial Regex NumberedRe();

    [GeneratedRegex(@"\G\[(?<text>[^\[\]`*]+)\]\(#(?<id>[a-z][a-z0-9-]*)\)")]
    private static partial Regex LinkRe();

    public static HelpDocument Parse(string text)
    {
        if (text.Length > 0 && text[0] == '﻿') text = text[1..];
        var lines = text.Replace("\r\n", "\n").Split('\n');
        var sections = new List<HelpSection>();
        var ids = new HashSet<string>(StringComparer.Ordinal);
        var links = new List<(int Line, string Target)>();
        HelpSection? cur = null;
        HelpBlock? open = null;            // 続けて行を足せる段落・注意の枠
        HelpBlock? lastItem = null;        // 直前の一覧の項目（2 段目の項目を付けられるか）
        // 番号つきの項目の次の番号。項目の間に段落・注意の枠・コードを挟んでも続きの番号を書ける（新しい一覧は 1 から）。
        // 見出しと項目の区切りで 1 に戻す。1 でも続きでもない番号（飛び・重なり）は書き間違いとして断る
        int expectNumber = 1;
        for (int i = 0; i < lines.Length; i++)
        {
            int ln = i + 1;
            var line = lines[i];
            if (line.Contains('\r')) throw new HelpFormatException(ln, "CR in line");
            if (line.Contains('\t')) throw new HelpFormatException(ln, "tab character");
            if (line.Length > 0 && char.IsWhiteSpace(line[^1])) throw new HelpFormatException(ln, "trailing space");
            if (line.Length == 0)
            {
                open = null;
                continue;
            }
            if (line == "```")
            {
                if (cur == null) throw new HelpFormatException(ln, "code block before the first section");
                var code = new HelpBlock(HelpBlockKind.Code);
                int j = i + 1;
                for (; j < lines.Length && lines[j].TrimEnd('\r') != "```"; j++)
                    code.Lines.Add([new HelpInline(HelpInlineKind.Text, lines[j].TrimEnd('\r'))]);
                if (j >= lines.Length) throw new HelpFormatException(ln, "code block is not closed");
                if (code.Lines.Count == 0) throw new HelpFormatException(ln, "empty code block");
                cur.Blocks.Add(code);
                i = j;
                open = null;
                lastItem = null;
                continue;
            }
            var m = SectionRe().Match(line);
            if (m.Success)
            {
                var id = m.Groups["id"].Value;
                if (!ids.Add(id)) throw new HelpFormatException(ln, "duplicate section id: " + id);
                cur = new HelpSection(id, m.Groups["title"].Value);
                sections.Add(cur);
                open = null;
                lastItem = null;
                expectNumber = 1;
                continue;
            }
            if (line.StartsWith('#') && !line.StartsWith("## ") && !line.StartsWith("### "))
                throw new HelpFormatException(ln, "bad heading (use '# Title {#id}', '## ' or '### ')");
            if (cur == null) throw new HelpFormatException(ln, "text before the first section");

            HelpBlock block;
            string body;
            bool listItem = false;
            if (line.StartsWith("### ")) { block = new HelpBlock(HelpBlockKind.Heading3); body = line[4..]; }
            else if (line.StartsWith("## ")) { block = new HelpBlock(HelpBlockKind.Heading2); body = line[3..]; }
            else if (line.StartsWith("- ")) { block = new HelpBlock(HelpBlockKind.Bullet, 1); body = line[2..]; listItem = true; }
            else if (line.StartsWith("  - "))
            {
                if (lastItem == null) throw new HelpFormatException(ln, "second-level item without a first-level item");
                block = new HelpBlock(HelpBlockKind.Bullet, 2);
                body = line[4..];
                listItem = true;
            }
            else if (line.StartsWith("!> ") || line.StartsWith("> "))
            {
                var kind = line[0] == '!' ? HelpBlockKind.Warning : HelpBlockKind.Note;
                body = line[(kind == HelpBlockKind.Warning ? 3 : 2)..];
                if (body.Length == 0 || char.IsWhiteSpace(body[0])) throw new HelpFormatException(ln, "empty or indented note");
                var inl = ParseInline(body, ln, links);
                if (open != null && open.Kind == kind) open.Lines.Add(inl);
                else
                {
                    open = new HelpBlock(kind);
                    open.Lines.Add(inl);
                    cur.Blocks.Add(open);
                }
                lastItem = null;
                continue;
            }
            else if (NumberedRe().Match(line) is { Success: true } nm)
            {
                int n = int.Parse(nm.Groups["n"].Value, CultureInfo.InvariantCulture);
                if (n != 1 && n != expectNumber) throw new HelpFormatException(ln, $"numbered item {n}, expected 1 or {expectNumber}");
                block = new HelpBlock(HelpBlockKind.Numbered, 1, n);
                body = nm.Groups["text"].Value;
                listItem = true;
            }
            else if (char.IsWhiteSpace(line[0]) || line.StartsWith('>') || line.StartsWith("!>") || line.StartsWith("* ") ||
                     line.StartsWith("+ ") || line.StartsWith('|') || line.StartsWith('<'))
                throw new HelpFormatException(ln, "unexpected indentation or marker (no tables, HTML or '*'/'+' bullets)");
            else
            {
                // 段落（続けて書いた行は同じ段落の改行）
                var inl = ParseInline(line, ln, links);
                if (open != null && open.Kind == HelpBlockKind.Paragraph) open.Lines.Add(inl);
                else
                {
                    open = new HelpBlock(HelpBlockKind.Paragraph);
                    open.Lines.Add(inl);
                    cur.Blocks.Add(open);
                }
                lastItem = null;
                continue;
            }
            if (body.Length == 0 || char.IsWhiteSpace(body[0])) throw new HelpFormatException(ln, "empty item or heading");
            block.Lines.Add(ParseInline(body, ln, links));
            cur.Blocks.Add(block);
            open = null;
            if (listItem)
            {
                if (block.Level == 1) lastItem = block;
                if (block.Kind == HelpBlockKind.Numbered) expectNumber = block.Number + 1;
            }
            else
            {
                // 見出し: 一覧と番号の続きを切る
                lastItem = null;
                expectNumber = 1;
            }
        }
        if (sections.Count == 0) throw new HelpFormatException(1, "no sections");
        foreach (var s in sections)
            if (s.Blocks.Count == 0) throw new HelpFormatException(1, "empty section: " + s.Id);
        foreach (var (ln, target) in links)
            if (!ids.Contains(target)) throw new HelpFormatException(ln, "link to an unknown section: " + target);
        return new HelpDocument(sections);
    }

    /// <summary>行の中の **太字**・`コード`・[文](#id) を読む（外部へのリンク・画像の書き方は誤り）。</summary>
    private static List<HelpInline> ParseInline(string s, int ln, List<(int, string)> links)
    {
        var res = new List<HelpInline>();
        var sb = new StringBuilder();
        bool bold = false;
        void Flush()
        {
            if (sb.Length > 0) res.Add(new HelpInline(HelpInlineKind.Text, sb.ToString(), bold));
            sb.Clear();
        }
        for (int i = 0; i < s.Length;)
        {
            char c = s[i];
            if (c == '`')
            {
                int end = s.IndexOf('`', i + 1);
                if (end < 0) throw new HelpFormatException(ln, "unclosed `code`");
                if (end == i + 1) throw new HelpFormatException(ln, "empty `code`");
                Flush();
                res.Add(new HelpInline(HelpInlineKind.Code, s[(i + 1)..end], bold));
                i = end + 1;
            }
            else if (c == '*' && i + 1 < s.Length && s[i + 1] == '*')
            {
                Flush();
                bold = !bold;
                i += 2;
            }
            else if (c == '[' && LinkRe().Match(s, i) is { Success: true } lm)
            {
                // 直前の「!」は画像の書き方（![文](…)）。画像は使わない決まりなので、「!」と項目へのリンクとして読まずに誤りにする
                if (i > 0 && s[i - 1] == '!') throw new HelpFormatException(ln, "image syntax ![...](...) is not supported");
                Flush();
                var target = lm.Groups["id"].Value;
                res.Add(new HelpInline(HelpInlineKind.Link, lm.Groups["text"].Value, bold, target));
                links.Add((ln, target));
                i += lm.Length;
            }
            // 項目へのリンクの形でない [文](…)（外部へのリンク・書き間違い）と画像は誤り（以前は「(#」の続くものだけを断り、
            // [文](https://…) や ![…](…) は記号のまま画面に出ていた）。コードの中の URL は上で読み飛ばすのでそのまま
            else if (c == '[' && s.IndexOf(']', i + 1) is int close && close > i && close + 1 < s.Length && s[close + 1] == '(')
                throw new HelpFormatException(ln, i > 0 && s[i - 1] == '!'
                    ? "image syntax ![...](...) is not supported"
                    : "bad or external link (only [text](#id) to a section; the text has no [ ] ` *)");
            else
            {
                sb.Append(c);
                i++;
            }
        }
        if (bold) throw new HelpFormatException(ln, "unclosed **bold**");
        Flush();
        if (res.Count == 0) throw new HelpFormatException(ln, "empty line content");
        return res;
    }
}
