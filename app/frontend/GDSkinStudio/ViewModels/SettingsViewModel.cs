using System.Collections.ObjectModel;
using System.Text.Json.Nodes;
using System.Windows.Input;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using GDSkinStudio.Views;
using Microsoft.Win32;

namespace GDSkinStudio.ViewModels;

public sealed record EnvItem(string Id, string Status, string Label, string Message, string? Code);

/// <summary>設定画面の「導入済みの項目とライセンス」の 1 行（再読み込みでは同じ行の値だけを変える）。</summary>
public sealed class SetupRow(string id, string name) : ObservableObject
{
    public string Id { get; } = id;
    public string Name { get; } = name;
    private string _status = "", _stateText = "", _license = "", _version = "", _badge = "";
    public string Status { get => _status; set => Set(ref _status, value); }
    public string StateText { get => _stateText; set => Set(ref _stateText, value); }
    public string License { get => _license; set => Set(ref _license, value); }
    public string Version { get => _version; set => Set(ref _version, value); }
    public string Badge { get => _badge; set => Set(ref _badge, value); }
}

/// <summary>設定画面（インストール先・導入先・TRELLIS 実行環境・保存の選択・表示言語）と環境チェック、基準メッシュの準備。</summary>
public sealed class SettingsViewModel : ObservableObject
{
    private readonly MainViewModel _main;

    public SettingsViewModel(MainViewModel main)
    {
        _main = main;
        // 未接続でも押せる（画面だけの設定 = 表示言語・Python の場所は保存できる。SaveAsync。点検 UG-R3）
        SaveCommand = new AsyncCommand(SaveAsync);
        DetectCommand = new AsyncCommand(DetectAsync, () => _main.IsReady);
        RecheckCommand = new AsyncCommand(() => RefreshAsync(true), () => _main.IsReady);
        PrepareRefsCommand = new AsyncCommand(PrepareRefsAsync, () => _main.IsReady && !RefsBusy);
        CancelRefsCommand = new AsyncCommand(async () => { if (_refsJob != null) await _main.RequireClient().CancelJobAsync(_refsJob); },
            () => RefsBusy);
        ShowConsentCommand = new AsyncCommand(async () => { await ((AsyncCommand)_main.Generate.ShowConsentCommand).ExecuteAsync(); await RefreshAsync(true); },
            () => _main.IsReady);
        BrowseGameCommand = new RelayCommand(() => Pick(v => GameDir = v));
        BrowseTargetCommand = new RelayCommand(() => Pick(v => InstallTargetDir = v));
        BrowseTrellisCommand = new RelayCommand(() => Pick(v => TrellisRoot = v));
        BrowsePythonCommand = new RelayCommand(() =>
        {
            var d = new OpenFileDialog { Filter = "python.exe|python.exe", CheckFileExists = true };
            if (d.ShowDialog() == true) PythonPath = d.FileName;
        });
        OpenDataCommand = new RelayCommand(() => Dialogs.OpenFolder(App.Paths.DataHome));
        ShowToolLicenseCommand = new RelayCommand(() => ShowTextFile(["LICENSE.txt", "LICENSE"], L.T("License_ToolTitle")));
        // 英語の画面では英語の THIRD_PARTY_NOTICES.en.txt（無ければ日本語）。以前は表示言語に依らず日本語のファイルを開いた（2026-11-05）
        ShowNoticesCommand = new RelayCommand(() => ShowTextFile(
            L.Language == "en" ? ["THIRD_PARTY_NOTICES.en.txt", "THIRD_PARTY_NOTICES.txt"] : ["THIRD_PARTY_NOTICES.txt"], L.T("License_NoticesTitle")));
        OpenSetupCommand = new RelayCommand(() => _main.Navigate("setup"));
        // セットアップの取得・準備の実行中はやり直せない（進捗と取消が隠れる。点検 R20A-07・R20B-02）
        RedoSetupCommand = new AsyncCommand(() => _main.Setup.RestartAsync(), () => _main.IsReady && !_main.Setup.Busy);
        SetupLicenseCommand = new AsyncCommand(async p =>
        {
            var t = await _main.RequireClient().GetAsync($"setup/license/{((SetupRow)p!).Id}?lang={L.Language}");     // CLIP の注意文は表示言語のもの
            LicenseDialog.Show(t!);
        }, _ => _main.IsReady);
        // TRELLIS 実行環境の管理（点検 SF04・SF16）。アプリのセットアップが作った取得先のときだけ押せる
        // 照合し直し・入れ直しは、何か入っているときだけ（全体の削除で利用者のファイルだけが残った取得先は、取得先の印が
        // 残って対象のままになる。点検 U-D2-R3）
        // セットアップの取得中は押せない（ジョブが同じ列に並び、取得の終了直後に削除・入れ直しが走る。点検 R20A-07）
        MaintVerifyCommand = new AsyncCommand(() => RepairAsync(false), () => _main.IsReady && MaintAvailable && MaintAnyPresent && !MaintBusy && !_main.Setup.Installing);
        MaintReinstallCommand = new AsyncCommand(() => RepairAsync(true), () => _main.IsReady && MaintAvailable && MaintAnyPresent && !MaintBusy && !_main.Setup.Installing);
        MaintRemoveTextCommand = new AsyncCommand(() => RemoveAsync("text"), () => _main.IsReady && MaintAvailable && MaintTextPresent && !MaintBusy && !_main.Setup.Installing);
        MaintRemoveAllCommand = new AsyncCommand(() => RemoveAsync("all"), () => _main.IsReady && MaintAvailable && !MaintBusy && !_main.Setup.Installing);
        MaintCancelCommand = new AsyncCommand(async () => { if (_maintJob != null) await _main.RequireClient().CancelJobAsync(_maintJob); },
            () => MaintBusy);
        PythonPath = App.Ui.PythonPath;
        Language = L.NormalizeLanguage(App.Ui.Language);
        Storage = new StorageViewModel(main);
    }

