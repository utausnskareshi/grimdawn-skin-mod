using System.Diagnostics;
using System.IO;
using System.Text.Json.Nodes;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using GDSkinStudio.Views;
using Microsoft.Win32;

namespace GDSkinStudio.ViewModels;

public sealed record Choice<T>(T Value, string Label)
{
    public override string ToString() => Label;      // 読み上げ・UI Automation の名前も表示と同じに
}

/// <summary>
/// 生成の画面（計画書 4.1・4.2・9 章）: 入力（画像・テキスト・GLB）、生成設定、開始・停止・進捗、プレビュー、
/// 再生成、装備名を付けて保存（再生成のときは装備の差し替え）。
/// </summary>
public sealed partial class GenerateViewModel : ObservableObject
{
    private readonly MainViewModel _main;
    private readonly DispatcherTimer _timer;
    private readonly Stopwatch _watch = new();

    public GenerateViewModel(MainViewModel main)
    {
        _main = main;
        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _timer.Tick += (_, _) => OnPropertyChanged(nameof(ElapsedText));
        _glbExists = new PathExistence(() => OnPropertyChanged(nameof(InputReady)));
        InitSlots();
        BrowseGlbCommand = new RelayCommand(BrowseGlb);
        GenerateCommand = new AsyncCommand(() => GenerateAsync(false), () => _main.IsReady && !IsBusy && InputReady);
        RegenerateCommand = new AsyncCommand(() => GenerateAsync(true), () => _main.IsReady && !IsBusy && HasDraft && InputReady);
        StopCommand = new AsyncCommand(StopAsync, () => IsBusy && JobId != null);
        SaveCommand = new AsyncCommand(SaveAsync, () => _main.IsReady && !IsBusy && DraftReady);
        DiscardCommand = new AsyncCommand(DiscardAsync, () => !IsBusy && HasDraft);
        ShowConsentCommand = new AsyncCommand(ShowConsentAsync, () => _main.IsReady);
        // 生成中は押せない（実行中のジョブは開始時の差し替え先を下書きに記録する。見た目の作り直しの取り消しと同じ）
        CancelReplaceCommand = new RelayCommand(() => { ReplaceEquipmentId = null; ReplaceLabel = null; _regenWasRotated = false; },
                                                () => !IsBusy);
        RestorePreviousCommand = new AsyncCommand(RestorePreviousAsync, () => _main.IsReady && !IsBusy && CanRestorePrevious);
        RotateCommand = new AsyncCommand(RotateAsync, () => _main.IsReady && !IsBusy && CanRotate);
        // 候補を選ぶ・保存した生成結果から GLB を作り直す（SF06・SF14。U-A2）
        PickCandidateCommand = new AsyncCommand(PickCandidateAsync, p => _main.IsReady && !IsBusy && CanRotate && p is long);
        RebuildGlbCommand = new AsyncCommand(() => RebuildAsync(null), () => _main.IsReady && !IsBusy && CanRebuildGlb);
        CancelVariantCommand = new RelayCommand(CancelVariant, () => !IsBusy);
    }

    // --- 見た目の作り直し（装備の生成結果の形のまま、文章で見た目を作り直す。SF10。2026-10-17 U-A3。既定では出さない） ---------
    /// <summary>装備一覧に「文章で見た目を作り直す」を出す（詳細設定。画面の設定 ui_settings.json に残す。既定オフ）。</summary>
    public bool TextVariantEnabled
    {
        get => App.Ui.TextVariantEnabled;
        set
        {
            if (App.Ui.TextVariantEnabled == value) return;
            App.Ui.TextVariantEnabled = value;
            App.Ui.Save(App.Paths.UiSettingsPath);
            AppLog.Info(L.T(value ? "Log_Gen_VariantShown" : "Log_Gen_VariantHidden"));
            OnPropertyChanged();
            if (!value && IsVariant && !IsBusy) CancelVariant();
            _main.Library.OnVariantSettingChanged();
        }
    }
    private string? _variantBaseId;
    /// <summary>形を使う元の装備（見た目の作り直しのときだけ。生成の開始はジョブ variant になる）。</summary>
    public string? VariantBaseId
    {
        get => _variantBaseId;
        private set
        {
            if (!Set(ref _variantBaseId, value)) return;
            OnPropertyChanged(nameof(IsVariant)); OnPropertyChanged(nameof(InputKindSelectable)); OnPropertyChanged(nameof(SelectPoseEnabled));
            OnPropertyChanged(nameof(SelectPoseShown)); OnPropertyChanged(nameof(CandidatesEnabled));
            OnPropertyChanged(nameof(KeepCandidatesShown)); OnPropertyChanged(nameof(KeepCandidatesEnabled));
        }
    }
    public bool IsVariant => VariantBaseId != null;
    private string? _variantLabel;
    public string? VariantLabel { get => _variantLabel; private set => Set(ref _variantLabel, value); }
    /// <summary>入力の種類を選べるか（見た目の作り直しの間は文章だけ）。</summary>
    public bool InputKindSelectable => !IsVariant;
    /// <summary>候補の選別（D-012）を選べるか（見た目の作り直しは元の形のままなので選別しない）。</summary>
    public bool SelectPoseEnabled => !IsVariant;
    // 見た目の作り直しの間は、使わない候補の選別・候補の数・候補の保存をオフの表示で選べなくする。利用者の設定（SelectPose・
    // KeepCandidates。次回の既定値として保存する値）は変えない（2026-10-17 点検 UA3-R2）
    public bool SelectPoseShown { get => SelectPose && !IsVariant; set { if (!IsVariant) SelectPose = value; } }
    public bool CandidatesEnabled => SelectPose && !IsVariant;
    public bool KeepCandidatesShown { get => KeepCandidates && !IsVariant; set { if (!IsVariant) KeepCandidates = value; } }
    public bool KeepCandidatesEnabled => !IsGlb && !IsVariant;
    public ICommand CancelVariantCommand { get; }

    /// <summary>
    /// 装備一覧の「文章で見た目を作り直す」: 装備 baseEq の生成結果の形を使い、文章で見た目を作り直す準備をする（入力は文章。
    /// 保存すると新しい装備になる）。見た目を作り直した装備の「再生成」は LoadForRegeneration（同じ元の装備で差し替える）。
    /// </summary>
    public void LoadForVariant(EquipmentItem baseEq)
    {
        ResetForNew();
        SetVariantBase(baseEq);
        InputKind = "text";
        StatusText = L.T("UA3_VariantReady");
        AppLog.Info(L.F("Log_Gen_VariantPrepared", baseEq.Id));
    }

    private void SetVariantBase(EquipmentItem baseEq)
    {
        VariantBaseId = baseEq.Id;
        VariantLabel = L.F("UA3_VariantLabel", baseEq.DisplayName, baseEq.Id);
    }

    private void CancelVariant()
    {
        VariantBaseId = null;
        VariantLabel = null;
    }

    // --- 入力 ----------------------------------------------------------------------------------------
    private string _inputKind = "image";
    public string InputKind
    {
        get => _inputKind;
        set
        {
            if (Set(ref _inputKind, value))
            {
                OnPropertyChanged(nameof(IsImage)); OnPropertyChanged(nameof(IsText)); OnPropertyChanged(nameof(IsGlb));
                OnPropertyChanged(nameof(ModelText)); OnPropertyChanged(nameof(ConsentNeeded)); OnPropertyChanged(nameof(InputReady));
                OnPropertyChanged(nameof(ProviderNotice)); OnPropertyChanged(nameof(IsMulti)); OnPropertyChanged(nameof(MultiNotice));
                OnPropertyChanged(nameof(MultiNoticeIsWarning)); OnPropertyChanged(nameof(KeepCandidatesEnabled));
            }
        }
    }
    public bool IsImage { get => InputKind == "image"; set { if (value) InputKind = "image"; } }
    public bool IsText { get => InputKind == "text"; set { if (value) InputKind = "text"; } }
    public bool IsGlb { get => InputKind == "glb"; set { if (value) InputKind = "glb"; } }

