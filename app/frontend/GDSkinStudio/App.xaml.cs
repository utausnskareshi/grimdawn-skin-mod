using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Windows;
using System.Windows.Threading;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using GDSkinStudio.ViewModels;
using GDSkinStudio.Views;
using Microsoft.Web.WebView2.Core;

namespace GDSkinStudio;

/// <summary>
/// 起動: 引数 --data-dir &lt;フォルダー&gt;（既定 %LOCALAPPDATA%\GrimDawnSkinMOD）。バックエンドの起動・終了は MainViewModel。
/// </summary>
public partial class App : Application
{
    public static AppPaths Paths { get; private set; } = null!;
    public static UiSettings Ui { get; private set; } = null!;
    public static MainViewModel Shell { get; private set; } = null!;

    private static Task<CoreWebView2Environment>? _webViewEnv;

    /// <summary>WebView2 の環境（全てのプレビューで共有。ユーザーデータは データフォルダー\webview）。</summary>
    public static Task<CoreWebView2Environment> WebViewEnvironment =>
        _webViewEnv ??= CoreWebView2Environment.CreateAsync(null, Paths.WebViewDataDir);

    /// <summary>
    /// 初期化に失敗した環境や、ブラウザーのプロセスが終わった環境を捨てる（次の WebViewEnvironment で作り直す）。
    /// 2 つのプレビューが同じ環境で失敗したときに作り直しが 1 回で済むよう、まだ同じ環境を持っているときだけ捨てる。
    /// </summary>
    public static void DiscardWebViewEnvironment(Task<CoreWebView2Environment>? env)
    {
        if (env != null && ReferenceEquals(_webViewEnv, env)) _webViewEnv = null;
    }

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        // 想定外のエラーの受け口は最初に登録する（以前はデータフォルダーの準備の後で、その失敗は案内なしに落ちた。
        // 2026-10-20 レビュー R17B-01）
        DispatcherUnhandledException += OnUnhandled;
        string? dataDir = null;
        for (int i = 0; i < e.Args.Length; i++)
            if (e.Args[i] == "--data-dir" && i + 1 < e.Args.Length) dataDir = e.Args[++i];
        try { Paths = new AppPaths(dataDir); }
        catch (Exception ex)
        {
            // パスの形が正しくない（Path.GetFullPath の例外）
            ExitDataDirError(dataDir ?? Environment.GetEnvironmentVariable("GDSKIN_HOME") ?? "", ex.Message);
            return;
        }
        Ui = UiSettings.Load(Paths.UiSettingsPath);
        L.SetLanguage(Ui.Language);
        // データフォルダー（ログのフォルダー）を作れない（外したドライブ・誤ったパス・アクセス権）ときは、案内を出して終了する
        if (!AppLog.Init(Paths.LogsDir, out var dirError))
        {
            ExitDataDirError(Paths.DataHome, dirError);
            return;
        }
        AppLog.Info(L.F("Log_App_Started", typeof(App).Assembly.GetName().Version, Paths.DataHome));
        // 読めなかった・既定値に戻した項目のあった ui_settings.json は、ログが使えるようになってから記録する（Load は
        // AppLog.Init の前。以前は理由をどこにも残さなかった。2026-10-27 R17B-03・X6X-07）
        if (Ui.LoadIssue != null)
            AppLog.Warn(L.F("Log_App_UiSettings_" + Ui.LoadIssue, Ui.LoadDetail, Ui.LoadBackup ?? L.T("Log_App_UiSettingsNoBackup")));
        HighContrast.Init(this);   // ハイコントラストなら独自の色をシステムの色に（最初の窓を作る前に）
        if (!AcquireInstance(Paths.DataHome))
        {
            AppLog.Warn(L.T("Log_App_AlreadyRunning"));
            // 主の窓が無いので、タスクバーに出す（他の窓の後ろに隠れても見つけられるように）
            new MessageDialog(new DialogSpec
            {
                Kind = DialogKind.Warning, Title = L.T("App_AlreadyRunningTitle"), Message = L.T("App_AlreadyRunningMessage"),
            }) { ShowInTaskbar = true, WindowStartupLocation = WindowStartupLocation.CenterScreen }.ShowDialog();
            Shutdown();
            return;
        }

