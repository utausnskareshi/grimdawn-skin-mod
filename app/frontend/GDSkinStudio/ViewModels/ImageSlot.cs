using System.Globalization;
using System.IO;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using Microsoft.Win32;

namespace GDSkinStudio.ViewModels;

/// <summary>
/// 画像入力の 1 枚（複数画像のときは 正面・背面・左側面・右側面 など）。名前（ラベル）・パス・プレビュー・透明部分の有無・
/// 背景除去後のプレビュー（D-020）を持つ。AutomationId は「Gen_ImagePath_1」のように番号つき。
/// </summary>
public sealed class ImageSlot : ObservableObject
{
    /// <summary>透明部分はあるが、ほぼ全体が不透明とみなす割合（不透明な画素 alpha ≥ 205 の割合。バックエンドの
    /// pipeline.NEARLY_OPAQUE_COVERAGE と同じ）。TRELLIS は背景を除かないので、背景ごと 3D になる（点検 C09）。</summary>
    public const double NearlyOpaqueCoverage = 0.95;
    /// <summary>プレビューを復号する幅（表示枠は高さ 190 px。縮めて 1 回だけ復号する。点検 C52）。</summary>
    public const int PreviewDecodeWidth = 1024;

    private readonly GenerateViewModel _owner;

    public ImageSlot(GenerateViewModel owner, int index, string label)
    {
        _owner = owner;
        Index = index;
        _label = label;
        // ネットワーク上のパスの存在は UI スレッドの外で確かめる。確かめ直して見つかったら（スリープから戻った NAS など）読み込む（点検 R18B-04）
        _pathCheck = new PathExistence(_owner.OnSlotChanged, () => { if (_probe == null) LoadPreview(); });
        BrowseCommand = new RelayCommand(Browse);
        // 透明部分の有無が不明（WebP でバックエンドの判定待ち）なら押せる。結果で HasAlpha を更新する（点検 C44）。
        // 背景除去を強制した画像（SF19）は背景を除くので、モデルが要る
        CheckBackgroundCommand = new AsyncCommand(CheckBackgroundAsync,
            () => _owner.BackendReady && !BgBusy && PathExists
                  && (_owner.BgAvailable || (_probe != null && HasAlpha != false && !ForceBgRemoval)));
    }

    private readonly PathExistence _pathCheck;
    /// <summary>画像のファイルがあるか（コマンドの確かめ直しで UI スレッドから呼ぶ。ネットワーク上のパスは確かめた結果。点検 R18B-04）。</summary>
    public bool PathExists => _pathCheck.Exists(Path);

    public int Index { get; }
    public ICommand BrowseCommand { get; }
    public ICommand CheckBackgroundCommand { get; }

    private string _label;
    public string Label { get => _label; set => Set(ref _label, value ?? ""); }

    private string _path = "";
    public string Path
    {
        get => _path;
        set
        {
            if (Set(ref _path, value?.Trim().Trim('"') ?? ""))
            {
                SetProcessed(null, "");
                _checkedSubject = null;
                ForceBgRemoval = false;             // 画像ごとの指定（SF19）。別の画像では外す
                LoadPreview();
                _owner.OnSlotChanged();
            }
        }
    }

    // --- この画像の詳細設定（2026-10-17 追加機能 SF19。既定オフ） --------------------------------------------
    private bool _forceBgRemoval;
    /// <summary>
    /// 背景除去を強制する: 透明部分のある画像でも alpha を使わず、透明部分を白で塗りつぶしてから背景を除く（バックエンドの
    /// pipeline.copy_image の flatten_alpha）。ほぼ全体が不透明な画像（点検 C09）向け。画像を変えると外す。
    /// </summary>
    public bool ForceBgRemoval
    {
        get => _forceBgRemoval;
        set
        {
            if (!Set(ref _forceBgRemoval, value)) return;
            if (!_fromGeneration) SetProcessed(null, "");     // 「背景除去を確認」の結果は前の指定のもの
            _checkedSubject = null;
            OnPropertyChanged(nameof(SlotAdvancedHeader));
            NotifyAlpha();
            UpdateInfo();
        }
    }
    /// <summary>背景除去の強制を指定できる（透明部分のある画像。オンのままなら外せるように出す）。</summary>
    public bool CanForceBg => HasAlpha == true || ForceBgRemoval;
    /// <summary>生成に送る指定（透明部分の無い画像では意味が無いので送らない）。</summary>
    public bool ForceBgEffective => ForceBgRemoval && HasAlpha != false;
    public string SlotAdvancedHeader => ForceBgRemoval ? L.T("UB_SlotAdvancedChanged") : L.T("UB_SlotAdvanced");