    // --- 画像（1〜4 枚。複数のときは TRELLIS の run_multi_image） --------------------------------------------
    public const int MaxImages = 4;
    private readonly List<ImageSlot> _allSlots = new();
    /// <summary>画面に出す画像の欄（枚数の分だけ）。</summary>
    public System.Collections.ObjectModel.ObservableCollection<ImageSlot> Slots { get; } = new();
    public IReadOnlyList<Choice<int>> ImageCountChoices { get; } =
        [new(1, L.T("Gen_ImageCount1")), new(2, L.F("Gen_ImageCountN", 2)), new(3, L.F("Gen_ImageCountN", 3)), new(4, L.F("Gen_ImageCountN", 4))];
    private int _imageCount = 1;
    public int ImageCount
    {
        get => _imageCount;
        set
        {
            if (!Set(ref _imageCount, Math.Clamp(value, 1, MaxImages))) return;
            SyncSlots();
            OnPropertyChanged(nameof(IsMulti)); OnPropertyChanged(nameof(MultiNotice)); OnPropertyChanged(nameof(InputReady));
        }
    }
    public bool IsMulti => IsImage && ImageCount > 1;
    public IReadOnlyList<Choice<string>> MultiModeChoices { get; } =
        [new("stochastic", L.T("Gen_MultiStochastic")), new("multidiffusion", L.T("Gen_MultiDiffusion"))];
    private string _multiMode = "stochastic";
    public string MultiMode { get => _multiMode; set => Set(ref _multiMode, value); }
    public bool MultiSupported => _providerInfo.Bool("multi_image_supported");
    /// <summary>複数画像の短い説明（学習なしの手法で、必ずしも良くなるとは限らない）と、生成側が未対応のときの案内。</summary>
    public string? MultiNotice => !IsMulti ? null : MultiSupported ? L.T("Gen_MultiNote") : L.T("Gen_MultiUnsupported");
    public bool MultiNoticeIsWarning => IsMulti && !MultiSupported;

    /// <summary>1 枚目のパス（再生成などで使う）。</summary>
    public string ImagePath { get => _allSlots[0].Path; set => _allSlots[0].Path = value; }

    /// <summary>画像の欄のパス（空でないもの。表示していない欄も含む）。データの整理で、欄が使う保存済みの入力画像を残すため（U-D1 UD1-R4）。</summary>
    public IEnumerable<string> SlotPaths => _allSlots.Select(s => s.Path).Where(p => !string.IsNullOrWhiteSpace(p)).ToList();

    internal bool BackendReady => _main.IsReady;
    internal BackendClient Client => _main.RequireClient();
    public bool BgAvailable => _providerInfo.Obj("background_removal").Bool("available");
    public string BgExpected => _providerInfo.Obj("background_removal").Str("expected") ?? "";

    private void InitSlots()
    {
        string[] labels = [L.T("Gen_LabelFront"), L.T("Gen_LabelBack"), L.T("Gen_LabelLeft"), L.T("Gen_LabelRight")];
        for (int i = 0; i < MaxImages; i++) _allSlots.Add(new ImageSlot(this, i + 1, labels[i]));
        SyncSlots();
    }

    private void SyncSlots()
    {
        while (Slots.Count > ImageCount) Slots.RemoveAt(Slots.Count - 1);
        while (Slots.Count < ImageCount) Slots.Add(_allSlots[Slots.Count]);
    }

    internal void OnSlotChanged() => OnPropertyChanged(nameof(InputReady));

    private void RefreshSlots()
    {
        foreach (var sl in _allSlots) sl.Refresh();
        OnPropertyChanged(nameof(BgAvailable)); OnPropertyChanged(nameof(MultiSupported)); OnPropertyChanged(nameof(MultiNotice));
        OnPropertyChanged(nameof(MultiNoticeIsWarning));
    }

    private string _promptText = "";
    public string PromptText
    {
        get => _promptText;
        set
        {
            if (!Set(ref _promptText, value)) return;
            OnPropertyChanged(nameof(InputReady)); OnPropertyChanged(nameof(PromptNotice));
            OnPropertyChanged(nameof(PromptForeignNote)); SchedulePromptEstimate();       // 入力の補助（SF07。U-A4）
        }
    }

    /// <summary>
    /// 英語以外の文字が多い文章への注意（止めない）。生成 AI（CLIP）は英語の文章を前提にしていて、日本語では約 30 文字で
    /// 読まれる長さの上限（77 トークン）を超える。全角の句読点や é などが少し混じるだけでは出さない（2 割以上。
    /// バックエンドの pipeline.text_notes と同じ判定）。
    /// </summary>
    public string? PromptNotice
    {
        get
        {
            var chars = PromptText.Where(c => !char.IsWhiteSpace(c)).ToList();
            if (chars.Count == 0) return null;
            return chars.Count(c => c > 127) >= 0.2 * chars.Count ? L.T("Gen_PromptNonEnglish") : null;
        }
    }

    private string _glbPath = "";
    public string GlbPath
    {
        get => _glbPath;
        set
        {
            if (!Set(ref _glbPath, value?.Trim().Trim('"') ?? "")) return;
            _glbExists.Probe(_glbPath);       // ネットワーク上のパスは UI スレッドの外で確かめる（点検 R18B-04）
            OnPropertyChanged(nameof(InputReady));
        }
    }

    // 存在の確認はコマンドの確かめ直し（クリック・キー入力のたび）で呼ばれる。ネットワーク上のパスは確かめた結果を使う（点検 R18B-04）
    public bool InputReady => InputKind switch
    {
        "image" => Slots.All(sl => sl.PathExists),
        "text" => !string.IsNullOrWhiteSpace(PromptText) && !ConsentNeeded,
        _ => _glbExists.Exists(GlbPath),
    };

    // --- 生成設定 ------------------------------------------------------------------------------------
    public IReadOnlyList<Choice<string>> Providers { get; private set; } = [new("trellis", "TRELLIS")];
    private string _provider = "trellis";
    public string Provider { get => _provider; set => Set(ref _provider, value); }
    private JsonNode? _providerInfo;

    public string ModelText => InputKind == "glb" ? L.T("Gen_ModelNone")
        : _providerInfo.Obj("models").Obj(InputKind).Str("id") ?? (InputKind == "image" ? "microsoft/TRELLIS-image-large" : "microsoft/TRELLIS-text-xlarge");

    public string? ProviderNotice
    {
        get
        {
            if (InputKind == "glb" || _providerInfo == null) return null;
            if (_providerInfo.Obj("available").Bool(InputKind, true)) return null;
            var bad = _providerInfo.Obj("checks").Arr(InputKind).FirstOrDefault(c => c.Str("status") is "missing" or "error");
            var code = bad.Str("code");
            if (code == "CONSENT_REQUIRED") return null;          // 同意の案内は別に出す
            if (code == null) return null;
            var err = ErrorText.From(code, bad.Obj("params").Str("reason"), null, null);   // reason: outdated（固定と違う版）など
            return string.IsNullOrEmpty(err.Hint) ? err.Message : err.Message + Environment.NewLine + err.Hint;    // 対処（左の「セットアップ」など）も出す
        }
    }

    private int _seed = 1;
    public int Seed { get => _seed; set => Set(ref _seed, Math.Clamp(value, 0, int.MaxValue)); }
    private bool _selectPose = true;
    public bool SelectPose
    {
        get => _selectPose;
        set { if (Set(ref _selectPose, value)) { OnPropertyChanged(nameof(SelectPoseShown)); OnPropertyChanged(nameof(CandidatesEnabled)); } }
    }
    private int _candidates = 4;
    public int Candidates { get => _candidates; set => Set(ref _candidates, Math.Clamp(value, 1, 8)); }
    private bool _allowWeapons;
    public bool AllowWeapons { get => _allowWeapons; set { if (Set(ref _allowWeapons, value)) SchedulePromptEstimate(); } }
    public IReadOnlyList<Choice<double>> DetailChoices { get; } =
        [new(0.9, L.T("Gen_DetailStandard")), new(0.95, L.T("Gen_DetailLight"))];
    private double _simplify = 0.9;
    public double Simplify { get => _simplify; set => Set(ref _simplify, value); }
    public IReadOnlyList<Choice<int>> TextureChoices { get; } = [new(512, "512"), new(1024, "1024"), new(2048, "2048")];
    private int _textureSize = 1024;
    public int TextureSize { get => _textureSize; set => Set(ref _textureSize, value); }
    private int _targetVertices = 25000;
    public int TargetVertices { get => _targetVertices; set => Set(ref _targetVertices, Math.Clamp(value, 2000, 65535)); }