        Shell = new MainViewModel(Paths);
        var w = new MainWindow { DataContext = Shell };
        if (Ui.WindowLeft is double l && Ui.WindowTop is double t &&
            l > SystemParameters.VirtualScreenLeft - 50 && t > SystemParameters.VirtualScreenTop - 50 &&
            l < SystemParameters.VirtualScreenWidth - 100 && t < SystemParameters.VirtualScreenHeight - 100)
        {
            w.WindowStartupLocation = WindowStartupLocation.Manual;
            w.Left = l;
            w.Top = t;
        }
        w.Width = Math.Max(1000, Ui.WindowWidth);
        w.Height = Math.Max(680, Ui.WindowHeight);
        // 大きさと位置は、表示するモニターの作業領域に収める（作業領域の低い画面で上や下にはみ出さないように）。
        // 窓が作られた時点（位置が決まる前後のどちらでも効くよう、表示の後にももう一度）。最大化で起動したときは
        // ここでは大きさだけを収め、位置は最大化を解除したとき（StateChanged）に収める
        w.SourceInitialized += (_, _) => WorkArea.KeepInside(w);
        // 表示の後は、最大化しているときは呼ばない（最大化中の Width・Height は最大化の大きさなので、作業領域まで下げると
        // 元に戻したときの大きさが作業領域いっぱいになる。閉じるときに RestoreBounds を残すため。2026-10-20 レビュー R17A-04）
        w.Loaded += (_, _) => { if (w.WindowState == WindowState.Normal) WorkArea.KeepInside(w); };
        if (Ui.WindowMaximized) w.WindowState = WindowState.Maximized;
        var lastState = w.WindowState;
        w.StateChanged += (_, _) =>
        {
            // 最大化を解除したときだけ（最小化から戻したときは、利用者が置いた位置を変えない）
            if (w.WindowState == WindowState.Normal && lastState == WindowState.Maximized) WorkArea.KeepInside(w);
            lastState = w.WindowState;
        };
        MainWindow = w;
        w.Show();
        _ = Shell.StartAsync();
    }

    /// <summary>データフォルダーを使えないときの案内を出して終了する（ログには書けないので、AppLog を経由しない）。</summary>
    private void ExitDataDirError(string path, string reason)
    {
        HighContrast.Init(this);
        ShutdownMode = ShutdownMode.OnExplicitShutdown;    // ダイアログ（最初の窓）を閉じたときの終了コード 0 にしない
        new MessageDialog(new DialogSpec
        {
            Kind = DialogKind.Error, Title = L.T("App_DataDirErrorTitle"),
            Message = L.F("App_DataDirErrorMessage", AppLog.Redact(path)), Detail = AppLog.Redact(reason),
        }) { ShowInTaskbar = true, WindowStartupLocation = WindowStartupLocation.CenterScreen }.ShowDialog();
        Shutdown(1);
    }

    // 多重起動の防止（2026-10-17 監査 C105）。同じデータフォルダーで 2 つのアプリ（とバックエンド）が動くと、導入の記録・
    // ビルドの後始末・下書きを互いに壊しうる。データフォルダーごとの名前付き Mutex（--data-dir が違えば同時に動ける）
    private static Mutex? _instanceMutex;
    private static bool _ownsInstance;
    /// <summary>前のアプリの終了（バックエンドの終了の待ちを含む）を待つ秒数。</summary>
    private static readonly TimeSpan InstanceWait = TimeSpan.FromSeconds(10);

    private static bool AcquireInstance(string dataHome)
    {
        string key;
        try { key = Path.TrimEndingDirectorySeparator(Path.GetFullPath(dataHome)).ToUpperInvariant(); }
        catch (Exception) { key = dataHome.ToUpperInvariant(); }
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key)))[..16];
        try
        {
            _instanceMutex = new Mutex(false, @"Local\GDSkinStudio-" + hash);
            try { _ownsInstance = _instanceMutex.WaitOne(InstanceWait); }
            catch (AbandonedMutexException) { _ownsInstance = true; }     // 前のアプリが異常終了した（取得できている）
        }
        catch (Exception ex)
        {
            // Mutex を使えない環境では確かめずに続ける（バックエンドのデータフォルダーのロックが二重の備え）
            AppLog.Warn(L.F("Log_App_InstanceCheckFailed", ex.Message));
            return true;
        }
        return _ownsInstance;
    }

    /// <summary>Windows の終了（サインアウト・シャットダウン・再起動）で閉じている（MainWindow は閉じる前の確認と待ちを行わない）。</summary>
    public static bool IsSessionEnding { get; private set; }

    /// <summary>
    /// Windows の終了では、WPF は窓の Closing の取り消しを無視して閉じるので、MainWindow.OnClosing の await の後（UI 設定の
    /// 保存・バックエンドの終了・取り消せないジョブの待ち）は実行されない。以前は窓の位置・大きさが失われ、導入などの途中で
    /// バックエンドが強制終了された（2026-10-27 R17B-02・R17A-04）。ここで同期に UI 設定を保存し、実行中の取り消せないジョブが
    /// あれば終了を取り消して、終わるまで Windows に理由を出す。利用者が「それでも終了」を選んだときや強制の再起動では、
    /// Windows がアプリを終了させるので守れない。取り消さないときは、従来どおりバックエンドは Job Object で終了する。
    /// </summary>
    protected override void OnSessionEnding(SessionEndingCancelEventArgs e)
    {
        base.OnSessionEnding(e);
        if (e.Cancel || Shell == null || MainWindow is not MainWindow w) return;
        try
        {
            w.CaptureBounds();
            Ui.Save(Paths.UiSettingsPath);
            var running = Shell.UncancellableJobLabels(TimeSpan.FromSeconds(2));
            if (running.Count > 0)
            {
                e.Cancel = true;
                AppLog.Warn(L.F("Log_App_SessionEndBlocked", e.ReasonSessionEnding, string.Join(", ", running)));
                _ = w.BlockSessionEndAsync(running);
                return;
            }
            IsSessionEnding = true;
            AppLog.Info(L.F("Log_App_SessionEnding", e.ReasonSessionEnding));
        }
        catch (Exception ex)
        {
            // 終了の確認で想定外のエラーを出さない（終了は止めない）
            AppLog.Error(L.F("Log_App_CloseError", ex));
            if (!e.Cancel) IsSessionEnding = true;
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        try { if (_ownsInstance) _instanceMutex?.ReleaseMutex(); }
        catch (Exception) { }
        _instanceMutex?.Dispose();
        base.OnExit(e);
    }

    private void OnUnhandled(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        AppLog.Error(L.F("Log_App_Unhandled", e.Exception));
        e.Handled = true;
        ShowError(e.Exception);
    }

    public static void ShowError(Exception e)
    {
        if (e is OperationCanceledException) return;
        AppLog.Error($"{e.GetType().Name}: {e.Message}");
        Dialogs.ShowError(ErrorText.From(e));
    }
}