    /// <summary>データの容量と整理（2026-10-17 機能 SF03）。</summary>
    public StorageViewModel Storage { get; }

    /// <summary>セットアップ（取得中の案内の表示に使う）。</summary>
    public SetupViewModel Setup => _main.Setup;

    public ICommand SaveCommand { get; }
    public ICommand DetectCommand { get; }
    public ICommand RecheckCommand { get; }
    public ICommand PrepareRefsCommand { get; }
    public ICommand CancelRefsCommand { get; }
    public ICommand ShowConsentCommand { get; }
    public ICommand BrowseGameCommand { get; }
    public ICommand BrowseTargetCommand { get; }
    public ICommand BrowseTrellisCommand { get; }
    public ICommand BrowsePythonCommand { get; }
    public ICommand OpenDataCommand { get; }
    public ICommand OpenSetupCommand { get; }
    public ICommand ShowToolLicenseCommand { get; }
    public ICommand ShowNoticesCommand { get; }

    // --- バージョン情報（D-024: ツール自体は MIT。著作権表示は csproj の <Copyright>） ---------------------------------
    private static readonly System.Reflection.Assembly Asm = typeof(SettingsViewModel).Assembly;
    public string AboutVersion => L.F("Set_AboutVersion",
        (System.Reflection.CustomAttributeExtensions.GetCustomAttribute<System.Reflection.AssemblyInformationalVersionAttribute>(Asm)?.InformationalVersion
         ?? Asm.GetName().Version?.ToString() ?? "").Split('+')[0]);
    public string AboutCopyright =>
        System.Reflection.CustomAttributeExtensions.GetCustomAttribute<System.Reflection.AssemblyCopyrightAttribute>(Asm)?.Copyright ?? "";

    /// <summary>アプリのフォルダー（配布物）またはリポジトリにある文章のファイルを表示する。</summary>
    private static void ShowTextFile(string[] names, string title)
    {
        var dirs = new[] { App.Paths.RepoRoot, App.Paths.AppBase }.Where(d => !string.IsNullOrEmpty(d)).Distinct();
        var path = dirs.SelectMany(d => names.Select(n => System.IO.Path.Combine(d!, n))).FirstOrDefault(System.IO.File.Exists);
        if (path == null)
        {
            Dialogs.Info(title, L.T("Set_AboutFileMissing"));
            return;
        }
        LicenseDialog.ShowText(title, title, path.Replace(App.Paths.AppBase, ""), System.IO.File.ReadAllText(path));
    }
    public ICommand RedoSetupCommand { get; }
    public ICommand SetupLicenseCommand { get; }