    // --- 被写体の検査（2026-10-17 追加機能 SF21。注意だけで生成は止めない） -------------------------------------
    private IReadOnlyList<string> _alphaSubject = [];     // 透明部分のある画像: バックエンドの inspect_image の注意
    private IReadOnlyList<string>? _checkedSubject;        // 「背景除去を確認」の結果の注意（背景を除いた後のマスク）
    /// <summary>被写体の注意（端で切れている・小さすぎる・複数の物体）。無ければ null。</summary>
    public string? SubjectNotice
    {
        get
        {
            var codes = _checkedSubject ?? (HasAlpha == true && !ForceBgRemoval ? _alphaSubject : []);
            var lines = codes.Where(c => c.StartsWith("subject_", StringComparison.Ordinal)).Distinct()
                             .Select(c => L.TryT("UB_Subject_" + c) ?? c).ToList();
            return lines.Count == 0 ? null : L.T("UB_SubjectHeader") + string.Concat(lines.Select(x => "\n" + L.F("Common_BulletFormat", x)));
        }
    }

    /// <summary>
    /// 生成の詳細設定の背景除去（SF20）を変えたとき: 背景を除く画像（透明部分が無い・不明、または強制した）では、前の
    /// 「背景除去を確認」の結果は今の設定と合わないので外す。透明部分をそのまま使う画像の結果・被写体の注意はこの設定に
    /// 依存しない（アプリ側の背景除去は背景を除く画像にだけ効く）ので残す（点検 UB-R5）。
    /// </summary>
    internal void OnBgSettingsChanged()
    {
        if (HasAlpha == true && !ForceBgEffective) return;
        if (Processed != null && !_fromGeneration) SetProcessed(null, "");
        _checkedSubject = null;
        NotifyAlpha();
    }

    private BitmapSource? _preview;
    /// <summary>元の画像（向きの EXIF を反映して正立。縮めて復号したもの）。</summary>
    public BitmapSource? Preview { get => _preview; private set { if (Set(ref _preview, value)) OnPropertyChanged(nameof(DisplayedImage)); } }
    private string _info = "";
    public string Info { get => _info; private set => Set(ref _info, value); }

    private bool? _hasAlpha;
    /// <summary>透明な部分があるか（無ければ TRELLIS が背景を除く）。読めないとき・WebP でバックエンドの判定待ちのときは null。</summary>
    public bool? HasAlpha { get => _hasAlpha; private set => Set(ref _hasAlpha, value); }
    private double? _coverage;
    /// <summary>透明部分はあるが、ほぼ全体が不透明（角だけ透明なスクリーンショットなど。背景は除かれない。点検 C09）。</summary>
    public bool NearlyOpaque => HasAlpha == true && _coverage >= NearlyOpaqueCoverage;
    /// <summary>背景の扱いの案内（透明部分の無い画像: モデルがあれば自動で除去、無ければ取得の案内。ほぼ不透明な画像: 背景が残る注意）。</summary>
    public string? BgNotice => ForceBgEffective
        ? (_owner.BgAvailable ? L.T("UB_BgForced") : L.F("UB_BgForcedMissing", AppLog.Redact(_owner.BgExpected)))
        : HasAlpha switch
        {
            true => NearlyOpaque ? L.T("Gen_BgNearlyOpaque") : null,
            false => _owner.BgAvailable ? L.T("Gen_BgAuto") : L.F("Gen_BgMissing", AppLog.Redact(_owner.BgExpected)),
            _ => null,
        };
    public bool BgNoticeIsWarning => ForceBgEffective ? !_owner.BgAvailable
        : NearlyOpaque || (HasAlpha == false && !_owner.BgAvailable);

    private void SetAlpha(bool? hasAlpha, double? coverage)
    {
        _coverage = hasAlpha == true ? coverage : null;
        HasAlpha = hasAlpha;
        NotifyAlpha();
    }

    private void NotifyAlpha()
    {
        OnPropertyChanged(nameof(NearlyOpaque)); OnPropertyChanged(nameof(BgNotice)); OnPropertyChanged(nameof(BgNoticeIsWarning));
        OnPropertyChanged(nameof(CanForceBg)); OnPropertyChanged(nameof(ForceBgEffective)); OnPropertyChanged(nameof(SubjectNotice));
        CommandManager.InvalidateRequerySuggested();
    }

