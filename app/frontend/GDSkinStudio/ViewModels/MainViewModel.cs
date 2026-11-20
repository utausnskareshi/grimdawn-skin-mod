using System.Diagnostics;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Input;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using GDSkinStudio.Views;

namespace GDSkinStudio.ViewModels;

public sealed class PageItem(string key, string title, string glyph, object viewModel)
{
    public string Key { get; } = key;
    public string Title { get; } = title;
    public string Glyph { get; } = glyph;
    public object ViewModel { get; } = viewModel;
}

/// <summary>画面全体: バックエンドの起動・停止・再起動、ページの切り替え、環境チェックの結果、閉じる前の確認。</summary>
public sealed class MainViewModel : ObservableObject
{
    /// <summary>閉じるときに取り消せないジョブを待つ上限。超えたら警告つきで「それでも閉じる」を聞く（2026-10-17 監査 C99）。</summary>
    private static readonly TimeSpan CloseWaitLimit = TimeSpan.FromMinutes(2);

    public AppPaths Paths { get; }
    public BackendHost Host { get; }
    public BackendClient? Client { get; private set; }

    public GenerateViewModel Generate { get; }
    public LibraryViewModel Library { get; }
    public ModViewModel Mod { get; }
    public LogViewModel Log { get; }
    public SettingsViewModel Settings { get; }
    public SetupViewModel Setup { get; }
    public HelpViewModel Help { get; }
    public IReadOnlyList<PageItem> Pages { get; }

    /// <summary>F1: ヘルプの画面を開く（どの画面からでも）。</summary>
    public ICommand OpenHelpCommand { get; }

    private PageItem _selectedPage;
    public PageItem SelectedPage
    {
        get => _selectedPage;
        set
        {
            // ナビの選択中の項目の Ctrl+クリックで ListBox が選択を外す（null）と、右側のページが全て重なって出た。null は
            // 受け付けない（ListBox の選択は MainWindow が今のページの項目に戻す。2026-10-20 レビュー R17A-02）
            if (value == null) return;
            if (Set(ref _selectedPage, value)) OnPageShown(value);
        }
    }

    private string _backendState = "starting";   // starting / ready / stopped / error
    public string BackendState
    {
        get => _backendState;
        private set
        {
            if (Set(ref _backendState, value))
            {
                if (value is not "ready") _notice = null;     // 停止・起動できないの文で置き換える
                OnPropertyChanged(nameof(BackendText));
                OnPropertyChanged(nameof(IsReady));
                OnPropertyChanged(nameof(ShowBackendBanner));
                // 「バックエンドを再起動」などの押せる状態をすぐに直す（入力が無いと WPF は確かめ直さない）
                CommandManager.InvalidateRequerySuggested();
            }
        }
    }
    public bool IsReady => BackendState == "ready";
    /// <summary>帯を出すか: 停止・起動できないとき、または接続中のままの注意（起動後の準備・ページの更新の失敗、終了の待ち）があるとき。</summary>
    public bool ShowBackendBanner => BackendState is "stopped" or "error" || _notice != null;
    public string BackendText => L.T("Backend_" + BackendState);

    private string _backendError = "";
    /// <summary>帯の文（停止・起動できない理由、または接続中のままの注意）。</summary>
    public string BackendError { get => _backendError; private set => Set(ref _backendError, value); }

    // 接続中のままの注意の種類（init:<手順> / page / closing）。null なら注意なし（2026-10-17 監査 C106）。起動後の準備の
    // 失敗は手順ごとの種類にし、その手順に当たる処理が後で成功したら消す（環境チェック・セットアップの状態。以前は解消した
    // 失敗の帯が残った。2026-10-27 R17A-01）。生成の画面の準備はやり直す経路が再起動だけなので消さない
    private string? _notice;

    private JsonNode? _environment;
    public JsonNode? Environment { get => _environment; private set { Set(ref _environment, value); OnPropertyChanged(nameof(GpuText)); } }
    public JsonObject? EnvSummary => Environment.Obj("summary");
    public string GpuText
    {
        get
        {
            var gpu = Environment.Arr("items").FirstOrDefault(i => i.Str("id") == "gpu");
            return gpu == null ? "" : (gpu.Str("status") == "ok" ? gpu.Obj("params").Str("name") ?? "" : L.T("Env_NoGpu"));
        }
    }

    private JsonNode? _pathsInfo;
    public JsonNode? PathsInfo { get => _pathsInfo; private set => Set(ref _pathsInfo, value); }