    // --- TRELLIS 実行環境の管理（点検 SF04・SF16） ------------------------------------------------------------------
    public ICommand MaintVerifyCommand { get; }
    public ICommand MaintReinstallCommand { get; }
    public ICommand MaintRemoveTextCommand { get; }
    public ICommand MaintRemoveAllCommand { get; }
    public ICommand MaintCancelCommand { get; }
    private string _maintRoot = "";
    private string? _maintJob;
    private bool _maintAvailable;
    /// <summary>使っている取得先が、アプリのセットアップが作ったもの（削除・修復の対象）か。</summary>
    public bool MaintAvailable { get => _maintAvailable; private set { if (Set(ref _maintAvailable, value)) CommandManager.InvalidateRequerySuggested(); } }
    private bool _maintTextPresent;
    public bool MaintTextPresent { get => _maintTextPresent; private set { if (Set(ref _maintTextPresent, value)) CommandManager.InvalidateRequerySuggested(); } }
    private bool _maintAnyPresent;
    /// <summary>取得先にアプリが作るもの（venv・models など）が 1 つでもあるか（status の maintenance.has_runtime。
    /// 無ければ照合し直し・入れ直しは押せない）。</summary>
    public bool MaintAnyPresent { get => _maintAnyPresent; private set { if (Set(ref _maintAnyPresent, value)) CommandManager.InvalidateRequerySuggested(); } }
    private bool _maintBusy;
    public bool MaintBusy { get => _maintBusy; private set { if (Set(ref _maintBusy, value)) CommandManager.InvalidateRequerySuggested(); } }
    private string _maintText = "";
    public string MaintText { get => _maintText; private set => Set(ref _maintText, value); }
    private double _maintProgress;
    public double MaintProgress { get => _maintProgress; private set => Set(ref _maintProgress, value); }
    private string _maintStatus = "";
    public string MaintStatus { get => _maintStatus; private set => Set(ref _maintStatus, value); }

    public ObservableCollection<SetupRow> SetupRows { get; } = new();
    private string _setupText = "";
    public string SetupText { get => _setupText; private set => Set(ref _setupText, value); }

    private string _gameDir = "";
    public string GameDir { get => _gameDir; set => Set(ref _gameDir, value); }
    private string _installTargetDir = "";
    public string InstallTargetDir { get => _installTargetDir; set => Set(ref _installTargetDir, value); }
    private string _trellisRoot = "";
    public string TrellisRoot { get => _trellisRoot; set => Set(ref _trellisRoot, value); }
    private bool _keepInputImage = true;
    public bool KeepInputImage { get => _keepInputImage; set => Set(ref _keepInputImage, value); }
    private bool _keepDrafts;
    public bool KeepDrafts { get => _keepDrafts; set => Set(ref _keepDrafts, value); }
    private bool _buildJapanese;
    public bool BuildJapanese { get => _buildJapanese; set => Set(ref _buildJapanese, value); }
    private bool _includeDev;
    public bool IncludeDev { get => _includeDev; set => Set(ref _includeDev, value); }
    private string _pythonPath = "";
    public string PythonPath { get => _pythonPath; set => Set(ref _pythonPath, value); }
    // 表示言語（2026-10-17 追加機能 SF08）。名前は表示言語によらず、その言語自身の書き方で出す。既定は日本語。
    // 切り替えはアプリの再起動の後に効く（XAML の {l:Tr} は画面を作るときに 1 回だけ読むため）
    public IReadOnlyList<Choice<string>> Languages { get; } = [new("ja", "日本語"), new("en", "English")];
    private string _language = "ja";
    public string Language { get => _language; set => Set(ref _language, value); }

    public string DataHome => App.Paths.DataHome;
    public string LibraryDir => _main.PathsInfo.Str("library") ?? "";
    public string DetectedGame { get => _detectedGame; private set => Set(ref _detectedGame, value); }
    private string _detectedGame = "";

    public ObservableCollection<EnvItem> EnvItems { get; } = new();
    private string _envChecked = "";
    public string EnvChecked { get => _envChecked; private set => Set(ref _envChecked, value); }

    private bool _refsReady;
    public bool RefsReady { get => _refsReady; private set => Set(ref _refsReady, value); }
    private bool _refsBusy;
    public bool RefsBusy { get => _refsBusy; private set { if (Set(ref _refsBusy, value)) CommandManager.InvalidateRequerySuggested(); } }
    private double _refsProgress;
    public double RefsProgress { get => _refsProgress; private set => Set(ref _refsProgress, value); }
    private string _refsText = "";
    public string RefsText { get => _refsText; private set => Set(ref _refsText, value); }
    private string? _refsJob;