    // --- 詳細設定（既定はどれも今までと同じ動き。2026-10-17 追加機能 U-A1） -----------------------------------
    /// <summary>SF05 サンプリングの段数・ガイダンスの強さ（standard = モデルの既定、fast = 12 段）。</summary>
    public IReadOnlyList<Choice<string>> SamplerChoices { get; } =
        [new("standard", L.T("GenAdv_SamplerStandard")), new("fast", L.T("GenAdv_SamplerFast"))];
    private string _samplerPreset = "standard";
    public string SamplerPreset
    {
        get => _samplerPreset;
        set { if (Set(ref _samplerPreset, value is "fast" ? "fast" : "standard")) OnAdvancedChanged(); }
    }
    /// <summary>SF15 低 VRAM モード（デコードの間、条件付け・流れのモデルを CPU へ）。</summary>
    private bool _lowVram;
    public bool LowVram { get => _lowVram; set { if (Set(ref _lowVram, value)) OnAdvancedChanged(); } }
    /// <summary>SF17 厳格なオフライン（生成の子プロセスの外部への接続を拒否して記録する）。</summary>
    private bool _strictOffline;
    public bool StrictOffline { get => _strictOffline; set { if (Set(ref _strictOffline, value)) OnAdvancedChanged(); } }
    /// <summary>SF24 変換のテクスチャの最大辺（2048 は今までと同じ）。</summary>
    public IReadOnlyList<Choice<int>> ConvertTextureChoices { get; } =
        [new(2048, L.T("GenAdv_TextureMax2048")), new(1024, "1024"), new(512, "512")];
    private int _convertTextureMax = 2048;
    public int ConvertTextureMax
    {
        get => _convertTextureMax;
        set { if (Set(ref _convertTextureMax, value is 512 or 1024 ? value : 2048)) OnAdvancedChanged(); }
    }
    /// <summary>SF14・SF06 候補の生成結果を下書きに残す（1 体約 50 MB。選んだ候補・GLB の作り直しを 3D 生成なしで行える）。</summary>
    private bool _keepCandidates;
    public bool KeepCandidates
    {
        get => _keepCandidates;
        set { if (Set(ref _keepCandidates, value)) { OnAdvancedChanged(); OnPropertyChanged(nameof(KeepCandidatesShown)); } }
    }
    /// <summary>SF20（U-B）背景除去を生成の前にアプリ側で行い、透明な画像を渡す（オフは今までどおり生成 AI の中で除く）。</summary>
    private bool _bgAppRemoval;
    public bool BgAppRemoval { get => _bgAppRemoval; set { if (Set(ref _bgAppRemoval, value)) OnBgChanged(); } }
    /// <summary>SF20 rembg の後処理（アプリ側の背景除去のときだけ使う）。</summary>
    private bool _bgPostProcessMask, _bgAlphaMatting, _bgDecontaminate;
    public bool BgPostProcessMask { get => _bgPostProcessMask; set { if (Set(ref _bgPostProcessMask, value)) OnBgChanged(); } }
    public bool BgAlphaMatting { get => _bgAlphaMatting; set { if (Set(ref _bgAlphaMatting, value)) OnBgChanged(); } }
    public bool BgDecontaminate { get => _bgDecontaminate; set { if (Set(ref _bgDecontaminate, value)) OnBgChanged(); } }
    private void OnBgChanged()
    {
        OnAdvancedChanged();
        foreach (var sl in _allSlots) sl.OnBgSettingsChanged();      // 前の「背景除去を確認」の結果は今の設定と合わない
    }
    /// <summary>背景除去の設定（生成の要求・設定の保存と「背景除去を確認」で同じ値。SF20）。</summary>
    internal JsonObject BgSettingsJson() => new()
    {
        ["bg_app_removal"] = BgAppRemoval, ["bg_post_process_mask"] = BgPostProcessMask, ["bg_alpha_matting"] = BgAlphaMatting,
        ["bg_decontaminate"] = BgDecontaminate,
    };
    public bool AdvancedChanged => SamplerPreset != "standard" || LowVram || StrictOffline || ConvertTextureMax != 2048 || KeepCandidates
                                   || BgAppRemoval;
    public string AdvancedHeader => AdvancedChanged ? L.T("GenAdv_HeaderChanged") : L.T("GenAdv_Header");
    private void OnAdvancedChanged() { OnPropertyChanged(nameof(AdvancedChanged)); OnPropertyChanged(nameof(AdvancedHeader)); }

    // --- CLIP の同意（D-011） ---------------------------------------------------------------------------
    private bool _consentAccepted;
    public bool ConsentAccepted
    {
        get => _consentAccepted;
        private set { if (Set(ref _consentAccepted, value)) { OnPropertyChanged(nameof(ConsentNeeded)); OnPropertyChanged(nameof(InputReady)); } }
    }
    public bool ConsentNeeded => IsText && !ConsentAccepted;

    // --- 実行の状態 ------------------------------------------------------------------------------------
    private bool _isBusy;
    public bool IsBusy { get => _isBusy; private set { if (Set(ref _isBusy, value)) CommandManager.InvalidateRequerySuggested(); } }
    private string? _jobId;
    public string? JobId { get => _jobId; private set => Set(ref _jobId, value); }
    private double _progress;
    public double Progress { get => _progress; private set => Set(ref _progress, value); }
    private string _stageText = "";
    public string StageText { get => _stageText; private set => Set(ref _stageText, value); }
    public string ElapsedText => _watch.IsRunning || _watch.ElapsedMilliseconds > 0 ? L.F("Gen_Elapsed", (int)_watch.Elapsed.TotalSeconds) : "";
    private string _statusText = "";
    public string StatusText { get => _statusText; private set => Set(ref _statusText, value); }

    // --- 下書き（生成結果） -------------------------------------------------------------------------------
    private JsonNode? _draft;
    public JsonNode? Draft
    {
        get => _draft;
        private set
        {
            Set(ref _draft, value);
            OnPropertyChanged(nameof(HasDraft)); OnPropertyChanged(nameof(DraftReady)); OnPropertyChanged(nameof(DraftSummary));
            OnPropertyChanged(nameof(DraftWarnings)); OnPropertyChanged(nameof(HasWarnings));
            OnPropertyChanged(nameof(IsRotated)); OnPropertyChanged(nameof(CanRotate));
            OnPropertyChanged(nameof(RotateButtonText)); OnPropertyChanged(nameof(RotateText));
            OnPropertyChanged(nameof(CanRebuildGlb)); OnPropertyChanged(nameof(RebuildText)); OnPropertyChanged(nameof(CanRestorePrevious));
            CandidateRows = CandidateRow.Build(value?.Obj("candidates"), PickCandidateCommand, DraftFailed);
            PreviewItems = BuildPreviewItems(value?.Obj("files"));
        }
    }
    public bool HasDraft => Draft != null;
    public bool DraftReady => Draft.Str("status") == "ready";
    public string DraftSummary
    {
        get
        {
            if (Draft == null) return "";
            var conv = Draft.Obj("conversion");
            var pose = Draft.Obj("pose");
            var parts = new List<string>();
            if (Draft.Long("seed") is long s) parts.Add(L.F("Gen_DraftSeed", s));
            // 利用者が自動の選別（D-012）と違う候補を選んだ（SF06）
            if (Draft.Obj("candidates") is { } cv && cv.Long("auto_seed") is long au && Draft.Long("seed") is long cs && au != cs)
                parts.Add(L.F("UA2_DraftPicked", au));
            if (conv.Long("vertices") is long v) parts.Add(L.F("Gen_DraftVertices", v));
            if (pose.Dbl("arm_angle_deg") is double a) parts.Add(L.F("Gen_DraftArm", a));
            if (Draft.Str("model_id") is string m) parts.Add(m);
            if (Draft.Obj("variant_of").Str("equipment_id") is string vb) parts.Add(L.F("UA3_DraftVariant", vb));   // SF10
            if (IsRotated) parts.Add(L.T("Gen_DraftRotated"));
            return string.Join(L.T("Common_Gap"), parts);
        }
    }
    public IReadOnlyList<string> DraftWarnings => QualityText.Describe(Draft.Strings("warnings"));
    public bool HasWarnings => DraftWarnings.Count > 0;

