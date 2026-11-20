using System.Collections.ObjectModel;
using System.Text.Json.Nodes;
using System.Windows.Input;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using GDSkinStudio.Views;
using Microsoft.Win32;

namespace GDSkinStudio.ViewModels;

public sealed record FileStatus(string Name, string Kind, string KindText, string Detail, string SizeText);

/// <summary>照合の詳細のファイルごとの短いコード（導入中の MOD と最新のビルド。2026-10-17 機能 SF13）。</summary>
public sealed record FileCodeRow(string Name, string Installed, string Latest, string Note);

/// <summary>MOD の作成・出力・導入・アンインストール（計画書 11 章、D-002）。導入の前に確認ダイアログを出す。</summary>
public sealed class ModViewModel : ObservableObject
{
    private readonly MainViewModel _main;

    public ModViewModel(MainViewModel main)
    {
        _main = main;
        RefreshCommand = new AsyncCommand(() => RefreshAsync(), () => _main.IsReady && !IsBusy);
        BuildCommand = new AsyncCommand(BuildAsync, () => _main.IsReady && !IsBusy && !_operating);
        CancelCommand = new AsyncCommand(async () => { if (JobId != null) await _main.RequireClient().CancelJobAsync(JobId); },
            () => IsBusy && JobId != null && _jobCancellable);
        ExportCommand = new AsyncCommand(() => OperateAsync(ExportAsync), () => _main.IsReady && !IsBusy && !_operating && HasBuild);
        InstallCommand = new AsyncCommand(() => OperateAsync(InstallAsync), () => _main.IsReady && !IsBusy && !_operating && HasBuild);
        UninstallCommand = new AsyncCommand(() => OperateAsync(UninstallAsync), () => _main.IsReady && !IsBusy && !_operating && IsInstalled);
        GuideCommand = new RelayCommand(ShowGuide);
        ShareExportCommand = new AsyncCommand(() => OperateAsync(ShareExportAsync), () => _main.IsReady && !IsBusy && !_operating && HasBuild);
        ShareImportCommand = new AsyncCommand(() => OperateAsync(() => ShareImportAsync()), () => _main.IsReady && !IsBusy && !_operating);
        ShareRestoreCommand = new AsyncCommand(() => OperateAsync(ShareRestoreAsync),
            () => _main.IsReady && !IsBusy && !_operating && CanRestorePrevious);
        OpenTargetCommand = new RelayCommand(OpenTarget, () => TargetPath != null);
        // ジョブの間は比べない（導入の途中の状態で比べた結果が残らないように。R19A-03）
        CompareCodeCommand = new AsyncCommand(CompareCodeAsync, () => _main.IsReady && !IsBusy && !string.IsNullOrWhiteSpace(FriendCode));
    }

    /// <summary>相手が伝えた照合コードを、導入中の MOD・最新のビルド・受け取った MOD と比べる（2026-10-17 機能 SF13）。</summary>
    public ICommand CompareCodeCommand { get; }

    public ICommand RefreshCommand { get; }
    public ICommand BuildCommand { get; }
    public ICommand CancelCommand { get; }
    public ICommand ExportCommand { get; }
    public ICommand InstallCommand { get; }
    public ICommand UninstallCommand { get; }
    public ICommand GuideCommand { get; }
    public ICommand OpenTargetCommand { get; }
    public ICommand ShareExportCommand { get; }
    public ICommand ShareImportCommand { get; }
    public ICommand ShareRestoreCommand { get; }

    // --- マルチプレイ用の共有（.gdskinmod。docs/app_architecture.md 15.2 節） ------------------------------------------
    public JsonObject? SharedInstalled => Status.Obj("installed").Obj("shared");
    public bool CanRestorePrevious => Status.Obj("installed").Obj("previous") != null && SharedInstalled != null;
    // 照合コードは MOD 全体のコード（mod_code）。check_code は database.arz だけのコード（データベースのコード）で、詳細の
    // 欄・ダイアログのファイルの情報にだけ出す（2026-10-17 監査 C15 の利用者の判断）
    public string SharedText => TargetUnavailable ? L.T("ModG19_InstallStateUnknown")
        : SharedInstalled is JsonObject s
        ? L.F("Share_InstalledShared", s.Str("file_name"), s.Arr("equipment").Count, s.Str("mod_code") ?? s.Str("check_code"),
              PreviousText(Status.Obj("installed").Obj("previous")))
        : IsInstalled ? L.T("Share_InstalledOwn") : L.T("Mod_NotInstalled");

    /// <summary>「前の状態に戻す」の戻り先。受け取った MOD なら「あなたの MOD」と言わない（2026-10-17 監査 C12）。</summary>
    private static string PreviousText(JsonNode? prev) => prev.Str("build_id") is string b
        ? (prev.Bool("shared") ? L.F("Share_PreviousShared", b) : L.F("Share_PreviousOwn", b))
        : L.T("Share_PreviousNone");
    public string CheckCodeText
    {
        get
        {
            var lines = new List<string>();
            if (Status.Obj("installed").Str("mod_code") is string c) lines.Add(L.F("Share_CodeInstalled", c));
            if (Status.Obj("latest_build").Str("mod_code") is string b) lines.Add(L.F("Share_CodeLatest", b, Status.Obj("latest_build").Str("build_id")));
            return lines.Count == 0 ? "—" : string.Join("\n", lines);
        }
    }

    private JsonNode? _status;
    public JsonNode? Status
    {
        get => _status;
        private set
        {
            Set(ref _status, value);
            foreach (var n in new[] { nameof(TargetPath), nameof(IsTestTarget), nameof(TargetOk), nameof(GameRunning), nameof(LaunchText),
                                      nameof(LaunchOk), nameof(InstalledText), nameof(IsInstalled), nameof(HasBuild), nameof(BuildText),
                                      nameof(BuildFilesText), nameof(GameLanguageText), nameof(SharedInstalled), nameof(CanRestorePrevious),
                                      nameof(SharedText), nameof(CheckCodeText), nameof(GameChanged), nameof(BackupsText),
                                      nameof(GameChangedText), nameof(ShowRebuildButton), nameof(ShowInstallLatestButton),
                                      nameof(GameVersionText), nameof(AiskinNoteText), nameof(HasAiskinNote), nameof(CompareCodesText),
                                      nameof(TargetUnavailable), nameof(LatestNotInstalled), nameof(LatestNotInstalledText) })
                OnPropertyChanged(n);
            // 比べた後に照合コードの組（導入中の MOD・最新のビルド）が変わったら、比較の結果は前の状態の判定なので消す。ページに
            // 戻る・更新を押すだけ（組が同じ）なら残す（R19A-03）
            if (CompareResultText.Length > 0 && CodesKey(value) != _comparedKey) CompareResultText = "";
            Files.Clear();
            foreach (var kv in value.Obj("files") ?? new JsonObject())
            {
                var kind = kv.Value.Str("kind") ?? "";
                var size = kv.Value.Long("size");
                Files.Add(new FileStatus(kv.Key, kind, L.TryT("FileKind_" + kind) ?? kind, AppLog.Redact(kv.Value.Str("detail") ?? ""),
                    size is long s ? $"{s:N0} B" : ""));
            }
            // mods\ 直下の他の .arc・.arz（このアプリは触らないが、/basemods で読み込まれ続ける。2026-10-17 監査 C85）
            foreach (var o in value.Arr("basemods_others"))
                Files.Add(new FileStatus(o.Str("name") ?? "", "basemods_other", L.T("FileKind_basemods_other"), L.T("Mod_OtherFileDetail"),
                    o.Long("size") is long s ? $"{s:N0} B" : ""));
            // ファイルごとの短いコード（導入中の MOD と最新のビルド。2026-10-17 機能 SF13）
            FileCodes.Clear();
            var ic = value.Obj("installed").Obj("file_codes");
            var lc = value.Obj("latest_build").Obj("file_codes");
            var names = (ic?.Select(kv => kv.Key) ?? []).Union(lc?.Select(kv => kv.Key) ?? [])
                .OrderBy(n => n == "database.arz" ? "" : n, StringComparer.Ordinal);
            foreach (var name in names)
            {
                string? a = ic.Str(name), b = lc.Str(name);
                var note = ic == null || lc == null ? ""
                    : a == null ? L.T("UE_NoteOnlyLatest") : b == null ? L.T("UE_NoteOnlyInstalled") : a != b ? L.T("UE_NoteDiffer") : "";
                FileCodes.Add(new FileCodeRow(name == "database.arz" ? name + " " + L.T("UE_NoteDatabase") : name, a ?? "—", b ?? "—", note));
            }
        }
    }

