using System.Collections.ObjectModel;
using System.Globalization;
using System.IO;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media.Imaging;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using GDSkinStudio.Views;
using Microsoft.Win32;

namespace GDSkinStudio.ViewModels;

/// <summary>装備 1 件（計画書 10 章の項目を表示用に）。</summary>
public sealed class EquipmentItem
{
    public JsonNode Data { get; }
    public EquipmentItem(JsonNode data) { Data = data; }

    public string Id => Data.Str("id") ?? "";
    public string NameEn => Data.Str("name") ?? "";
    public string? NameJa => Data.Str("name_ja");
    public string DisplayName => string.IsNullOrEmpty(NameJa) ? NameEn : L.F("Common_ParenFormat", NameEn, NameJa);
    public string MethodText => L.TryT("Method_" + Data.Str("method")) ?? Data.Str("method") ?? "";
    public string CreatedText => FormatTime(Data.Str("created_at"));
    public string ConversionText => L.TryT("Conversion_" + Data.Str("conversion")) ?? Data.Str("conversion") ?? "";
    public string ModStatus => Data.Str("mod") ?? "";
    public string ModText => L.TryT("ModStatus_" + ModStatus) ?? ModStatus;
    public bool DevOnly => Data.Bool("dev_only");
    public JsonObject? Files => Data.Obj("files");
    /// <summary>生成結果（source\generated.glb）が保存されていて、3D 生成をやり直さずに変換し直せるか。</summary>
    public bool HasGeneratedGlb => Files?.ContainsKey("generated_glb") == true;
    public string ModelText => string.Join(" / ", new[] { Data.Obj("generation").Str("model_id"),
        Data.Obj("generation").Long("seed") is long s ? L.F("Gen_DraftSeed", s) : null }.Where(x => !string.IsNullOrEmpty(x)));
    /// <summary>入力の記録（画像から作った装備: 枚数・画像の名前・複数画像のモード）。</summary>
    public string InputText
    {
        get
        {
            var g = Data.Obj("generation");
            // 見た目を作り直した装備（SF10）: 形を使った元の装備
            if (g.Obj("variant_of").Str("equipment_id") is string vb) return L.F("UA3_LibVariantOf", vb);
            if ((g.Str("method") ?? Data.Str("method")) != "image") return "";
            var labels = Data.Arr("input_images").Select(x => x.Str("label")).Where(x => !string.IsNullOrEmpty(x)).ToList();
            var n = g.Long("image_count") ?? Math.Max(1, Data.Arr("input_images").Count);
            var t = labels.Count > 0 ? L.F("Lib_InputImagesNamed", n, string.Join(L.T("Common_ListSepDot"), labels)) : L.F("Lib_InputImages", n);
            var mode = g.Str("multi_mode");
            if (n > 1 && mode != null)
                t += L.F("Lib_InputMultiMode", L.TryT(mode == "multidiffusion" ? "Gen_MultiDiffusion" : "Gen_MultiStochastic") ?? mode);
            return t;
        }
    }
    /// <summary>変換の前に 180° 回した装備（生成結果の正面が逆向きだったもの。docs/app_architecture.md 12 章 #16）。</summary>
    public string? RotateText => (Data.Long("yaw_deg") ?? 0) == 180 ? L.T("Lib_Rotated") : null;
    /// <summary>変換の前に回した角度（0 / 180。絶対の値）。</summary>
    public long YawDeg => Data.Long("yaw_deg") ?? 0;
    /// <summary>消耗品を自分で持つか（共有する装備の消耗品の文は、持ち主の装備で編集する。機能 SF11）。</summary>
    public bool OwnsSet => Data.Bool("owns_set", true);
    public string? SetOwner => Data.Str("set_owner");
    /// <summary>消耗品の名前（set）・説明文（set_desc）の上書き（無ければ null）。</summary>
    public string? ItemText(string role, string lang) => Data.Obj("item_texts").Obj(role).Str(lang);
    /// <summary>空欄のときに使う既定の文（バックエンドが modbuilder と同じ規則で作る）。</summary>
    public string? ItemTextDefault(string role, string lang) => Data.Obj("item_text_defaults").Obj(role).Str(lang);
    public bool HasItemTextOverride => new[] { "set", "set_desc" }.Any(r => ItemText(r, "en") != null || ItemText(r, "ja") != null);
    /// <summary>詳細の欄の 1 行: 消耗品の名前・説明文が既定か、変えてあるか。</summary>
    public string ItemTextsSummary => !OwnsSet ? L.F("LibC_TextsSummaryShared", SetOwner ?? "")
        : HasItemTextOverride ? L.F("LibC_TextsSummaryCustom", ItemText("set", "en") ?? ItemTextDefault("set", "en") ?? "")
        : L.T("LibC_TextsSummaryDefault");
    public IReadOnlyList<string> Warnings => QualityText.Describe(Data.Strings("conversion_warnings"));
    public string WarningsText => Warnings.Count == 0 ? L.T("Lib_NoWarnings") : string.Join(" / ", Warnings);
    public string VerticesText
    {
        get
        {
            var v = Data.Obj("vertices");
            return v == null ? "" : L.F("Lib_Vertices", v.Long("m"), v.Long("f"));
        }
    }