    /// <summary>
    /// 生成の失敗で失敗の下書きに切り替える前に表示していた、保存前の下書き（ready）の ID。消さずに覚えておき、「前の結果に
    /// 戻す」と表示中の下書きの破棄で表示し直す。次の生成の成功・保存では、前の下書きと同じく置き換えて消す。以前は参照を
    /// 失い、画面からもデータの整理からも消せないまま残った（点検 R18B-02）。
    /// </summary>
    private string? _supersededDraftId;
    private string? SupersededDraftId
    {
        get => _supersededDraftId;
        set { _supersededDraftId = value; OnPropertyChanged(nameof(CanRestorePrevious)); CommandManager.InvalidateRequerySuggested(); }
    }
    /// <summary>失敗の下書きを表示していて、その前の結果に戻せる。</summary>
    public bool CanRestorePrevious => SupersededDraftId != null && DraftFailed;
    public ICommand RestorePreviousCommand { get; }

    private IReadOnlyList<PreviewItem> _previewItems = [];
    public IReadOnlyList<PreviewItem> PreviewItems { get => _previewItems; private set => Set(ref _previewItems, value); }

    // --- 候補を選ぶ・保存した生成結果から GLB を作り直す（SF06・SF14。2026-10-17 U-A2） ------------------------------
    /// <summary>プレビューの「候補」のタブの一覧（候補の選別の記録が無い下書きでは空）。</summary>
    private IReadOnlyList<CandidateRow> _candidateRows = [];
    public IReadOnlyList<CandidateRow> CandidateRows { get => _candidateRows; private set => Set(ref _candidateRows, value); }
    private bool DraftFailed => Draft.Str("status") == "failed";
    /// <summary>今の体の生成結果が下書きに保存してあり、GLB だけを作り直せる（「候補を保存する」で生成した下書き）。</summary>
    public bool CanRebuildGlb => CanRotate && Draft.Obj("candidates").Bool("rebuild_available");
    public string RebuildText
    {
        get
        {
            var cv = Draft.Obj("candidates");
            if (cv == null) return "";
            double mb = (cv.Long("samples_bytes") ?? 0) / (1024.0 * 1024.0);
            return L.F("UA2_RebuildHint", cv.Long("samples") ?? 0, Math.Round(mb));
        }
    }

    // --- 向き（正面が −Z の生成結果を 180° 回して変換し直す。docs/app_architecture.md 12 章 #16） -------------------
    /// <summary>今の変換で、変換の前にモデルを回した角度（0 / 180）。</summary>
    public bool IsRotated => (Draft.Long("yaw_deg") ?? 0) == 180;
    /// <summary>変換し直せる下書き（生成結果の GLB があり、生成・変換の途中ではない）。</summary>
    public bool CanRotate => Draft != null && Draft.Obj("files").Str("generated_glb") != null && (Draft.Str("status") is "ready" or "failed");
    public string RotateButtonText => IsRotated ? L.T("Gen_RotateBack") : L.T("Gen_Rotate180");
    /// <summary>回したときは、候補の選別（腕の角度・武器の判定。正面 +Z を仮定）が回す前の向きで行われたことも知らせる。</summary>
    public string RotateText => !IsRotated ? L.T("Gen_RotateHint")
        : Draft.Obj("settings").Bool("select_pose") ? L.T("Gen_RotatedHint") + " " + L.T("Gen_RotatedSelectionNote") : L.T("Gen_RotatedHint");
    /// <summary>再生成の元（装備・前の下書き）が 180° 回して変換したものだった（生成の後の案内に使う）。</summary>
    private bool _regenWasRotated;
    /// <summary>変換し直した後に、プレビューの表示を切り替える（GenerateView が受け取る）。</summary>
    public event Action<string>? PreviewSelectRequested;

    // --- 保存 ----------------------------------------------------------------------------------------
    private string _nameEn = "";
    public string NameEn { get => _nameEn; set => Set(ref _nameEn, value); }
    private string _nameJa = "";
    public string NameJa { get => _nameJa; set => Set(ref _nameJa, value); }
    private bool _keepImage = true;
    public bool KeepImage { get => _keepImage; set => Set(ref _keepImage, value); }
    private string? _replaceEquipmentId;
    public string? ReplaceEquipmentId { get => _replaceEquipmentId; private set { Set(ref _replaceEquipmentId, value); OnPropertyChanged(nameof(IsReplacing)); OnPropertyChanged(nameof(SaveButtonText)); } }
    private string? _replaceLabel;
    public string? ReplaceLabel { get => _replaceLabel; private set => Set(ref _replaceLabel, value); }
    public bool IsReplacing => ReplaceEquipmentId != null;
    public string SaveButtonText => IsReplacing ? L.T("Gen_SaveReplace") : L.T("Gen_Save");

    public ICommand BrowseGlbCommand { get; }
    public ICommand GenerateCommand { get; }
    public ICommand RegenerateCommand { get; }
    public ICommand StopCommand { get; }
    public ICommand SaveCommand { get; }
    public ICommand DiscardCommand { get; }
    public ICommand ShowConsentCommand { get; }
    public ICommand CancelReplaceCommand { get; }
    public ICommand RotateCommand { get; }
    public ICommand PickCandidateCommand { get; }
    public ICommand RebuildGlbCommand { get; }

    // ---------------------------------------------------------------------------------------------------
    public async Task InitAsync()
    {
        var c = _main.RequireClient();
        var s = await c.GetAsync("settings");
        var g = s.Obj("generation");
        Seed = (int)(g.Long("seed") ?? 1);
        SelectPose = g.Bool("select_pose", true);
        Candidates = (int)(g.Long("candidates") ?? 4);
        AllowWeapons = g.Bool("allow_weapons");
        Simplify = g.Dbl("simplify") ?? 0.9;
        TextureSize = (int)(g.Long("texture_size") ?? 1024);
        TargetVertices = (int)(g.Long("target_vertices") ?? 25000);
        MultiMode = g.Str("multi_mode") ?? "stochastic";
        SamplerPreset = g.Str("sampler_preset") ?? "standard";
        LowVram = g.Bool("low_vram");
        StrictOffline = g.Bool("strict_offline");
        ConvertTextureMax = (int)(g.Long("convert_texture_max") ?? 2048);
        KeepCandidates = g.Bool("keep_candidates");
        BgAppRemoval = g.Bool("bg_app_removal");               // SF20（U-B）
        BgPostProcessMask = g.Bool("bg_post_process_mask");
        BgAlphaMatting = g.Bool("bg_alpha_matting");
        BgDecontaminate = g.Bool("bg_decontaminate");
        KeepImage = s.Bool("keep_input_image", true);
        await RefreshProviderAsync();
        // 前回の下書き（保存していないもの）が残っていれば表示する。設定「下書きを残す」で保存の後も残した下書き（保存済みの
        // 記録 saved あり）は出さない（未保存として出し、もう一度保存すると装備が重複した。点検 R18A-02・R18B-01）。再生成・見た目の
        // 作り直しの準備の後（バックエンドの再起動）は、同じ差し替え先・元の装備の下書きだけを戻す（別の生成結果を差し替えの帯の
        // まま出さない。点検 R18B-02）
        var drafts = (await c.GetAsync("drafts"))!.AsArray();
        bool prepared = ReplaceEquipmentId != null || IsVariant;
        var last = drafts.FirstOrDefault(d => d.Str("status") == "ready" && d.Obj("saved") == null
            && (!prepared || (d.Str("replace_equipment_id") == ReplaceEquipmentId && d.Obj("variant_of").Str("equipment_id") == VariantBaseId)));
        if (last != null && Draft == null)
        {
            Draft = last.Clone();
            StatusText = L.T("Gen_RestoredDraft");
            // 再生成の下書き（差し替え先の記録あり）は差し替えの帯を戻し、保存すると差し替えになることを見せる。装備が無ければ
            // （削除済み）新規の表示のまま（保存は as_new で新しい装備になる。点検 R18A-01）
            if (ReplaceEquipmentId == null && Draft.Str("replace_equipment_id") is string rid)
            {
                try
                {
                    var eq = new EquipmentItem((await c.GetAsync($"library/{rid}")).Clone()!);
                    ReplaceEquipmentId = eq.Id;
                    ReplaceLabel = L.F("Gen_ReplaceLabel", eq.DisplayName, eq.Id);
                    NameEn = eq.NameEn;
                    NameJa = eq.NameJa ?? "";
                }
                catch (ApiException e) { AppLog.Info(L.F("Log_Gen_RestoredReplaceUnreadable", rid, e.Message)); }
            }
        }
    }