    // --- ゲームの更新の目安と作り直しの案内（2026-10-17 機能 SF09。判定は補給官のレコードの中身、Steam のビルド番号は目安） ---
    private JsonObject? InstalledUpdate => Status.Obj("installed").Obj("game_update");
    private JsonObject? LatestUpdate => Status.Obj("latest_build").Obj("game_update");

    /// <summary>導入した後にゲームが更新され、補給官のレコードが MOD と違う（2026-10-17 監査 C84）。最新のビルドが今のゲームと
    /// 違う場合も、導入する前に作り直すよう案内する（機能 SF09）。</summary>
    public bool GameChanged => Status.Obj("installed").Arr("game_changed").Count > 0 || LatestUpdate.Arr("records_changed").Count > 0;
    private bool InstalledChanged => Status.Obj("installed").Arr("game_changed").Count > 0;
    /// <summary>導入中の MOD は受け取った MOD（相手の .gdskinmod）で、補給官のレコードが古い。自分の MOD を作り直す・導入すると
    /// 相手の MOD を置き換えて照合コードが合わなくなるので、作った人に作り直してもらうよう案内し、ボタンは出さない（UE-R1）。</summary>
    private bool SharedChanged => InstalledChanged && (InstalledUpdate.Str("guidance") == "shared_rebuild" || SharedInstalled != null);
    private bool InstallLatestFixes => InstalledChanged && !SharedChanged && InstalledUpdate.Str("guidance") == "install_latest";
    public bool ShowRebuildButton => GameChanged && !InstallLatestFixes && !SharedChanged;
    public bool ShowInstallLatestButton => InstallLatestFixes;

    /// <summary>警告のバナーの文: 変わった補給官のティア、何をすればよいか（作り直す / 最新のビルドを導入し直す）、ビルド番号。</summary>
    public string GameChangedText
    {
        get
        {
            if (!GameChanged) return "";
            var lb = Status.Obj("latest_build");
            var gu = InstalledChanged ? InstalledUpdate : LatestUpdate;
            var tiers = Tiers(InstalledChanged ? Status.Obj("installed").Strings("game_changed") : LatestUpdate.Strings("records_changed"));
            var text = !InstalledChanged ? L.F("UE_GameUpdateLatestOld", tiers, lb.Str("build_id"))
                : SharedChanged ? L.F("UE_GameUpdateShared", tiers, SharedInstalled.Str("file_name") ?? Status.Obj("installed").Str("build_id"))
                : InstallLatestFixes ? L.F("UE_GameUpdateInstallLatest", tiers, lb.Str("build_id"))
                : L.F("UE_GameUpdateRebuild", tiers);
            // ビルド番号は、作ったゲームのフォルダーと導入先が同じで番号が違うときだけ（buildid_changed。UE-R6）
            if (gu.Bool("buildid_changed") && gu.Str("build_buildid") is string then && gu.Str("current_buildid") is string now)
                text += "\n" + L.F("UE_GameUpdateBuildIds", then, now);
            return text;
        }
    }

    /// <summary>補給官のレコードの名前 → ティアの名前（Friendly・Respected など）。</summary>
    private static string Tiers(IEnumerable<string> records)
    {
        var names = records.Select(r =>
        {
            var m = System.Text.RegularExpressions.Regex.Match(r, @"devilscrossing_([a-z]+)_\d+\.dbr$");
            return m.Success ? char.ToUpperInvariant(m.Groups[1].Value[0]) + m.Groups[1].Value[1..] : r;
        });
        return L.F("UE_TierSuffix", string.Join(L.T("Common_ListSepDot"), names));
    }

    /// <summary>「ゲームの版」の欄: 今の Steam のビルド番号、MOD を作ったときとの違い、更新の待ち（導入中の MOD、無ければ最新のビルド）。</summary>
    public string GameVersionText
    {
        get
        {
            if (Status == null || !TargetOk) return "—";
            var cur = Status.Obj("game_build").Str("buildid");
            var gu = InstalledUpdate ?? LatestUpdate;
            var first = cur != null ? L.F("UE_GameVersionBuild", cur) : L.T("UE_GameVersionNoBuild");
            if (gu.Bool("buildid_changed") && gu.Str("build_buildid") is string then) first += L.F("UE_GameVersionThen", then);
            var lines = new List<string> { first };
            if (gu.Str("state") is string state && (state != "unknown" || cur != null)) lines.Add(L.T("UE_GameVersionState_" + state));
            if (Status.Obj("game_build").Bool("update_pending"))
                lines.Add(L.F("UE_GameVersionPending", Status.Obj("game_build").Str("target_buildid") ?? "?"));
            return string.Join("\n", lines);
        }
    }

    // --- アンインストールで残る AI Skin の古い MOD（2026-10-17 利用者の判断 C126。「全部外す」ボタンは作らない） ---
    private static string ModsList(IEnumerable<string> files) => string.Join(L.T("Common_ListSep"), files.Select(f => "mods\\" + f));

    /// <summary>アンインストールすると最初の導入の前の AI Skin の古い MOD に戻る（残る）こと、または導入の記録が無いのに
    /// mods フォルダーに AI Skin の MOD があること。無ければ空。</summary>
    public string AiskinNoteText
    {
        get
        {
            if (TargetUnavailable) return "";       // 導入先が見つからないときは mods フォルダーを確かめられない（R19B-02）
            var remains = Status.Obj("installed").Obj("after_uninstall").Strings("aiskin").ToList();
            if (remains.Count > 0) return L.F("UE_AiskinRemainsNote", ModsList(remains));
            var unrecorded = Status.Strings("aiskin_unrecorded").ToList();
            return !IsInstalled && unrecorded.Count > 0 ? L.F("UE_AiskinUnrecordedNote", ModsList(unrecorded)) : "";
        }
    }
    public bool HasAiskinNote => AiskinNoteText.Length > 0;

    // --- 照合の詳細（2026-10-17 機能 SF13。畳んだ欄に出す。照合コードの定義は変えない） ---
    public ObservableCollection<FileCodeRow> FileCodes { get; } = new();

    /// <summary>導入中の MOD と最新のビルドの照合コード、両者の違い（同じ / 見た目・表示名だけ / データベース）。</summary>
    public string CompareCodesText
    {
        get
        {
            var lines = new List<string>();
            if (Status.Obj("installed").Str("mod_code") is string c) lines.Add(L.F("UE_CodesInstalled", c));
            if (Status.Obj("latest_build") is JsonObject lb && lb.Str("mod_code") is string m) lines.Add(L.F("UE_CodesLatest", m, lb.Str("build_id")));
            if (Status.Obj("latest_build").Obj("compare_installed") is JsonObject cmp && cmp.Str("relation") is string rel)
                lines.Add(L.F("UE_Relation_" + rel, FilesSummary(cmp.Strings("differ"))));
            return lines.Count == 0 ? L.T("UE_CodesNone") : string.Join("\n", lines);
        }
    }

    /// <summary>違うファイルの名前の一覧（Text_XX.arc が 3 個以上なら 1 つにまとめ、6 個を超えたら「ほか N 個」）。</summary>
    private static string FilesSummary(IEnumerable<string> names)
    {
        var list = names.ToList();
        var texts = list.Where(n => n.StartsWith("Text_", StringComparison.OrdinalIgnoreCase)).ToList();
        var outList = list.Where(n => !texts.Contains(n)).ToList();
        if (texts.Count > 2) outList.Add(L.F("UE_TextArcs", texts.Count)); else outList.AddRange(texts);
        var sep = L.T("Common_ListSep");
        return outList.Count > 6 ? string.Join(sep, outList.Take(5).Append(L.F("UE_FilesMore", outList.Count - 5))) : string.Join(sep, outList);
    }

    private string _friendCode = "";
    public string FriendCode
    {
        get => _friendCode;
        set { if (Set(ref _friendCode, value)) { CompareResultText = ""; CommandManager.InvalidateRequerySuggested(); } }
    }
    private string _compareResultText = "";
    public string CompareResultText { get => _compareResultText; private set => Set(ref _compareResultText, value); }

    /// <summary>比べた時点の照合コードの組（Status の setter で比べ、変わったら比較の結果を消す。R19A-03）。</summary>
    private string? _comparedKey;
    private static string CodesKey(JsonNode? s) => string.Join("|", s.Obj("installed").Str("mod_code"), s.Obj("installed").Str("check_code"),
        s.Obj("installed").Obj("shared").Str("share_id"), s.Obj("latest_build").Str("build_id"), s.Obj("latest_build").Str("mod_code"),
        s.Obj("latest_build").Str("check_code"));

