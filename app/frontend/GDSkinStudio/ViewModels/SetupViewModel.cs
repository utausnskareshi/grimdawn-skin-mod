using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Globalization;
using System.Text.Json.Nodes;
using System.Windows.Input;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using GDSkinStudio.Views;
using Microsoft.Web.WebView2.Core;
using Microsoft.Win32;

namespace GDSkinStudio.ViewModels;

public sealed class SetupStep(string key, string title) : ObservableObject
{
    public string Key { get; } = key;
    public string Title { get; } = title;
    private string _state = "pending";      // done / current / pending
    public string State { get => _state; set => Set(ref _state, value); }
}

public sealed record SetupFlag(string Kind, string Text);

/// <summary>導入するもの 1 件（scripts/trellis/setup_manifest.json の部品）。</summary>
public sealed class SetupComponent : ObservableObject
{
    private readonly SetupViewModel _owner;

    public SetupComponent(SetupViewModel owner, JsonNode c)
    {
        _owner = owner;
        Id = c.Str("id") ?? "";
        Name = c.Str("name") ?? Id;
        Description = c.Str("description") ?? "";
        Group = c.Str("group") ?? "";
        Required = c.Bool("required");
        Consent = c.Str("consent");
        Depends = c.Strings("depends").ToList();
        Version = c.Str("version") ?? "";
        DownloadBytes = c.Long("download_bytes") ?? 0;
        DiskBytes = c.Long("disk_bytes") ?? 0;
        License = c.Str("license") ?? "";
        LicenseClass = c.Str("license_class") ?? "";
        LicenseSha = c.Str("license_sha256") ?? "";
        SourceName = c.Obj("source").Str("name") ?? "";
        SourceUrl = c.Obj("source").Str("url") ?? "";
        Flags = c.Arr("flags").Select(f => new SetupFlag(f.Str("kind") ?? "", f.Str("text") ?? "")).ToList();
        _selected = Required;
        Update(c);
    }

    public string Id { get; }
    public string Name { get; }
    public string Description { get; }
    public string Group { get; }
    public bool Required { get; }
    public bool Optional => !Required;
    public string? Consent { get; }
    public IReadOnlyList<string> Depends { get; }
    public string Version { get; }
    public long DownloadBytes { get; }
    public long DiskBytes { get; }
    public string License { get; }
    public string LicenseClass { get; }
    public string LicenseSha { get; }
    public string SourceName { get; }
    public string SourceUrl { get; }
    public IReadOnlyList<SetupFlag> Flags { get; }
    public bool HasFlags => Flags.Count > 0;
    public string FlagText => string.Join("\n", Flags.Select(f => f.Text));

    /// <summary>目立たせる区分（GPL・非商用のファイル・注意事項）。無ければ空。</summary>
    public string Badge => BadgeFor(Flags.Select(f => f.Kind));

    private static readonly string[] BadgeKinds = ["gpl", "noncommercial_files", "model_card"];

    /// <summary>印の種類 → 目立たせる区分の表示。当てはまるものを全て並べる（packages は GPL と非商用のファイルの両方。
    /// 1 つだけにすると、印の本文が日本語だけの英語の画面で非商用の部分が見えない。R16B-01）。設定の画面も使う。</summary>
    public static string BadgeFor(IEnumerable<string> kinds)
    {
        var set = kinds.ToHashSet();
        return string.Join(" / ", BadgeKinds.Where(set.Contains).Select(k =>
            L.T(k == "noncommercial_files" ? "Setup_Badge_noncommercial" : "Setup_Badge_" + k)));
    }
    public bool HasBadge => Badge.Length > 0;

    public string SizeText => L.F("Setup_Size", SetupViewModel.Fmt(DownloadBytes), SetupViewModel.Fmt(DiskBytes));
    public string KindText => Required ? L.T("Setup_Required") : L.T("Setup_Optional");

    private bool _selected;
    public bool Selected
    {
        get => _selected;
        set
        {
            if (Required) value = true;
            if (Set(ref _selected, value)) _owner.OnSelectionChanged(this);
        }
    }

    private string _state = "missing";
    public string State { get => _state; private set { if (Set(ref _state, value)) { OnPropertyChanged(nameof(StateText)); OnPropertyChanged(nameof(StateStatus)); } } }
    public string StateText => L.TryT("Setup_State_" + State) ?? State;
    /// <summary>環境チェックと同じ表示（ok / warning / missing）。</summary>
    public string StateStatus => State == "installed" ? "ok" : State == "missing" ? "missing" : "warning";
    private string _stateDetail = "";
    public string StateDetail { get => _stateDetail; private set => Set(ref _stateDetail, value); }

    public void Update(JsonNode c)
    {
        State = c.Str("state") ?? "missing";
        StateDetail = c.Str("state_detail") ?? "";
    }

    // --- 取得中の表示 ---------------------------------------------------------------------------
    private string _run = "";          // waiting / running / verifying / installed / reused / failed / cancelled
    public string Run
    {
        get => _run;
        set { if (Set(ref _run, value)) { OnPropertyChanged(nameof(RunText)); OnPropertyChanged(nameof(RunStatus)); } }
    }
    public string RunText => Run.Length == 0 ? "" : L.TryT("Setup_Run_" + Run) ?? Run;
    public string RunStatus => Run switch { "installed" or "reused" => "ok", "failed" => "error", "running" or "verifying" => "warning", _ => "missing" };     // cancelled は灰色
    private string _runDetail = "";
    public string RunDetail { get => _runDetail; set => Set(ref _runDetail, value); }
}

public sealed record SetupEnvRow(string Status, string Label, string Message);

/// <summary>
/// 初回セットアップ（docs/packaging.md 4 章）: 環境チェック → 導入するもの（ライセンス）→ 同意 → 取得 → 基準メッシュ（B-002）→ /basemods の案内。
/// 何度でもやり直せる（ナビゲーションの「セットアップ」、設定画面の「セットアップをやり直す」）。
/// </summary>
public sealed class SetupViewModel : ObservableObject
{
    private readonly MainViewModel _main;
    private string? _jobId;
    private string? _refsJob;