    /// <summary>
    /// 提供元の情報（モデル・背景除去の有無）と CLIP の同意を読み直す。セットアップの取得の後はこれだけを呼ぶ（InitAsync を呼び直すと、
    /// 画面の未保存の設定や、再生成で記録から戻した設定・次のシードが保存済みの値に戻り、下書きの復元も走る。点検 R18A-02・R20B-03）。
    /// </summary>
    public async Task RefreshProviderAsync()
    {
        var provs = (await _main.RequireClient().GetAsync("providers"))!.AsArray();
        Providers = provs.Select(p => new Choice<string>(p.Str("id")!, p.Str("display_name")!)).ToList();
        OnPropertyChanged(nameof(Providers));
        _providerInfo = provs.FirstOrDefault(p => p.Str("id") == Provider) ?? provs.FirstOrDefault();
        OnPropertyChanged(nameof(ModelText)); OnPropertyChanged(nameof(ProviderNotice)); RefreshSlots();
        await RefreshConsentAsync();
    }

    private async Task RefreshConsentAsync()
    {
        var st = await _main.RequireClient().GetAsync("consent/clip");
        ConsentAccepted = st.Bool("accepted");
        SchedulePromptEstimate();          // 同意の後は取得済みのトークナイザーで数え直す（SF07。U-A4）
    }

    private void BrowseGlb()
    {
        var d = new OpenFileDialog { Filter = "GLB (*.glb)|*.glb", CheckFileExists = true };
        if (d.ShowDialog() == true) GlbPath = d.FileName;
    }

    private JsonObject SettingsJson(int seed) => new()
    {
        ["provider"] = Provider, ["seed"] = seed, ["select_pose"] = SelectPose, ["candidates"] = Candidates,
        ["allow_weapons"] = AllowWeapons, ["simplify"] = Simplify, ["texture_size"] = TextureSize, ["target_vertices"] = TargetVertices,
        ["multi_mode"] = MultiMode,
        ["sampler_preset"] = SamplerPreset, ["low_vram"] = LowVram, ["strict_offline"] = StrictOffline,
        ["convert_texture_max"] = ConvertTextureMax,
        ["keep_candidates"] = KeepCandidates,
        ["bg_app_removal"] = BgAppRemoval, ["bg_post_process_mask"] = BgPostProcessMask, ["bg_alpha_matting"] = BgAlphaMatting,
        ["bg_decontaminate"] = BgDecontaminate,
    };

    private async Task GenerateAsync(bool regenerate)
    {
        var c = _main.RequireClient();
        int seed = Seed;
        if (regenerate)
        {
            // 別のシードで作り直す（候補を選ぶときは候補の数だけずらす）。前の生成で試したシードは繰り返さない
            long used = Draft.Long("seed") ?? Seed;
            seed = NextRegenSeed(Math.Max(used, Seed), SelectPose && !IsVariant, Candidates, TriedSeedEnd(Draft.Obj("settings")));
            Seed = seed;
        }
        var input = new JsonObject { ["kind"] = InputKind };
        List<string>? sentPaths = null;     // 送った画像のパス（生成の途中で欄の画像を変えたかを完了後に確かめる）
        if (IsImage)
        {
            sentPaths = Slots.Select(sl => sl.Path).ToList();
            input["image_paths"] = new JsonArray(sentPaths.Select(sp => (JsonNode)sp).ToArray());
            input["image_labels"] = new JsonArray(Slots.Select(sl => (JsonNode)sl.Label.Trim()).ToArray());
            // 背景除去の強制（画像ごと。SF19）。どれも外していれば送らない（要求は今までと同じ）
            if (Slots.Any(sl => sl.ForceBgEffective))
                input["image_force_bg_removal"] = new JsonArray(Slots.Select(sl => (JsonNode)sl.ForceBgEffective).ToArray());
        }
        else if (IsText) input["text"] = PromptText;
        else input["glb_path"] = GlbPath;
        var p = new JsonObject { ["input"] = input, ["settings"] = SettingsJson(seed) };
        if (ReplaceEquipmentId != null) p["replace_equipment_id"] = ReplaceEquipmentId;
        // 見た目の作り直し（SF10）: 元の装備の生成結果の形を使うジョブ variant（入力は文章だけ）
        var jobType = IsVariant ? "variant" : "generate";
        if (IsVariant)
        {
            p = new JsonObject { ["equipment_id"] = VariantBaseId, ["text"] = PromptText, ["settings"] = SettingsJson(seed) };
            if (ReplaceEquipmentId != null) p["replace_equipment_id"] = ReplaceEquipmentId;
        }
        var nextDefaults = new JsonObject { ["generation"] = SettingsJson(Seed) };

        var oldDraft = Draft;
        bool wasRotated = _regenWasRotated || (regenerate && (oldDraft.Long("yaw_deg") ?? 0) == 180);
        IsBusy = true;
        Progress = 0;
        StageText = L.T("Stage_generate_check");
        StatusText = "";
        _watch.Restart();
        _timer.Start();
        var startInfo = new List<string> { InputKind };
        if (IsImage) startInfo.Add(L.F("Log_Gen_StartedImages", ImageCount));
        if (IsVariant) startInfo.Add(L.F("Log_Gen_StartedVariant", VariantBaseId));
        AppLog.Info(L.F("Log_Gen_Started", string.Join(L.T("Common_ListSep"), startInfo)));
        try
        {
            // 生成設定を次回の既定値として保存する（忙しい状態にしてから。待つ間に生成・保存などを重ねて動かさない。点検 R18B-03）
            try { await c.PutAsync("settings", nextDefaults); } catch (ApiException) { }
            var prog = new Progress<JobProgress>(ReportProgress);
            var res = await c.RunJobAsync(jobType, p, id => JobId = id, prog);
            Draft = res.Obj("draft").Clone();
            if (sentPaths != null)
            {
                // 生成に使った画像（背景除去・切り抜きの後）を各欄に。枚数が合わなければ 1 枚目だけ。生成の途中で画像を変えた欄には
                // 付けない（前の画像の切り抜きを新しい画像の「背景除去後」として出さない。「背景除去を確認」の C47 と同じ。点検 R18A-05）
                var pres = Draft.Strings("preprocessed_images").ToList();
                if (pres.Count != sentPaths.Count) pres = Draft.Obj("files").Str("preprocessed_png") is string p1 ? [p1] : [];
                for (int i = 0; i < pres.Count && i < sentPaths.Count; i++)
                {
                    if (string.Equals(_allSlots[i].Path, sentPaths[i], StringComparison.OrdinalIgnoreCase))
                        _allSlots[i].SetProcessed(pres[i], L.T("Gen_BgUsedForGeneration"), _allSlots[i].ShowProcessed, fromGeneration: true);
                    else
                        AppLog.Info(L.F("Log_Gen_SlotChanged", i + 1));
                }
            }
            // 生成は回さずに変換する。前の結果を回していたら、もう一度回すかを利用者が決められるように知らせる
            StatusText = wasRotated && !IsRotated ? L.T("Gen_DoneWasRotated") : L.T("Gen_Done");
            if (oldDraft != null && oldDraft.Str("id") != Draft.Str("id"))
            {
                try { await c.DeleteAsync($"drafts/{oldDraft.Str("id")}"); } catch (ApiException) { }   // 前の下書きは置き換える
            }
            await DropSupersededDraftAsync();      // 失敗の前に表示していた下書きも置き換える（点検 R18B-02）
        }
        catch (OperationCanceledException)
        {
            StatusText = L.T("Gen_Cancelled");
        }
        catch (ApiException e)
        {
            StatusText = L.T("Gen_Failed");
            var did = e.Param("draft_id");
            if (did != null)
            {
                try
                {
                    var failed = (await c.GetAsync($"drafts/{did}")).Clone();
                    // 表示していた保存前の結果（ready）は消さずに覚え、「前の結果に戻す」で戻せるようにする。失敗の下書きの表示中に
                    // また失敗したときは、覚えている結果をそのまま残す（間の失敗の下書きは status failed なのでデータの整理に出る。点検 R18B-02）
                    if (oldDraft != null && oldDraft.Str("id") != did && oldDraft.Str("status") == "ready")
                    {
                        if (SupersededDraftId != null && SupersededDraftId != oldDraft.Str("id"))
                            await DropSupersededDraftAsync();     // 失敗の下書きを変換し直して ready にした後の失敗: 古い方は置き換える
                        SupersededDraftId = oldDraft.Str("id");
                    }
                    Draft = failed;
                }
                catch (ApiException) { }
            }
            if (e.Code == "REFS_MISSING") await _main.Setup.OfferRefsAsync(e);    // B-002: エラーの画面から準備を始められる
            else App.ShowError(e);
        }
        finally
        {
            _watch.Stop();
            _timer.Stop();
            OnPropertyChanged(nameof(ElapsedText));
            if (StageText == L.T("Gen_Stopping")) StageText = "";     // 停止の後・停止が間に合わず終わった後に残さない（点検 R18A-06）
            IsBusy = false;
            JobId = null;
        }
    }