    private bool _consentAccepted;
    public bool ConsentAccepted { get => _consentAccepted; private set => Set(ref _consentAccepted, value); }
    private string _consentText = "";
    public string ConsentText { get => _consentText; private set => Set(ref _consentText, value); }

    private string _savedText = "";
    public string SavedText { get => _savedText; private set => Set(ref _savedText, value); }
    /// <summary>表示言語・Python の場所の変更を ui_settings.json に保存できなかった（次に保存できたとき再起動の案内を出す）。</summary>
    private bool _uiRestartPending;

    private bool _loaded;

    /// <summary>欄に対応するバックエンドの設定の値。</summary>
    private sealed record SettingsFields(string GameDir, string InstallTargetDir, string TrellisRoot, bool KeepInputImage, bool KeepDrafts,
        bool BuildJapanese, bool IncludeDev);
    /// <summary>最後にバックエンドから読んだ（保存した）設定の値。これと違う欄は、利用者が変えてまだ保存していない欄。</summary>
    private SettingsFields? _saved;

    /// <summary>
    /// バックエンドの設定を欄に入れる。overwriteEdited が false なら、利用者が変えてまだ保存していない欄は残す（再チェック・同意・
    /// 準備・修復の後に入力が黙って消えないように。バックエンドが書き換えた値＝取得先の削除・既存の環境の設定・取得の後の
    /// trellis_root は、変えていない欄にだけ反映する。点検 R20A-03・R20B-01）。
    /// </summary>
    private void ApplySettings(JsonNode? s, bool overwriteEdited)
    {
        var n = new SettingsFields(s.Str("game_dir") ?? "", s.Str("install_target_dir") ?? "", s.Str("trellis_root") ?? "",
            s.Bool("keep_input_image", true), s.Bool("keep_drafts"), s.Strings("build_languages").Contains("JA"), s.Bool("include_dev_equipment"));
        var o = overwriteEdited ? null : _saved;
        if (o == null || GameDir == o.GameDir) GameDir = n.GameDir;
        if (o == null || InstallTargetDir == o.InstallTargetDir) InstallTargetDir = n.InstallTargetDir;
        if (o == null || TrellisRoot == o.TrellisRoot) TrellisRoot = n.TrellisRoot;
        if (o == null || KeepInputImage == o.KeepInputImage) KeepInputImage = n.KeepInputImage;
        if (o == null || KeepDrafts == o.KeepDrafts) KeepDrafts = n.KeepDrafts;
        if (o == null || BuildJapanese == o.BuildJapanese) BuildJapanese = n.BuildJapanese;
        if (o == null || IncludeDev == o.IncludeDev) IncludeDev = n.IncludeDev;
        _saved = n;
    }

    /// <summary>セットアップの取得の後: バックエンドが書き換えた設定（trellis_root）を欄に反映する（未保存の入力は残す。点検 R20A-01）。</summary>
    public async Task ReloadSettingsAsync()
    {
        if (!_loaded || _main.Client is not { } c) return;        // まだ開いていなければ、開いたときに読む
        ApplySettings(await c.GetAsync("settings"), false);
    }

    public async Task RefreshAsync(bool forceEnv)
    {
        var c = _main.Client;
        if (c == null) return;
        if (!_loaded || forceEnv)
        {
            ApplySettings(await c.GetAsync("settings"), !_loaded);
            _loaded = true;
        }
        await _main.RefreshEnvironmentAsync(forceEnv);
        var env = _main.Environment;
        EnvItems.Clear();
        foreach (var it in env.Arr("items"))
        {
            var id = it.Str("id") ?? "";
            var label = L.TryT("Env_" + id.Replace('.', '_')) ?? id;
            EnvItems.Add(new EnvItem(id, it.Str("status") ?? "", label, AppLog.Redact(it.Str("message") ?? ""), it.Str("code")));
        }
        EnvChecked = L.F("Set_EnvChecked", EquipmentItem.FormatTime(env.Str("checked_at")));
        var refs = env.Arr("items").FirstOrDefault(i => i.Str("id") == "refs");
        RefsReady = refs.Str("status") == "ok";
        // あるが開けない基準ファイルは「まだありません」と言わない（ゲームからの準備では直らない。2026-11-04 回帰の確認 第 2 回 F1）
        var unreadable = !RefsReady && refs.Obj("params").Str("reason") == "refs_unreadable";
        // 壊れている基準ファイルも「まだありません」と言わない（取り出し直しを案内する。2026-11-04 段階 H HR_user-10）
        var corrupt = !RefsReady && refs.Obj("params").Str("reason") == "refs_corrupt";
        if (!RefsBusy)
            RefsText = RefsReady ? L.T("Set_RefsReady") : unreadable ? L.T("Set_RefsUnreadable")
                : corrupt ? L.T("Set_RefsCorrupt") : L.T("Set_RefsMissing");
        await RefreshSetupAsync(c);
        var st = await c.GetAsync("consent/clip");
        ConsentAccepted = st.Bool("accepted");
        ConsentText = ConsentAccepted ? L.F("Set_ConsentAccepted", EquipmentItem.FormatTime(st.Str("accepted_at"))) : L.T("Set_ConsentMissing");
        OnPropertyChanged(nameof(LibraryDir));
    }