    public SetupViewModel(MainViewModel main)
    {
        _main = main;
        Steps =
        [
            new SetupStep("env", L.T("Setup_Step_env")),
            new SetupStep("items", L.T("Setup_Step_items")),
            new SetupStep("download", L.T("Setup_Step_download")),
            new SetupStep("refs", L.T("Setup_Step_refs")),
            new SetupStep("basemods", L.T("Setup_Step_basemods")),
            new SetupStep("done", L.T("Setup_Step_done")),
        ];
        Steps[0].State = "current";
        NextCommand = new AsyncCommand(NextAsync, () => _main.IsReady && !Busy && CanNext);
        BackCommand = new RelayCommand(Back, () => !Busy && CurrentIndex > 0);
        SkipCommand = new AsyncCommand(SkipAsync, () => _main.IsReady && !Busy);
        RecheckCommand = new AsyncCommand(() => RefreshAsync(true), () => _main.IsReady && !Busy);
        BrowseRootCommand = new RelayCommand(() =>
        {
            var d = new OpenFolderDialog { Title = L.T("Setup_BrowseRootTitle") };
            if (d.ShowDialog() == true) _rootLoad = SetRootAsync(RootFromPicked(d.FolderName));
        }, () => !Busy);
        DefaultRootCommand = new AsyncCommand(() => _rootLoad = SetRootAsync(DefaultRoot), () => !Busy);
        // 設定画面の TRELLIS 実行環境の削除・修復の実行中は取得を始めない（同じ列に並び、削除の後に取得が走る。点検 R20A-07）
        UseExistingCommand = new AsyncCommand(UseExistingAsync, () => _main.IsReady && !Busy && !_main.Settings.MaintBusy);
        ShowLicenseCommand = new AsyncCommand(p => ShowLicenseAsync((SetupComponent)p!), _ => _main.IsReady);
        OpenSourceCommand = new RelayCommand(p => OpenUrl(((SetupComponent)p!).SourceUrl));
        ShowClipNoticeCommand = new RelayCommand(ShowClipNotice);
        StartCommand = new AsyncCommand(StartAsync, () => _main.IsReady && !Busy && !_main.Settings.MaintBusy && CanStart);
        CancelCommand = new AsyncCommand(async () => { if (_jobId != null) await _main.RequireClient().CancelJobAsync(_jobId); }, () => Installing);
        PrepareRefsCommand = new AsyncCommand(PrepareRefsAsync, () => _main.IsReady && !Busy);
        CancelRefsCommand = new AsyncCommand(async () => { if (_refsJob != null) await _main.RequireClient().CancelJobAsync(_refsJob); }, () => RefsBusy);
        OpenSettingsCommand = new RelayCommand(() => _main.Navigate("settings"));
        GuideCommand = new RelayCommand(() => _main.Mod.GuideCommand.Execute(null));
        FinishCommand = new AsyncCommand(FinishAsync, () => _main.IsReady && !Busy);
    }

    public ObservableCollection<SetupStep> Steps { get; }
    public ICommand NextCommand { get; }
    public ICommand BackCommand { get; }
    public ICommand SkipCommand { get; }
    public ICommand RecheckCommand { get; }
    public ICommand BrowseRootCommand { get; }
    public ICommand DefaultRootCommand { get; }
    public ICommand UseExistingCommand { get; }
    public ICommand ShowLicenseCommand { get; }
    public ICommand OpenSourceCommand { get; }
    public ICommand ShowClipNoticeCommand { get; }
    public ICommand StartCommand { get; }
    public ICommand CancelCommand { get; }
    public ICommand PrepareRefsCommand { get; }
    public ICommand CancelRefsCommand { get; }
    public ICommand OpenSettingsCommand { get; }
    public ICommand GuideCommand { get; }
    public ICommand FinishCommand { get; }

    private int _currentIndex;
    public int CurrentIndex
    {
        get => _currentIndex;
        private set
        {
            if (!Set(ref _currentIndex, value)) return;
            for (int i = 0; i < Steps.Count; i++) Steps[i].State = i < value ? "done" : i == value ? "current" : "pending";
            OnPropertyChanged(nameof(CurrentKey));
            OnPropertyChanged(nameof(CanNext));
            CommandManager.InvalidateRequerySuggested();
        }
    }
    public string CurrentKey => Steps[CurrentIndex].Key;

    public bool CanNext => CurrentKey switch
    {
        "download" => DownloadDone || ReadyImage,
        "done" => false,
        _ => true,
    };

    /// <summary>取得または基準メッシュの準備の実行中。2 つを 1 つの値で持つと、片方の終了がもう片方の実行中を消すので、
    /// 両方から求める（点検 R20B-02）。</summary>
    public bool Busy => Installing || RefsBusy;

    // --- 1. 環境チェック -------------------------------------------------------------------------
    public ObservableCollection<SetupEnvRow> EnvRows { get; } = new();
    private string _envSummary = "";
    public string EnvSummary { get => _envSummary; private set => Set(ref _envSummary, value); }
    private bool _gpuOk;
    public bool GpuOk { get => _gpuOk; private set => Set(ref _gpuOk, value); }

    // --- 2. 導入するもの ------------------------------------------------------------------------
    public ObservableCollection<SetupComponent> Components { get; } = new();
    public IEnumerable<SetupComponent> RequiredComponents => Components.Where(c => c.Required);
    public IEnumerable<SetupComponent> OptionalComponents => Components.Where(c => !c.Required);
    public string DefaultRoot { get; private set; } = "";
    private string _root = "";
    /// <summary>取得先の欄。エクスプローラーの「パスのコピー」の引用符は外す（設定画面の欄・生成の画面の GLB の欄と同じ。
    /// 外さないと「ドライブから始まるフォルダー」の問題になり開始できない。点検 R20B-04）。</summary>
    public string Root
    {
        get => _root;
        set { if (Set(ref _root, value?.Trim().Trim('"').Trim() ?? "") && _loaded) _rootLoad = SetRootAsync(_root); }
    }
    /// <summary>取得先を変えたときの status の読み込み（取得の開始はこれを待ってから判定する。点検 R20A-02）。</summary>
    private Task _rootLoad = Task.CompletedTask;
    private int _rootSeq;
    /// <summary>最後に画面に反映した status の取得先（Root と違えば、画面の状態は前の取得先のもの）。</summary>
    private string _statusRoot = "";
    /// <summary>取得先がアプリのセットアップが作ったものか・あるか（status の maintenance・root_exists。点検 R20A-01・R20B-02）。</summary>
    private bool _rootAppCreated;
    private bool _rootExists;
    private string _rootReason = "";
    private string _rootProblems = "";
    public string RootProblems { get => _rootProblems; private set => Set(ref _rootProblems, value); }
    private string _totalsText = "";
    public string TotalsText { get => _totalsText; private set => Set(ref _totalsText, value); }
    private string _freeText = "";
    public string FreeText { get => _freeText; private set => Set(ref _freeText, value); }
    private bool _diskOk = true;
    public bool DiskOk { get => _diskOk; private set => Set(ref _diskOk, value); }
    private bool _acceptLicenses;
    public bool AcceptLicenses { get => _acceptLicenses; set { if (Set(ref _acceptLicenses, value)) CommandManager.InvalidateRequerySuggested(); } }
    private bool _acceptClip;
    public bool AcceptClip { get => _acceptClip; set { if (Set(ref _acceptClip, value)) CommandManager.InvalidateRequerySuggested(); } }
    public bool ClipSelected => Components.Any(c => c.Consent == "clip" && c.Selected);
    private string _clipNotice = "";
    private string _clipSha = "";
    private string _clipLang = "ja";
    private bool _clipAccepted;
    public bool ClipNeedsConsent => ClipSelected && !_clipAccepted;
    private bool _readyImage;
    public bool ReadyImage { get => _readyImage; private set { if (Set(ref _readyImage, value)) OnPropertyChanged(nameof(CanNext)); } }
    private bool _rootUsable = true;