    public ICommand RestartBackendCommand { get; }

    public event Action? Ready;

    public MainViewModel(AppPaths paths)
    {
        Paths = paths;
        Host = new BackendHost(paths, () => App.Ui.PythonPath);
        Host.Exited += tail => Application.Current.Dispatcher.BeginInvoke(() =>
        {
            _notice = null;
            BackendError = L.T("Backend_ExitedMessage");
            BackendState = "stopped";
            OnPropertyChanged(nameof(ShowBackendBanner));
        });
        Generate = new GenerateViewModel(this);
        Library = new LibraryViewModel(this);
        Mod = new ModViewModel(this);
        Log = new LogViewModel(this);
        Settings = new SettingsViewModel(this);
        Setup = new SetupViewModel(this);
        Help = new HelpViewModel();
        Pages =
        [
            new PageItem("generate", L.T("Nav_Generate"), "", Generate),
            new PageItem("library", L.T("Nav_Library"), "", Library),
            new PageItem("mod", L.T("Nav_Mod"), "", Mod),
            new PageItem("log", L.T("Nav_Log"), "", Log),
            new PageItem("setup", L.T("Nav_Setup"), "", Setup),
            new PageItem("settings", L.T("Nav_Settings"), "", Settings),
            // ヘルプは設定の下（2026-11-05。バックエンドを使わないので、起動中・停止中でも読める）
            new PageItem("help", L.T("Nav_Help"), "", Help),
        ];
        _selectedPage = Pages[0];
        RestartBackendCommand = new AsyncCommand(RestartAsync, () => BackendState is "stopped" or "error");
        OpenHelpCommand = new RelayCommand(() => Navigate("help"));
    }

    public void Navigate(string key)
    {
        var p = Pages.FirstOrDefault(x => x.Key == key);
        if (p != null) SelectedPage = p;
    }

    private void OnPageShown(PageItem p)
    {
        if (!IsReady) return;
        var t = p.ViewModel switch
        {
            LibraryViewModel l => l.RefreshAsync(),
            ModViewModel m => m.RefreshAsync(),
            LogViewModel g => g.RefreshAsync(),
            SettingsViewModel s => s.RefreshAsync(false),
            SetupViewModel u => u.RefreshAsync(false),
            _ => Task.CompletedTask,
        };
        _ = ObservePageRefreshAsync(t, p.Key);
    }

    /// <summary>ページの更新を見届ける。失敗はログと帯に出す（以前は Task ごと捨てていた。2026-10-17 監査 C106）。</summary>
    private async Task ObservePageRefreshAsync(Task t, string page)
    {
        try
        {
            await t;
            ClearNotice("page");
            if (page == "setup") ClearNotice("init:setup");     // セットアップの画面の更新は setup/status を読み直す
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            AppLog.Warn(L.F("Log_App_PageRefreshFailed", page, e.GetType().Name, e.Message));
            if (IsReady) ShowNotice("page", L.F("App_PageRefreshFailed", ErrorText.From(e).Message));
        }
    }

    private void ShowNotice(string kind, string text)
    {
        _notice = kind;
        BackendError = text;
        OnPropertyChanged(nameof(ShowBackendBanner));
    }

    private void ClearNotice(string? kind = null)
    {
        if (_notice == null || (kind != null && _notice != kind)) return;
        _notice = null;
        BackendError = "";
        OnPropertyChanged(nameof(ShowBackendBanner));
    }

    public async Task StartAsync()
    {
        BackendState = "starting";
        _notice = null;
        BackendError = "";
        OnPropertyChanged(nameof(ShowBackendBanner));
        try
        {
            await Host.StartAsync();
            Client?.Dispose();
            Client = new BackendClient(Host.Port, Host.Token);
            PathsInfo = await Client.GetAsync("paths");
            PreviewControl.LibraryRoot = PathsInfo.Str("library");
        }
        catch (Exception e)
        {
            // バックエンドを起動できない（起動の直後の paths を読めないときを含む）。動いたままのプロセスを残さない
            // （以前は起動後の準備の失敗でも「起動できません」のまま、バックエンドが動き続けた。2026-10-17 監査 C106）
            Host.Kill();
            BackendState = "error";
            var err = ErrorText.From(e);
            BackendError = err.Message;
            AppLog.Error(L.F("Log_App_BackendStartFailed", e.Message + (e is BackendStartException b ? " " + b.Detail : "")));
            Dialogs.ShowError(err);
            return;
        }
        BackendState = "ready";
        // 起動後の準備。失敗してもバックエンドは動いているので「接続中」のまま、どの準備に失敗したかを帯に出す
        await InitStepAsync("environment", () => RefreshEnvironmentAsync(false));
        Ready?.Invoke();
        await InitStepAsync("generate", Generate.InitAsync);
        await InitStepAsync("setup", async () =>
        {
            // 初回起動（セットアップが完了も「後で」もされていない）ならセットアップの画面を開く（docs/packaging.md 4 章）
            var setup = await RequireClient().GetAsync("setup/status");
            if (setup.Bool("needs_setup") && SelectedPage.Key != "setup")
            {
                AppLog.Info(L.T("Log_App_OpenSetup"));
                Navigate("setup");
            }
            else
                OnPageShown(SelectedPage);
        });
    }