    private async Task RefreshSetupAsync(BackendClient c)
    {
        var st = await c.GetAsync("setup/status");
        var comps = st.Arr("components").ToList();
        if (SetupRows.Select(r => r.Id).SequenceEqual(comps.Select(x => x.Str("id") ?? "")) == false)
        {
            SetupRows.Clear();
            foreach (var x in comps) SetupRows.Add(new SetupRow(x.Str("id") ?? "", x.Str("name") ?? ""));
        }
        foreach (var (row, x) in SetupRows.Zip(comps))
        {
            var state = x.Str("state") ?? "missing";
            row.Status = state == "installed" ? "ok" : state == "missing" ? "missing" : "warning";
            row.StateText = L.TryT("Setup_State_" + state) ?? state;
            row.License = x.Str("license") ?? "";
            row.Version = x.Str("version") ?? "";
            row.Badge = SetupComponent.BadgeFor(x.Arr("flags").Select(f => f.Str("kind") ?? ""));
        }
        var done = st.Str("completed_at");
        SetupText = L.F("Set_SetupState", AppLog.Redact(st.Str("root") ?? ""),
            done != null ? L.F("Set_SetupDone", EquipmentItem.FormatTime(done)) : L.T("Set_SetupNotDone"));
        // 削除・修復の対象（点検 SF04・SF16）: status の maintenance（アプリが作った取得先か）と、テキスト→3D の項目の有無
        var mt = st.Obj("maintenance");
        _maintRoot = st.Str("root") ?? "";
        MaintAvailable = mt.Bool("app_created") && mt.Bool("exists");
        MaintTextPresent = comps.Any(x => x.Str("group") == "text" && (x.Str("state") ?? "missing") != "missing");
        MaintAnyPresent = mt.Bool("has_runtime");
        var reason = mt.Bool("app_created") && !mt.Bool("exists") ? "not_found" : mt.Str("reason") ?? "not_app_root";
        MaintText = MaintAvailable ? L.T(MaintAnyPresent ? "Maint_Available" : "Maint_Empty")
            : L.TryT("Maint_Reason_" + reason) ?? L.T("Maint_Reason_not_app_root");
    }

    // --- TRELLIS 実行環境の管理（点検 SF04・SF16。対象・ジョブの実行中の断りはバックエンドが決める） -------------------
    private string SetupName(string id) => SetupRows.FirstOrDefault(r => r.Id == id)?.Name ?? id;

    /// <summary>
    /// 削除・修復の確認の文の先頭: 対象の取得先（plan の root）。「TRELLIS 実行環境」の欄を変えてまだ保存していなければ、対象は
    /// 欄の値ではなく保存済みの取得先であることも添える（欄の値の環境が対象だと誤解して、使っている実行環境を消さない。点検 R20A-03）。
    /// </summary>
    private string MaintTargetText(JsonNode plan)
    {
        var text = L.F("Maint_TargetRoot", AppLog.Redact(plan.Str("root") ?? _maintRoot));
        if (_saved != null && TrellisRoot.Trim() != _saved.TrellisRoot) text += "\n" + L.T("Maint_UnsavedRootNote");
        return text + "\n\n";
    }