    /// <summary>比較の結果を消す（受け取った MOD の削除など、Status に出ないものが変わったとき。R19A-03）。</summary>
    public void ForgetCompareResult() => CompareResultText = "";

    private async Task CompareCodeAsync()
    {
        try
        {
            var key = CodesKey(Status);
            var r = await _main.RequireClient().PostAsync("mod/compare-code", new JsonObject { ["code"] = FriendCode });
            _comparedKey = key;
            var verdict = r.Str("verdict") ?? "none";
            var first = r.Arr("matches").FirstOrDefault();
            CompareResultText = verdict switch
            {
                "installed" => L.T("UE_Match_installed"),
                "latest_build" => L.F("UE_Match_latest_build", first.Str("build_id")),
                "received" => L.F("UE_Match_received", first.Str("file_name") ?? first.Str("build_id")),
                _ when verdict.StartsWith("database_", StringComparison.Ordinal)
                    => L.F("UE_Match_database", L.T("UE_Target_" + verdict["database_".Length..])),
                _ => L.T("UE_Match_none"),
            };
            AppLog.Info(L.F("Log_Mod_CodeCompared", r.Str("code"), verdict));
        }
        catch (ApiException e) when (e.Code == "INVALID_INPUT") { CompareResultText = L.T("UE_Match_bad"); }
    }
    /// <summary>導入の退避の件数と大きさ（2026-10-17 監査 C88）。</summary>
    public string BackupsText => Status.Obj("backups") is JsonObject b
        ? L.F("Mod_BackupsText", b.Long("count") ?? 0, (b.Long("bytes") ?? 0) / 1024.0 / 1024.0) : "";
    public ObservableCollection<FileStatus> Files { get; } = new();
    public string? TargetPath => Status.Str("target");
    public bool IsTestTarget => Status.Bool("is_test_target");
    public bool TargetOk => Status.Bool("target_ok");
    /// <summary>導入先（Grim Dawn のフォルダー・テスト用の導入先）が見つからない。バックエンドは導入の記録を読まず installed を
    /// 返さないので、「導入していません」とは言わない（外付けのドライブを外したときなど。R19B-02）。</summary>
    public bool TargetUnavailable => Status != null && !TargetOk;
    public bool GameRunning => Status.Bool("game_running");
    public string LaunchState => LaunchInfo.State(Status.Obj("launch_option"));
    public bool LaunchOk => LaunchState == "all";
    /// <summary>今の Steam ユーザーで判定した /basemods の有無と補足（2026-10-17 機能 SF12）。</summary>
    public string LaunchText => LaunchInfo.Describe(Status.Obj("launch_option"));
    /// <summary>ゲームの言語。英語と日本語の設定のゲームは MOD の装備名・説明（Text_EN.arc・Text_JA.arc）を読む（日本語は
    /// 2026-11-18 にゲーム内で確認）ので言語の名前だけを出し、ほかの言語には未確認の注記を添える（大文字・小文字は問わない。
    /// modbuilder の導入の計画の言語の注意と同じ分け方）。</summary>
    public string GameLanguageText => Status.Str("game_language") is not string lang ? ""
        : lang.Equals("English", StringComparison.OrdinalIgnoreCase) || lang.Equals("Japanese", StringComparison.OrdinalIgnoreCase)
            ? lang : L.F("Mod_GameLanguage", lang);
    public bool IsInstalled => Status.Obj("installed") != null;
    public string InstalledText => TargetUnavailable ? L.T("ModG19_InstallStateUnknown")
        : Status.Obj("installed") is JsonObject i
        ? (i.Obj("shared") is JsonObject sh
            ? L.F("Mod_InstalledShared", sh.Str("file_name"), EquipmentItem.FormatTime(i.Str("at")), i.Arr("equipment").Count)
            : L.F("Mod_Installed", i.Str("build_id"), EquipmentItem.FormatTime(i.Str("at")), i.Arr("equipment").Count))
        : L.T("Mod_NotInstalled");
    /// <summary>
    /// 最新のビルドをまだ導入していない: このアプリで導入していない、または導入中の自分のビルドと番号も中身も違う（照合の
    /// relation が same なら同じ中身なので出さない）。受け取った MOD を導入中のとき（共有の欄で扱う）と、導入先が見つからない
    /// ときは出さない。2026-11-05 の利用者の ZIP のテストで、MOD を作成しただけで導入していないことに気づけなかった（B-039）。
    /// </summary>
    public bool LatestNotInstalled
    {
        get
        {
            if (TargetUnavailable || Status.Obj("latest_build") is not JsonObject lb) return false;
            if (Status.Obj("installed") is not JsonObject i) return true;
            if (i.Obj("shared") != null || i.Str("build_id") == lb.Str("build_id")) return false;
            return lb.Obj("compare_installed").Str("relation") != "same";
        }
    }
    /// <summary>「導入の状態」の下に出す案内（最新のビルドの番号と、導入のボタンの名前）。上の帯がもう作り直しを案内している
    /// （RebuildFirst）ときは、そのビルドの導入を勧めず、作り直してから導入する文にする。出さないときは空。</summary>
    public string LatestNotInstalledText => !LatestNotInstalled ? ""
        : L.F(RebuildFirst ? "Mod_LatestNotInstalledRebuild" : "Mod_LatestNotInstalled", Status.Obj("latest_build").Str("build_id"));
    /// <summary>上の帯が「作り直してから導入」を案内している: 装備一覧が変わった（Mod_LibraryChanged）、またはゲームの更新の後で
    /// 最新のビルドが今のゲームに合っていると言えない（「MOD を作り直す」のボタンの出る帯）。以前は未導入の案内（B-039）が、帯の
    /// 言う作り直す前のビルドの導入を勧めていた（2026-11-18 frontend_mod_setup-01）。</summary>
    private bool RebuildFirst => LibraryChanged || ShowRebuildButton;
    public bool HasBuild => Status.Obj("latest_build") != null;
    public string BuildText => Status.Obj("latest_build") is JsonObject b
        ? L.F("Mod_LatestBuild", b.Str("build_id"), EquipmentItem.FormatTime(b.Str("finished_at") ?? b.Str("created_at")),
              b.Arr("equipment").Count, b.Arr("warnings").Count)
        : L.T("Mod_NoBuild");
    public string BuildFilesText => Status.Obj("latest_build").Obj("files") is JsonObject f
        ? string.Join(L.T("Common_Gap"), f.Select(kv => $"{kv.Key} {kv.Value.Long("size"):N0} B"))
          + (Status.Obj("latest_build").Str("check_code") is string db ? L.T("Common_Gap") + L.F("Share_DbCode", db) : "") : "";

    private bool _libraryChanged;
    public bool LibraryChanged
    {
        get => _libraryChanged;
        // 未導入の案内の文は帯によって変わる（RebuildFirst）。RefreshAsync は Status を先に、帯を後に決めるので、ここでも
        // 知らせないと、案内の文が前の帯のまま残る（作成で帯が消えても「作り直してから導入」の文のままなど。frontend_mod_setup-01）
        private set { if (Set(ref _libraryChanged, value)) OnPropertyChanged(nameof(LatestNotInstalledText)); }
    }
    public void MarkLibraryChanged() => LibraryChanged = true;

    private bool _isBusy;
    public bool IsBusy { get => _isBusy; private set { if (Set(ref _isBusy, value)) CommandManager.InvalidateRequerySuggested(); } }
    private bool _jobCancellable;
    private string? _jobId;
    public string? JobId { get => _jobId; private set => Set(ref _jobId, value); }
    private double _progress;
    public double Progress { get => _progress; private set => Set(ref _progress, value); }
    private string _stageText = "";
    public string StageText { get => _stageText; private set => Set(ref _stageText, value); }
    private string _statusText = "";
    public string StatusText { get => _statusText; private set => Set(ref _statusText, value); }