    public bool CanStart => _rootUsable && AcceptLicenses && (!ClipNeedsConsent || AcceptClip) && DiskOk && GpuOk;

    public void OnSelectionChanged(SetupComponent c)
    {
        // テキスト→3D のモデルは CLIP と一緒に使う（どちらかを選ぶと両方）
        if (c.Selected)
            foreach (var d in c.Depends)
                if (Components.FirstOrDefault(x => x.Id == d) is { Selected: false } dep) dep.Selected = true;
        if (!c.Selected)
            foreach (var x in Components.Where(x => x.Depends.Contains(c.Id) && x.Selected)) x.Selected = false;
        // depends は片向き（trellis_text → clip）なので、任意のテキスト→3D の項目（group text）は組として選ぶ・外す
        // （CLIP だけを取得してテキスト→3D が使えない、を防ぐ。点検 R20A-05）
        if (!c.Required && c.Group == "text")
            foreach (var x in Components.Where(x => x != c && !x.Required && x.Group == "text" && x.Selected != c.Selected))
                x.Selected = c.Selected;
        UpdateTotals();
        OnPropertyChanged(nameof(ClipSelected));
        OnPropertyChanged(nameof(ClipNeedsConsent));
        CommandManager.InvalidateRequerySuggested();
    }

    // --- 3. 取得 -----------------------------------------------------------------------------
    private bool _installing;
    public bool Installing { get => _installing; private set { if (Set(ref _installing, value)) { OnPropertyChanged(nameof(Busy)); CommandManager.InvalidateRequerySuggested(); } } }
    private bool _downloadDone;
    public bool DownloadDone { get => _downloadDone; private set { if (Set(ref _downloadDone, value)) OnPropertyChanged(nameof(CanNext)); } }
    private bool _downloadFailed;
    public bool DownloadFailed { get => _downloadFailed; private set => Set(ref _downloadFailed, value); }
    private double _overall;
    public double Overall { get => _overall; private set => Set(ref _overall, value); }
    private string _overallText = "";
    public string OverallText { get => _overallText; private set => Set(ref _overallText, value); }
    private string _currentText = "";
    public string CurrentText { get => _currentText; private set => Set(ref _currentText, value); }
    public ObservableCollection<string> LogLines { get; } = new();
    private DateTime _startedAt;

    // --- 4. 基準メッシュ -----------------------------------------------------------------------
    private bool _refsReady;
    public bool RefsReady { get => _refsReady; private set => Set(ref _refsReady, value); }
    private bool _refsUnreadable;       // 基準メッシュはあるが開けない（環境チェックの refs の params.reason refs_unreadable）
    private bool _refsCorrupt;          // 基準メッシュはあるが壊れている（環境チェックの refs の params.reason refs_corrupt）
    private bool _refsBusy;
    public bool RefsBusy { get => _refsBusy; private set { if (Set(ref _refsBusy, value)) { OnPropertyChanged(nameof(Busy)); CommandManager.InvalidateRequerySuggested(); } } }
    private double _refsProgress;
    public double RefsProgress { get => _refsProgress; private set => Set(ref _refsProgress, value); }
    private string _refsText = "";
    public string RefsText { get => _refsText; private set => Set(ref _refsText, value); }
    private string _gameText = "";
    public string GameText { get => _gameText; private set => Set(ref _gameText, value); }
    private bool _gameFound;
    public bool GameFound { get => _gameFound; private set => Set(ref _gameFound, value); }

    // --- 5. /basemods ------------------------------------------------------------------------
    private string _launchText = "";
    public string LaunchText { get => _launchText; private set => Set(ref _launchText, value); }
    private bool _launchOk;
    public bool LaunchOk { get => _launchOk; private set => Set(ref _launchOk, value); }

    // --- 完了 --------------------------------------------------------------------------------
    public ObservableCollection<string> SummaryLines { get; } = new();

    private bool _loaded;

    public static string Fmt(long bytes) => bytes >= 1_000_000_000
        ? (bytes / 1e9).ToString("0.0", CultureInfo.InvariantCulture) + " GB"
        : bytes >= 1_000_000 ? (bytes / 1e6).ToString("0", CultureInfo.InvariantCulture) + " MB"
        : (bytes / 1e3).ToString("0", CultureInfo.InvariantCulture) + " KB";

    /// <summary>ページを開いたとき（初回起動では自動で開く）。</summary>
    public async Task RefreshAsync(bool forceEnv)
    {
        var c = _main.Client;
        if (c == null) return;
        await _main.RefreshEnvironmentAsync(forceEnv);
        // 2 回目以降は画面で選んだ取得先を保つ。ただし設定の取得先が変わっていれば（設定画面の保存・全体の削除で自動に戻した等）、
        // 前の取得先を捨てて設定のものを読む（前の場所に取得して設定を戻さない。点検 R20A-02）
        var st = await GetStatusAsync(_loaded ? Root : null);
        if (_loaded && (st.Str("configured_root") ?? "") != _configuredRoot) st = await GetStatusAsync(null);
        ApplyStatus(st);
        BuildEnvRows();
        await UpdateRefsAndLaunchAsync();
    }