    private BitmapSource? _thumb;
    private bool _thumbTried;
    /// <summary>
    /// 変換の確認画像（5 列 × 4 行、1 枚 340×500）の「結果（バインド姿勢）」の部分。元の画像（約 13 MB）は持たず、
    /// その部分だけを高さ 140 px に縮めて読む（表示枠は 48×70 DIP なので 200% の画面まで足りる）。
    /// 1028×500 より小さい画像（切り出せない）は、全体を縮めて表示する。
    /// </summary>
    public BitmapSource? Thumbnail
    {
        get
        {
            if (_thumbTried) return _thumb;
            _thumbTried = true;
            var p = Files.Str("preview_png");
            if (p == null || !File.Exists(p)) return null;
            _thumb = ImageFiles.Load(p, decodeHeight: ThumbDecodeHeight, sourceRect: new Int32Rect(2 * (340 + 4), 0, 340, 500))
                     ?? ImageFiles.Load(p, decodeHeight: ThumbDecodeHeight);
            return _thumb;
        }
    }
    private const int ThumbDecodeHeight = 140;

    /// <summary>日時の表示。解釈できない値（受け取ったファイルの記録の書き換えなど）は 1 行・短く切って出す（R19B-01）。</summary>
    public static string FormatTime(string? iso) =>
        DateTimeOffset.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.None, out var t) ? t.ToLocalTime().ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture) : OneLine(iso, 40);

    /// <summary>
    /// 信用しない文（受け取った共有ファイルの記録の自由な文）を確認のダイアログに出す形にする: 制御文字・書式の文字・行や段落の
    /// 区切り（改行・U+202E など）を空白にし、max 文字を超えたら切って「…」を付ける（改行で偽の行を作る・数 MB の文で画面が
    /// 止まるのを防ぐ。R19B-01）。
    /// </summary>
    public static string OneLine(string? s, int max)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var cut = s.Length > max;
        var head = cut ? s[..(char.IsHighSurrogate(s[max - 1]) ? max - 1 : max)] : s;
        var chars = head.Select(c => char.GetUnicodeCategory(c) is UnicodeCategory.Control or UnicodeCategory.Format
                                     or UnicodeCategory.LineSeparator or UnicodeCategory.ParagraphSeparator ? ' ' : c).ToArray();
        return new string(chars).Trim() + (cut ? "…" : "");
    }
}

/// <summary>削除した装備（trash の 1 件。GET /library/trash の項目。機能 SF02）。</summary>
public sealed class TrashEntry
{
    public JsonNode Data { get; }
    /// <summary>表示（確認画像・名前・生成方法）は装備一覧と同じものを使う。</summary>
    public EquipmentItem Item { get; }
    public TrashEntry(JsonNode data) { Data = data; Item = new EquipmentItem(data); }

    public string TrashName => Data.Str("trash_name") ?? "";
    public bool Restorable => Data.Bool("restorable");
    public string Title => string.IsNullOrEmpty(Item.NameEn) ? TrashName : Item.DisplayName;
    public string InfoText => L.F("LibC_TrashInfo", string.IsNullOrEmpty(Item.Id) ? "?" : Item.Id,
                                  EquipmentItem.FormatTime(Data.Str("removed_at")), Item.MethodText);
    /// <summary>戻せない理由（戻せるなら null）。</summary>
    public string? ProblemsText => Restorable ? null : L.F("LibC_TrashCannot", string.Join("\n", Data.Strings("problems")));
}

/// <summary>装備一覧（作成・再編集・削除・再生成・MOD の再出力。計画書 10 章）。</summary>
public sealed class LibraryViewModel : ObservableObject
{
    private readonly MainViewModel _main;
    public ObservableCollection<EquipmentItem> Items { get; } = new();

    private EquipmentItem? _selected;
    public EquipmentItem? Selected
    {
        get => _selected;
        set
        {
            if (Set(ref _selected, value))
            {
                EditNameEn = value?.NameEn ?? "";
                EditNameJa = value?.NameJa ?? "";
                PreviewItems = GenerateViewModel.BuildPreviewItems(value?.Files);
                OnPropertyChanged(nameof(HasSelection));
                OnPropertyChanged(nameof(VariantHint));
            }
        }
    }
    public bool HasSelection => Selected != null;

    // --- 文章で見た目を作り直す（SF10。2026-10-17 U-A3。生成の詳細設定でオンにしたときだけ出す） ------------------------------
    /// <summary>「文章で見た目を作り直す」を出すか（生成の詳細設定。既定オフ）。</summary>
    public bool VariantEnabled => App.Ui.TextVariantEnabled;
    private bool _variantInstalled;
    /// <summary>テキストの任意部品（TRELLIS-text-xlarge と CLIP）があり、生成スクリプトが対応している（同意は生成の画面で確かめる）。</summary>
    public bool VariantInstalled { get => _variantInstalled; private set { if (Set(ref _variantInstalled, value)) OnPropertyChanged(nameof(VariantHint)); } }
    private bool _variantConsented;
    public string VariantHint => !VariantInstalled ? L.T("UA3_LibNotInstalled")
        : Selected?.HasGeneratedGlb != true ? L.T("UA3_LibNoGlb")
        : _variantConsented ? L.T("UA3_LibHint") : L.T("UA3_LibHintConsent");
    public ICommand VariantCommand { get; private set; } = null!;

    /// <summary>生成の詳細設定で「文章で見た目を作り直す」を切り替えた。</summary>
    public void OnVariantSettingChanged()
    {
        OnPropertyChanged(nameof(VariantEnabled));
        OnPropertyChanged(nameof(VariantHint));
        CommandManager.InvalidateRequerySuggested();
        if (VariantEnabled && _main.Client != null) _ = RefreshVariantAvailabilityAsync();
    }