    /// <summary>MOD の状態と装備一覧の印を読み直す。導入などのジョブの間は何もしない（ジョブが書いている state.json を
    /// 読みに行かない。終わった後に RunAsync が読み直す。R07B-01）。</summary>
    public async Task RefreshAsync()
    {
        var c = _main.Client;
        if (c == null || IsBusy) return;
        Status = await c.GetAsync("mod/status");
        var lib = (await c.GetAsync("library"))!.AsArray();
        // 削除した装備は残りの装備の状態を変えないので、最新のビルドに入っていて装備一覧に無い装備があるかも見る（以前は MOD の
        // 画面を開くと削除の印が消えた。R19A-01）。一覧にあってビルドに無い装備は not_built なので、片方向だけでよい
        var ids = lib.Select(e => e.Str("id")).ToHashSet();
        var removed = Status.Obj("latest_build").Strings("equipment").Any(id => !ids.Contains(id));
        // 製品版のビルド（設定の include_dev_equipment がオフ）は開発用の装備（dev_only。D-018）を入れず、その状態も変えない
        // （ずっと not_built のまま）。数えると何度作り直しても帯が消えなかったので、ビルドに入らない装備は除く（X1X-02）
        var includeDev = (await c.GetAsync("settings")).Bool("include_dev_equipment");
        LibraryChanged = lib.Where(e => includeDev || !e.Bool("dev_only")).Any(e => e.Str("mod") is "not_built" or "build_failed") || removed;
    }

    /// <summary>MOD の操作（出力・導入・アンインストール・共有の書き出し・受け取り・元に戻す）の準備から終わりまで立てる印。
    /// 状態の読み直し・計画・確認のダイアログの間は IsBusy が立たず、別の操作を重ねて始められ、先に終わったジョブが実行中の
    /// ジョブを残して IsBusy を下ろした（R19A-04）。IsBusy を早く立てると RefreshAsync が入口で戻るので、別の印にする。</summary>
    private bool _operating;

    private async Task OperateAsync(Func<Task> body)
    {
        if (_operating || IsBusy) return;
        _operating = true;
        CommandManager.InvalidateRequerySuggested();
        try { await body(); }
        finally
        {
            _operating = false;
            CommandManager.InvalidateRequerySuggested();
        }
    }

    private async Task<JsonNode?> RunAsync(string type, JsonObject p, bool cancellable)
    {
        // ジョブを重ねて始めない（先に終わった方の finally が、もう一方の IsBusy・JobId・取消を消すため。R19A-04）
        if (IsBusy) throw new InvalidOperationException("A MOD job is already running.");
        IsBusy = true;
        _jobCancellable = cancellable;
        Progress = 0;
        StageText = L.T("Stage_" + type + "_start");
        try
        {
            return await _main.RequireClient().RunJobAsync(type, p, id => JobId = id,
                new Progress<JobProgress>(x => { Progress = x.Progress * 100; StageText = StageNames.Describe(x.Stage, x.Detail); }));
        }
        finally
        {
            IsBusy = false;
            JobId = null;
            await RefreshAfterJobAsync();
        }
    }

    /// <summary>ジョブの後の画面の更新。失敗はログに残すだけにし、ジョブの結果・エラー（rollback_incomplete の退避 ID など）を
    /// 置き換えない（以前は成功した導入も「導入に失敗しました」になった。R19A-02）。</summary>
    private async Task RefreshAfterJobAsync()
    {
        try { await RefreshAsync(); }
        catch (Exception e) { AppLog.Warn(L.F("Log_Mod_StatusRefreshFailed", e.GetType().Name, e.Message)); }
    }

    /// <summary>導入などの成功の後の装備一覧の更新。失敗はログに残すだけにし、成功の表示を失敗に変えない（R19A-02）。</summary>
    private async Task RefreshLibraryAfterJobAsync()
    {
        try { await _main.Library.RefreshAsync(); }
        catch (Exception e) { AppLog.Warn(L.F("Log_Mod_LibraryRefreshFailed", e.GetType().Name, e.Message)); }
    }

    /// <summary>MOD を作成する。ジョブの前に待つ処理が無い（RunAsync がすぐ IsBusy を立てる）ので OperateAsync では包まず、
    /// 他の操作の準備中（_operating）なら始めない（装備一覧の「MOD を作成し直す」からも呼ぶ。R19A-04）。</summary>
    public async Task BuildAsync()
    {
        if (IsBusy || _operating) return;
        try
        {
            // 帯（LibraryChanged）は RunAsync の後の読み直しが装備一覧から決める。ここで false にすると、作成中に変換し直した
            // 装備の「作り直してください」の帯を消した（R19A-05）
            var r = await RunAsync("build", new JsonObject(), true);
            // 作成だけではゲームに反映されないので、導入を促す（B-039）。RunAsync の後の読み直しで最新のビルドがこのビルドに
            // なっていれば、導入中の自分の MOD と同じ中身のときだけ出さない（受け取った MOD の導入中は、作ったのは自分の MOD
            // なので出す。置き換えることは導入の確認に出る）。読み直しに失敗して状態が古いときは、作ったばかりのビルドは導入して
            // いないので出す。導入先が見つからないときは導入できないので出さない（帯が案内する）。読み直した帯がまだ作り直しを
            // 案内している（作成中に装備を変えたなど。RebuildFirst）ときも出さない（未導入の案内が作り直してから導入する文になる）。
            // 読み直しに失敗したときの帯は作成の前のままなので見ない（frontend_mod_setup-01）
            bool fresh = Status.Obj("latest_build").Str("build_id") == r.Str("build_id");
            bool hint = !TargetUnavailable && (!fresh || ((LatestNotInstalled || SharedInstalled != null) && !RebuildFirst));
            StatusText = L.F("Mod_BuildDone", r.Str("build_id"), r.Arr("equipment").Count, r.Arr("warnings").Count)
                         + (hint ? L.T("Mod_BuildDoneInstallHint") : "");
            AppLog.Info(L.F("Log_Mod_Built", r.Str("build_id")));
        }
        catch (OperationCanceledException) { StatusText = L.T("Mod_BuildCancelled"); }
        catch (Exception e) { StatusText = L.T("Mod_BuildFailed"); App.ShowError(e); }
    }

    /// <summary>導入先の mods フォルダーを開く。導入先として使えないとき（テスト用の導入先の打ち間違い・消したフォルダーなど）は
    /// mods フォルダー（と上位のフォルダー）を作らず、在る mods フォルダーだけを開く。相対パスはアプリとバックエンドで指す場所が
    /// 違うので開かない（R19B-06）。</summary>
    private void OpenTarget()
    {
        if (TargetPath == null) return;
        var mods = System.IO.Path.Combine(TargetPath, "mods");
        if (System.IO.Path.IsPathFullyQualified(mods) && (TargetOk || System.IO.Directory.Exists(mods)))
            Dialogs.OpenFolder(mods);
        else
            Dialogs.ShowError(ErrorText.From("GAME_NOT_FOUND", null, TargetPath, null));
    }

    private async Task ExportAsync()
    {
        var d = new OpenFolderDialog { Title = L.T("Mod_ExportPick") };
        if (d.ShowDialog() != true) return;
        var r = await _main.RequireClient().PostAsync("mod/export",
            new JsonObject { ["dest"] = d.FolderName, ["build_id"] = Status.Obj("latest_build").Str("build_id") });
        Dialogs.Info(L.T("Mod_ExportDoneTitle"), L.F("Mod_ExportDone", r.Str("dest")), L.T("Mod_ExportHint"));
    }

    // --- 導入 ------------------------------------------------------------------------------------------
    private async Task<JsonNode?> PlanAsync(string action, bool replaceForeign, bool force, string? sharedId = null) =>
        await _main.RequireClient().PostAsync("mod/plan", new JsonObject
        {
            ["action"] = action, ["build_id"] = sharedId == null ? Status.Obj("latest_build").Str("build_id") : null,
            ["replace_foreign"] = replaceForeign, ["force"] = force, ["shared_id"] = sharedId,
        });