    /// <summary>
    /// 「180° 回して変換し直す」/「元の向きに戻して変換し直す」: 下書きの生成結果の GLB を、y 軸回りに回してから変換し直す
    /// （ジョブ convert。3D 生成はやり直さない）。回した角度は下書きと、保存した装備に記録される。
    /// </summary>
    private async Task RotateAsync()
    {
        if (Draft == null) return;
        var c = _main.RequireClient();
        var id = Draft.Str("id");
        int yaw = IsRotated ? 0 : 180;
        IsBusy = true;
        Progress = 0;
        StageText = L.T("Stage_convert_load");
        StatusText = "";
        _watch.Restart();
        _timer.Start();
        AppLog.Info(L.F("Log_Gen_ReconvertYaw", id, yaw));
        try
        {
            var res = await c.RunJobAsync("convert", new JsonObject { ["draft_id"] = id, ["yaw_deg"] = yaw }, jid => JobId = jid,
                new Progress<JobProgress>(ReportProgress));
            Draft = res.Obj("draft").Clone();
            StatusText = yaw == 180 ? L.T("Gen_RotateDone") : L.T("Gen_RotateBackDone");
            PreviewSelectRequested?.Invoke("preview_m_glb");      // 回した結果（変換後）を見せる。「生成結果」は回さない
        }
        catch (OperationCanceledException)
        {
            StatusText = L.T("Gen_RotateCancelled");
            await ReloadDraftAsync(id);
        }
        catch (ApiException e)
        {
            StatusText = L.T("Gen_RotateFailed");
            await ReloadDraftAsync(id);
            App.ShowError(e);
        }
        finally
        {
            _watch.Stop();
            _timer.Stop();
            OnPropertyChanged(nameof(ElapsedText));
            if (StageText == L.T("Gen_Stopping")) StageText = "";     // 停止の後・停止が間に合わず終わった後に残さない（点検 R18A-06）
            IsBusy = false;
            JobId = null;
        }
    }

    private async Task ReloadDraftAsync(string? id)
    {
        if (id == null) return;
        try { Draft = (await _main.RequireClient().GetAsync($"drafts/{id}")).Clone(); } catch (ApiException) { }
    }

    /// <summary>
    /// 「この候補で作る」（SF06）: 候補のシードの体で GLB を作り、変換し直す（ジョブ rebuild_glb）。生成結果が保存してあれば
    /// 3D 生成なし（候補の図と同じ体）、無ければ確認のうえ、そのシードで候補の選別なしに生成し直す。細かさ・テクスチャは
    /// 下書きの生成のときのまま（変えるときは「GLB を作り直す」）。変換の途中で止まった下書きの今の体は、変換だけをやり直す。
    /// </summary>
    private async Task PickCandidateAsync(object? p)
    {
        if (Draft == null || p is not long seed) return;
        var row = CandidateRows.FirstOrDefault(r => r.Seed == seed);
        if (row == null || !row.CanPick) return;
        if (row.IsCurrent)
        {
            await RunDraftJobAsync("convert", new JsonObject { ["draft_id"] = Draft.Str("id") }, null);
            return;
        }
        if (!row.HasSample && !Dialogs.Confirm(L.T("UA2_RegenTitle"), L.F("UA2_RegenMessage", seed), L.T("UA2_RegenPrimary"),
                                               L.T("UA2_RegenHint"))) return;
        await RunDraftJobAsync("rebuild_glb", new JsonObject
        {
            ["draft_id"] = Draft.Str("id"), ["seed"] = seed,
            ["settings"] = new JsonObject { ["low_vram"] = LowVram, ["strict_offline"] = StrictOffline },
            // 保存済みとして確認なしで選んだ候補は、ジョブの開始までに生成結果が消えていれば生成し直さずに止める（点検 UA2-R2）
            ["require_sample"] = row.HasSample,
        }, seed);
    }

    /// <summary>「GLB を作り直す」（SF14）: 今の体の保存した生成結果から、生成設定の細かさ・テクスチャで GLB を作り直す。</summary>
    private Task RebuildAsync(long? seed)
    {
        if (Draft == null) return Task.CompletedTask;
        return RunDraftJobAsync("rebuild_glb", new JsonObject
        {
            ["draft_id"] = Draft.Str("id"),
            ["settings"] = new JsonObject { ["simplify"] = Simplify, ["texture_size"] = TextureSize, ["low_vram"] = LowVram,
                                            ["strict_offline"] = StrictOffline },
        }, seed);
    }

    /// <summary>下書きのジョブ（rebuild_glb・convert）を実行し、結果の下書きを表示する。pickSeed は選んだ候補（作り直しは null）。</summary>
    private async Task RunDraftJobAsync(string type, JsonObject body, long? pickSeed)
    {
        var c = _main.RequireClient();
        var id = Draft.Str("id");
        bool wasRotated = IsRotated;
        IsBusy = true;
        Progress = 0;
        StageText = L.T(type == "convert" ? "Stage_convert_load" : "Stage_generate_check");
        StatusText = "";
        _watch.Restart();
        _timer.Start();
        AppLog.Info(type == "convert" ? L.F("Log_Gen_ReconvertCurrent", id)
            : pickSeed is long ps ? L.F("Log_Gen_PickCandidate", id, ps) : L.F("Log_Gen_RebuildGlb", id));
        try
        {
            var res = await c.RunJobAsync(type, body, jid => JobId = jid, new Progress<JobProgress>(ReportProgress));
            Draft = res.Obj("draft").Clone();
            long now = Draft.Long("seed") ?? 0;
            StatusText = type == "convert" ? L.T("UA2_ReconvertDone")
                : pickSeed == null ? L.T("UA2_RebuildDone")
                : (res.Obj("picked").Str("source") == "regenerated" ? L.F("UA2_PickDoneRegenerated", now) : L.F("UA2_PickDone", now))
                  + (wasRotated && !IsRotated ? " " + L.T("UA2_WasRotated") : "");
        }
        catch (OperationCanceledException)
        {
            await ReloadDraftAsync(id);
            StatusText = DraftReady ? L.T("UA2_CancelledKept") : L.T("UA2_CancelledConvert");
        }
        catch (ApiException e)
        {
            await ReloadDraftAsync(id);
            StatusText = DraftReady ? L.T("UA2_FailedKept") : L.T("UA2_FailedConvert");
            App.ShowError(e);
        }
        finally
        {
            _watch.Stop();
            _timer.Stop();
            OnPropertyChanged(nameof(ElapsedText));
            if (StageText == L.T("Gen_Stopping")) StageText = "";     // 停止の後・停止が間に合わず終わった後に残さない（点検 R18A-06）
            IsBusy = false;
            JobId = null;
        }
    }

    private void ReportProgress(JobProgress p)
    {
        Progress = p.Progress * 100;
        StageText = StageNames.Describe(p.Stage, p.Detail);
    }

    private async Task StopAsync()
    {
        if (JobId == null) return;
        StageText = L.T("Gen_Stopping");
        await _main.RequireClient().CancelJobAsync(JobId);
    }