    private async Task<JsonNode> GetStatusAsync(string? root) =>
        (await _main.RequireClient().GetAsync("setup/status?lang=" + L.Language + (string.IsNullOrEmpty(root) ? "" : "&root=" + Uri.EscapeDataString(root))))!;

    private async Task LoadStatusAsync(string? root) => ApplyStatus(await GetStatusAsync(root));

    /// <summary>最後に読んだ status の設定の取得先（settings の trellis_root。空 = 自動）。</summary>
    private string _configuredRoot = "";

    private void ApplyStatus(JsonNode st)
    {
        DefaultRoot = st.Str("default_root") ?? "";
        _configuredRoot = st.Str("configured_root") ?? "";
        _root = st.Str("root") ?? DefaultRoot;
        _statusRoot = _root;
        OnPropertyChanged(nameof(Root));
        var mt = st.Obj("maintenance");
        _rootReason = mt.Str("reason") ?? "";
        // 作成の印が無い以前の版の取得先は、取得の記録があればアプリの取得先（バックエンドの run_install と同じ扱い。削除・
        // 修復の対象は既定の取得先だけなので maintenance の app_created だけでは決めない）
        _rootAppCreated = mt.Bool("app_created") || (_rootReason == "not_app_root" &&
            (st["last_install"] != null || st.Arr("components").Any(c => c?["record"] != null)));
        _rootExists = st.Bool("root_exists");
        var probs = st.Arr("root_problems").Select(p => p.Str("message") ?? "").ToList();
        _rootUsable = probs.Count == 0;
        RootProblems = string.Join("\n", probs);
        var comps = st.Arr("components");
        if (Components.Count == 0)
        {
            foreach (var n in comps) Components.Add(new SetupComponent(this, n!));
            OnPropertyChanged(nameof(RequiredComponents));
            OnPropertyChanged(nameof(OptionalComponents));
        }
        else
            foreach (var n in comps)
                Components.FirstOrDefault(x => x.Id == n.Str("id"))?.Update(n!);
        // CLIP の注意文は表示言語のもの（notices に両方の言語がある。取得・既存の環境の設定の結果の status は日本語の text を
        // 返すので、notices から選ぶ。2026-10-18 未決定 #2）
        var clip = st.Obj("clip_notice");
        var shown = clip.Obj("notices").Obj(L.Language);
        _clipLang = shown != null ? L.Language : clip.Str("lang") ?? "ja";
        _clipNotice = (shown ?? clip).Str("text") ?? "";
        _clipSha = (shown ?? clip).Str("sha256") ?? "";
        _clipAccepted = clip.Bool("accepted");
        ReadyImage = st.Bool("ready_image");
        _freeBytes = st.Long("free_bytes");
        _rebuildReclaimBytes = st.Long("rebuild_reclaim_bytes") ?? 0;
        _loaded = true;
        UpdateTotals();
        OnPropertyChanged(nameof(ClipNeedsConsent));
    }

    private long? _freeBytes;
    private long _rebuildReclaimBytes;

    /// <summary>
    /// 取得する項目に要るディスク（余裕を含まない）。バックエンドの setup.disk_need_bytes と同じ規則:
    /// 項目の展開後の合計 + PyTorch のホイール（入れ終わるまで両方を置く）− Python の仮想環境の作り直しで消える分
    /// （status の rebuild_reclaim_bytes。差し引かないと空きが足りるのに開始できない。点検 FG02-R1）。
    /// </summary>
    private long NeedDiskBytes(IReadOnlyCollection<SetupComponent> todo)
    {
        long disk = todo.Sum(c => c.DiskBytes);
        if (todo.FirstOrDefault(c => c.Id == "torch") is { } torch) disk += torch.DownloadBytes;
        if (todo.Any(c => c.Id == "python")) disk -= _rebuildReclaimBytes;
        return Math.Max(disk, 0);
    }

    /// <summary>
    /// 取得を始めるのに要る空き（余裕の 5 % と 1 GiB を含む。取得する項目が無ければ 0）。バックエンドの setup.disk_need_bytes と
    /// 同じ式で、画面に出す「必要な空き」と開始の判定（DiskOk）の両方に使う（以前は余裕を含まない量を出し、判定だけ余裕を含めたので、
    /// 空きが約 16.2〜18.0 GB のときは「約 16.2 GB が必要」と出たまま開始できなかった。B-036）。
    /// </summary>
    private long NeedFreeBytes(IReadOnlyCollection<SetupComponent> todo) =>
        todo.Count == 0 ? 0 : (long)(NeedDiskBytes(todo) * 1.05) + (1L << 30);

    private void UpdateTotals()
    {
        var todo = Components.Where(c => c.Selected && c.State != "installed").ToList();
        long dl = todo.Sum(c => c.DownloadBytes), need = NeedFreeBytes(todo);
        TotalsText = todo.Count == 0 ? L.T("Setup_TotalsNone") : L.F("Setup_Totals", todo.Count, Fmt(dl), Fmt(need));
        // バックエンド（run_install）は空きが必要量より少ないときに止めるので、同じ量ちょうどは開始できる
        DiskOk = _freeBytes is null || todo.Count == 0 || _freeBytes.Value >= need;
        FreeText = _freeBytes is long f ? L.F(DiskOk ? "Setup_Free" : "Setup_FreeLow", Fmt(f)) : "";
        CommandManager.InvalidateRequerySuggested();
    }