    /// <summary>計画が止まっている理由を利用者に見せ、続けられるもの（他の MOD の退避・改変の上書き）は同意を取る。</summary>
    private async Task<JsonNode?> ResolveBlockingAsync(string action, JsonNode? plan, string? sharedId = null)
    {
        bool replace = false, force = false;
        for (int guard = 0; guard < 3 && plan != null && !plan.Bool("executable"); guard++)
        {
            var code = plan.Str("blocking_code") ?? "INSTALL_BLOCKED";
            if (code == "GAME_RUNNING" || code == "NOT_FOUND")
            {
                Dialogs.ShowError(ErrorText.From(code, action == "uninstall" && code == "NOT_FOUND" ? "uninstall_nothing" : null,
                    string.Join("\n", plan.Strings("blocking")), null));
                return null;
            }
            // conflicts は退避して置き換えるファイルだけ（置き換えない他の MOD のファイルは KeptSection。2026-10-17 監査 C89）
            bool canReplace = plan.Bool("can_replace_foreign"), canForce = plan.Bool("can_force");
            // 他の MOD のファイルと変更されたファイルが両方あるときは、1 回目は他の MOD のファイルだけを見せて同意を取り、変更された
            // ファイルは作り直した計画で 2 回目のダイアログ（Mod_ConflictForceCheck）で同意を取る。2 回目には同意済みの他の MOD の
            // ファイルを出さない（R19A-03）
            var hide = canReplace && canForce ? "modified" : replace ? "foreign" : null;
            var shown = plan.Arr("conflicts").Where(c => hide == null || c.Str("kind") != hide).ToList();
            var conflicts = shown.Select(c => $"mods\\{c.Str("file")}: {AppLog.Redact(c.Str("detail") ?? "")}").ToList();
            if (!canReplace && !canForce)
            {
                // 止めた理由はビルドのファイルの欠け・退避の欠けなど mods フォルダー以外のこともあるので、理由の種類（reason）が
                // あればその文を出す（無ければ原因を断定しない共通の文。R19B-03）
                Dialogs.ShowError(ErrorText.From("INSTALL_BLOCKED", plan.Str("reason"), string.Join("\n", plan.Strings("blocking")), null));
                return null;
            }
            // アンインストール・元に戻すでは、戻す先の位置にある他の MOD のファイル（kind foreign）も can_force で置き換える。
            // 「このアプリが導入した後に変更された」とは言わず、他の MOD のファイルを退避して置き換えることを言う（R19A-01）
            var foreignForce = !canReplace && shown.Any(c => c.Str("kind") == "foreign");
            var mixed = foreignForce && shown.Any(c => c.Str("kind") != "foreign");
            var conflictSections = new List<DialogSection> { new() { Header = L.T("Mod_ConflictFiles"), Items = conflicts, Brush = Res("WarnBrush") } };
            if (KeptSection(plan) is DialogSection kept) conflictSections.Add(kept);
            var r = Dialogs.Show(new DialogSpec
            {
                Kind = DialogKind.Warning, Title = L.T("Mod_ConflictTitle"),
                Message = canReplace ? L.T("Mod_ConflictForeign")
                    : foreignForce ? L.T(mixed ? "ModG19_ConflictMixedForce" : "ModG19_ConflictForeignForce") : L.T("Mod_ConflictModified"),
                Sections = conflictSections,
                Hint = canReplace ? L.T("Mod_ConflictForeignHint")
                    : foreignForce ? L.T("ModG19_ConflictForeignForceHint") : L.T("Mod_ConflictModifiedHint"),
                CheckText = canReplace ? L.T("Mod_ConflictReplaceCheck")
                    : foreignForce ? L.T(mixed ? "ModG19_ConflictMixedForceCheck" : "Mod_ConflictReplaceCheck") : L.T("Mod_ConflictForceCheck"),
                CheckRequired = true,
                PrimaryText = L.T("Mod_Continue"), SecondaryText = L.T("Dialog_Cancel"),
            });
            if (!r.Accepted || !r.Checked) return null;
            replace |= canReplace;
            force |= canForce && !canReplace;       // 見せた文の同意だけを送る（R19A-03）
            plan = await PlanAsync(action, replace, force, sharedId);
        }
        return plan != null && plan.Bool("executable") ? plan : null;
    }

    private static IReadOnlyList<string> List(JsonNode? plan, string key, string prefix = "mods\\")
    {
        var n = plan?[key];
        if (n is JsonObject o) return o.Select(kv => prefix + kv.Key).ToList();
        return plan.Strings(key).Select(s => prefix + s).ToList();
    }

    /// <summary>導入しても mods フォルダーに残り、/basemods で読み込まれ続けるファイル（置き換えない他の MOD のファイルと、
    /// 4 つ以外の .arc・.arz。2026-10-17 監査 C85・C89）。無ければ null。</summary>
    private static DialogSection? KeptSection(JsonNode? plan)
    {
        var items = plan.Arr("kept_foreign").Select(c => $"mods\\{c.Str("file")}: {AppLog.Redact(c.Str("detail") ?? "")}")
            .Concat(plan.Arr("basemods_others").Select(o => o.Long("size") is long s
                ? L.F("Common_ParenFormat", $"mods\\{o.Str("name")}", $"{s:N0} B") : $"mods\\{o.Str("name")}"))
            .ToList();
        return items.Count == 0 ? null : new DialogSection { Header = L.T("Mod_PlanKept"), Items = items, Brush = Res("WarnBrush") };
    }

    /// <summary>計画の注意（ゲームの更新・起動の判定・言語など）。競合は「確認したこと」と競合のダイアログで、触らないファイルは
    /// KeptSection で説明するので除く（modbuilder の CLI 向けの文は出さない）。ゲームの起動を判定できなかった注意は、
    /// 「確認したこと」に GameCheck の行を出すダイアログでは除き、その欄が無いダイアログ（gameCheckShown = false）では
    /// アプリの文 Mod_CheckGameUnknown を先頭に出す（2026-10-17 FG06-R2）。</summary>
    private static List<string> PlanWarnings(JsonNode? plan, bool gameCheckShown = true)
    {
        var list = ShownWarnings(plan.Strings("warnings")).Select(AppLog.Redact).ToList();
        if (!gameCheckShown && plan != null && plan["game_running"] is null) list.Insert(0, L.T("Mod_CheckGameUnknown"));
        return list;
    }

    // modbuilder（install.py）の注意の文の目印。バックエンドの文は表示言語（GDSKIN_LANG）で日本語か英語になるので、両方の文で
    // 見分ける（英語は install.py の tr の 2 つ目の文と同じにする。2026-10-24 D-060）
    /// <summary>置き換えない他の MOD のファイルの注意（KeptSection で説明する）。</summary>
    private static readonly string[] KeptMarkers = ["（触らない）", "(not touched)"];
    /// <summary>ゲームの起動を判定できなかった注意（「確認したこと」の GameCheck・Mod_CheckGameUnknown で説明する）。</summary>
    private static readonly string[] GameCheckMarkers = ["起動しているか確かめられません", "Could not check whether Grim Dawn is running"];

    /// <summary>画面に出す注意（modbuilder の CLI 向けの文・別の欄で説明する文を除く）。</summary>
    private static IEnumerable<string> ShownWarnings(IEnumerable<string> warnings) =>
        warnings.Where(w => !w.Contains("--replace-foreign") && !w.Contains("--force") && !KeptMarkers.Any(m => w.Contains(m))
                            && !GameCheckMarkers.Any(m => w.Contains(m)));

    /// <summary>modbuilder の LIBRARY_UPDATE_WARNING（install.py）の書き出し。mods\ の変更の後に装備ライブラリの導入状態を
    /// 更新できなかったときの注意で、画面ではアプリの文 Mod_ResultLibraryNotUpdated（日英）に置き換える。</summary>
    private const string LibraryUpdateWarningHead = "装備ライブラリの導入状態を更新できませんでした";
    /// <summary>LibraryUpdateWarningHead の英語の文（表示言語が英語のときのバックエンドの文。install.py の tr の 2 つ目の文）。</summary>
    private const string LibraryUpdateWarningHeadEn = "Could not update the install status in the equipment library";

    /// <summary>導入・アンインストール・元に戻すの実行の結果の注意のうち、実行前の計画（確認のダイアログ）に無かったもの。
    /// mods\ の変更の後に装備ライブラリの導入状態を更新できなかったとき（ジョブは成功のまま、結果の warnings に注意が入る。
    /// R07B-02）など。以前は実行の結果の warnings を読まず、注意はジョブのログにしか残らなかった（D7_contracts-01）。
    /// ログには WARN として残す。</summary>
    private static List<string> ResultWarnings(JsonNode? result, JsonNode? plan)
    {
        var before = plan.Strings("warnings").ToHashSet(StringComparer.Ordinal);
        var raw = ShownWarnings(result.Strings("warnings")).Where(w => !before.Contains(w)).Distinct(StringComparer.Ordinal).ToList();
        foreach (var w in raw) AppLog.Warn(L.F("Log_Mod_ResultWarning", AppLog.Redact(w)));
        return raw.Select(w => w.StartsWith(LibraryUpdateWarningHead, StringComparison.Ordinal)
                               || w.StartsWith(LibraryUpdateWarningHeadEn, StringComparison.Ordinal)
                               ? L.T("Mod_ResultLibraryNotUpdated") : AppLog.Redact(w))
            .Distinct(StringComparer.Ordinal).ToList();
    }

    /// <summary>実行の結果の注意の節（Mod_PlanWarnings と同じ WarnBrush）。注意が無ければ null。</summary>
    private static DialogSection? ResultWarningsSection(List<string> warnings) =>
        warnings.Count == 0 ? null : new DialogSection { Header = L.T("Mod_ResultWarnings"), Items = warnings, Brush = Res("WarnBrush") };