    /// <summary>テキストの任意部品の有無（GET /providers の checks.text。同意 clip_consent 以外がすべて使える状態か）。</summary>
    private async Task RefreshVariantAvailabilityAsync()
    {
        try
        {
            var provs = await _main.RequireClient().GetAsync("providers");
            var t = (provs as JsonArray ?? new JsonArray()).FirstOrDefault(p => p.Str("id") == "trellis");
            var checks = t.Obj("checks").Arr("text");
            VariantInstalled = t.Bool("text_variant") && checks.Count > 0
                               && checks.Where(c => c.Str("id") != "clip_consent").All(c => c.Str("status") is "ok" or "warning");
            _variantConsented = checks.Any(c => c.Str("id") == "clip_consent" && c.Str("status") == "ok");
        }
        catch (ApiException e)
        {
            AppLog.Warn(L.F("Log_Lib_TextPartsCheckFailed", e.Message));
            VariantInstalled = false;
        }
        OnPropertyChanged(nameof(VariantHint));
        CommandManager.InvalidateRequerySuggested();
    }

    private void StartVariant()
    {
        if (Selected == null) return;
        _main.Generate.LoadForVariant(Selected);
        _main.Navigate("generate");
    }

    private string _editNameEn = "";
    public string EditNameEn { get => _editNameEn; set { if (Set(ref _editNameEn, value)) CommandManager.InvalidateRequerySuggested(); } }
    private string _editNameJa = "";
    public string EditNameJa { get => _editNameJa; set { if (Set(ref _editNameJa, value)) CommandManager.InvalidateRequerySuggested(); } }
    /// <summary>名前の欄が今の名前と違うか。同じ名前で保存すると、MOD の内容は変わらないのに「MOD 未作成」に戻るので押せなくする（R19A-04）。</summary>
    private bool NameEdited => Selected != null
                               && (EditNameEn.Trim() != Selected.NameEn || EditNameJa.Trim() != (Selected.NameJa ?? ""));
    private IReadOnlyList<PreviewItem> _previewItems = [];
    public IReadOnlyList<PreviewItem> PreviewItems { get => _previewItems; private set => Set(ref _previewItems, value); }
    private string _summary = "";
    public string Summary { get => _summary; private set => Set(ref _summary, value); }
    public bool IsEmpty => Items.Count == 0;

    public ICommand RefreshCommand { get; }
    public ICommand RenameCommand { get; }
    public ICommand DeleteCommand { get; }
    public ICommand RegenerateCommand { get; }
    public ICommand ReconvertCommand { get; }
    public ICommand NewCommand { get; }
    public ICommand RebuildModCommand { get; }
    public ICommand OpenFolderCommand { get; }
    public ICommand ImportExternalCommand { get; }
    public ICommand ExportPackageCommand { get; }
    public ICommand ImportPackageCommand { get; }
    /// <summary>削除した装備（trash）の一覧を開き、選んだものを元に戻す（機能 SF02）。</summary>
    public ICommand TrashCommand { get; }
    /// <summary>消耗品の名前・説明文の編集（機能 SF11）。</summary>
    public ICommand EditItemTextsCommand { get; }

    // --- 複数選択と、装備パッケージ（.gdskin）の書き出し・読み込み（docs/app_architecture.md 15.1 節） -------------------
    private IReadOnlyList<EquipmentItem> _multi = [];
    /// <summary>書き出す装備（一覧で選んだもの。Ctrl・Shift で複数）。</summary>
    public IReadOnlyList<EquipmentItem> ExportTargets => _multi.Count > 0 ? _multi : (Selected != null ? [Selected] : []);
    public string? SelectionText => _multi.Count > 1 ? L.F("Lib_SelectedCount", _multi.Count) : null;
    public void SetMultiSelection(IReadOnlyList<EquipmentItem> items)
    {
        _multi = items;
        OnPropertyChanged(nameof(SelectionText));
        CommandManager.InvalidateRequerySuggested();
    }

    private bool _isBusy;
    public bool IsBusy { get => _isBusy; private set { if (Set(ref _isBusy, value)) CommandManager.InvalidateRequerySuggested(); } }
    private double _progress;
    public double Progress { get => _progress; private set => Set(ref _progress, value); }
    private string _busyText = "";
    public string BusyText { get => _busyText; private set => Set(ref _busyText, value); }

    // --- 別のアプリの仮想フォルダーにある装備（docs/app_architecture.md 12 章 #15） ---------------------------------
    private JsonNode? _datastores;
    public int ExternalMissing => _datastores.Arr("stores").Sum(s => s.Arr("missing").Count);
    public bool HasExternal => ExternalMissing > 0;
    public string ExternalNotice => L.F("Lib_ExternalNotice", ExternalMissing);
    public bool IsVirtualized => _datastores.Str("virtualized_into") != null;
    public string VirtualizedNotice => L.T("Lib_VirtualizedNotice");