    private void BuildEnvRows()
    {
        var env = _main.Environment;
        var items = env.Arr("items").ToDictionary(i => i.Str("id") ?? "", i => i);
        EnvRows.Clear();
        void Row(string id, string? labelKey = null)
        {
            if (!items.TryGetValue(id, out var it)) return;
            EnvRows.Add(new SetupEnvRow(it.Str("status") ?? "", L.TryT(labelKey ?? "Env_" + id) ?? id, AppLog.Redact(it.Str("message") ?? "")));
        }
        Row("os");
        if (items.ContainsKey("gpu") && items["gpu"].Str("status") == "ok")
        {
            Row("gpu");
            Row("gpu_memory");
            Row("gpu_driver");
            Row("gpu_compute");
            Row("vc_runtime");
        }
        else
            EnvRows.Add(new SetupEnvRow("error", L.T("Env_gpu"), L.T("Setup_NoGpu")));
        if (_freeBytes is long f)
        {
            // 行の数字と印は、行が述べる必須の項目の量で決める（選んだ任意の項目の分は「2. 導入するもの・同意」の合計と注意に出る。
            // 必須の項目が導入済みなら「約 0 KB が必要」とは出さない。B-036）
            var required = Components.Where(c => c.Required && c.State != "installed").ToList();
            long need = NeedFreeBytes(required);
            EnvRows.Add(new SetupEnvRow(f >= need ? "ok" : "warning", L.T("Setup_EnvDisk"), required.Count == 0
                ? L.F("Setup_EnvDiskTextNone", Root, Fmt(f)) : L.F("Setup_EnvDiskText", Root, Fmt(f), Fmt(need))));
        }
        Row("game");
        Row("game_aom");
        string wv;
        string wvStatus;
        try
        {
            var v = CoreWebView2Environment.GetAvailableBrowserVersionString();
            (wvStatus, wv) = string.IsNullOrEmpty(v) ? ("missing", L.T("Setup_WebView2Missing")) : ("ok", L.F("Setup_WebView2", v));
        }
        catch (Exception) { (wvStatus, wv) = ("missing", L.T("Setup_WebView2Missing")); }
        EnvRows.Add(new SetupEnvRow(wvStatus, "WebView2", wv));
        var sum = env.Obj("summary");
        GpuOk = sum.Bool("can_generate_image") || (items.TryGetValue("gpu", out var g) && g.Str("status") == "ok" &&
                                                   (!items.TryGetValue("gpu_driver", out var dr) || dr.Str("status") == "ok") &&
                                                   (!items.TryGetValue("gpu_compute", out var gc) || gc.Str("status") != "error") &&
                                                   (!items.TryGetValue("gpu_memory", out var gm0) || gm0.Str("status") != "error"));
        // GPU メモリが 12〜16 GB 級（推奨未満。2026-10-17 利用者の判断 C127）と、Visual C++ 再頒布可能パッケージが古い・無い（C31）は、
        // 止めずに注意を出し、取得を始めるときに続けるかを選んでもらう
        _lowVramMib = GpuOk && items.TryGetValue("gpu_memory", out var gm) && gm.Str("status") == "warning"
            ? gm.Obj("params").Long("total_mib") ?? 0 : null;
        _vcRuntimeOld = GpuOk && items.TryGetValue("vc_runtime", out var vc) && vc.Str("status") == "warning";
        EnvSummary = !GpuOk ? L.T("Setup_EnvNoGenerate")
            : (_lowVramMib != null ? L.T("Setup_EnvOkLowVram") : L.T("Setup_EnvOk")) + (_vcRuntimeOld ? "\n" + L.T("Setup_EnvVcRuntime") : "");
    }

    private long? _lowVramMib;
    private bool _vcRuntimeOld;

    /// <summary>取得の前の確認（GPU メモリが推奨未満・Visual C++ 再頒布可能パッケージが古い）。続けるなら true。</summary>
    private bool ConfirmStartWarnings()
    {
        if (!Components.Any(c => c.Selected && c.State != "installed")) return true;     // 照合だけ（取得しない）
        var lines = new List<string>();
        if (_lowVramMib is long mib) lines.Add(L.F("Setup_StartWarnLowVram", mib));
        if (_vcRuntimeOld) lines.Add(L.T("Setup_StartWarnVc"));
        if (lines.Count == 0) return true;
        var ok = Dialogs.Confirm(L.T("Setup_StartWarnTitle"), string.Join("\n\n", lines), L.T("Setup_StartWarnContinue"),
            L.T("Setup_StartWarnHint"), DialogKind.Warning);
        AppLog.Info(L.T(ok ? "Log_Setup_StartWarnContinued" : "Log_Setup_StartWarnCancelled"));
        return ok;
    }

    /// <summary>アプリが取得先の直下に作るもの（setup/maintenance.py の ALL_ENTRIES と同じ。.gdskin で始まる作業用の名前も）。</summary>
    private static readonly string[] RuntimeEntries =
        ["tools", "python", "venv", "uv-cache", "src", "models", "torch-home", "hf-home", "u2net", "downloads", "warp-cache",
         "consents.json", "consents.json.tmp"];

    /// <summary>
    /// 取得先が、アプリが作ったものでも既存の実行環境として登録したものでもない、中身のあるフォルダー（ドライブ直下・ドキュメント・
    /// ゲームのフォルダーなど）なら、その直下に venv・models などを直接作り、アプリから削除・修復できないことを示して確認する。
    /// 続けるなら true（取得先の欄への手入力は、参照と違い trellis-runtime を足さない。点検 R20B-02）。
    /// </summary>
    private bool ConfirmForeignRoot()
    {
        if (_rootAppCreated || !_rootExists || _rootReason is "existing_runtime" or "dev_runtime") return true;
        if (!Components.Any(c => c.Selected && c.State != "installed")) return true;     // 照合だけ（取得しない）
        List<string> others;
        try
        {
            others = System.IO.Directory.EnumerateFileSystemEntries(Root).Select(e => System.IO.Path.GetFileName(e))
                .Where(n => !RuntimeEntries.Contains(n, StringComparer.OrdinalIgnoreCase) && !n.StartsWith(".gdskin", StringComparison.OrdinalIgnoreCase))
                .ToList();
        }
        catch (Exception) { return true; }        // 一覧を読めない（アクセス権など）ときは、取得の始めの検査に任せる
        if (others.Count == 0) return true;
        var names = string.Join(L.T("Common_ListSep"), others.Order(StringComparer.OrdinalIgnoreCase).Take(5));
        var ok = Dialogs.Confirm(L.T("Setup_ForeignRootTitle"), L.F("Setup_ForeignRootMessage", AppLog.Redact(Root), names, others.Count),
            L.T("Setup_ForeignRootDo"), L.F("Setup_ForeignRootHint", AppLog.Redact(System.IO.Path.Combine(Root, "trellis-runtime"))), DialogKind.Warning);
        AppLog.Info(L.T(ok ? "Log_Setup_ForeignRootContinued" : "Log_Setup_ForeignRootCancelled"));
        return ok;
    }