    private async Task SaveAsync()
    {
        var c = _main.RequireClient();
        if (!IsReplacing && string.IsNullOrWhiteSpace(NameEn))
        {
            Dialogs.Info(L.T("Gen_NameRequiredTitle"), L.T("Gen_NameRequired"));
            return;
        }
        // 表示中の生成結果が、差し替える装備の再生成で作ったものでない（新規・別の装備の再生成の結果を表示したまま、装備一覧で
        // 「再生成」を押した、など）: 元のモデルと生成の記録は戻せないので、差し替える前に確かめる（点検 R18A-01・R18B-02）
        var madeFor = Draft.Str("replace_equipment_id");
        if (IsReplacing && madeFor != ReplaceEquipmentId)
        {
            var made = madeFor == null ? L.T("GenR18_MadeForNew")
                : L.F("GenR18_MadeForEquipment", _main.Library.Items.FirstOrDefault(i => i.Id == madeFor)?.DisplayName ?? madeFor, madeFor);
            var target = _main.Library.Items.FirstOrDefault(i => i.Id == ReplaceEquipmentId)?.DisplayName ?? NameEn;
            if (!Dialogs.Confirm(L.T("GenR18_MismatchTitle"), L.F("GenR18_MismatchMessage", made, target, ReplaceEquipmentId),
                                 L.T("Gen_SaveReplace"), L.T("GenR18_MismatchHint"), DialogKind.Warning)) return;
        }
        var body = new JsonObject { ["name_en"] = NameEn.Trim(), ["keep_image"] = KeepImage };
        if (!string.IsNullOrWhiteSpace(NameJa)) body["name_ja"] = NameJa.Trim();
        if (ReplaceEquipmentId != null) body["replace_equipment_id"] = ReplaceEquipmentId;
        // 新規の保存は明示する（再生成の下書きに記録した差し替え先を使わせない。「新規として作る」・装備一覧の「新規作成」の
        // 後や、差し替え先が無くなった復元の下書き。以前は記録に戻って元の装備が差し替えられ、改名された。点検 R18A-01）
        else body["as_new"] = true;
        var startReplace = ReplaceEquipmentId;
        var startVariant = VariantBaseId;
        JsonNode? eq;
        // 保存の応答を待つ間も忙しい状態にする（同じ下書きの変換し直し・破棄、装備一覧の「再生成」などを重ねて動かさない。点検 R18B-03）
        IsBusy = true;
        try { eq = await c.PostAsync($"drafts/{Draft.Str("id")}/save", body); }
        finally { IsBusy = false; }
        AppLog.Info(L.F("Log_Gen_Saved", eq.Str("id")));
        var replaced = startReplace != null;
        Draft = null;
        await DropSupersededDraftAsync();         // 失敗の前に表示していた下書きは、保存した結果で置き換えた（点検 R18B-02）
        foreach (var sl in _allSlots) sl.ClearGenerationResult();
        // 待つ間に別の装備の再生成・見た目の作り直しの準備を読み込んでいたら、その準備は消さない（点検 R18B-03）
        if (ReplaceEquipmentId == startReplace && VariantBaseId == startVariant)
        {
            ReplaceEquipmentId = null;
            ReplaceLabel = null;
            _regenWasRotated = false;
            CancelVariant();            // 見た目の作り直し（SF10）も終える
            NameEn = "";
            NameJa = "";
        }
        StatusText = replaced ? L.T("Gen_Replaced") : L.T("Gen_Saved");
        await _main.Library.RefreshAsync(eq.Str("id"));
        _main.Navigate("library");
        // 保存は止めない注意（英語名に ASCII の外の文字がある、など。応答の warnings。2026-10-17 点検 C75・FG05-R3）
        var warnings = eq.Strings("warnings").Where(w => !string.IsNullOrWhiteSpace(w)).ToList();
        if (warnings.Count > 0)
        {
            foreach (var w in warnings) AppLog.Warn(L.F("Log_Gen_NameWarning", w));
            Dialogs.Show(new DialogSpec
            {
                Kind = DialogKind.Warning, Title = L.T("Gen_NameWarningTitle"), Message = L.T("Gen_NameWarningMessage"),
                Hint = L.T("Gen_NameWarningHint"),
                Sections = [new DialogSection { Header = L.T("Gen_NameWarningHeader"), Items = warnings,
                                                Brush = (Brush)System.Windows.Application.Current.Resources["WarnBrush"] }],
            });
        }
    }

    private async Task DiscardAsync()
    {
        if (Draft == null) return;
        if (!Dialogs.Confirm(L.T("Gen_DiscardTitle"), L.T("Gen_DiscardMessage"), L.T("Gen_Discard"), kind: DialogKind.Warning)) return;
        try { await _main.RequireClient().DeleteAsync($"drafts/{Draft.Str("id")}"); } catch (ApiException) { }
        Draft = null;
        foreach (var sl in _allSlots) sl.ClearGenerationResult();
        StatusText = "";
        // 生成の失敗の前に表示していた結果があれば、それを表示し直す（点検 R18B-02）
        if (SupersededDraftId != null) await RestorePreviousAsync();
    }

    /// <summary>「前の結果に戻す」: 生成の失敗の前に表示していた保存前の下書きを表示し直す（表示中の失敗の下書きは消さない。
    /// status failed なのでデータの整理から消せる。D-040）。</summary>
    private async Task RestorePreviousAsync()
    {
        var id = SupersededDraftId;
        if (id == null) return;
        SupersededDraftId = null;
        try
        {
            Draft = (await _main.RequireClient().GetAsync($"drafts/{id}")).Clone();
            StatusText = L.T("Gen_RestoredPrevious");
            AppLog.Info(L.F("Log_Gen_RestoredPrevious", id));
        }
        catch (ApiException e)
        {
            AppLog.Warn(L.F("Log_Gen_RestorePreviousFailed", id, e.Message));
            App.ShowError(e);
        }
    }

    /// <summary>覚えていた生成の失敗の前の下書きを消す（生成の成功・保存で置き換えたとき）。</summary>
    private async Task DropSupersededDraftAsync()
    {
        var id = SupersededDraftId;
        if (id == null) return;
        SupersededDraftId = null;
        try { await _main.RequireClient().DeleteAsync($"drafts/{id}"); } catch (ApiException) { }
    }