    /// <summary>完了のダイアログが無い操作（アンインストール・元に戻す）の実行の結果の注意を、注意のダイアログで見せる。</summary>
    private static void ShowResultWarnings(List<string> warnings)
    {
        if (ResultWarningsSection(warnings) is not DialogSection s) return;
        Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Warning, Title = L.T("Mod_ResultWarningsTitle"), Message = L.T("Mod_ResultWarningsMessage"),
            Sections = [s], ShowOpenLogs = true,
        });
    }

    /// <summary>「確認したこと」のゲームの起動。判定できなかった（null）ときは「起動していません」と言わない（2026-10-17 監査 C90）。</summary>
    private static string GameCheck(JsonNode? plan) =>
        plan?["game_running"] is null ? L.T("Mod_CheckGameUnknown") : L.T("Mod_CheckGameClosed");

    /// <summary>導入・アンインストール・元に戻すの失敗の状態の文。mods フォルダーがどうなったかをエラーに応じて言う
    /// （以前は無条件に「元に戻しています」と出していた。2026-10-17 監査 C11）。バックエンドに接続できなくなったときは、ジョブが
    /// mods フォルダーをどこまで書き換えたか分からないので「変更していません」と言わない（R19A-02）。</summary>
    private static string FailText(string key, Exception e)
    {
        string suffix = e is ApiException a
            ? a.Code is "INTERNAL" or "BACKEND_UNAVAILABLE" ? "Mod_FailUnknown"
              : a.Param("reason") == "rollback_incomplete" ? "Mod_FailIncomplete"
              : a.Param("reason") == "state_failed" ? "Mod_FailStateNotSaved"        // mods は変更済み、記録だけ失敗（R07B-02）
              : a.Param("reason") is string r && r.StartsWith("write_failed", StringComparison.Ordinal) ? "Mod_FailRolledBack"
              : "Mod_FailUnchanged"
            : "Mod_FailUnknown";
        return L.T(key) + L.T(suffix);
    }

    private async Task InstallAsync()
    {
        await RefreshAsync();
        var plan = await ResolveBlockingAsync("install", await PlanAsync("install", false, false));
        if (plan == null) return;
        var copy = List(plan, "copy");
        var backup = List(plan, "backup");
        var remove = List(plan, "remove");
        var checks = new List<string> { GameCheck(plan) };
        var conflicts = plan.Arr("conflicts").Count;
        checks.Add(conflicts == 0 ? L.T("Mod_CheckNoConflict") : L.F("Mod_CheckConflictReplaced", conflicts));
        checks.Add(backup.Count == 0 ? L.T("Mod_CheckNoBackupNeeded") : L.F("Mod_CheckBackup", backup.Count));
        var warnings = PlanWarnings(plan);
        if (plan.Obj("launch_option").Str("state") is string ls && ls != "all") warnings.Insert(0, LaunchInfo.Describe(plan.Obj("launch_option")));
        var sections = new List<DialogSection>
        {
            new() { Header = L.T("Mod_PlanTarget"), Items = [AppLog.Redact(plan.Str("target") ?? "") + (plan.Bool("is_test_target") ? L.T("Mod_TestTargetMark") : "")] },
            new() { Header = L.T("Mod_PlanChecks"), Items = checks },
            new() { Header = L.T("Mod_PlanCopy"), Items = copy },
        };
        if (backup.Count > 0) sections.Add(new() { Header = L.T("Mod_PlanBackup"), Items = backup });
        if (remove.Count > 0) sections.Add(new() { Header = L.T("Mod_PlanRemove"), Items = remove });
        if (KeptSection(plan) is DialogSection kept) sections.Add(kept);
        if (warnings.Count > 0) sections.Add(new() { Header = L.T("Mod_PlanWarnings"), Items = warnings,
            Brush = (System.Windows.Media.Brush)System.Windows.Application.Current.Resources["WarnBrush"] });
        // 導入でゲームから消える装備（今の mods\ にあって、このビルドに無いもの）。消えるときは確認のチェックを必須にする
        var change = plan.Obj("equipment_change");
        var removed = change.Arr("removed").Select(r => r.Str("name") ?? r.Str("record") ?? "").ToList();
        var added = change.Arr("added").Select(r => r.Str("name") ?? r.Str("record") ?? "").ToList();
        if (removed.Count > 0)
            sections.Insert(1, new() { Header = L.F("Mod_PlanRemovedEquipment", removed.Count), Items = removed,
                Brush = (System.Windows.Media.Brush)System.Windows.Application.Current.Resources["ErrBrush"] });
        if (change != null && change.Bool("known"))
            sections.Insert(removed.Count > 0 ? 2 : 1, new() { Header = L.T("Mod_PlanEquipmentSummary"),
                Items = [L.F("Mod_PlanEquipmentCounts", change.Long("kept") ?? 0, added.Count, removed.Count)] });
        var ok = Dialogs.Show(new DialogSpec
        {
            Kind = removed.Count > 0 ? DialogKind.Warning : DialogKind.Question, Title = L.T("Mod_InstallTitle"),
            Message = removed.Count > 0 ? L.F("Mod_InstallMessageRemoved", removed.Count) : L.T("Mod_InstallMessage"), Sections = sections,
            Hint = L.T("Mod_InstallHint"), PrimaryText = L.T("Mod_InstallDo"), SecondaryText = L.T("Dialog_Cancel"),
            CheckText = removed.Count > 0 ? L.F("Mod_RemovedCheck", removed.Count) : null, CheckRequired = removed.Count > 0,
        }).Accepted;
        if (!ok) return;
        try
        {
            var r = await RunAsync("install", new JsonObject
            {
                ["build_id"] = plan.Str("build_id") ?? Status.Obj("latest_build").Str("build_id"),
                ["replace_foreign"] = plan.Bool("replace_foreign"), ["force"] = plan.Bool("force"),
            }, false);
            var resultWarnings = ResultWarnings(r, plan);   // 実行中に起きた注意（D7_contracts-01）
            StatusText = L.F("Mod_InstallDone", r.Str("build_id")) + (resultWarnings.Count > 0 ? L.T("Mod_ResultWarningsTail") : "");
            AppLog.Info(L.F("Log_Mod_Installed", r.Str("build_id")));
            var doneSections = new List<DialogSection>();
            if (ResultWarningsSection(resultWarnings) is DialogSection rw) doneSections.Add(rw);
            doneSections.Add(new DialogSection { Header = L.T("Mod_Guide_Header"), Items = GuideLines() });
            Dialogs.Show(new DialogSpec
            {
                Kind = resultWarnings.Count > 0 ? DialogKind.Warning : DialogKind.Info,
                Title = L.T("Mod_InstallDoneTitle"), Message = L.T("Mod_InstallDoneMessage"),
                Sections = doneSections,
                Hint = L.T("Mod_IllusionSlotHint"), ShowOpenLogs = resultWarnings.Count > 0,
            });
            await RefreshLibraryAfterJobAsync();
        }
        catch (Exception e) { StatusText = FailText("Mod_InstallFailed", e); App.ShowError(e); }
    }

    private async Task UninstallAsync()
    {
        await RefreshAsync();
        var plan = await ResolveBlockingAsync("uninstall", await PlanAsync("uninstall", false, false));
        if (plan == null) return;
        // 戻すファイルに最初の導入の前の持ち主を添える。AI Skin の古い MOD に戻るなら、残ることをはっきり出す
        // （2026-10-17 利用者の判断 C126。アンインストールの意味は「最初の導入の前の状態に戻す」のまま）
        var kinds = plan.Obj("restore_kinds");
        var restore = plan.Strings("restore").Select(r => "mods\\" + r + (kinds.Str(r) switch
        {
            "aiskin-project" => L.T("UE_RestoreKind_aiskin"),
            "foreign" => L.T("UE_RestoreKind_foreign"),
            _ => "",
        })).ToList();
        var aiskin = plan.Strings("aiskin_remains").ToList();
        var remove = List(plan, "remove");
        var sections = new List<DialogSection>
        {
            new() { Header = L.T("Mod_PlanTarget"), Items = [AppLog.Redact(plan.Str("target") ?? "") + (plan.Bool("is_test_target") ? L.T("Mod_TestTargetMark") : "")] },
            new() { Header = L.T("Mod_PlanChecks"), Items = [GameCheck(plan)] },
        };
        if (aiskin.Count > 0)
            sections.Insert(1, new() { Header = L.T("UE_UninstallRemainsHeader"),
                Items = aiskin.Select(f => "mods\\" + f).Append(L.T("UE_UninstallRemainsTail")).ToList(), Brush = Res("WarnBrush") });
        if (remove.Count > 0) sections.Add(new() { Header = L.T("Mod_PlanRemoveOwn"), Items = remove });
        if (restore.Count > 0) sections.Add(new() { Header = L.T("Mod_PlanRestore"), Items = restore });
        var ok = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Warning, Title = L.T("Mod_UninstallTitle"),
            Message = aiskin.Count > 0 ? L.T("UE_UninstallMessageAiskin") : L.T("Mod_UninstallMessage"), Sections = sections,
            Hint = L.T("Mod_UninstallHint"), PrimaryText = L.T("Mod_UninstallDo"), SecondaryText = L.T("Dialog_Cancel"),
        }).Accepted;
        if (!ok) return;
        try
        {
            var r = await RunAsync("uninstall", new JsonObject { ["force"] = plan.Bool("force") }, false);
            var resultWarnings = ResultWarnings(r, plan);   // 実行中に起きた注意（D7_contracts-01）
            StatusText = (aiskin.Count > 0 ? L.F("UE_UninstallDoneAiskin", ModsList(aiskin)) : L.T("Mod_UninstallDone"))
                         + (resultWarnings.Count > 0 ? L.T("Mod_ResultWarningsTail") : "");
            AppLog.Info(L.T("Log_Mod_Uninstalled"));
            await RefreshLibraryAfterJobAsync();
            ShowResultWarnings(resultWarnings);
        }
        catch (Exception e) { StatusText = FailText("Mod_UninstallFailed", e); App.ShowError(e); }
    }

    // --- マルチプレイ用の共有 ---------------------------------------------------------------------------------------
    private static System.Windows.Media.Brush Res(string key) => (System.Windows.Media.Brush)System.Windows.Application.Current.Resources[key];

    private async Task ShareExportAsync()
    {
        var b = Status.Obj("latest_build");
        if (b == null) return;
        if (LibraryChanged && !Dialogs.Confirm(L.T("Share_ExportStaleTitle"), L.T("Share_ExportStale"), L.T("Share_ExportAnyway"),
                kind: DialogKind.Warning))
            return;
        var d = new SaveFileDialog
        {
            Title = L.T("Share_ExportPick"), Filter = L.T("Share_Filter"), DefaultExt = ".gdskinmod", AddExtension = true,
            FileName = $"GDSkin_{b.Str("build_id")}.gdskinmod", OverwritePrompt = true,
        };
        if (d.ShowDialog() != true) return;
        try
        {
            var r = await RunAsync("export_mod", new JsonObject { ["build_id"] = b.Str("build_id"), ["dest"] = d.FileName, ["overwrite"] = true }, true);
            StatusText = L.F("Share_ExportDoneStatus", r.Str("mod_code"));
            AppLog.Info(L.F("Log_Mod_ShareExported", r.Str("build_id"), r.Str("mod_code"), r.Str("check_code")));
            Dialogs.Show(new DialogSpec
            {
                Title = L.T("Share_ExportDoneTitle"), Message = L.F("Share_ExportDone", r.Arr("equipment").Count),
                Sections = [new DialogSection { Header = L.T("Pkg_File"), Items = [AppLog.Redact(r.Str("dest") ?? ""),
                                L.F("Share_SizeAndCode", (r.Long("size") ?? 0) / 1024.0 / 1024.0, r.Str("mod_code")),
                                L.F("Share_DbCode", r.Str("check_code"))] },
                            new DialogSection { Header = L.T("Share_TellFriend"), Items = [L.T("Share_TellFriend1"), L.T("Share_TellFriend2"), L.T("Share_TellFriend3")] }],
                Hint = L.T("Share_ExportDoneHint"),
            });
        }
        catch (OperationCanceledException) { StatusText = L.T("Share_Cancelled"); }
    }

    /// <summary>受け取った .gdskinmod を確認して導入する。path を渡すとファイルの選択を省く。</summary>
    public async Task ShareImportAsync(string? path = null)
    {
        if (path == null)
        {
            var d = new OpenFileDialog { Title = L.T("Share_ImportPick"), Filter = L.T("Share_Filter"), CheckFileExists = true };
            if (d.ShowDialog() != true) return;
            path = d.FileName;
        }
        await RefreshAsync();
        StatusText = L.T("Share_Checking");
        JsonNode? info;
        try { info = await _main.RequireClient().PostAsync("packages/inspect", new JsonObject { ["path"] = path, ["expect"] = "mod" }); }
        finally { StatusText = ""; }
        var equipment = info.Arr("equipment").Select(e => $"{e.Str("name")}  {e.Str("id")}").ToList();
        // 記録の自由な文（作成日時・ビルド ID・ツールの版）は作った人が書き換えられるので、1 行・短く切って出す（改行で偽の行を
        // 作る・数 MB の文で画面が止まるのを防ぐ。R19B-01）
        var about = new List<string>
        {
            L.F("Pkg_About", info.Str("file_name"), EquipmentItem.FormatTime(info.Str("created_at")), info.Str("package_id")),
            L.F("Share_AboutBuild", EquipmentItem.OneLine(info.Obj("build").Str("build_id") ?? "?", 64),
                EquipmentItem.OneLine(info.Obj("tool").Str("backend") ?? "?", 64), EquipmentItem.OneLine(info.Obj("tool").Str("modbuilder") ?? "?", 64)),
            L.F("Share_AboutFiles", string.Join(L.T("Common_ListSep"), info.Obj("files")?.Select(kv => $"{kv.Key} {kv.Value.Long("size"):N0} B") ?? [])),
            L.F("Share_DbCode", info.Str("check_code")),
        };
        // ファイルごとの短いコード（主なファイル。全ファイルは MOD の画面の「照合の詳細」。2026-10-17 機能 SF13）
        if (info.Obj("file_codes") is JsonObject fc && fc.Count > 0)
        {
            var main = new[] { "database.arz", "Items.arc", "Text_EN.arc", "Text_JA.arc" }.Where(n => fc.ContainsKey(n)).ToList();
            var sep = L.T("Common_ListSep");
            var line = string.Join(sep, main.Select(n => $"{n} {fc.Str(n)}"));
            if (fc.Count > main.Count) line += sep + L.F("UE_FilesMore", fc.Count - main.Count);
            about.Add(L.F("UE_ShareFileCodes", line));
        }
        if (!info.Bool("can_install"))
        {
            Dialogs.Show(new DialogSpec
            {
                Kind = DialogKind.Error, Title = L.T("Share_RejectedTitle"), Message = L.T("Share_Rejected"),
                Sections = [new DialogSection { Header = L.T("Share_RejectedReasons"), Items = info.Strings("blocking").Take(20).ToList(), Brush = Res("ErrBrush") },
                            new DialogSection { Header = L.T("Pkg_AboutHeader"), Items = about }],
                Hint = L.T("Share_RejectedHint"),
            });
            return;
        }
        var sharedId = info.Str("share_id")!;
        var plan = await ResolveBlockingAsync("install", await PlanAsync("install", false, false, sharedId), sharedId);
        if (plan == null) return;
        var game = info.Obj("game");
        var gameLines = new List<string>();
        if (game.Bool("expansions_differ"))
            gameLines.Add(L.F("Share_ExpansionsDiffer", string.Join(", ", game.Strings("theirs")), string.Join(", ", game.Strings("mine"))));
        else if (game.Bool("version_may_differ")) gameLines.Add(L.T("Share_VersionMayDiffer"));
        if (!game.Bool("game_found")) gameLines.Add(L.T("Share_GameNotFound"));
        var change = plan.Obj("equipment_change");
        var removed = change.Arr("removed").Select(r => r.Str("name") ?? r.Str("record") ?? "").ToList();
        var copy = List(plan, "copy");
        var backup = List(plan, "backup");
        var sections = new List<DialogSection>
        {
            new() { Header = L.T("Share_CodeHeader"), Items = [L.F("Share_CodeLine", info.Str("mod_code"))] },
        };
        // 導入中の MOD・最新のビルドとの比較（同じなら導入は不要、見た目・表示名だけ違う、データベースが違う。2026-10-17 機能 SF13）
        var cmpLines = new List<string>();
        foreach (var what in new[] { "installed", "latest_build" })
            if (info.Obj("compare").Obj(what) is JsonObject cmp && cmp.Str("relation") is string rel)
                cmpLines.Add(L.F($"UE_ShareCmp_{what}_{rel}", FilesSummary(cmp.Strings("differ"))));
        if (cmpLines.Count > 0) sections.Add(new() { Header = L.T("UE_ShareCompareHeader"), Items = cmpLines });
        // 本ツールが作る値・公式のデータと違う項目（ゲームの版の違いか、作った人による書き換え）は、照合コードのすぐ下に
        // レコードごとの違いの一覧で出す。導入は止めない（2026-10-17 監査 C14 の利用者の判断）
        var warn = info.Strings("warnings").Take(20).Select(AppLog.Redact).ToList();
        if (warn.Count > 0) sections.Add(new() { Header = L.F("Share_Warnings", info.Arr("warnings").Count), Items = warn, Brush = Res("WarnBrush") });
        sections.Add(new() { Header = L.F("Share_Equipment", equipment.Count), Items = equipment });
        if (removed.Count > 0)
            sections.Add(new() { Header = L.F("Share_Replaced", removed.Count), Items = removed, Brush = Res("ErrBrush") });
        else if (IsInstalled)
            sections.Add(new() { Header = L.T("Share_ReplacedHeader"), Items = [L.T("Share_ReplacedNoLoss")] });
        if (gameLines.Count > 0) sections.Add(new() { Header = L.T("Share_GameHeader"), Items = gameLines, Brush = Res("WarnBrush") });
        var planWarnings = PlanWarnings(plan, gameCheckShown: false);     // このダイアログには「確認したこと」の欄が無い
        if (plan.Obj("launch_option").Str("state") is string ls && ls != "all") planWarnings.Insert(0, LaunchInfo.Describe(plan.Obj("launch_option")));
        if (planWarnings.Count > 0)
            sections.Add(new() { Header = L.T("Mod_PlanWarnings"), Items = planWarnings, Brush = Res("WarnBrush") });
        sections.Add(new() { Header = L.T("Mod_PlanTarget"), Items = [AppLog.Redact(plan.Str("target") ?? "") + (plan.Bool("is_test_target") ? L.T("Mod_TestTargetMark") : "")] });
        sections.Add(new() { Header = L.T("Mod_PlanCopy"), Items = copy });
        if (backup.Count > 0) sections.Add(new() { Header = L.T("Mod_PlanBackup"), Items = backup });
        if (KeptSection(plan) is DialogSection kept) sections.Add(kept);
        sections.Add(new() { Header = L.T("Pkg_AboutHeader"), Items = about });
        var ok = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Warning, Title = L.T("Share_InstallTitle"),
            Message = IsInstalled ? L.T("Share_InstallMessageReplace") : L.T("Share_InstallMessage"), Sections = sections,
            Hint = L.T("Share_InstallHint"),
            CheckText = (IsInstalled ? L.T("Share_ReplaceCheck") : L.T("Share_InstallCheck"))
                        + (warn.Count > 0 ? L.T("Share_WarningsChecked") : ""), CheckRequired = true,
            PrimaryText = L.T("Share_InstallDo"), SecondaryText = L.T("Dialog_Cancel"),
        }).Accepted;
        if (!ok) return;
        try
        {
            var r = await RunAsync("install", new JsonObject
            {
                ["shared_id"] = sharedId, ["replace_foreign"] = plan.Bool("replace_foreign"), ["force"] = plan.Bool("force"),
            }, false);
            var resultWarnings = ResultWarnings(r, plan);   // 実行中に起きた注意（D7_contracts-01）
            StatusText = L.F("Share_InstallDoneStatus", r.Str("mod_code")) + (resultWarnings.Count > 0 ? L.T("Mod_ResultWarningsTail") : "");
            AppLog.Info(L.F("Log_Mod_SharedInstalled", sharedId, r.Str("mod_code"), r.Str("check_code")));
            var doneSections = new List<DialogSection>();
            if (ResultWarningsSection(resultWarnings) is DialogSection rw) doneSections.Add(rw);
            doneSections.Add(new DialogSection { Header = L.T("Mod_Guide_Header"), Items = GuideLines() });
            Dialogs.Show(new DialogSpec
            {
                Kind = resultWarnings.Count > 0 ? DialogKind.Warning : DialogKind.Info,
                Title = L.T("Share_InstallDoneTitle"), Message = L.F("Share_InstallDone", r.Str("mod_code")),
                Sections = doneSections,
                Hint = L.T("Share_InstallDoneHint"), ShowOpenLogs = resultWarnings.Count > 0,
            });
            await RefreshLibraryAfterJobAsync();
        }
        catch (Exception e) { StatusText = FailText("Mod_InstallFailed", e); App.ShowError(e); }
    }

    private async Task ShareRestoreAsync()
    {
        await RefreshAsync();
        var plan = await ResolveBlockingAsync("restore", await PlanAsync("restore", false, false));
        if (plan == null) return;
        var prev = plan.Obj("previous");
        var change = plan.Obj("equipment_change");
        var back = change.Arr("added").Select(r => r.Str("name") ?? r.Str("record") ?? "").ToList();
        var gone = change.Arr("removed").Select(r => r.Str("name") ?? r.Str("record") ?? "").ToList();
        var sections = new List<DialogSection>
        {
            new() { Header = L.T("Share_RestoreTo"), Items = [PreviousText(prev)] },
        };
        if (back.Count > 0) sections.Add(new() { Header = L.F("Share_RestoreBack", back.Count), Items = back });
        if (gone.Count > 0) sections.Add(new() { Header = L.F("Share_RestoreGone", gone.Count), Items = gone, Brush = Res("WarnBrush") });
        sections.Add(new() { Header = L.T("Mod_PlanTarget"), Items = [AppLog.Redact(plan.Str("target") ?? "") + (plan.Bool("is_test_target") ? L.T("Mod_TestTargetMark") : "")] });
        var restore = List(plan, "restore");
        var remove = List(plan, "remove");
        if (restore.Count > 0) sections.Add(new() { Header = L.T("Mod_PlanRestore"), Items = restore });
        if (remove.Count > 0) sections.Add(new() { Header = L.T("Mod_PlanRemoveOwn"), Items = remove });
        // 計画の注意（ゲームの起動を判定できなかったときなど。以前はこのダイアログに出していなかった。2026-10-17 FG06-R2）
        var warnings = PlanWarnings(plan, gameCheckShown: false);
        if (warnings.Count > 0) sections.Add(new() { Header = L.T("Mod_PlanWarnings"), Items = warnings, Brush = Res("WarnBrush") });
        if (!Dialogs.Show(new DialogSpec
            {
                Kind = DialogKind.Question, Title = L.T("Share_RestoreTitle"), Message = L.T("Share_RestoreMessage"), Sections = sections,
                Hint = L.T("Share_RestoreHint"), PrimaryText = L.T("Share_RestoreDo"), SecondaryText = L.T("Dialog_Cancel"),
            }).Accepted)
            return;
        try
        {
            var r = await RunAsync("restore", new JsonObject { ["backup_id"] = prev.Str("backup_id"), ["force"] = plan.Bool("force") }, false);
            var resultWarnings = ResultWarnings(r, plan);   // 実行中に起きた注意（D7_contracts-01）
            StatusText = L.T("Share_RestoreDone") + (resultWarnings.Count > 0 ? L.T("Mod_ResultWarningsTail") : "");
            AppLog.Info(L.T("Log_Mod_SharedRestored"));
            await RefreshLibraryAfterJobAsync();
            ShowResultWarnings(resultWarnings);
        }
        catch (Exception e) { StatusText = FailText("Share_RestoreFailed", e); App.ShowError(e); }
    }

    private List<string> GuideLines()
    {
        var lines = new List<string> { LaunchText };
        if (!LaunchOk) lines.AddRange([L.T("Mod_Guide_1"), L.T("Mod_Guide_2"), L.T("Mod_Guide_3")]);
        lines.Add(L.T("Mod_Guide_Exclusive"));
        return lines;
    }

    private void ShowGuide() => Dialogs.Show(new DialogSpec
    {
        Title = L.T("Mod_GuideTitle"), Message = L.T("Mod_GuideMessage"),
        Sections = [new DialogSection { Header = L.T("Mod_Guide_Steps"), Items = [L.T("Mod_Guide_1"), L.T("Mod_Guide_2"), L.T("Mod_Guide_3")] },
                    new DialogSection { Header = L.T("Mod_Guide_Header"), Items = [LaunchText, L.T("Mod_Guide_Exclusive"), L.T("Mod_Guide_Remove")] }],
        Hint = L.T("Mod_IllusionSlotHint"),
    });
}
