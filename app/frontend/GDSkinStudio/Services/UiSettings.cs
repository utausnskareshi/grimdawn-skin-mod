using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;
using GDSkinStudio.Localization;

namespace GDSkinStudio.Services;

/// <summary>画面の設定（ui_settings.json。表示言語・Python の場所・ウィンドウ）。</summary>
public sealed class UiSettings
{
    public string Language { get; set; } = "ja";
    public string PythonPath { get; set; } = "";
    public double? WindowLeft { get; set; }
    public double? WindowTop { get; set; }
    public double WindowWidth { get; set; } = 1360;
    public double WindowHeight { get; set; } = 860;
    public bool WindowMaximized { get; set; }
    /// <summary>装備一覧に「文章で見た目を作り直す」を出す（2026-10-17 追加機能 SF10。生成の詳細設定。既定オフ）。</summary>
    public bool TextVariantEnabled { get; set; }

    /// <summary>
    /// 読み込みで既定値に戻した理由の種類（read: ファイルを読めない / json: JSON として読めない / items: 型の合わない項目がある）。
    /// 問題が無ければ null。Load は AppLog.Init の前に呼ばれるので、ログには App が書く（Log_App_UiSettings_&lt;種類&gt;）。
    /// </summary>
    [JsonIgnore] public string? LoadIssue { get; private set; }
    /// <summary>LoadIssue の詳細（例外の文、または既定値に戻した項目名）。</summary>
    [JsonIgnore] public string? LoadDetail { get; private set; }
    /// <summary>元のファイルを写した先（ui_settings.json.bad-&lt;日時&gt;）。写していなければ null。</summary>
    [JsonIgnore] public string? LoadBackup { get; private set; }
    /// <summary>ファイルはあるのに読めなかった（共有違反など）。既定値で動き、保存の前に読み直す。</summary>
    [JsonIgnore] public bool ReadFailed { get; private set; }
    // 読み込んだ時点の値（ReadFailed のとき、保存の前に、この回に変えた項目だけをファイルの内容に重ねるため）
    private UiSettings? _loaded;

    private static readonly JsonSerializerOptions Opt = new() { WriteIndented = true };
    // 手で直したファイルの末尾のカンマ・コメントは受け付ける（Loc.SetLanguage の注釈のとおり、手での書き換えを想定する）
    private static readonly JsonDocumentOptions ReadOpt = new() { AllowTrailingCommas = true, CommentHandling = JsonCommentHandling.Skip };

    /// <summary>
    /// 設定を読む（例外は投げない）。以前は 1 項目の型の誤り・null・末尾のカンマ・一時的な読み取りの失敗でも黙って全項目を
    /// 既定値にし、閉じるときの保存で表示言語・Python の場所などが失われた（"PythonPath": null では設定画面の保存が NRE。
    /// 2026-10-27 R17A-05・R17B-03・X6X-07）。バックエンドの設定（settings.py。点検 R09B-04）と同じく、読み取りの失敗は
    /// 少し待って読み直し、型の合わない項目だけを既定値に戻して、元のファイルを .bad-&lt;日時&gt; に写す。
    /// </summary>
    public static UiSettings Load(string path)
    {
        if (!File.Exists(path)) return Loaded(new UiSettings());
        string? text = null;
        Exception? readError = null;
        for (int i = 0; i < 3 && text == null; i++)
        {
            try { text = File.ReadAllText(path); }
            catch (Exception e)
            {
                readError = e;
                if (e is not (IOException or UnauthorizedAccessException)) break;
                if (i < 2) Thread.Sleep(200);
            }
        }
        if (text == null)
            return Loaded(new UiSettings { ReadFailed = true, LoadIssue = "read", LoadDetail = readError!.GetType().Name + ": " + readError.Message });
        var s = Parse(text, out var issue, out var detail);
        if (issue != null)
        {
            s.LoadIssue = issue;
            s.LoadDetail = detail;
            s.LoadBackup = Backup(path);
        }
        return Loaded(s);
    }

    private static UiSettings Loaded(UiSettings s)
    {
        s._loaded = (UiSettings)s.MemberwiseClone();
        return s;
    }