    private async Task ShowConsentAsync()
    {
        var c = _main.RequireClient();
        // 注意文は表示言語のもの（英語の画面は英語、ほかは日本語）。同意の記録には、表示した注意文の言語と SHA-256 を残す
        // （2026-10-18 未決定 #2。以前の英語の画面は日本語の注意文に案内 UG_ConsentLangNote を添えていた）
        var st = await c.GetAsync("consent/clip?lang=" + L.Language);
        if (st.Bool("accepted"))
        {
            Dialogs.Info(L.T("Consent_Title"), L.F("Consent_AlreadyAccepted", st.Str("accepted_at")));
            ConsentAccepted = true;
            return;
        }
        var r = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Warning, Title = L.T("Consent_Title"), Message = st.Str("notice") ?? "",
            CheckText = L.T("Consent_Check"), CheckRequired = true, PrimaryText = L.T("Consent_Accept"), SecondaryText = L.T("Dialog_Cancel"),
        });
        if (!r.Accepted || !r.Checked) return;
        await c.PostAsync("consent/clip", new JsonObject
        {
            ["accept"] = true, ["notice_sha256"] = st.Str("notice_sha256"), ["lang"] = st.Str("notice_lang") ?? L.Language,
        });
        await RefreshConsentAsync();
        await _main.RefreshEnvironmentAsync(true);
        var provs = (await c.GetAsync("providers"))!.AsArray();
        _providerInfo = provs.FirstOrDefault(p => p.Str("id") == Provider);
        OnPropertyChanged(nameof(ProviderNotice));
        RefreshSlots();
    }

    /// <summary>装備一覧の「再生成」: 装備の入力と設定を読み込み、保存すると同じ装備（ID）を差し替える。</summary>
    public void LoadForRegeneration(EquipmentItem eq)
    {
        var gen = eq.Data.Obj("generation");
        var method = gen.Str("method");
        // 見た目を作り直した装備（SF10）: 元の装備が一覧にあり（生成結果つき）、詳細設定で使えるようにしてあれば、同じ形で
        // 作り直す。無ければ文章だけから作り直す（形も変わる。案内を出す）
        var variantOf = gen.Obj("variant_of").Str("equipment_id");
        var variantBase = variantOf == null || !TextVariantEnabled ? null
            : _main.Library.Items.FirstOrDefault(i => i.Id == variantOf && i.HasGeneratedGlb);
        CancelVariant();
        ReplaceEquipmentId = eq.Id;
        ReplaceLabel = L.F("Gen_ReplaceLabel", eq.DisplayName, eq.Id);
        NameEn = eq.NameEn;
        NameJa = eq.NameJa ?? "";
        if (method == "text")
        {
            InputKind = "text";
            PromptText = gen.Str("input_text") ?? "";
            if (variantBase != null) SetVariantBase(variantBase);
        }
        else if (method == "image")
        {
            InputKind = "image";
            var imgs = eq.Data.Arr("input_images");
            ImageCount = Math.Clamp(imgs.Count, 1, MaxImages);
            for (int i = 0; i < ImageCount; i++)
            {
                var it = i < imgs.Count ? imgs[i] : null;
                if (it?.Str("label") is string lab && lab.Length > 0) _allSlots[i].Label = lab;
                _allSlots[i].Path = it?.Str("path") ?? (i == 0 ? eq.Files.Str("input_image") ?? "" : "");
                _allSlots[i].ForceBgRemoval = it?.Bool("force_bg_removal") ?? false;     // 背景除去の強制（SF19）は記録のとおり
            }
            if (gen.Str("multi_mode") is string mm) MultiMode = mm;
        }
        else
        {
            InputKind = "glb";
            GlbPath = eq.Files.Str("generated_glb") ?? "";
        }
        var st = gen.Obj("settings");
        // 見た目を作り直した装備の記録の候補の選別・候補の数・背景の設定は、作り直しのジョブが使わないためにバックエンドがオフと
        // して残した値で、利用者が選んだ値ではない。記録からは戻さず今の設定（利用者の既定）のままにする。戻すと生成の開始で次回の
        // 既定値として保存され、以後の普通の生成で選別がオフのまま残る（2026-10-17 点検 UA3-R1）
        bool variantRecord = variantOf != null;
        if (st != null)
        {
            if (st.Long("seed") is long sd) Seed = (int)sd;
            if (!variantRecord)
            {
                SelectPose = st.Bool("select_pose", SelectPose);
                if (st.Long("candidates") is long cd) Candidates = (int)cd;
            }
            AllowWeapons = st.Bool("allow_weapons", AllowWeapons);
            if (st.Dbl("simplify") is double sp) Simplify = sp;
            if (st.Long("texture_size") is long ts) TextureSize = (int)ts;
            if (st.Long("target_vertices") is long tv) TargetVertices = (int)tv;
        }
        // 結果を左右する詳細設定は記録のとおりにする（記録に無い装備は、生成の設定の記録ごと無い装備も含めて、その設定が
        // できる前の既定で作ったもの）。低 VRAM・通信の遮断は結果を変えない PC 側の選択なので、今の設定のまま
        SamplerPreset = st?.Str("sampler_preset") ?? "standard";
        if (!variantRecord)
        {
            BgAppRemoval = st?.Bool("bg_app_removal") ?? false;          // SF20（U-B）: 背景除去の方法と後処理も記録のとおり
            BgPostProcessMask = st?.Bool("bg_post_process_mask") ?? false;
            BgAlphaMatting = st?.Bool("bg_alpha_matting") ?? false;
            BgDecontaminate = st?.Bool("bg_decontaminate") ?? false;
        }
        ConvertTextureMax = (int)(st?.Long("convert_texture_max") ?? 2048);
        // 次のシード: 前の生成で試したシードの次（見た目の作り直しは記録のシード 1 つだけを試している）
        if (gen.Long("seed") is long used)
            Seed = NextRegenSeed(used, SelectPose && !variantRecord, Candidates, TriedSeedEnd(st));
        _regenWasRotated = (eq.Data.Long("yaw_deg") ?? 0) == 180;
        StatusText = method == "image" && Slots.Any(sl => string.IsNullOrEmpty(sl.Path)) ? L.T("Gen_RegenNoImage")
            : _regenWasRotated ? L.T("Gen_RegenReadyRotated") : L.T("Gen_RegenReady");
        if (variantOf != null && method == "text")
            StatusText = variantBase != null ? L.T("UA3_RegenReady") : L.F("UA3_RegenNoBase", variantOf);
        // 表示中の生成結果はこの装備の再生成で作ったものではない（そのまま保存すると確認を出す。点検 R18A-01）
        if (DraftReady && Draft.Str("replace_equipment_id") != eq.Id)
            StatusText += " " + L.T("GenR18_RegenDraftOther");
    }

    /// <summary>候補の選別の最大の回数（generate_glb.py の --max-rounds の既定。アプリは指定しない）。</summary>
    public const int MaxSelectRounds = 2;

    /// <summary>
    /// 前の生成で試したシードの次（生成の設定 settings から）。候補の選別は、1 回目の候補がすべて除外されると 2 回目
    /// （要求のシード + 候補数 から）まで進み、そのうえで 1 回目の候補が代わりに選ばれることもあるので、選ばれたシードに
    /// 関係なく、要求のシード + 最大の回数 × 候補数 までは使った可能性がある。選別しないときは要求のシード + 1。
    /// （バックエンドは実際に使った範囲を生成の記録 info.next_seed に残すが、下書きの要約には出ていない）
    /// </summary>
    public static long TriedSeedEnd(JsonObject? settings)
    {
        if (settings?.Long("seed") is not long requested) return 0;
        bool select = settings.Bool("select_pose");
        long per = select ? Math.Max(1, settings.Long("candidates") ?? 1) : 1;
        return requested + (select ? MaxSelectRounds * per : 1);
    }

    /// <summary>
    /// 再生成の次のシード: baseSeed（前に使ったシード）の次（候補を選ぶときは候補の数だけずらす）と、前の生成で試したシードの
    /// 範囲の終わり triedEnd の大きい方。次の生成が使うシード（候補の選別では 最大の回数 × 候補数）が上限（2147483647）を
    /// 超えるときは 0 に折り返す（以前は上限で切り詰め、以後の再生成が同じシードを繰り返した。点検 R18A-04）。
    /// </summary>
    public static int NextRegenSeed(long baseSeed, bool select, int candidates, long triedEnd)
    {
        long per = select ? Math.Max(1, candidates) : 1;
        long next = Math.Max(baseSeed + per, triedEnd);
        long span = select ? MaxSelectRounds * per : 1;
        return next + span - 1 > int.MaxValue ? 0 : (int)next;
    }

    public void ResetForNew()
    {
        ReplaceEquipmentId = null;
        ReplaceLabel = null;
        _regenWasRotated = false;
        CancelVariant();
        NameEn = "";
        NameJa = "";
        StatusText = "";
    }

    public static IReadOnlyList<PreviewItem> BuildPreviewItems(JsonObject? files)
    {
        var list = new List<PreviewItem>();
        if (files == null) return list;
        void add(string key, string label, string kind, bool ground = false)
        {
            var p = files.Str(key);
            if (!string.IsNullOrEmpty(p)) list.Add(new PreviewItem(key, label, p, kind, ground));
        }
        add("generated_glb", L.T("Preview_Generated"), "glb");
        add("preview_m_glb", L.T("Preview_Male"), "glb", true);
        add("preview_f_glb", L.T("Preview_Female"), "glb", true);
        add("preview_png", "", "check");
        add("candidates_png", "", "candidates");
        return list;
    }
}

/// <summary>段階の名前（Stage_&lt;stage&gt;）。候補の番号などは detail から。</summary>
public static class StageNames
{
    public static string Describe(string? stage, JsonObject? detail)
    {
        if (stage == null) return "";
        var s = L.TryT("Stage_" + stage) ?? stage;
        if (detail != null && detail.Long("index") is long i && detail.Long("count") is long n)
            s = L.F("Stage_WithCount", s, i, n);
        return s;
    }
}

/// <summary>品質の注意（gdconvert の quality flags、生成の注意）→ 利用者向けの文（Quality_&lt;flag&gt;）。</summary>
public static class QualityText
{
    public static IReadOnlyList<string> Describe(IEnumerable<string> flags)
    {
        var outp = new List<string>();
        foreach (var f in flags.Distinct())
        {
            var key = f.StartsWith("selection:") ? "selection" : f.Split(':')[0];
            key = System.Text.RegularExpressions.Regex.Replace(key, "_[RL]$", "");      // hand_far_from_weapon_R など
            var text = L.TryT("Quality_" + key) ?? f;
            if (!outp.Contains(text)) outp.Add(text);          // 左右の手など、同じ文になる注意は 1 回だけ
        }
        return outp;
    }
}