    private BitmapImage? _processed;
    /// <summary>背景を除いて切り抜いた画像（「背景除去を確認」の結果、または生成に使った画像）。</summary>
    public BitmapImage? Processed
    {
        get => _processed;
        private set { if (Set(ref _processed, value)) { OnPropertyChanged(nameof(HasProcessed)); OnPropertyChanged(nameof(DisplayedImage)); } }
    }
    public bool HasProcessed => Processed != null;
    private string _processedInfo = "";
    public string ProcessedInfo { get => _processedInfo; private set => Set(ref _processedInfo, value); }
    private bool _showProcessed;
    public bool ShowProcessed
    {
        get => _showProcessed;
        set { if (Set(ref _showProcessed, value)) { OnPropertyChanged(nameof(ShowOriginal)); OnPropertyChanged(nameof(DisplayedImage)); } }
    }
    public bool ShowOriginal { get => !ShowProcessed; set => ShowProcessed = !value; }
    public ImageSource? DisplayedImage => ShowProcessed && Processed != null ? Processed : Preview;
    private bool _bgBusy;
    public bool BgBusy { get => _bgBusy; private set { if (Set(ref _bgBusy, value)) CommandManager.InvalidateRequerySuggested(); } }

    private bool _fromGeneration;
    public void SetProcessed(string? path, string info, bool show = false, bool fromGeneration = false)
    {
        Processed = path != null && File.Exists(path) ? LoadBitmap(path) : null;
        ProcessedInfo = Processed != null ? info : "";
        ShowProcessed = show && Processed != null;
        _fromGeneration = fromGeneration && Processed != null;
    }

    /// <summary>下書きを保存・破棄したとき: 前の生成に使った画像の表示を外す（「背景除去を確認」の結果は残す）。</summary>
    public void ClearGenerationResult()
    {
        if (_fromGeneration) SetProcessed(null, "");
    }

    /// <summary>
    /// 背景除去のモデルの有無が変わったとき・バックエンドにつながったとき（案内と画像の説明を作り直す）。画像は読み直さない
    /// （非表示の欄も呼ばれるため。点検 C52）。WebP の透明部分の判定が済んでいなければ、ここでバックエンドに問い合わせる。
    /// </summary>
    public void Refresh()
    {
        NotifyAlpha();
        UpdateInfo();
        // WebP の透明部分の判定・透明部分のある画像の被写体の検査（SF21）が済んでいなければ、ここで問い合わせる
        if (_probe != null && !_inspected && ((HasAlpha == null && _alphaError == null) || HasAlpha == true))
            _ = InspectAsync(_loadSeq, Path);
    }

    private InputImageProbe? _probe;      // 読み込んだ画像の大きさ・形式・透明の判定（Refresh で使い回す）
    private string? _alphaError;           // WebP の判定の問い合わせが失敗したときの文（情報欄に出す）
    private int _loadSeq;                  // 読み込みの番号（読み込み・問い合わせの途中で Path が変わったら、古い結果を捨てる）
    private bool _inspected;               // この画像についてバックエンドの inspect_image の応答（成功・失敗）を受け取った

    /// <summary>プレビューを UI スレッドの外で読む（大きな写真で画面を止めない。点検 C52）。</summary>
    private async void LoadPreview()
    {
        int seq = ++_loadSeq;
        string path = Path;
        Preview = null;
        _probe = null;
        _alphaError = null;
        _inspected = false;
        _alphaSubject = [];
        Info = "";
        SetAlpha(null, null);
        // ネットワーク上のパスは少し待ってから UI スレッドの外で確かめる（入力の 1 文字ごとに画面を止めない。点検 R18B-04）
        if (!await _pathCheck.ProbeAsync(path) || seq != _loadSeq) return;
        try
        {
            var probe = await Task.Run(() => InputImageProbe.Read(path, PreviewDecodeWidth));
            if (seq != _loadSeq) return;
            if (probe == null)
            {
                Info = L.T("Gen_ImageUnreadable");
                return;
            }
            _probe = probe;
            Preview = probe.Preview;
            SetAlpha(probe.HasAlpha, probe.Coverage);
            UpdateInfo();
            // WebP（透明部分の判定）と透明部分のある画像（被写体の検査。SF21）はバックエンドに問い合わせる
            if (probe.HasAlpha != false) await InspectAsync(seq, path);
        }
        catch (Exception e) { AppLog.Warn(L.F("Log_Gen_InputPreviewFailed", Index, e.GetType().Name)); }
    }

