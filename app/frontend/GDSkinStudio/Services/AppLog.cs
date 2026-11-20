using System.Collections.ObjectModel;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows;
using GDSkinStudio.Localization;

namespace GDSkinStudio.Services;

public sealed record LogLine(DateTime Time, string Level, string Source, string Message)
{
    public string TimeText => Time.ToString("HH:mm:ss", CultureInfo.InvariantCulture);
    public bool IsError => Level == "ERROR";
    public bool IsWarning => Level == "WARN";
}

/// <summary>
/// 画面側のログ（logs\app-YYYYMMDD.log と、ログ画面に出す一覧）。利用者のフォルダー名は %USERPROFILE% に置き換える
/// （計画書 12 章。入力の文章・画像の内容はここに書かない）。
/// </summary>
public static class AppLog
{
    private static readonly object Lock = new();
    private static string? _dir;
    private static bool _writeFailed;   // ログ ファイルに書けないことを一覧で知らせた
    private static readonly string Home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);

    public static ObservableCollection<LogLine> Lines { get; } = new();

    /// <summary>
    /// ログのフォルダーを作る。作れないとき（外したドライブ・誤ったパス・アクセス権）は false と理由を返し、ファイルへの
    /// 記録は止める（画面の一覧には加える）。以前は例外のまま起動が止まった（2026-10-20 レビュー R17B-01）。
    /// </summary>
    public static bool Init(string logsDir, out string error)
    {
        try { Directory.CreateDirectory(logsDir); }
        catch (Exception e)
        {
            _dir = null;
            error = e.Message;
            return false;
        }
        _dir = logsDir;
        error = "";
        return true;
    }

    public static string Redact(string s)
    {
        if (string.IsNullOrEmpty(s) || Home.Length < 4) return s;
        return s.Replace(Home, "%USERPROFILE%", StringComparison.OrdinalIgnoreCase)
                .Replace(Home.Replace('\\', '/'), "%USERPROFILE%", StringComparison.OrdinalIgnoreCase);
    }

    public static void Info(string msg) => Write("INFO", "app", msg);
    public static void Warn(string msg) => Write("WARN", "app", msg);
    public static void Error(string msg) => Write("ERROR", "app", msg);

    /// <summary>バックエンドのジョブのログ（既に個人情報を除いたもの）を画面の一覧にだけ加える。</summary>
    public static void FromBackend(string level, string msg) => Add(new LogLine(DateTime.Now, level, "backend", msg));

    public static void Write(string level, string source, string msg)
    {
        msg = Redact(msg.Replace("\r", " ").Replace("\n", " | "));
        var line = new LogLine(DateTime.Now, level, source, msg);
        string? failure = null;
        lock (Lock)
        {
            if (_dir != null)
            {
                try
                {
                    File.AppendAllText(Path.Combine(_dir, "app-" + DateTime.Now.ToString("yyyyMMdd", CultureInfo.InvariantCulture) + ".log"),
                        DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz", CultureInfo.InvariantCulture) + $" {level,-5} {msg}\n", Encoding.UTF8);
                }
                catch (Exception e)
                {
                    // 読み取り専用のファイル・アクセス権（UnauthorizedAccessException は IOException ではない）などで書けなくても、
                    // ログの呼び出し元（エラーの表示・スレッドプールの処理）を落とさない。知らせるのは最初の 1 回だけ（R17B-01）
                    if (!_writeFailed) failure = e.Message;
                    _writeFailed = true;
                }
            }
        }
        Add(line);
        if (failure != null) Add(new LogLine(DateTime.Now, "WARN", "app", Redact(L.F("Log_App_LogFileWriteFailed", failure))));
    }

    private static void Add(LogLine line)
    {
        var app = Application.Current;
        void add()
        {
            Lines.Add(line);
            while (Lines.Count > 3000) Lines.RemoveAt(0);
        }
        if (app == null) return;
        if (app.Dispatcher.CheckAccess()) add(); else app.Dispatcher.BeginInvoke(add);
    }
}