    /// <summary>削除・修復のジョブを実行する。失敗・取消は画面に出して null。</summary>
    private async Task<JsonNode?> RunMaintAsync(string type, JsonObject p)
    {
        MaintBusy = true;
        MaintProgress = 0;
        MaintStatus = L.T("Maint_Running");
        try
        {
            var r = await _main.RequireClient().RunJobAsync(type, p, id => _maintJob = id,
                new Progress<JobProgress>(x => { MaintProgress = x.Progress * 100; MaintStatus = StageNames.Describe(x.Stage, x.Detail); }));
            MaintStatus = L.T("Maint_Done");
            return r;
        }
        catch (OperationCanceledException) { MaintStatus = L.T("Maint_Cancelled"); return null; }
        catch (Exception e) { MaintStatus = L.T("Maint_Failed"); App.ShowError(e); return null; }
        finally
        {
            MaintBusy = false;
            _maintJob = null;
            await RefreshAsync(true);
        }
    }

    private async Task RepairAsync(bool venv)
    {
        var plan = await _main.RequireClient().GetAsync("setup/repair-plan?root=" + Uri.EscapeDataString(_maintRoot));
        if (plan == null) return;
        if (!plan.Bool("app_created"))
        {
            Dialogs.Info(L.T("Maint_Title"), MaintText);
            return;
        }
        string message = MaintTargetText(plan) + (venv
            ? L.F("Maint_ReinstallConfirm", SetupViewModel.Fmt(plan.Long("venv_download_bytes") ?? 0), SetupViewModel.Fmt(plan.Long("venv_need_bytes") ?? 0),
                  plan.Long("free_bytes") is long f ? SetupViewModel.Fmt(f) : "-")
            : L.F("Maint_VerifyConfirm", plan.Arr("models").Count, SetupViewModel.Fmt(plan.Long("verify_bytes") ?? 0)));
        if (!Dialogs.Confirm(L.T(venv ? "Maint_ReinstallTitle" : "Maint_VerifyTitle"), message, L.T(venv ? "Maint_ReinstallDo" : "Maint_VerifyDo"),
                L.T(venv ? "Maint_ReinstallHint" : "Maint_VerifyHint"), venv ? DialogKind.Warning : DialogKind.Question)) return;
        AppLog.Info(L.T(venv ? "Log_Maint_ReinstallStarted" : "Log_Maint_VerifyStarted"));
        var r = await RunMaintAsync("setup_repair", new JsonObject { ["root"] = _maintRoot, [venv ? "venv" : "models"] = true });
        if (r == null) return;
        var lines = new List<string>();
        if (venv) lines.Add(L.T("Maint_ReinstallDone"));
        else
        {
            long n = r.Long("refetched_count") ?? 0;
            // 合わないファイルを取り直さなかった（needs_setup）ときは「すべて合っていた」と出さない
            if (n > 0) lines.Add(L.F("Maint_VerifyRepaired", n));
            else if (r.Obj("needs_setup") is not { Count: > 0 }) lines.Add(L.T("Maint_VerifyOk"));
        }
        if (r.Obj("needs_setup") is { Count: > 0 } needs)
            lines.Add(L.F("Maint_NeedsSetup", string.Join(L.T("Common_ListSep"), needs.Select(kv => SetupName(kv.Key)))));
        AppLog.Info(L.F("Log_Maint_RepairDone", string.Join(" / ", lines)));
        Dialogs.Info(L.T("Maint_DoneTitle"), string.Join("\n", lines));
    }