    /// <summary>情報欄（回した後の大きさ・形式・透明部分の有無）。</summary>
    private void UpdateInfo()
    {
        if (_probe is not { } p) return;
        string alpha = HasAlpha switch
        {
            true => ForceBgRemoval ? L.T("UB_ImageForced") : NearlyOpaque ? L.T("Gen_ImageNearlyOpaque") : L.T("Gen_ImageHasAlpha"),
            false => _owner.BgAvailable ? L.T("Gen_ImageNoAlphaAuto") : L.T("Gen_ImageNoAlpha"),
            _ => _alphaError ?? L.T("Gen_ImageAlphaChecking"),
        };
        Info = L.F("Gen_ImageInfo", p.Width, p.Height, p.Format, alpha);
    }

    /// <summary>
    /// WebP の透明部分の有無をバックエンド（pipeline.inspect_image。Pillow で TRELLIS と同じ判定）に問い合わせる。WIC は
    /// 透明な WebP も透明なしの画素形式で返すので、画面だけでは判定できない（点検 C44）。失敗はダイアログにせず情報欄に出す。
    /// 透明部分のある画像（WebP 以外も）は、同じ応答の warnings から被写体の検査（SF21）の注意を受け取る（透明部分の判定は
    /// 画面の判定のまま。失敗しても情報欄は変えない）。
    /// </summary>
    private async Task InspectAsync(int seq, string path)
    {
        if (!_owner.BackendReady) return;          // つながった後の Refresh でもう一度
        bool unknown = HasAlpha == null;
        try
        {
            var r = await _owner.Client.PostAsync("images/inspect", new JsonObject { ["path"] = path });
            if (seq != _loadSeq) return;
            _inspected = true;
            _alphaSubject = r.Strings("warnings").Where(c => c.StartsWith("subject_", StringComparison.Ordinal)).ToList();
            if (unknown)
            {
                _alphaError = null;
                SetAlpha(r.Bool("has_alpha"), r.Dbl("coverage"));
            }
            else NotifyAlpha();
        }
        catch (ApiException e)
        {
            if (seq != _loadSeq) return;
            _inspected = true;
            if (unknown) _alphaError = ErrorText.From(e).Message;
        }
        catch (Exception) { return; }              // 接続の切断など（判定は不明のまま）
        UpdateInfo();
    }

    private void Browse()
    {
        var d = new OpenFileDialog { Filter = L.T("Gen_ImageFilter") + "|*.png;*.jpg;*.jpeg;*.webp;*.bmp", CheckFileExists = true };
        if (d.ShowDialog() == true) Path = d.FileName;
    }

    private async Task CheckBackgroundAsync()
    {
        var c = _owner.Client;
        var requested = Path;
        bool force = ForceBgEffective;
        var settings = _owner.BgSettingsJson();
        BgBusy = true;
        ProcessedInfo = L.T("Gen_BgChecking");
        try
        {
            // 背景除去の強制（SF19）とアプリ側の背景除去の設定（SF20）は、生成と同じ指定で確かめる（点検 C54）
            var p = new JsonObject { ["image_path"] = requested, ["settings"] = settings };
            if (force) p["force_bg_removal"] = true;
            var r = await c.RunJobAsync("bg_preview", p, null, null);
            // 背景除去の設定（SF20）は背景を除いた画像にだけ効くので、透明部分をそのまま使った結果は設定が変わっても使える（UB-R5）
            bool removes = force || !r.Bool("has_alpha");
            if (Path != requested || ForceBgEffective != force
                || (removes && _owner.BgSettingsJson().ToJsonString() != settings.ToJsonString()))
            {
                // 確認の途中で画像・指定を変えた: 前の結果を今の画像の「背景除去後」に出さない（点検 C47）
                AppLog.Info(L.F("Log_Gen_BgCheckDiscarded", Index));
                if (Path == requested) ProcessedInfo = "";
                return;
            }
            // 透明部分の有無はバックエンドの判定（Pillow。TRELLIS と同じ）で更新する（WebP など。点検 C44）
            SetAlpha(r.Bool("has_alpha"), r.Dbl("input_coverage"));
            _alphaError = null;
            _checkedSubject = r.Strings("warnings").Where(w => w.StartsWith("subject_", StringComparison.Ordinal)).ToList();
            NotifyAlpha();
            UpdateInfo();
            bool nearly = r.Strings("warnings").Contains("alpha_nearly_opaque");
            var info = r.Bool("removed") ? L.F("Gen_BgCheckedRemoved", r.Dbl("seconds"))
                : nearly ? L.T("Gen_BgCheckedNearlyOpaque") : L.T("Gen_BgCheckedCropOnly");
            if (r.Bool("app_removal"))
            {
                var opts = r.Strings("rembg").ToList();
                info += opts.Count > 0 ? L.F("UB_BgCheckedOptions", string.Join(L.T("Common_ListSepDot"), opts)) : L.T("UB_BgCheckedApp");
            }
            SetProcessed(r.Str("path"), info, show: true);
            AppLog.Info(L.F("Log_Gen_BgChecked", Index));
        }
        catch (OperationCanceledException) { if (Path == requested) ProcessedInfo = ""; }
        catch (Exception e)
        {
            if (Path != requested) return;          // 前の画像についての失敗は出さない（点検 C47）
            ProcessedInfo = "";
            App.ShowError(e);
        }
        finally { BgBusy = false; }
    }