    /// <summary>
    /// アプリのセットアップが作ったものではない取得先（既存の TRELLIS 実行環境・ほかのフォルダー）で、取得が中身を消す・入れ替える
    /// 項目（Python の版が違う venv の削除と作り直し、版の違うソースのフォルダーの入れ替え、パッケージの版の入れ替え）があれば、
    /// それを並べて確認する。足りない項目を足すだけなら確認しない。続けるなら true、allowReplace は同意したか（点検 R20A-01）。
    /// </summary>
    private bool ConfirmReplaceExisting(out bool allowReplace)
    {
        allowReplace = false;
        if (_rootAppCreated || !_rootExists) return true;
        var todo = Components.Where(c => c.Selected && c.State != "installed").ToList();
        var items = new List<string>();
        var venv = System.IO.Path.Combine(Root, "venv");
        bool rebuild = todo.Any(c => c.Id == "python") && System.IO.Directory.Exists(venv);
        if (rebuild)
            items.Add(L.F("Setup_ReplaceVenv", AppLog.Redact(venv), todo.First(c => c.Id == "python").StateDetail));
        foreach (var c in todo)
        {
            if (c.Id is "trellis_source" or "dinov2" && c.State is "outdated" or "partial")
                items.Add(L.F("Setup_ReplaceSource", c.Name));
            else if (!rebuild && c.Id is "torch" or "packages" && c.State == "outdated")      // venv を作り直すなら、その中に入れ直す
                items.Add(L.F("Setup_ReplacePackages", c.Name));
        }
        if (items.Count == 0) return true;
        var r = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Warning, Title = L.T("Setup_ReplaceTitle"), Message = L.F("Setup_ReplaceMessage", AppLog.Redact(Root)),
            Hint = L.T("Setup_ReplaceHint"), Sections = new List<DialogSection> { new() { Header = L.T("Setup_ReplaceHeader"), Items = items } },
            CheckText = L.T("Setup_ReplaceCheck"), CheckRequired = true, PrimaryText = L.T("Setup_ReplaceDo"), SecondaryText = L.T("Dialog_Cancel"),
        });
        allowReplace = r.Accepted && r.Checked;
        AppLog.Info(L.T(allowReplace ? "Log_Setup_ReplaceAccepted" : "Log_Setup_ReplaceCancelled"));
        return allowReplace;
    }

    private async Task UpdateRefsAndLaunchAsync()
    {
        var items = _main.Environment.Arr("items").ToDictionary(i => i.Str("id") ?? "", i => i);
        RefsReady = items.TryGetValue("refs", out var r) && r.Str("status") == "ok";
        // あるが開けない基準ファイル（ほかのアプリが使用中・読み取りの権限が無い）は「まだありません」と言わない
        // （ゲームからの準備では直らない。2026-11-04 回帰の確認 第 2 回 F1）
        _refsUnreadable = !RefsReady && r.Obj("params").Str("reason") == "refs_unreadable";
        // 壊れている基準ファイルも、ファイルはあるので「まだありません」と言わない（「ゲームから準備する」で取り出し直せば直る。
        // 2026-11-04 段階 H HR_user-10）
        _refsCorrupt = !RefsReady && r.Obj("params").Str("reason") == "refs_corrupt";
        GameFound = items.TryGetValue("game", out var g) && g.Str("status") == "ok";
        GameText = GameFound ? L.F("Setup_GameFound", AppLog.Redact(g!.Str("message") ?? "")) : L.T("Setup_GameMissing");
        if (!RefsBusy)
            RefsText = RefsReady ? L.T("Set_RefsReady") : _refsUnreadable ? L.T("Set_RefsUnreadable")
                : _refsCorrupt ? L.T("Set_RefsCorrupt") : L.T("Set_RefsMissing");
        var lop = items.TryGetValue("launch_option", out var l) ? l.Obj("params") : null;
        LaunchOk = LaunchInfo.State(lop) == "all";
        LaunchText = LaunchInfo.Describe(lop);           // 今の Steam ユーザーで判定（2026-10-17 機能 SF12）
        await Task.CompletedTask;
    }

    private async Task SetRootAsync(string root)
    {
        if (string.IsNullOrWhiteSpace(root)) return;
        // 続けて取得先を変えたとき、前の取得先の遅い応答で画面を戻さない（点検 R20A-02）
        var seq = ++_rootSeq;
        try
        {
            var st = await GetStatusAsync(root);
            if (seq != _rootSeq) return;
            ApplyStatus(st);
            BuildEnvRows();
        }
        catch (Exception e) { if (seq == _rootSeq) App.ShowError(e); }
    }

    private async Task UseExistingAsync()
    {
        var d = new OpenFolderDialog { Title = L.T("Setup_UseExistingTitle") };
        if (d.ShowDialog() != true) return;
        var st = await _main.RequireClient().PostAsync("setup/use-existing", new JsonObject { ["root"] = d.FolderName });
        ApplyStatus(st!);
        BuildEnvRows();
        AppLog.Info(L.T("Log_Setup_UseExisting"));
        await _main.RefreshEnvironmentAsync(true);
        await _main.Settings.RefreshAsync(true);
    }

    private async Task ShowLicenseAsync(SetupComponent c)
    {
        var t = await _main.RequireClient().GetAsync($"setup/license/{c.Id}?lang={L.Language}");     // CLIP の注意文は表示言語のもの
        LicenseDialog.Show(t!);
    }

    private void ShowClipNotice()
    {
        var r = Dialogs.Show(new DialogSpec
        {
            // 注意文は表示言語のもの（英語の画面は英語。取得のときに、表示した注意文の言語と SHA-256 を同意として記録する。
            // 2026-10-18 未決定 #2）
            Kind = DialogKind.Warning, Title = L.T("Consent_Title"), Message = _clipNotice, CheckText = L.T("Consent_Check"),
            CheckRequired = true, PrimaryText = L.T("Consent_Accept"), SecondaryText = L.T("Dialog_Cancel"),
        });
        AcceptClip = r.Accepted && r.Checked;
    }

    private static void OpenUrl(string url)
    {
        if (!url.StartsWith("https://", StringComparison.Ordinal)) return;
        try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
        catch (Exception e) { AppLog.Warn(L.F("Log_Setup_BrowserFailed", e.Message)); }
    }

    private async Task NextAsync()
    {
        if (CurrentKey == "items")
        {
            // 取得しないで次へ（導入済み、または後で取得する）
            CurrentIndex = 2;
            return;
        }
        if (CurrentIndex < Steps.Count - 1) CurrentIndex++;
        if (CurrentKey is "refs" or "basemods" or "items") await RefreshAsync(true);
        if (CurrentKey == "done") BuildSummary();
    }

    private void Back()
    {
        if (CurrentIndex > 0) CurrentIndex--;
    }

    private async Task StartAsync()
    {
        // 取得先の欄を変えた直後（フォーカスの移動で読み込みが始まったところ）に押されたときは、新しい取得先の状態を待ってから
        // 開始できるか・確認が要るかを判定し直す（前の取得先の状態で確認を省いて、新しい取得先へ取得しない。点検 R20A-02）
        await _rootLoad;
        if (Root != _statusRoot)
        {
            await LoadStatusAsync(Root);
            BuildEnvRows();
        }
        if (!CanStart) return;
        if (!ConfirmStartWarnings()) return;
        if (!ConfirmForeignRoot()) return;
        if (!ConfirmReplaceExisting(out bool allowReplace)) return;
        var sel = Components.Where(c => c.Selected).ToList();
        var accepted = new JsonObject();
        foreach (var c in Components.Where(c => c.Selected)) accepted[c.Id] = c.LicenseSha;
        var p = new JsonObject
        {
            ["root"] = Root,
            ["components"] = new JsonArray(sel.Where(c => !c.Required).Select(c => (JsonNode)c.Id).ToArray()),
            ["accepted"] = accepted,
        };
        if (ClipSelected)
        {
            p["clip_notice_sha256"] = _clipSha;
            p["clip_notice_lang"] = _clipLang;
        }
        if (allowReplace)
        {
            // 既存の環境の venv の作り直し・ソースとパッケージの入れ替えに、確認で同意した（点検 R20A-01・R11B-01）
            p["allow_venv_rebuild"] = true;
            p["allow_replace"] = true;
        }
        CurrentIndex = 2;
        Installing = true;
        DownloadFailed = false;
        DownloadDone = false;
        LogLines.Clear();
        foreach (var c in Components) c.Run = c.Selected ? "waiting" : "";
        _startedAt = DateTime.Now;
        AppLog.Info(L.F("Log_Setup_Started", sel.Count));
        try
        {
            var r = await _main.RequireClient().RunJobAsync("setup", p, id => _jobId = id, new Progress<JobProgress>(OnProgress), OnEvent);
            DownloadDone = true;
            Overall = 100;
            OverallText = L.F("Setup_DownloadDone", (int)(DateTime.Now - _startedAt).TotalSeconds);
            CurrentText = "";
            foreach (var kv in r.Obj("components") ?? new JsonObject())
                if (Components.FirstOrDefault(x => x.Id == kv.Key) is { } c) c.Run = kv.Value?.GetValue<string>() ?? "";
            if (r?["status"] is JsonNode st) ApplyStatus(st);
            AppLog.Info(L.T("Log_Setup_DownloadDone"));
        }
        catch (OperationCanceledException)
        {
            OverallText = L.T("Setup_DownloadCancelled");
            DownloadFailed = true;
            EndRunningRows("cancelled");
        }
        catch (Exception e)
        {
            DownloadFailed = true;
            OverallText = L.T("Setup_DownloadFailed");
            EndRunningRows("failed");
            App.ShowError(e);
        }
        finally
        {
            Installing = false;
            _jobId = null;
            await _main.RefreshEnvironmentAsync(true);
            // 取消・失敗では結果の status が無いので読み直す（途中で入った項目の状態・合計・空き・ReadyImage。点検 R20A-04・R20B-04）
            if (!DownloadDone)
            {
                try { await LoadStatusAsync(Root); BuildEnvRows(); }
                catch (Exception e) { AppLog.Warn(L.F("Log_Setup_ReloadFailed", e.Message)); }
            }
            // 取得で変わるのは取得先の設定（trellis_root）と提供元の情報・同意だけ。生成の画面の設定は読み直さない（点検 R18A-02・R20B-03）。
            // 設定画面の欄は、未保存の入力を残して取得先を反映する（点検 R20A-01）
            await _main.Settings.ReloadSettingsAsync();
            await _main.Generate.RefreshProviderAsync();
        }
    }

    /// <summary>取消・失敗の後、取得中・照合中のまま残った行を直す（バックエンドは取消・失敗の後に進捗を送らない）。</summary>
    private void EndRunningRows(string run)
    {
        CurrentText = "";
        foreach (var c in Components.Where(c => c.Run is "running" or "verifying")) { c.Run = run; c.RunDetail = ""; }
        foreach (var c in Components.Where(c => c.Run == "waiting")) c.Run = "";
    }

    private void OnProgress(JobProgress p)
    {
        Overall = p.Progress * 100;
        var d = p.Detail;
        if (d == null) return;
        foreach (var kv in d.Obj("components") ?? new JsonObject())
            if (Components.FirstOrDefault(x => x.Id == kv.Key) is { } c && kv.Value is JsonValue v && v.TryGetValue<string>(out var s))
                c.Run = s;
        var cur = Components.FirstOrDefault(x => x.Id == d.Str("component"));
        string bytes = d.Long("done") is long done && d.Long("total") is long total && total > 0 ? L.F("Setup_Bytes", Fmt(done), Fmt(total)) : "";
        string speed = d.Long("speed") is long sp && sp > 0 ? L.F("Setup_Speed", Fmt(sp)) : "";
        if (cur != null) cur.RunDetail = string.Join("  ", new[] { bytes, speed }.Where(x => x.Length > 0));
        var stage = L.TryT("Stage_" + p.Stage) ?? p.Stage ?? "";
        CurrentText = cur == null ? stage
            : d.Str("file") is string f && f.Length > 0 ? L.F("Common_ParenFormat", $"{stage}: {cur.Name}", f) : $"{stage}: {cur.Name}";
        var elapsed = DateTime.Now - _startedAt;
        OverallText = L.F("Setup_Overall", (p.Progress * 100).ToString("0.0", CultureInfo.InvariantCulture),
            $"{(int)elapsed.TotalMinutes}:{elapsed.Seconds:00}");
    }

    private void OnEvent(JsonObject e)
    {
        if ((string?)e["kind"] != "log") return;
        var msg = AppLog.Redact((string?)e["message"] ?? "");
        AddLog(msg);
    }

    private void AddLog(string msg)
    {
        LogLines.Add(msg);
        while (LogLines.Count > 8) LogLines.RemoveAt(0);
    }

    private async Task PrepareRefsAsync()
    {
        if (!GameFound)
        {
            Dialogs.Info(L.T("Setup_GameMissingTitle"), L.T("Setup_GameMissing"), L.T("Setup_GameMissingHint"));
            return;
        }
        RefsBusy = true;
        RefsProgress = 0;
        try
        {
            var r = await _main.RequireClient().RunJobAsync("refs", new JsonObject(), id => _refsJob = id,
                new Progress<JobProgress>(p => { RefsProgress = p.Progress * 100; RefsText = StageNames.Describe(p.Stage, p.Detail); }));
            RefsText = L.T("Set_RefsReady");
            AppLog.Info(L.F("Log_Setup_RefsPrepared", r.Arr("extracted").Count));
        }
        catch (OperationCanceledException) { RefsText = L.T("Set_RefsCancelled"); }
        catch (Exception e) { RefsText = L.T("Set_RefsFailed"); App.ShowError(e); }
        finally
        {
            RefsBusy = false;
            _refsJob = null;
            await _main.RefreshEnvironmentAsync(true);
            await UpdateRefsAndLaunchAsync();
        }
    }

    private void BuildSummary()
    {
        SummaryLines.Clear();
        SummaryLines.Add(ReadyImage ? L.T("Setup_SumImageOk") : L.T("Setup_SumImageMissing"));
        var text = Components.Where(c => c.Group == "text").ToList();
        SummaryLines.Add(text.All(c => c.State == "installed") && _clipAccepted ? L.T("Setup_SumTextOk") : L.T("Setup_SumTextMissing"));
        SummaryLines.Add(RefsReady ? L.T("Setup_SumRefsOk") : _refsUnreadable ? L.T("Setup_SumRefsUnreadable")
            : _refsCorrupt ? L.T("Setup_SumRefsCorrupt") : L.T("Setup_SumRefsMissing"));
        SummaryLines.Add(LaunchOk ? L.T("Setup_SumLaunchOk") : L.T("Setup_SumLaunchMissing"));
        SummaryLines.Add(L.T("Setup_SumRedo"));
    }

    private async Task FinishAsync()
    {
        await _main.RequireClient().PostAsync("setup/mark", new JsonObject { ["state"] = "completed" });
        AppLog.Info(L.T("Log_Setup_Completed"));
        await _main.RefreshEnvironmentAsync(true);
        _main.Navigate("generate");
    }

    private async Task SkipAsync()
    {
        if (!Dialogs.Confirm(L.T("Setup_SkipTitle"), L.T("Setup_SkipMessage"), L.T("Setup_SkipDo"))) return;
        await _main.RequireClient().PostAsync("setup/mark", new JsonObject { ["state"] = "skipped" });
        AppLog.Info(L.T("Log_Setup_Skipped"));
        _main.Navigate("generate");
    }

    /// <summary>B-002: 生成が REFS_MISSING で止まったとき、エラーの画面から基準メッシュの準備を始める。</summary>
    public async Task OfferRefsAsync(ApiException e)
    {
        // あるが開けない基準ファイル（ほかのアプリが使用中・読み取りの権限が無い）は、ゲームからの準備では直らない（準備は
        // 開けないファイルを飛ばす）。準備を勧めず、エラーの文（ほかのアプリ・権限の案内）だけを出す（2026-11-04 回帰の確認 第 2 回 F1）
        if (e.Param("reason") == "refs_unreadable")
        {
            App.ShowError(e);
            return;
        }
        var err = ErrorText.From(e);
        var r = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Error, Title = err.Title, Message = err.Message, Hint = L.T("Setup_RefsOfferHint"),
            Detail = (string.IsNullOrWhiteSpace(err.Detail) ? "" : err.Detail + "\n\n") + L.F("Dialog_ErrorCode", err.Code),
            PrimaryText = L.T("Setup_RefsOfferDo"), SecondaryText = L.T("Dialog_Close"), ShowOpenLogs = true,
        });
        if (!r.Accepted) return;
        // 取得・準備の実行中は手順を変えず、実行中の手順を見せる（進捗と取消を隠さない。2 つ目の準備も始めない。点検 R20B-02）
        if (Busy)
        {
            ShowRunningStep();
            if (Installing) Dialogs.Info(L.T("Set_RefsTitle"), L.T("Setup_RefsOfferBusy"));
            return;
        }
        CurrentIndex = 3;
        _main.Navigate("setup");
        await RefreshAsync(true);
        await ((AsyncCommand)PrepareRefsCommand).ExecuteAsync();
    }

    /// <summary>取得・基準メッシュの準備の実行中の手順（進捗と取消のある手順）を表示する。</summary>
    private void ShowRunningStep()
    {
        if (Installing) CurrentIndex = 2;
        else if (RefsBusy) CurrentIndex = 3;
        _main.Navigate("setup");
    }

    /// <summary>
    /// 参照で選んだフォルダーから取得先を決める。ふつうはその中に trellis-runtime を作る（ドライブ直下や中身のあるフォルダーでも
    /// 専用のフォルダーになる）。選んだのが trellis-runtime フォルダー自体ならそのまま使う（入れ子の取得先に取り直さない。点検 R20B-06）。
    /// venv\Scripts\python.exe の有無では既存の実行環境とみなさない（ほかのアプリの venv のあるフォルダーが取得先になり、取得で
    /// その venv が作り直される。点検 R20B-01）。名前の違う既存の実行環境は「既存の TRELLIS 実行環境を使う…」で選ぶ。
    /// </summary>
    internal static string RootFromPicked(string picked)
    {
        var name = System.IO.Path.GetFileName(picked.TrimEnd('\\', '/'));
        if (name.Equals("trellis-runtime", StringComparison.OrdinalIgnoreCase))
            return picked;
        return System.IO.Path.Combine(picked, "trellis-runtime");
    }

    /// <summary>設定画面の「セットアップをやり直す」。</summary>
    public async Task RestartAsync()
    {
        // 取得・準備の実行中はやり直さない（手順 1 に戻すと進捗と取消が見えなくなる。点検 R20B-02・R20A-07）
        if (Busy)
        {
            ShowRunningStep();
            return;
        }
        await _main.RequireClient().PostAsync("setup/mark", new JsonObject { ["state"] = "reset" });
        _loaded = false;               // やり直しは設定の取得先から始める（点検 R20A-02）
        CurrentIndex = 0;
        AcceptLicenses = false;
        AcceptClip = false;
        DownloadDone = false;
        DownloadFailed = false;
        _main.Navigate("setup");
    }
}