    public LibraryViewModel(MainViewModel main)
    {
        _main = main;
        RefreshCommand = new AsyncCommand(() => RefreshAsync(), () => _main.IsReady);
        RenameCommand = new AsyncCommand(RenameAsync, () => _main.IsReady && Selected != null && NameEdited);
        DeleteCommand = new AsyncCommand(DeleteAsync, () => _main.IsReady && Selected != null);
        RegenerateCommand = new RelayCommand(Regenerate, () => Selected != null && !_main.Generate.IsBusy);
        ReconvertCommand = new AsyncCommand(ReconvertAsync, () => _main.IsReady && !IsBusy && Selected?.HasGeneratedGlb == true && !_main.Generate.IsBusy);
        NewCommand = new RelayCommand(() => { _main.Generate.ResetForNew(); _main.Navigate("generate"); });
        RebuildModCommand = new AsyncCommand(async () => { _main.Navigate("mod"); await _main.Mod.BuildAsync(); },
            () => _main.IsReady && Items.Count > 0);
        OpenFolderCommand = new RelayCommand(() => { if (Selected?.Data.Str("dir") is string d) Dialogs.OpenFolder(d); }, () => Selected != null);
        ImportExternalCommand = new AsyncCommand(ImportExternalAsync, () => _main.IsReady && HasExternal);
        ExportPackageCommand = new AsyncCommand(ExportPackageAsync, () => _main.IsReady && !IsBusy && ExportTargets.Count > 0);
        ImportPackageCommand = new AsyncCommand(() => ImportPackageAsync(), () => _main.IsReady && !IsBusy);
        TrashCommand = new AsyncCommand(ShowTrashAsync, () => _main.IsReady && !IsBusy);
        EditItemTextsCommand = new AsyncCommand(EditItemTextsAsync, () => _main.IsReady && !IsBusy && Selected?.OwnsSet == true);
        VariantCommand = new RelayCommand(StartVariant, () => _main.IsReady && VariantEnabled && VariantInstalled
                                                             && Selected?.HasGeneratedGlb == true && !_main.Generate.IsBusy);
    }