    private async Task InitStepAsync(string step, Func<Task> action)
    {
        try { await action(); }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            AppLog.Warn(L.F("Log_App_InitStepFailed", step, e.GetType().Name, e.Message));
            if (IsReady) ShowNotice("init:" + step, L.F("App_InitFailed", L.T("App_InitStep_" + step), ErrorText.From(e).Message));
        }
    }

    private async Task RestartAsync()
    {
        Host.Kill();
        await StartAsync();
    }

    public async Task RefreshEnvironmentAsync(bool force)
    {
        if (Client == null) return;
        Environment = await Client.GetAsync("environment" + (force ? "?refresh=1" : ""));
        OnPropertyChanged(nameof(EnvSummary));
        ClearNotice("init:environment");
    }

    /// <summary>バックエンドが使えるか（使えなければエラー表示）。</summary>
    public BackendClient RequireClient()
    {
        // 起動中は「バックエンドを再起動」（帯）が出ていないので、起動を待つ案内の文にする（reason starting。2026-10-27 R17A-02）
        if (Client == null || !IsReady)
            throw new ApiException("BACKEND_UNAVAILABLE", "backend not ready",
                parameters: BackendState == "starting" ? new JsonObject { ["reason"] = "starting" } : null);
        return Client;
    }

    // --- 閉じる（docs/app_architecture.md 3.4 節。2026-10-17 監査 C99） -------------------------------------------
    /// <summary>未完了（queued / running）のジョブ。バックエンドが使えない・応答しない（既定 5 秒）ときは null。</summary>
    private async Task<List<JsonNode>?> ActiveJobsAsync(TimeSpan? limit = null)
    {
        var c = Client;
        if (c == null || !IsReady) return null;
        try
        {
            using var cts = new CancellationTokenSource(limit ?? TimeSpan.FromSeconds(5));
            var arr = (await c.GetAsync("jobs?limit=1000", cts.Token)) as JsonArray;
            return arr?.Where(j => j.Str("state") is "queued" or "running").Select(j => j!).ToList() ?? [];
        }
        catch (Exception e) when (e is ApiException or OperationCanceledException)
        {
            AppLog.Warn(L.F("Log_App_JobsCheckFailed", e.Message));
            return null;
        }
    }

    /// <summary>実行中の取り消せないジョブ（導入・アンインストール・復元・装備の取り込み）か。途中で止めると mods\ や装備一覧が中途半端になる。</summary>
    private static bool IsUncancellableRunning(JsonNode j) => j.Str("state") == "running" && !j.Bool("cancellable", true);

    private static string JobLabel(JsonNode j) => L.TryT("App_JobType_" + j.Str("type")) ?? j.Str("type") ?? "";

    /// <summary>
    /// Windows の終了（サインアウト・シャットダウン・再起動）の確認で使う: 実行中の取り消せないジョブの名前。App.OnSessionEnding は
    /// UI スレッドで同期に答える必要があるので、上限まで待っても分からないときは空（終了を止めない）。2026-10-27 R17B-02。
    /// </summary>
    public List<string> UncancellableJobLabels(TimeSpan limit)
    {
        // UI スレッドを止めて待つので、問い合わせは Task.Run の中で行う（UI スレッドに戻る await があると止まったままになる）
        var t = Task.Run(() => ActiveJobsAsync(limit));
        List<JsonNode>? jobs;
        try { jobs = t.Wait(limit + TimeSpan.FromSeconds(1)) ? t.Result : null; }
        catch (AggregateException) { jobs = null; }
        return jobs?.Where(IsUncancellableRunning).Select(JobLabel).ToList() ?? [];
    }

    /// <summary>実行中の取り消せないジョブが終わるまで待つ（Windows の終了を止めている間。バックエンドが止まった・応答しないときも終える）。</summary>
    public async Task WaitUncancellableForSessionEndAsync()
    {
        while (IsReady && Client != null)
        {
            var jobs = await ActiveJobsAsync();
            if (jobs == null || !jobs.Any(IsUncancellableRunning)) return;
            await Task.Delay(1000);
        }
    }

    /// <summary>
    /// 閉じる前の確認と待ち → (閉じるか, 強制か)。setWindowEnabled は待つ間に窓を無効にするため。
    /// 実行中の取り消せないジョブがあれば「中止して閉じる」は出さず、終わるのを待ってから閉じる（上限を超えるか、
    /// バックエンドが応答しなくなったら、警告つきで「それでも閉じる」を聞く）。それ以外の未完了のジョブ（順番待ちを含む）や
    /// 画面の処理の実行中は、中止してよいかを確かめる（順番待ちのジョブは種類を問わず、バックエンドが終了の時に取り消す）。
    /// </summary>
    public async Task<(bool Close, bool Force)> PrepareCloseAsync(Action<bool> setWindowEnabled)
    {
        var jobs = await ActiveJobsAsync();
        if (jobs != null && jobs.Any(IsUncancellableRunning))
        {
            var wait = Dialogs.Show(new DialogSpec
            {
                Kind = DialogKind.Warning, Title = L.T("App_CloseWaitTitle"), Message = L.T("App_CloseWaitMessage"),
                PrimaryText = L.T("App_CloseWait"), SecondaryText = L.T("App_CloseCancel"),
            }).Accepted;
            return wait ? await WaitUncancellableAsync(setWindowEnabled) : (false, false);
        }
        bool busy = (jobs?.Count ?? 0) > 0 || Generate.IsBusy || Mod.IsBusy || Library.IsBusy || Setup.Busy || Settings.RefsBusy;
        if (busy && !Dialogs.Confirm(L.T("App_CloseBusyTitle"), L.T("App_CloseBusyMessage"), L.T("App_CloseAnyway"), kind: DialogKind.Warning))
            return (false, false);
        return (true, false);
    }

    private async Task<(bool, bool)> WaitUncancellableAsync(Action<bool> setWindowEnabled)
    {
        setWindowEnabled(false);
        var sw = Stopwatch.StartNew();
        AppLog.Info(L.T("Log_App_CloseWaiting"));
        try
        {
            while (true)
            {
                if (!IsReady || Client == null) return (true, false);     // バックエンドが止まった（守るものが無い）
                var jobs = await ActiveJobsAsync();
                var running = jobs?.Where(IsUncancellableRunning).ToList();
                if (running is { Count: 0 })
                {
                    AppLog.Info(L.T("Log_App_CloseWaitDone"));
                    return (true, false);
                }
                if (running != null)
                    ShowNotice("closing", L.F("App_CloseWaiting", string.Join(L.T("Common_ListSep"), running.Select(JobLabel))));
                if (running == null || sw.Elapsed > CloseWaitLimit)
                {
                    var force = Dialogs.Show(new DialogSpec
                    {
                        Kind = DialogKind.Warning, Title = L.T("App_CloseWaitTimeoutTitle"),
                        Message = running == null ? L.T("App_CloseWaitNoResponse")
                                                  : L.F("App_CloseWaitTimeoutMessage", (int)CloseWaitLimit.TotalMinutes),
                        PrimaryText = L.T("App_CloseForce"), SecondaryText = L.T("App_CloseKeepWaiting"),
                    }).Accepted;
                    if (force)
                    {
                        AppLog.Warn(L.T("Log_App_CloseForced"));
                        return (true, true);
                    }
                    sw.Restart();
                }
                await Task.Delay(500);
            }
        }
        finally
        {
            ClearNotice("closing");
        }
    }

    /// <summary>バックエンドを止める。force は利用者が「それでも閉じる」を選んだとき（終了を長く待たない）。</summary>
    public async Task ShutdownAsync(bool force = false)
    {
        try { await Host.StopAsync(Client, force); }
        catch (Exception) { }
        Host.Dispose();
        Client?.Dispose();
        AppLog.Info(L.T("Log_App_Exited"));
    }
}