    /// <summary>ファイルのストリームから読む（ファイル名の %41 などをそのまま扱う。ファイルはつかんだままにしない）。</summary>
    public static BitmapImage? LoadBitmap(string path, int decodeWidth = 0) => ImageFiles.Load(path, decodeWidth);
}

/// <summary>
/// 入力画像のプレビューと情報（UI スレッドの外で読む。結果は Freeze 済み）。
/// <list type="bullet">
/// <item>大きさ（Width・Height）は向きの EXIF を反映した後の値。元の大きさは全体を復号せずにフレームから取る（点検 C43・C52）。</item>
/// <item>透明部分の有無（HasAlpha）と不透明な割合（Coverage）は、縮める前の元の解像度で数える（縮めると alpha 254 などの
/// まばらな画素を見落とす。点検 C44・C09）。画素形式に透明の無いもの（JPEG など）は数えない。WebP は WIC が透明でも
/// 透明なしの形式で返すので null（不明。バックエンドに問い合わせる）。</item>
/// <item>プレビューは向きの EXIF（System.Photo.Orientation）で回す・反転する（バックエンドの copy_image＝Pillow の
/// exif_transpose と同じ向き）。PNG の eXIf チャンクは WIC が読まないので回さない（まれ）。</item>
/// </list>
/// </summary>
public sealed record InputImageProbe(BitmapSource Preview, int Width, int Height, string Format, bool? HasAlpha, double? Coverage,
                                     int Orientation)
{
    /// <summary>読めないときは null。previewWidth はプレビューを復号する最大の幅（回す前の幅）。</summary>
    public static InputImageProbe? Read(string path, int previewWidth)
    {
        try
        {
            string ext = System.IO.Path.GetExtension(path).TrimStart('.').ToUpperInvariant();
            int w, h, orientation;
            bool? hasAlpha = false;
            double? coverage = null;
            BitmapSource? full = null;           // 透明部分を数えるために全体を復号したもの（プレビューにも使う）
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                var dec = BitmapDecoder.Create(fs, BitmapCreateOptions.DelayCreation, BitmapCacheOption.None);
                var frame = dec.Frames[0];
                w = frame.PixelWidth;
                h = frame.PixelHeight;
                orientation = ReadOrientation(frame);
                if (IsWebp(dec, ext)) hasAlpha = null;
                else if (HasAlphaFormat(frame.Format))
                {
                    full = new CachedBitmap(frame, BitmapCreateOptions.None, BitmapCacheOption.OnLoad);
                    (hasAlpha, coverage) = CountAlpha(full);
                }
            }
            BitmapSource? preview;
            if (full != null)
            {
                double s = w > previewWidth ? (double)previewWidth / w : 1.0;
                preview = s < 1 ? new CachedBitmap(new TransformedBitmap(full, new ScaleTransform(s, s)), BitmapCreateOptions.None,
                                                   BitmapCacheOption.OnLoad)
                                : full;
            }
            else preview = ImageFiles.Load(path, w > previewWidth ? previewWidth : 0);
            if (preview == null) return null;
            preview = Orient(preview, orientation);
            if (!preview.IsFrozen) preview.Freeze();
            bool swap = orientation >= 5;           // 5〜8 は 90° 回すので縦横が入れ替わる
            return new InputImageProbe(preview, swap ? h : w, swap ? w : h, ext, hasAlpha, coverage, orientation);
        }
        catch (Exception) { return null; }
    }

    /// <summary>向きの EXIF（1〜8）。無い・読めないときは 1。JPEG・WebP・PNG（XMP）で読める（/app1/ifd/{ushort=274} は JPEG 専用）。</summary>
    public static int ReadOrientation(BitmapFrame frame)
    {
        try
        {
            if (frame.Metadata is BitmapMetadata md && md.GetQuery("System.Photo.Orientation") is { } v)
            {
                int o = Convert.ToInt32(v, CultureInfo.InvariantCulture);
                return o is >= 1 and <= 8 ? o : 1;
            }
        }
        catch (Exception) { }
        return 1;
    }

    /// <summary>
    /// 向きの EXIF → 正立にする変換（Pillow の exif_transpose と同じ。8 通りとも確認済み）。1 は null。
    /// 2=左右反転、3=180°、4=上下反転、5=左右反転→270°、6=90°、7=左右反転→90°、8=270°（角度は時計回り）。
    /// </summary>
    public static Transform? OrientationTransform(int orientation)
    {
        var g = new TransformGroup();
        if (orientation is 2 or 5 or 7) g.Children.Add(new ScaleTransform(-1, 1));
        if (orientation == 4) g.Children.Add(new ScaleTransform(1, -1));
        double angle = orientation switch { 3 => 180, 6 or 7 => 90, 5 or 8 => 270, _ => 0 };
        if (angle != 0) g.Children.Add(new RotateTransform(angle));
        return g.Children.Count == 0 ? null : g;
    }

    public static BitmapSource Orient(BitmapSource src, int orientation)
    {
        var t = OrientationTransform(orientation);
        return t == null ? src : new CachedBitmap(new TransformedBitmap(src, t), BitmapCreateOptions.None, BitmapCacheOption.OnLoad);
    }

    private static bool IsWebp(BitmapDecoder dec, string ext)
    {
        if (ext == "WEBP") return true;
        try { return dec.CodecInfo?.FileExtensions?.Contains(".webp", StringComparison.OrdinalIgnoreCase) == true; }
        catch (Exception) { return false; }
    }

    /// <summary>画素形式に透明（alpha・パレット）があるか。無ければ画素を数えずに「透明部分なし」。</summary>
    public static bool HasAlphaFormat(PixelFormat f) =>
        f == PixelFormats.Bgra32 || f == PixelFormats.Pbgra32 || f == PixelFormats.Rgba64 || f == PixelFormats.Prgba64
        || f == PixelFormats.Rgba128Float || f == PixelFormats.Prgba128Float || f == PixelFormats.Indexed1
        || f == PixelFormats.Indexed2 || f == PixelFormats.Indexed4 || f == PixelFormats.Indexed8;

    /// <summary>
    /// 透明な画素（alpha &lt; 255）があるか（TRELLIS と同じく、全部が不透明なら「透明部分なし」）と、不透明な画素
    /// （alpha ≥ 205。TRELLIS は不透明度 0.8 を超える範囲で切り抜く）の割合。行のまとまりごとに数える。
    /// </summary>
    public static (bool HasAlpha, double? Coverage) CountAlpha(BitmapSource src)
    {
        var conv = new FormatConvertedBitmap(src, PixelFormats.Bgra32, null, 0);
        int w = conv.PixelWidth, h = conv.PixelHeight, stride = w * 4;
        if (w <= 0 || h <= 0) return (false, null);
        int rows = Math.Clamp((4 << 20) / stride, 1, h);
        var buf = new byte[stride * rows];
        long transparent = 0, opaque = 0;
        for (int y = 0; y < h; y += rows)
        {
            int n = Math.Min(rows, h - y);
            conv.CopyPixels(new Int32Rect(0, y, w, n), buf, stride, 0);
            int len = stride * n;
            for (int i = 3; i < len; i += 4)
            {
                byte a = buf[i];
                if (a < 255) transparent++;
                if (a >= 205) opaque++;
            }
        }
        return transparent == 0 ? (false, null) : (true, opaque / ((double)w * h));
    }
}