    private async Task RemoveAsync(string scope)
    {
        var plan = await _main.RequireClient().GetAsync($"setup/remove-plan?scope={scope}&root=" + Uri.EscapeDataString(_maintRoot));
        if (plan == null) return;
        if (!plan.Bool("app_created"))
        {
            Dialogs.Info(L.T("Maint_Title"), MaintText);
            return;
        }
        if (plan.Bool("nothing"))
        {
            Dialogs.Info(L.T("Maint_Title"), L.T("Maint_RemoveNothing"));
            return;
        }
        var items = plan.Arr("entries").Where(e => e.Bool("exists"))
            .Select(e => L.F("Maint_RemoveItem", e.Str("path") ?? "", SetupViewModel.Fmt(e.Long("bytes") ?? 0), e.Long("files") ?? 0)).ToList();
        var sections = new List<DialogSection> { new() { Header = L.T("Maint_RemoveItemsHeader"), Items = items } };
        var kept = plan.Strings("kept").ToList();
        if (kept.Count > 0) sections.Add(new DialogSection { Header = L.T("Maint_RemoveKeptHeader"), Items = kept });
        long shared = plan.Long("shared_bytes") ?? 0;
        string message = MaintTargetText(plan)
                         + L.F(scope == "text" ? "Maint_RemoveTextConfirm" : "Maint_RemoveAllConfirm", SetupViewModel.Fmt(plan.Long("freed_bytes") ?? 0))
                         + (shared > 0 ? "\n" + L.F("Maint_RemoveShared", SetupViewModel.Fmt(shared)) : "")
                         + (plan.Bool("clip_consent_removed") ? "\n" + L.T("Maint_RemoveClipConsent") : "");
        var ok = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Warning, Title = L.T(scope == "text" ? "Maint_RemoveTextTitle" : "Maint_RemoveAllTitle"), Message = message,
            Hint = L.T(scope == "text" ? "Maint_RemoveTextHint" : "Maint_RemoveAllHint"), Sections = sections,
            CheckText = L.T("Maint_RemoveCheck"), CheckRequired = true, PrimaryText = L.T("Maint_RemoveDo"), SecondaryText = L.T("Dialog_Cancel"),
        });
        if (!(ok.Accepted && ok.Checked)) return;
        AppLog.Info(L.F("Log_Maint_RemoveStarted", scope));
        var r = await RunMaintAsync("setup_remove", new JsonObject { ["root"] = _maintRoot, ["scope"] = scope });
        if (r == null) return;
        var lines = new List<string> { L.F("Maint_RemoveDone", SetupViewModel.Fmt(r.Long("freed_bytes") ?? 0)) };
        if ((r.Long("failed_count") ?? 0) > 0) lines.Add(L.F("Maint_RemoveFailed", r.Long("failed_count") ?? 0));
        var keptAfter = r.Strings("kept").ToList();
        if (keptAfter.Count > 0) lines.Add(L.F("Maint_RemoveKept", string.Join(", ", keptAfter.Take(10))));
        if (scope == "all") lines.Add(L.T("Maint_RemoveAllAfter"));
        AppLog.Info(L.F("Log_Maint_RemoveDone", string.Join(" / ", lines)));
        Dialogs.Info(L.T("Maint_DoneTitle"), string.Join("\n", lines));
    }

    private static void Pick(Action<string> set)
    {
        var d = new OpenFolderDialog();
        if (d.ShowDialog() == true) set(d.FolderName);
    }

    private async Task DetectAsync()
    {
        var r = await _main.RequireClient().GetAsync("settings/detect-game");
        var found = r.Strings("found").ToList();
        DetectedGame = found.Count == 0 ? L.T("Set_DetectNone") : L.F("Set_DetectFound", string.Join(", ", found));
        if (found.Count > 0) GameDir = "";          // 空欄 = 自動検出を使う
    }

    /// <summary>設定を保存する。バックエンドの設定を保存できたら true（未接続・保存の失敗は false）。</summary>
    private async Task<bool> SaveAsync()
    {
        SavedText = "";        // 前回の「保存しました。」を、今回の保存の結果が出るまで残さない（点検 R20A-04）
        // バックエンドの設定は接続しているときだけ送る。画面だけの設定（表示言語・Python の場所。ui_settings.json）は未接続でも
        // 保存する（Python の不具合などでバックエンドを起動できないときも、表示言語を変えたり Python の場所を直したりできるように。
        // 直した Python の場所は帯の「バックエンドを再起動」で使われる。点検 UG-R3）。接続中にバックエンドの保存が失敗した
        // （settings.json に書けない・バックエンドが応答しない）ときも、画面だけの設定は保存する（点検 R20A-04）
        bool connected = _main.IsReady;
        ApiException? putError = null;
        if (connected && !_loaded)
        {
            // 設定の読み込みが遅れた・失敗した画面で保存すると、空の欄で game_dir・install_target_dir・trellis_root を上書きした
            // （R21A-01）。先にバックエンドの設定を読み、初期値から変えていない欄だけを埋めてから保存する
            _saved ??= new SettingsFields("", "", "", true, false, false, false);
            try
            {
                ApplySettings(await _main.RequireClient().GetAsync("settings"), false);
                _loaded = true;
            }
            catch (ApiException ge) { putError = ge; }      // 読めなければ送らない（下で保存の失敗として出す）
        }
        if (connected && putError == null)
        {
            var body = new JsonObject
            {
                ["game_dir"] = GameDir.Trim(), ["install_target_dir"] = InstallTargetDir.Trim(), ["trellis_root"] = TrellisRoot.Trim(),
                ["keep_input_image"] = KeepInputImage, ["keep_drafts"] = KeepDrafts,
                ["build_languages"] = BuildJapanese ? new JsonArray("EN", "JA") : new JsonArray("EN"),
                ["include_dev_equipment"] = IncludeDev,
            };
            // 保存した値をそのまま欄と控えにする（以後の再読み込みで、保存した欄を変えていない欄として扱う）
            // 失敗したときは欄と控えを変えない（未保存の欄として残る）
            try { ApplySettings(await _main.RequireClient().PutAsync("settings", body), true); }
            catch (ApiException e) { putError = e; }
        }
        // 表示言語は正規化した値で比べる（ui_settings.json の値が "en-US"・不正な値などのとき、変えていないのに再起動の案内を
        // 出さない。点検 UG-R2）
        bool restart = _uiRestartPending || App.Ui.PythonPath != PythonPath.Trim() || L.NormalizeLanguage(App.Ui.Language) != Language;
        App.Ui.PythonPath = PythonPath.Trim();
        App.Ui.Language = Language;
        App.Ui.Save(App.Paths.UiSettingsPath);
        // Save は失敗をログに書くだけなので、読み直して次の起動で使われる値になったかを確かめる（読み取り専用・ディスクがいっぱい
        // などで「保存しました」と出さない。点検 R20B-05）
        var back = UiSettings.Load(App.Paths.UiSettingsPath);
        bool uiSaved = back.PythonPath == App.Ui.PythonPath && back.Language == App.Ui.Language;
        _uiRestartPending = restart && !uiSaved;       // もう一度保存して成功したときに、再起動の案内を出す
        if (!uiSaved) AppLog.Warn(L.T("Log_Set_UiSaveFailed"));
        if (putError != null)
        {
            SavedText = L.T(uiSaved ? "Set_BackendSaveFailed" : "Set_SaveFailedAll");
            AppLog.Warn(L.T(uiSaved ? "Log_Set_BackendSaveFailed" : "Log_Set_SaveFailedAll"));
            App.ShowError(putError);
            return false;
        }
        if (!connected && !uiSaved)
        {
            SavedText = L.T("UG_UiSaveFailed");
            return false;
        }
        if (!connected)
        {
            SavedText = L.T("UG_SavedUiOnly");
            AppLog.Info(L.T("Log_Set_SavedUiOnly"));
            return false;
        }
        _main.Generate.KeepImage = KeepInputImage;
        SavedText = !uiSaved ? L.T("Set_UiSaveFailed") : restart ? L.T("Set_SavedRestart") : L.T("Set_Saved");
        AppLog.Info(L.T("Log_Set_Saved"));
        await RefreshAsync(true);
        await _main.Mod.RefreshAsync();
        return true;
    }

    private async Task PrepareRefsAsync()
    {
        // 準備のジョブは保存済みの Grim Dawn のインストール先を使うので、欄を変えて保存していなければ、保存してから準備する
        // （入力した場所が使われずに GAME_NOT_FOUND になるのを防ぐ。点検 R20A-03）
        bool unsavedGame = _saved != null && GameDir.Trim() != _saved.GameDir;
        if (unsavedGame)
        {
            if (!Dialogs.Confirm(L.T("Set_RefsTitle"), L.T("Set_RefsUnsavedGame") + "\n\n" + L.T("Set_RefsConfirm"), L.T("Set_RefsSaveAndStart"),
                    L.T("Set_RefsHint"))) return;
            if (!await SaveAsync()) return;         // 保存できなければ（エラーは表示済み）、入力した場所を使えないので始めない
        }
        else if (!Dialogs.Confirm(L.T("Set_RefsTitle"), L.T("Set_RefsConfirm"), L.T("Set_RefsStart"), L.T("Set_RefsHint"))) return;
        RefsBusy = true;
        RefsProgress = 0;
        try
        {
            var r = await _main.RequireClient().RunJobAsync("refs", new JsonObject(), id => _refsJob = id,
                new Progress<JobProgress>(p => { RefsProgress = p.Progress * 100; RefsText = StageNames.Describe(p.Stage, p.Detail); }));
            RefsText = L.T("Set_RefsReady");
            AppLog.Info(L.F("Log_Set_RefsPrepared", r.Arr("extracted").Count));
        }
        catch (OperationCanceledException) { RefsText = L.T("Set_RefsCancelled"); }
        catch (Exception e) { RefsText = L.T("Set_RefsFailed"); App.ShowError(e); }
        finally
        {
            RefsBusy = false;
            _refsJob = null;
            await RefreshAsync(true);
        }
    }
}