    /// <summary>JSON の文字列 → 設定。型の合わない項目（null を含む）だけ既定値のまま。全体が読めなければ全て既定値。</summary>
    internal static UiSettings Parse(string text, out string? issue, out string? detail)
    {
        var s = new UiSettings();
        var bad = new List<string>();
        try
        {
            using var doc = JsonDocument.Parse(text, ReadOpt);
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
            {
                issue = "json";
                detail = "root: " + doc.RootElement.ValueKind;
                return s;
            }
            foreach (var p in doc.RootElement.EnumerateObject())
            {
                var v = p.Value;
                bool ok = true;
                switch (p.Name)
                {
                    case nameof(Language): if (ok = v.ValueKind == JsonValueKind.String) s.Language = v.GetString()!; break;
                    case nameof(PythonPath): if (ok = v.ValueKind == JsonValueKind.String) s.PythonPath = v.GetString()!; break;
                    case nameof(WindowLeft): if (ok = TryPosition(v, out var l)) s.WindowLeft = l; break;
                    case nameof(WindowTop): if (ok = TryPosition(v, out var t)) s.WindowTop = t; break;
                    case nameof(WindowWidth): if (ok = TrySize(v, out var w)) s.WindowWidth = w; break;
                    case nameof(WindowHeight): if (ok = TrySize(v, out var h)) s.WindowHeight = h; break;
                    case nameof(WindowMaximized): if (ok = v.ValueKind is JsonValueKind.True or JsonValueKind.False) s.WindowMaximized = v.GetBoolean(); break;
                    case nameof(TextVariantEnabled): if (ok = v.ValueKind is JsonValueKind.True or JsonValueKind.False) s.TextVariantEnabled = v.GetBoolean(); break;
                }
                if (!ok) bad.Add(p.Name);
            }
        }
        catch (JsonException e)
        {
            issue = "json";
            detail = e.Message;
            return new UiSettings();
        }
        issue = bad.Count > 0 ? "items" : null;
        detail = bad.Count > 0 ? string.Join(", ", bad) : null;
        return s;
    }

    // 窓の位置は null（まだ決まっていない）か有限の数
    private static bool TryPosition(JsonElement v, out double? value)
    {
        value = null;
        if (v.ValueKind == JsonValueKind.Null) return true;
        if (v.ValueKind != JsonValueKind.Number || !v.TryGetDouble(out var d) || !double.IsFinite(d)) return false;
        value = d;
        return true;
    }

    // 窓の大きさは正の有限の数
    private static bool TrySize(JsonElement v, out double value)
    {
        value = 0;
        return v.ValueKind == JsonValueKind.Number && v.TryGetDouble(out value) && double.IsFinite(value) && value > 0;
    }

    /// <summary>読めなかった・型の合わない項目のあった元のファイルを .bad-&lt;日時&gt; に写す（同じ内容の写しがあれば写さない）。失敗は無視する。</summary>
    private static string? Backup(string path)
    {
        try
        {
            var raw = File.ReadAllBytes(path);
            var dir = Path.GetDirectoryName(path)!;
            foreach (var p in Directory.EnumerateFiles(dir, Path.GetFileName(path) + ".bad-*"))
                if (File.ReadAllBytes(p).AsSpan().SequenceEqual(raw)) return null;
            var dest = path + ".bad-" + DateTime.Now.ToString("yyyyMMdd-HHmmss", CultureInfo.InvariantCulture);
            File.Copy(path, dest, false);
            return dest;
        }
        catch (Exception) { return null; }
    }

    public void Save(string path)
    {
        if (ReadFailed && !MergeUnread(path)) return;
        var tmp = path + ".tmp";
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(tmp, JsonSerializer.Serialize(this, Opt));
            File.Move(tmp, path, true);
            _loaded = (UiSettings)MemberwiseClone();
        }
        catch (Exception e)
        {
            AppLog.Warn(L.F("Log_App_UiSettingsSaveFailed", e.Message));
            try { File.Delete(tmp); } catch (Exception) { /* 書きかけの一時ファイルを残さない（R20B-05）。消せなければそのまま */ }
        }
    }

    /// <summary>
    /// 起動時に読めなかったファイルを、既定値で上書きする前に読み直す（X6X-07）。読めたら、この回に変えた項目だけをその内容に
    /// 重ねる（保存してよい）。読めないままなら、表示言語・Python の場所・SF10 の設定を変えていないとき（閉じるときの窓の位置・
    /// 大きさだけの保存）は元のファイルを残す（false）。
    /// </summary>
    private bool MergeUnread(string path)
    {
        if (!File.Exists(path))
        {
            ReadFailed = false;
            return true;
        }
        var now = Load(path);
        var old = _loaded ?? new UiSettings();
        if (now.ReadFailed)
        {
            if (Language != old.Language || PythonPath != old.PythonPath || TextVariantEnabled != old.TextVariantEnabled) return true;
            AppLog.Warn(L.F("Log_App_UiSettingsKeptUnread", now.LoadDetail));
            return false;
        }
        if (Language == old.Language) Language = now.Language;
        if (PythonPath == old.PythonPath) PythonPath = now.PythonPath;
        if (WindowLeft == old.WindowLeft) WindowLeft = now.WindowLeft;
        if (WindowTop == old.WindowTop) WindowTop = now.WindowTop;
        if (WindowWidth == old.WindowWidth) WindowWidth = now.WindowWidth;
        if (WindowHeight == old.WindowHeight) WindowHeight = now.WindowHeight;
        if (WindowMaximized == old.WindowMaximized) WindowMaximized = now.WindowMaximized;
        if (TextVariantEnabled == old.TextVariantEnabled) TextVariantEnabled = now.TextVariantEnabled;
        ReadFailed = false;
        return true;
    }
}