    /// <summary>
    /// 保存は止めない注意（英語の名前・文に ASCII の外の文字がある、など。応答の warnings）を表示し、ログにも残す
    /// （2026-10-17 点検 C75。生成の画面の保存と同じ形）。
    /// </summary>
    private static void ShowSaveWarnings(JsonNode? r, string titleKey, string messageKey, string hintKey)
    {
        var warnings = r.Strings("warnings").Where(w => !string.IsNullOrWhiteSpace(w)).ToList();
        if (warnings.Count == 0) return;
        foreach (var w in warnings) AppLog.Warn(L.F("Log_Lib_TextWarning", w));
        Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Warning, Title = L.T(titleKey), Message = L.T(messageKey), Hint = L.T(hintKey),
            Sections = [new DialogSection { Header = L.T("Gen_NameWarningHeader"), Items = warnings,
                                            Brush = (System.Windows.Media.Brush)Application.Current.Resources["WarnBrush"] }],
        });
    }

    /// <summary>削除した装備の一覧 → 選んだものを同じ内部ID・レコードのまま装備一覧に戻す（MOD は作り直しが必要になる）。</summary>
    private async Task ShowTrashAsync()
    {
        var arr = await _main.RequireClient().GetAsync("library/trash");
        var entries = (arr as JsonArray ?? new JsonArray()).Where(n => n != null).Select(n => new TrashEntry(n!.Clone()!)).ToList();
        var pick = TrashDialog.Pick(entries);
        if (pick == null) return;
        var r = await _main.RequireClient().PostAsync("library/trash/restore", new JsonObject { ["name"] = pick.TrashName });
        var id = r.Str("id");
        AppLog.Info(L.F("Log_Lib_TrashRestored", id));
        await RefreshAsync(id);
        _main.Mod.MarkLibraryChanged();
        Dialogs.Info(L.T("LibC_RestoreDoneTitle"), L.F("LibC_RestoreDone", pick.Title, id), L.T("LibC_RestoreDoneHint"));
    }

    /// <summary>消耗品の名前・説明文（英語・日本語）を編集する。空欄の欄は既定の文を使う（ビルドは今と同じ）。</summary>
    private async Task EditItemTextsAsync()
    {
        var item = Selected;
        if (item == null || !item.OwnsSet) return;
        JsonNode? result = null;
        async Task Save(IReadOnlyDictionary<(string Role, string Lang), string> values)
        {
            var texts = new JsonObject();
            foreach (var role in new[] { "set", "set_desc" })
            {
                var o = new JsonObject();
                foreach (var lang in new[] { "en", "ja" })
                {
                    var v = values.TryGetValue((role, lang), out var s) ? s.Trim() : "";
                    o[lang] = v.Length == 0 ? null : JsonValue.Create(v);      // 空欄 → 上書きを外す（既定の文）
                }
                texts[role] = o;
            }
            result = await _main.RequireClient().PutAsync($"library/{item.Id}/texts", new JsonObject { ["texts"] = texts });
        }
        ItemTextsDialog.Edit(item, Save);
        if (result == null) return;                // 保存していない（キャンセル・失敗）。保存できたらダイアログの閉じ方によらず一覧を更新する（U-C-R3）
        var changed = result.Strings("changed").ToList();
        AppLog.Info(changed.Count == 0 ? L.F("Log_Lib_ItemTextsSavedNone", item.Id)
                                       : L.F("Log_Lib_ItemTextsSaved", item.Id, string.Join(", ", changed)));
        await RefreshAsync(item.Id);
        if (changed.Count > 0) _main.Mod.MarkLibraryChanged();
        ShowSaveWarnings(result, "LibC_TextsWarningTitle", "LibC_TextsWarningMessage", "LibC_TextsWarningHint");
    }

    private async Task<JsonNode?> RunAsync(string type, JsonObject p)
    {
        IsBusy = true;
        Progress = 0;
        BusyText = L.T("Stage_" + type + "_start");
        try
        {
            return await _main.RequireClient().RunJobAsync(type, p, null,
                new Progress<JobProgress>(x => { Progress = x.Progress * 100; BusyText = StageNames.Describe(x.Stage, x.Detail); }));
        }
        finally { IsBusy = false; }
    }

    private static string SafeFileName(string s)
    {
        var bad = Path.GetInvalidFileNameChars();
        var t = new string(s.Select(c => bad.Contains(c) ? '_' : c).ToArray()).Trim();
        return string.IsNullOrEmpty(t) ? "equipment" : (t.Length > 60 ? t[..60] : t);
    }

    private async Task ExportPackageAsync()
    {
        var items = ExportTargets.ToList();
        if (items.Count == 0) return;
        var withInputs = items.Any(i => i.Data.Obj("generation").Str("input_text") != null || i.Data.Bool("has_input_image"));
        var r = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Question, Title = L.T("Pkg_ExportTitle"), Message = L.F("Pkg_ExportMessage", items.Count),
            Sections = [new DialogSection { Header = L.F("Pkg_ExportItems", items.Count), Items = items.Select(i => $"{i.DisplayName}  {i.Id}").ToList() },
                        new DialogSection { Header = L.T("Pkg_ExportContents"), Items = [L.T("Pkg_ExportContent1"), L.T("Pkg_ExportContent2"), L.T("Pkg_ExportContent3")] }],
            Hint = L.T("Pkg_ExportHint"),
            CheckText = withInputs ? L.T("Pkg_IncludeInputs") : null, CheckDefault = false,
            PrimaryText = L.T("Pkg_ExportNext"), SecondaryText = L.T("Dialog_Cancel"),
        });
        if (!r.Accepted) return;
        var name = items.Count == 1 ? SafeFileName(items[0].NameEn) : L.F("Pkg_DefaultName", items.Count);
        var d = new SaveFileDialog
        {
            Title = L.T("Pkg_ExportPick"), Filter = L.T("Pkg_FilterEquipment"), DefaultExt = ".gdskin", AddExtension = true,
            FileName = name + ".gdskin", OverwritePrompt = true,
        };
        if (d.ShowDialog() != true) return;
        var ids = new JsonArray(items.Select(i => (JsonNode)JsonValue.Create(i.Id)!).ToArray());
        var res = await RunAsync("export_equipment", new JsonObject
        {
            ["ids"] = ids, ["dest"] = d.FileName, ["include_inputs"] = withInputs && r.Checked, ["overwrite"] = true,
        });
        AppLog.Info(L.F("Log_Lib_PackageExported", items.Count, res.Str("package_id")));
        Dialogs.Show(new DialogSpec
        {
            Title = L.T("Pkg_ExportDoneTitle"), Message = L.F("Pkg_ExportDone", items.Count),
            Sections = [new DialogSection { Header = L.T("Pkg_File"), Items = [AppLog.Redact(res.Str("dest") ?? ""),
                L.F("Pkg_SizeAndId", (res.Long("size") ?? 0) / 1024.0 / 1024.0, res.Str("package_id"))] }],
            Hint = res.Bool("include_inputs") ? L.T("Pkg_ExportDoneHintInputs") : L.T("Pkg_ExportDoneHint"),
        });
    }

    /// <summary>
    /// 「内容が違う」装備を置き換えたときの、手元にしか無いもの（生成結果・プロンプト・入力画像）の扱い（2026-10-17 監査 C95）。
    /// 3D モデル・テクスチャが同じなら引き継ぎ、違えば失われる（元の装備は trash に残る）。手元に何も無ければ空。
    /// </summary>
    private static string LocalOnlyText(JsonNode? item)
    {
        var lo = item.Obj("local_only");
        var parts = new List<string>();
        if (lo.Bool("generated_glb")) parts.Add(L.T("Pkg_Local_generated_glb"));
        if (lo.Bool("prompt")) parts.Add(L.T("Pkg_Local_prompt"));
        if ((lo.Long("input_images") ?? 0) > 0) parts.Add(L.F("Pkg_Local_input_images", lo.Long("input_images") ?? 0));
        if (parts.Count == 0) return "";
        if (item.Bool("keeps_local")) return "\n" + L.F("Pkg_LocalKept", string.Join(L.T("Common_ListSepDot"), parts));
        return "\n" + L.F("Pkg_LocalLost", string.Join(L.T("Common_ListSepDot"), parts))
               + (lo.Bool("generated_glb") ? "\n" + L.T("Pkg_LocalLostGlb") : "");
    }

    /// <summary>装備パッケージを読み込む（確認 → 同じ内部IDの扱いを選ぶ → 取り込み）。path を渡すとファイルの選択を省く。</summary>
    public async Task ImportPackageAsync(string? path = null)
    {
        if (path == null)
        {
            var d = new OpenFileDialog { Title = L.T("Pkg_ImportPick"), Filter = L.T("Pkg_FilterEquipment"), CheckFileExists = true };
            if (d.ShowDialog() != true) return;
            path = d.FileName;
        }
        IsBusy = true;
        BusyText = L.T("Pkg_Checking");
        JsonNode? info;
        try { info = await _main.RequireClient().PostAsync("packages/inspect", new JsonObject { ["path"] = path, ["expect"] = "equipment" }); }
        finally { IsBusy = false; }
        // 開発用・不正の項目は記録の検査を通らないので、名前・ID は 1 行・短く切って出す（R19B-01）
        string Line(JsonNode? i) => (i.Str("name_ja") is string ja
                                        ? L.F("Common_ParenFormat", EquipmentItem.OneLine(i.Str("name"), 80), EquipmentItem.OneLine(ja, 80))
                                        : EquipmentItem.OneLine(i.Str("name"), 80))
                                    + $"  {EquipmentItem.OneLine(i.Str("id"), 64)}";
        var items = info.Arr("items");
        List<string> Of(string status) => items.Where(i => i.Str("status") == status).Select(Line).ToList();
        var news = Of("new");
        var same = Of("same");
        var differ = items.Where(i => i.Str("status") == "different").ToList();
        var bad = items.Where(i => i.Str("status") is "invalid" or "conflict" or "dev_only")
            .Select(i => L.F("Common_ColonFormat", Line(i), L.T("Pkg_Status_" + i.Str("status")))).ToList();
        if (info.Strings("blocking").Any())
        {
            Dialogs.Show(new DialogSpec
            {
                Kind = DialogKind.Error, Title = L.T("Pkg_ImportBlockedTitle"), Message = L.T("Pkg_ImportBlocked"),
                Sections = [new DialogSection { Header = L.T("Pkg_ImportProblems"), Items = info.Strings("blocking").Take(20).ToList(),
                            Brush = (System.Windows.Media.Brush)Application.Current.Resources["ErrBrush"] }],
                Hint = L.T("Pkg_ImportBlockedHint"),
            });
            return;
        }
        if (news.Count == 0 && differ.Count == 0)
        {
            Dialogs.Info(L.T("Pkg_ImportNothingTitle"), L.F("Pkg_ImportNothing", same.Count), bad.Count > 0 ? string.Join("\n", bad) : null);
            return;
        }
        var tool = info.Obj("tool");
        string Tool(string key) => EquipmentItem.OneLine(tool.Str(key) ?? "?", 64);     // 作った人が書き換えられる文（R19B-01）
        var about = new List<string>
        {
            L.F("Pkg_About", info.Str("file_name"), EquipmentItem.FormatTime(info.Str("created_at")), info.Str("package_id")),
            L.F("Pkg_AboutTool", Tool("app"), Tool("backend"), Tool("modbuilder")),
            info.Bool("include_inputs") ? L.T("Pkg_AboutInputs") : L.T("Pkg_AboutNoInputs"),
        };
        // 展開後の大きさ（取り込みで書かれる量の目安。R12B-06）
        if (info.Long("extracted_size") is long ext) about.Add(L.F("Pkg_AboutExtracted", SetupViewModel.Fmt(ext)));
        var sections = new List<DialogSection> { new() { Header = L.T("Pkg_AboutHeader"), Items = about } };
        if (news.Count > 0) sections.Add(new() { Header = L.F("Pkg_New", news.Count), Items = news });
        if (differ.Count > 0)
            sections.Add(new()
            {
                Header = L.F("Pkg_Different", differ.Count),
                Items = differ.Select(i => Line(i) + "\n" + L.F("Pkg_DifferentDetail",
                    string.Join(L.T("Common_ListSepDot"), i.Strings("differences").Select(k => L.TryT("Pkg_Diff_" + k) ?? k)),
                    i.Obj("local").Str("name") ?? "") + LocalOnlyText(i)).ToList(),
                Brush = (System.Windows.Media.Brush)Application.Current.Resources["WarnBrush"],
            });
        if (same.Count > 0) sections.Add(new() { Header = L.F("Pkg_Same", same.Count), Items = same });
        if (bad.Count > 0) sections.Add(new() { Header = L.F("Pkg_Skipped", bad.Count), Items = bad });
        var r = Dialogs.Show(new DialogSpec
        {
            Kind = differ.Count > 0 ? DialogKind.Warning : DialogKind.Question, Title = L.T("Pkg_ImportTitle"),
            Message = L.F("Pkg_ImportMessage", news.Count, differ.Count, same.Count), Sections = sections,
            Hint = L.T("Pkg_ImportHint"),
            CheckText = differ.Count > 0 ? L.F("Pkg_ReplaceCheck", differ.Count) : null, CheckDefault = false,
            PrimaryText = L.T("Pkg_ImportDo"), SecondaryText = L.T("Dialog_Cancel"),
        });
        if (!r.Accepted) return;
        var replace = r.Checked ? new JsonArray(differ.Select(i => (JsonNode)JsonValue.Create(i.Str("id"))!).ToArray()) : new JsonArray();
        var res = await RunAsync("import_equipment", new JsonObject
        {
            ["path"] = path, ["package_id"] = info.Str("package_id"), ["replace"] = replace,
        });
        int nNew = res.Arr("imported").Count, nRep = res.Arr("replaced").Count;
        AppLog.Info(L.F("Log_Lib_PackageImported", nNew, nRep, info.Str("package_id")));
        await RefreshAsync(res.Strings("imported").Concat(res.Strings("replaced")).FirstOrDefault());
        _main.Mod.MarkLibraryChanged();
        Dialogs.Info(L.T("Pkg_ImportDoneTitle"), L.F("Pkg_ImportDone", nNew, nRep, res.Arr("skipped").Count), L.T("Pkg_ImportDoneHint"));
    }

    /// <summary>装備一覧を読み直す。keepEdits が true なら、名前の欄に入力中（未保存）の名前を、同じ装備を選び直したときに戻す
    /// （名前を変更した後は保存した名前を出すので false）。</summary>
    public async Task RefreshAsync(string? selectId = null, bool keepEdits = true)
    {
        var c = _main.Client;
        if (c == null) return;
        var keep = selectId ?? Selected?.Id;
        var arr = (await c.GetAsync("library"))!.AsArray();
        // 一覧を作り直すと Selected が新しいオブジェクトになり、名前の欄が保存済みの名前に戻って、MOD の画面の導入の後などの更新で
        // 入力中の名前が黙って消えた。Items.Clear() で一覧の選択が先に Selected = null を書くので、setter では残せない（R19B-04）
        (string Id, string En, string Ja)? edit = keepEdits && NameEdited ? (Selected!.Id, EditNameEn, EditNameJa) : null;
        Items.Clear();
        foreach (var n in arr) Items.Add(new EquipmentItem(n!.Clone()!));
        Selected = Items.FirstOrDefault(i => i.Id == keep) ?? Items.FirstOrDefault();
        if (edit is { } e && Selected?.Id == e.Id)
        {
            EditNameEn = e.En;
            EditNameJa = e.Ja;
        }
        Summary = L.F("Lib_Summary", Items.Count, Items.Count(i => i.ModStatus == "installed"));
        OnPropertyChanged(nameof(IsEmpty));
        try { _datastores = await c.GetAsync("datastores"); } catch (ApiException) { _datastores = null; }
        foreach (var n in new[] { nameof(ExternalMissing), nameof(HasExternal), nameof(ExternalNotice), nameof(IsVirtualized) })
            OnPropertyChanged(n);
        if (VariantEnabled) await RefreshVariantAvailabilityAsync();      // SF10（出すときだけ問い合わせる）
    }

    private async Task RenameAsync()
    {
        if (Selected == null || !NameEdited) return;
        var body = new JsonObject { ["name_en"] = EditNameEn.Trim(), ["name_ja"] = EditNameJa.Trim() };
        var r = await _main.RequireClient().PatchAsync($"library/{Selected.Id}", body);
        await RefreshAsync(Selected.Id, keepEdits: false);
        _main.Mod.MarkLibraryChanged();
        // 保存は止めない注意（英語名に ASCII の外の文字がある、など。応答の warnings。点検 C75 の残り）
        ShowSaveWarnings(r, "Gen_NameWarningTitle", "LibC_RenameWarningMessage", "LibC_RenameWarningHint");
    }

    private async Task DeleteAsync()
    {
        // 押したときの装備を使う。導入中の MOD に入っているかを問い合わせる間も一覧の選択は変えられるので、Selected を読み直すと
        // 別の装備の判定の注意で、今選んでいる装備を消した（R19A-06）
        var item = Selected;
        if (item == null) return;
        var installed = await InInstalledModAsync(item);
        var ok = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Warning, Title = L.T("Lib_DeleteTitle"),
            Message = L.F("Lib_DeleteMessage", item.DisplayName, item.Id),
            Hint = installed ? L.T("Lib_DeleteInstalledHint") : L.T("Lib_DeleteHint"),
            PrimaryText = L.T("Lib_Delete"), SecondaryText = L.T("Dialog_Cancel"),
        }).Accepted;
        if (!ok) return;
        await _main.RequireClient().DeleteAsync($"library/{item.Id}");
        AppLog.Info(L.F("Log_Lib_Removed", item.Id));
        await RefreshAsync();
        _main.Mod.MarkLibraryChanged();
    }

    /// <summary>
    /// 装備が導入中の MOD に入っているか（削除の確認の注意に使う）。名前・文の変更や変換し直しで状態が「MOD 未作成」に戻っても、
    /// 導入中の MOD には入ったまま（R19B-04）。installed_build_id はアンインストールの後も残ることがあるので、導入の記録
    /// （mod/status の installed.equipment）で確かめる。確かめられなければ、ゲームに残る注意を出す側に倒す。
    /// </summary>
    private async Task<bool> InInstalledModAsync(EquipmentItem item)
    {
        if (item.ModStatus == "installed") return true;
        if (item.Data.Obj("mod_detail").Str("installed_build_id") == null) return false;      // 一度も導入していない
        try
        {
            var st = await _main.RequireClient().GetAsync("mod/status");
            // 導入先が見つからないとき（外付けのドライブを外したなど）は導入の記録を読めず installed が無い。確かめられないので
            // 注意を出す側に倒す（R19B-02）
            if (!st.Bool("target_ok")) return true;
            return st.Obj("installed").Strings("equipment").Contains(item.Id);
        }
        catch (ApiException e)
        {
            AppLog.Warn(L.F("Log_Lib_InstalledCheckFailed", e.Message));
            return true;
        }
    }

    private async Task ImportExternalAsync()
    {
        var names = _datastores.Arr("stores").SelectMany(s => s.Arr("missing"))
            .Select(m => (m.Str("name_ja") is string ja ? L.F("Common_ParenFormat", m.Str("name"), ja) : $"{m.Str("name")}")
                         + $"  {m.Str("id")}").ToList();
        var where = _datastores.Arr("stores").Where(s => s.Arr("missing").Count > 0).Select(s => AppLog.Redact(s.Str("path") ?? "")).ToList();
        var ok = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Question, Title = L.T("Lib_ImportTitle"), Message = L.F("Lib_ImportMessage", names.Count),
            Sections = [new DialogSection { Header = L.T("Lib_ImportItems"), Items = names },
                        new DialogSection { Header = L.T("Lib_ImportFrom"), Items = where }],
            Hint = L.T("Lib_ImportHint"), PrimaryText = L.T("Lib_ImportDo"), SecondaryText = L.T("Dialog_Cancel"),
        }).Accepted;
        if (!ok) return;
        var r = await _main.RequireClient().RunJobAsync("import_external", new JsonObject(), null, null);
        AppLog.Info(L.F("Log_Lib_ExternalImported", r.Arr("imported").Count));
        await RefreshAsync();
        await _main.RefreshEnvironmentAsync(true);
        _main.Mod.MarkLibraryChanged();
        Dialogs.Info(L.T("Lib_ImportDoneTitle"), L.F("Lib_ImportDone", r.Arr("imported").Count), L.T("Lib_ImportDoneHint"));
    }

    /// <summary>
    /// 「変換し直す」: 保存してある生成結果から、今の変換（gdconvert）で作り直す（3D 生成はやり直さない。B-001 の修正などを保存済みの装備に当てる）。
    /// 内部ID・レコードのパス・タグ・表示名はそのまま。ゲームに反映するには MOD を作成し直して導入する。
    /// </summary>
    private async Task ReconvertAsync()
    {
        var item = Selected;
        if (item == null) return;
        var rotated = item.YawDeg == 180;
        var answer = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Question, Title = L.T("Lib_ReconvertTitle"), Message = L.F("Lib_ReconvertMessage", item.DisplayName),
            Sections = [new DialogSection { Header = L.T("Lib_ReconvertKeepHeader"), Items = [L.T("Lib_ReconvertKeep1"), L.T("Lib_ReconvertKeep2")] },
                        new DialogSection { Header = L.T("Lib_ReconvertChangeHeader"), Items = [L.T("Lib_ReconvertChange1"), L.T("Lib_ReconvertChange2")] }],
            Hint = rotated ? L.T("Lib_ReconvertHintRotated") : L.T("Lib_ReconvertHint"),
            // 正面の向きを変えて変換し直す（機能 SF01。後ろ向きに気づいた装備を、3D 生成をやり直さずに直す）
            CheckText = rotated ? L.T("LibC_ReconvertRotateBack") : L.T("LibC_ReconvertRotate180"), CheckDefault = false,
            PrimaryText = L.T("Lib_ReconvertDo"), SecondaryText = L.T("Dialog_Cancel"),
        });
        if (!answer.Accepted) return;
        // yaw_deg は前回からの相対ではなく、変換の前に回す絶対の角度。回すときは今の向きの逆（0⇔180）を送る
        // （毎回 180 を送ると、2 回目に元へ戻らない）。送らなければ保存時と同じ向きで変換し直す
        int? yaw = answer.Checked ? (rotated ? 0 : 180) : null;
        var jobParams = new JsonObject { ["equipment_id"] = item.Id };
        if (yaw != null) jobParams["yaw_deg"] = yaw.Value;
        AppLog.Info(yaw != null ? L.F("Log_Lib_ReconvertYaw", item.Id, item.YawDeg, yaw) : L.F("Log_Lib_Reconvert", item.Id));
        JsonNode? r;
        try { r = await RunAsync("reconvert_equipment", jobParams); }
        catch (OperationCanceledException) { return; }
        catch (ApiException e) when (e.Code == "REFS_MISSING")
        {
            await _main.Setup.OfferRefsAsync(e);        // B-002: 基準メッシュの準備に案内する
            return;
        }
        await RefreshAsync(item.Id);
        _main.Mod.MarkLibraryChanged();
        var after = QualityText.Describe(r.Obj("equipment").Strings("conversion_warnings"));
        var before = QualityText.Describe(r.Strings("previous_warnings"));
        var wasBuilt = r.Str("mod_status_before") is "built" or "installed";
        AppLog.Info(L.F("Log_Lib_Reconverted", item.Id, before.Count, after.Count));
        var done = Dialogs.Show(new DialogSpec
        {
            Kind = after.Count == 0 ? DialogKind.Info : DialogKind.Warning, Title = L.T("Lib_ReconvertDoneTitle"),
            Message = L.F("Lib_ReconvertDone", item.DisplayName) + yaw switch
            {
                180 => "\n" + L.T("LibC_ReconvertDoneRotated"),
                0 => "\n" + L.T("LibC_ReconvertDoneRotatedBack"),
                _ => "",
            },
            Sections = [new DialogSection { Header = L.T("Lib_ReconvertAfter"), Items = after.Count == 0 ? [L.T("Lib_NoWarnings")] : after.ToList() },
                        new DialogSection { Header = L.T("Lib_ReconvertBefore"), Items = before.Count == 0 ? [L.T("Lib_NoWarnings")] : before.ToList() }],
            Hint = wasBuilt ? L.T("Lib_ReconvertRebuildHint") : L.T("Lib_ReconvertBuildHint"),
            PrimaryText = L.T("Lib_ReconvertRebuild"), SecondaryText = L.T("Dialog_Close"),
        });
        if (done.Accepted) RebuildModCommand.Execute(null);
    }

    private void Regenerate()
    {
        if (Selected == null) return;
        _main.Generate.LoadForRegeneration(Selected);
        _main.Navigate("generate");
    }
}
