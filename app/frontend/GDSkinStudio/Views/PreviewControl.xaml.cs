using System.IO;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using Microsoft.Web.WebView2.Core;

namespace GDSkinStudio.Views;

/// <summary>プレビューに出すもの。Kind は "glb"（3D）、"check"（変換の確認画像）、"candidates"（候補の正面図）。</summary>
public sealed record PreviewItem(string Key, string Label, string Path, string Kind, bool KeepGround = false);

/// <summary>
/// 3D プレビュー（WebView2 + three.js。docs/app_architecture.md 7 章）。GLB は仮想ホスト
/// https://work.gdskin.example/（データフォルダー\work。生成結果の下書き）と https://lib.gdskin.example/（装備ライブラリ）から読む。
/// データフォルダーの他の場所（設定・ログ・WebView2 のデータ・入力画像）はページに公開しない。
/// </summary>
public partial class PreviewControl : UserControl
{
    public const string AppHost = "app.gdskin.example";
    public const string WorkHost = "work.gdskin.example";
    public const string LibHost = "lib.gdskin.example";

    /// <summary>装備ライブラリの場所（バックエンドの paths の library。まだ分からないときは データフォルダー\library）。</summary>
    public static string? LibraryRoot { get; set; }

    private static string WorkRoot => System.IO.Path.Combine(App.Paths.DataHome, "work");
    private static string CurrentLibraryRoot => LibraryRoot ?? System.IO.Path.Combine(App.Paths.DataHome, "library");

    public static readonly DependencyProperty ItemsProperty = DependencyProperty.Register(
        nameof(Items), typeof(IReadOnlyList<PreviewItem>), typeof(PreviewControl),
        new PropertyMetadata(null, (d, _) => ((PreviewControl)d).OnItemsChanged()));

    public IReadOnlyList<PreviewItem>? Items
    {
        get => (IReadOnlyList<PreviewItem>?)GetValue(ItemsProperty);
        set => SetValue(ItemsProperty, value);
    }

    /// <summary>
    /// 「候補」のタブの候補の一覧（ViewModels.CandidateRow。「この候補で作る」のボタンを含む。SF06・SF14、2026-10-17 U-A2）。
    /// 生成の画面だけが渡す（装備一覧のプレビューでは出さない）。空なら一覧を隠す。
    /// </summary>
    public static readonly DependencyProperty CandidateRowsProperty = DependencyProperty.Register(
        nameof(CandidateRows), typeof(System.Collections.IEnumerable), typeof(PreviewControl),
        new PropertyMetadata(null, (d, _) => ((PreviewControl)d).OnCandidateRowsChanged()));

    public System.Collections.IEnumerable? CandidateRows
    {
        get => (System.Collections.IEnumerable?)GetValue(CandidateRowsProperty);
        set => SetValue(CandidateRowsProperty, value);
    }

    private void OnCandidateRowsChanged()
    {
        var rows = CandidateRows;
        bool any = rows != null && rows.GetEnumerator().MoveNext();
        CandList.ItemsSource = any ? rows : null;
        CandListPanel.Visibility = any ? Visibility.Visible : Visibility.Collapsed;
    }

    private bool _ready;
    private bool _initStarted;
    private bool _navigated;
    private PreviewItem? _current;
    private string? _mappedLib;
    private Task<CoreWebView2Environment>? _env;   // このプレビューが使った環境（ブラウザーのプロセスが終わったら捨てる）
    private int _unresponsive;

    // 自動の回復の上限。同じモデルを読むと描画のプロセスが必ず落ちる場合（巨大な GLB でメモリが足りないなど）に、
    // 読み直し → 送り直し → 終了 → 読み直し…を繰り返さないようにする
    private const long FailureWindowMs = 60_000;   // 前の障害からこの時間のうちに起きた障害を「続けて」と数える
    private const int HoldAfterFailures = 3;       // 続けて 3 回目からは今のモデルを送り直さない
    private const int StopAfterFailures = 4;       // 続けて 4 回目（モデルを送らなくても落ちる）で自動の回復をやめる
    private int _failures;
    private long _lastFailureTick;
    private bool _holdCurrent;   // 障害のため、読み直した後に今のモデルを送り直さない（利用者がモデルを選ぶと解除）
    private bool _stopped;       // 障害が続いたため自動の回復をやめた（モデルを選ぶか、表示し直すと再開）

    /// <summary>
    /// ページの準備（'ready'）の待ち。ページのスクリプトを読めない（同梱の three.js が欠けた・隔離された など）ときは 'ready' も
    /// 'fatal' も来ず、「読み込み中…」のまま理由が出なかったので、待ち時間を過ぎたら案内を出す（点検 R18B-05）。後で 'ready' が
    /// 来れば案内を消す。
    /// </summary>
    private const int ReadyTimeoutSeconds = 20;
    private readonly System.Windows.Threading.DispatcherTimer _readyTimer = new() { Interval = TimeSpan.FromSeconds(ReadyTimeoutSeconds) };
    private bool _unavailable;   // ページを動かせない案内を出している（WebView2 は隠している）

    public PreviewControl()
    {
        InitializeComponent();
        _readyTimer.Tick += OnReadyTimeout;
        // WebView2 は表示されてから初期化する（非表示のページの中では窓が作られない）
        IsVisibleChanged += async (_, _) => { if (IsVisible) await InitAsync(); };
        Loaded += async (_, _) => { if (IsVisible) await InitAsync(); };
    }

    private async Task InitAsync()
    {
        if (_initStarted) return;
        _initStarted = true;
        _stopped = false;
        _unavailable = false;
        try
        {
            Web.Visibility = Visibility.Visible;
            FallbackText.Visibility = Visibility.Collapsed;
            _env = App.WebViewEnvironment;
            var env = await _env;
            await Web.EnsureCoreWebView2Async(env);
            var core = Web.CoreWebView2;
            core.Settings.AreDevToolsEnabled = System.Diagnostics.Debugger.IsAttached;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsZoomControlEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsGeneralAutofillEnabled = false;
            core.Settings.IsPasswordAutosaveEnabled = false;
            core.SetVirtualHostNameToFolderMapping(AppHost, App.Paths.WebRoot, CoreWebView2HostResourceAccessKind.Allow);
            // ページが読むのは work\ と装備ライブラリの GLB だけなので、その 2 か所だけを割り当てる（データフォルダー全体は公開しない）。
            // どちらもページ（app.gdskin.example）から fetch で読むので Allow（DenyCors では fetch が拒まれる）
            Directory.CreateDirectory(WorkRoot);
            core.SetVirtualHostNameToFolderMapping(WorkHost, WorkRoot, CoreWebView2HostResourceAccessKind.Allow);
            MapLibrary();
            // 自分のページ以外には移動しない・新しい窓を開かない（外部のサイトに接続しない）
            core.NavigationStarting += (_, e) =>
            {
                if (!Uri.TryCreate(e.Uri, UriKind.Absolute, out var u) || u.Host != AppHost) e.Cancel = true;
            };
            core.NewWindowRequested += (_, e) => e.Handled = true;
            core.WebMessageReceived += OnWebMessage;
            core.ProcessFailed += OnProcessFailed;
            core.Navigate($"https://{AppHost}/preview.html");
            _navigated = true;
            StartReadyTimer();
        }
        catch (Exception e)
        {
            _readyTimer.Stop();
            AppLog.Error(L.F("Preview_InitFailed", e.Message));
            Web.Visibility = Visibility.Collapsed;
            FallbackText.Text = e is WebView2RuntimeNotFoundException ? L.T("Preview_NoRuntime") : L.F("Preview_InitFailed", e.Message);
            FallbackText.Visibility = Visibility.Visible;
            // 次にこの画面を表示したときにやり直せるようにする（失敗した環境と WebView2 のインスタンスは使い回さない）
            App.DiscardWebViewEnvironment(_env);
            ReplaceWebView();
            _initStarted = false;
        }
    }

    /// <summary>
    /// WebView2 のプロセスの障害。描画のプロセスが終わったときはページを読み直し（'ready' で今のモデルを送り直す）、
    /// ブラウザーのプロセスが終わったときは WebView2 を作り直す。GPU などのプロセスは WebView2 が自動で起動し直すので記録だけ。
    /// 自動の回復には上限がある（Recover）。
    /// </summary>
    private void OnProcessFailed(object? sender, CoreWebView2ProcessFailedEventArgs e)
    {
        AppLog.Warn(L.F("Log_Preview_ProcessFailed", e.ProcessFailedKind, e.Reason, e.ExitCode));
        switch (e.ProcessFailedKind)
        {
            case CoreWebView2ProcessFailedKind.RenderProcessExited:
                // ページはエラーの表示に置き換わる
                Recover(e.Reason, browserExited: false, L.T("Log_Preview_RenderExited"));
                break;
            case CoreWebView2ProcessFailedKind.RenderProcessUnresponsive:
                // 応答しない間は数秒ごとに届く。一時的に重いだけのこともあるので、続いたときだけ読み直す
                if (++_unresponsive >= 3)
                {
                    _unresponsive = 0;
                    Recover(e.Reason, browserExited: false, L.T("Log_Preview_RenderUnresponsive"));
                }
                break;
            case CoreWebView2ProcessFailedKind.BrowserProcessExited:
                Recover(e.Reason, browserExited: true, L.T("Log_Preview_BrowserExited"));
                break;
        }
    }

    /// <summary>
    /// 障害からの自動の回復（描画のプロセスはページの読み直し、ブラウザーのプロセスは WebView2 と環境の作り直し）。
    /// 前の障害から 60 秒のうちに続けて起きた回数を数え、3 回目（理由がメモリ不足のときは 1 回目）からは
    /// 読み直しても今のモデルを送り直さない。4 回目（モデルを送らなくても落ちる）で自動の回復をやめて案内を出す。
    /// 利用者がモデルを選ぶ（選択中のボタンを押し直す）と、回数を数え直して再開する（Load）。
    /// </summary>
    private void Recover(CoreWebView2ProcessFailedReason reason, bool browserExited, string why)
    {
        _ready = false;
        long now = Environment.TickCount64;
        if (_failures > 0 && now - _lastFailureTick > FailureWindowMs) _failures = 0;
        _lastFailureTick = now;
        int n = ++_failures;
        if (n >= StopAfterFailures)
        {
            AppLog.Warn(L.F("Log_Preview_RecoveryStopped", n, why));
            StopRecovery(browserExited ? _env : null);
            return;
        }
        if (_current != null && !_holdCurrent && (n >= HoldAfterFailures || reason == CoreWebView2ProcessFailedReason.OutOfMemory))
        {
            _holdCurrent = true;
            AppLog.Warn(L.F("Log_Preview_HoldCurrent", n, reason, _current.Label));
        }
        if (!browserExited)
        {
            ReloadPage(why);
            return;
        }
        // WebView2 は閉じられ、同じインスタンスは初期化し直せない。イベントの処理を終えてから新しいものに置き換える
        var env = _env;
        _ = Dispatcher.InvokeAsync(async () =>
        {
            App.DiscardWebViewEnvironment(env);
            ReplaceWebView();
            _initStarted = false;
            if (IsVisible) await InitAsync();
        });
    }

    /// <summary>
    /// 自動の回復をやめて案内を出す。WebView2 は新しいインスタンスに置き換えておき、モデルが選ばれたとき（Load）か、
    /// 次にこの画面が表示されたとき（InitAsync）に開き直す。
    /// </summary>
    private void StopRecovery(Task<CoreWebView2Environment>? deadEnv)
    {
        _ = Dispatcher.InvokeAsync(() =>
        {
            if (deadEnv != null) App.DiscardWebViewEnvironment(deadEnv);
            ReplaceWebView();
            _initStarted = false;
            _failures = 0;
            _stopped = true;
            _unavailable = false;
            Web.Visibility = Visibility.Collapsed;
            FallbackText.Text = L.T("Preview_Stopped");
            FallbackText.Visibility = Visibility.Visible;
        });
    }

    private void ReloadPage(string why)
    {
        _ready = false;
        try
        {
            Web.CoreWebView2?.Reload();
            AppLog.Info(L.F("Log_Preview_Reload", why));
            StartReadyTimer();
        }
        catch (Exception e) { AppLog.Warn(L.F("Log_Preview_ReloadFailed", e.Message)); }
    }

    /// <summary>WebView2 を新しいインスタンスに置き換える（古いものは閉じる）。表示と初期化は InitAsync で行う。</summary>
    private void ReplaceWebView()
    {
        _ready = false;
        _navigated = false;
        _readyTimer.Stop();
        _mappedLib = null;
        var old = Web;
        if (old.Parent is not Panel parent) return;
        var fresh = new Microsoft.Web.WebView2.Wpf.WebView2 { DefaultBackgroundColor = old.DefaultBackgroundColor, Visibility = old.Visibility };
        parent.Children.Insert(parent.Children.IndexOf(old), fresh);
        parent.Children.Remove(old);
        Web = fresh;
        try { old.Dispose(); } catch (Exception e) { AppLog.Warn(L.F("Log_Preview_DisposeFailed", e.Message)); }
    }

    /// <summary>装備ライブラリを lib.gdskin.example に割り当てる。割り当てを変えたときは true。</summary>
    private bool MapLibrary()
    {
        var root = CurrentLibraryRoot;
        if (Web.CoreWebView2 == null || root == _mappedLib || !Directory.Exists(root)) return false;
        Web.CoreWebView2.SetVirtualHostNameToFolderMapping(LibHost, root, CoreWebView2HostResourceAccessKind.Allow);
        _mappedLib = root;
        return true;
    }

    private static bool IsWithin(string path, string root)
    {
        var p = System.IO.Path.GetFullPath(path).TrimEnd('\\') + "\\";
        var r = System.IO.Path.GetFullPath(root).TrimEnd('\\') + "\\";
        return p.StartsWith(r, StringComparison.OrdinalIgnoreCase);
    }

    private string? ToUrl(string path)
    {
        string? host = null, rel = null;
        var lib = CurrentLibraryRoot;
        if (IsWithin(path, WorkRoot))
        {
            host = WorkHost;
            rel = System.IO.Path.GetRelativePath(WorkRoot, path);
        }
        else if (IsWithin(path, lib))
        {
            // ページの表示の後に割り当てを変えると今のページに反映されないことがあるので、そのときは読み直す
            // （'ready' で今のモデルを送り直す）
            if (MapLibrary() && _navigated) ReloadPage(L.T("Log_Preview_LibraryRemapped"));
            host = LibHost;
            rel = System.IO.Path.GetRelativePath(lib, path);
        }
        if (host == null || rel == null) return null;
        var segs = rel.Split('\\', '/').Select(Uri.EscapeDataString);
        long v = File.Exists(path) ? File.GetLastWriteTimeUtc(path).Ticks : 0;   // 再生成で同じパスが変わったときに読み直す
        return $"https://{host}/{string.Join("/", segs)}?v={v}";
    }

    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        _unresponsive = 0;
        JsonNode? m;
        try { m = JsonNode.Parse(e.WebMessageAsJson); } catch (Exception) { return; }
        switch (m.Str("event"))
        {
            case "ready":
                _ready = true;
                _readyTimer.Stop();
                if (_unavailable)
                {
                    // 待ち時間を過ぎた後に準備ができた（読み込みが遅かっただけ）: 案内を消して 3D の表示に戻す
                    _unavailable = false;
                    FallbackText.Visibility = Visibility.Collapsed;
                    Web.Visibility = Visibility.Visible;
                }
                Post(new JsonObject
                {
                    ["cmd"] = "init",
                    ["strings"] = new JsonObject
                    {
                        ["loading"] = L.T("Preview_Loading"), ["empty"] = L.T("Preview_Empty"), ["error"] = L.T("Preview_Error"),
                        ["height"] = L.T("Preview_Height"), ["vertices"] = L.T("Preview_Vertices"), ["triangles"] = L.T("Preview_Triangles"),
                        ["frontLabel"] = L.T("UF_PreviewFrontLabel"),
                    },
                    // 正面の印（SF23）・テクスチャ・ワイヤーフレームの今の状態。読み直し・作り直しの後や、準備の前に切り替えたとき
                    // （その送信は捨てられる）もチェックボックスと表示を合わせる（点検 R18A-04）
                    ["frontMarker"] = FrontMarkOn,
                    ["texture"] = TextureCheck.IsChecked == true,
                    ["wireframe"] = WireCheck.IsChecked == true,
                });
                if (_current != null)
                {
                    // 障害が続いたモデルは自動では送り直さない（モデルのボタンを押すと読み込む）
                    if (_holdCurrent) StatusText.Text = L.F("Preview_CrashHeld", _current.Label);
                    else
                    {
                        AppLog.Info(L.F("Log_Preview_Resend", _current.Label));
                        Load(_current, resend: true);
                    }
                }
                break;
            case "loaded":
                StatusText.Text = L.F("Preview_Loaded", m.Str("label"), m.Long("vertices"), m.Long("triangles"));
                break;
            case "error":
                StatusText.Text = L.F("Preview_LoadError", m.Str("message"));
                AppLog.Warn(L.F("Log_Preview_LoadFailed", m.Str("message")));
                break;
            case "fatal":
                // ページを動かせない（WebGL を使えない）。'ready' は来ないので案内を出す。自動では読み直さない（点検 R18B-05）
                _readyTimer.Stop();
                AppLog.Warn(L.F("Log_Preview_Unavailable", m.Str("kind"), m.Str("message")));
                ShowUnavailable(L.F("Preview_WebGLUnavailable", m.Str("message")));
                break;
        }
    }

    private void StartReadyTimer()
    {
        _readyTimer.Stop();
        _readyTimer.Start();
    }

    private void OnReadyTimeout(object? sender, EventArgs e)
    {
        _readyTimer.Stop();
        if (_ready || !_navigated || _stopped) return;
        AppLog.Warn(L.F("Log_Preview_NotReady", ReadyTimeoutSeconds));
        ShowUnavailable(L.F("Preview_NotReady", ReadyTimeoutSeconds));
    }

    /// <summary>3D の表示の代わりに案内を出す（WebView2 は窓なので、重ねた文字が見えるように隠す）。</summary>
    private void ShowUnavailable(string text)
    {
        _unavailable = true;
        Web.Visibility = Visibility.Collapsed;
        FallbackText.Text = text;
        FallbackText.Visibility = Visibility.Visible;
    }

    private void Post(JsonObject msg)
    {
        if (!_ready || Web.CoreWebView2 == null) return;
        try { Web.CoreWebView2.PostWebMessageAsJson(msg.ToJsonString()); }
        catch (Exception e)
        {
            // プロセスの障害の直後など。作り直し・読み直しの後の 'ready' で今のモデルを送り直す
            _ready = false;
            AppLog.Warn(L.F("Log_Preview_PostFailed", e.Message));
        }
    }

    private void OnItemsChanged()
    {
        ModelButtons.Children.Clear();
        var items = Items ?? [];
        var glbs = items.Where(i => i.Kind == "glb" && File.Exists(i.Path)).ToList();
        // 正面の印のチェックボックスは 3D のモデルがあるときだけ出す（モデルの選択の行の高さを今までと同じにする）。
        // 隠している間は印も出さない（チェックの状態は残し、また出したときに印を戻す）
        var markVis = glbs.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        if (FrontMarkCheck.Visibility != markVis)
        {
            FrontMarkCheck.Visibility = markVis;
            if (FrontMarkCheck.IsChecked == true) SendFrontMark();
        }
        RadioButton? first = null;
        foreach (var it in glbs)
        {
            var rb = new RadioButton { Content = it.Label, Tag = it, Margin = new Thickness(0, 0, 14, 0), GroupName = "pv" + GetHashCode() };
            System.Windows.Automation.AutomationProperties.SetAutomationId(rb, "PreviewModel_" + it.Key);
            rb.Checked += (_, _) => Load(it);
            // 選択中のボタンを押し直したとき（Checked は起きない）は、障害のために止めた読み込みだけをやり直す
            rb.Click += (_, _) => { if (ReferenceEquals(_current, it) && (_holdCurrent || _stopped)) Load(it); };
            ModelButtons.Children.Add(rb);
            first ??= rb;
        }
        PlaceFrontMark();
        SetImage(CheckImage, CheckEmpty, items.FirstOrDefault(i => i.Kind == "check")?.Path);
        SetImage(CandImage, CandEmpty, items.FirstOrDefault(i => i.Kind == "candidates")?.Path);
        if (first != null)
        {
            var keep = _current != null ? ModelButtons.Children.OfType<RadioButton>().FirstOrDefault(r => ((PreviewItem)r.Tag).Key == _current.Key) : null;
            _current = null;
            (keep ?? first).IsChecked = true;
        }
        else
        {
            _current = null;
            _holdCurrent = false;
            Post(new JsonObject { ["cmd"] = "clear" });
            StatusText.Text = L.T("Preview_MouseHint");
        }
    }

    public void SelectModel(string key)
    {
        var rb = ModelButtons.Children.OfType<RadioButton>().FirstOrDefault(r => ((PreviewItem)r.Tag).Key == key);
        if (rb != null) rb.IsChecked = true;
    }

    /// <summary>モデルを表示する。resend は読み直し・作り直しの後の 'ready' での送り直し（利用者の操作ではない）。</summary>
    private void Load(PreviewItem it, bool resend = false)
    {
        _current = it;
        if (!resend)
        {
            // モデルが選ばれた（押し直された）: 障害の回数を数え直し、送り直しの保留と自動の回復の停止を解く
            _failures = 0;
            _holdCurrent = false;
            if (_stopped && IsVisible) _ = InitAsync();   // 開き直した後の 'ready' で今のモデルを送る
        }
        var url = ToUrl(it.Path);
        if (url == null)
        {
            StatusText.Text = L.F("Preview_LoadError", it.Path);
            return;
        }
        StatusText.Text = L.T("Preview_Loading");
        Post(new JsonObject { ["cmd"] = "load", ["url"] = url, ["label"] = it.Label, ["keepGround"] = it.KeepGround });
    }

    private static void SetImage(Image img, TextBlock empty, string? path)
    {
        // ファイルのストリームから読む（new Uri(path) は %41 のような並びを復号して別の名前を開くため）
        var bi = path != null && File.Exists(path) ? ImageFiles.Load(path) : null;
        img.Source = bi;
        empty.Visibility = bi != null ? Visibility.Collapsed : Visibility.Visible;
    }

    private void OnView(object sender, RoutedEventArgs e) =>
        Post(new JsonObject { ["cmd"] = "view", ["dir"] = (string)((Button)sender).Tag });

    private void OnTexture(object sender, RoutedEventArgs e) =>
        Post(new JsonObject { ["cmd"] = "texture", ["on"] = TextureCheck.IsChecked == true });

    private void OnWire(object sender, RoutedEventArgs e) =>
        Post(new JsonObject { ["cmd"] = "wireframe", ["on"] = WireCheck.IsChecked == true });

    /// <summary>正面（+Z）の印を出すか（SF23、2026-10-17 U-F）。チェックがあり、チェックボックスを出している（3D のモデルがある）とき。</summary>
    private bool FrontMarkOn => FrontMarkCheck.IsChecked == true && FrontMarkCheck.Visibility == Visibility.Visible;

    /// <summary>正面（+Z）の印の切り替え。まだページの準備ができていなければ 'ready' の init で送る。</summary>
    private void OnFrontMark(object sender, RoutedEventArgs e) => SendFrontMark();

    private void SendFrontMark() => Post(new JsonObject { ["cmd"] = "frontMarker", ["on"] = FrontMarkOn });

    private void OnRowSize(object sender, SizeChangedEventArgs e) => PlaceFrontMark();

    /// <summary>
    /// 正面の印のチェックボックスの置き場所（UF-R1、2026-10-17）。モデルの選択の行の右の端に、その行の行数を増やさずに
    /// 入るならそこに置き、入らなければ向きのボタンの行の最後に置く（その行の最後の行に空きがあれば行は増えない）。
    /// どちらの行も行が増えなければ、3D の表示の位置と高さはこの機能の前と同じ。決め方は行の幅と各要素の幅だけによるので、
    /// 置き直して行の高さが変わっても（SizeChanged がまた来ても）同じ結果になり、繰り返さない。
    /// </summary>
    private void PlaceFrontMark()
    {
        if (FrontMarkCheck.Visibility != Visibility.Visible) return;
        double avail = ModelRow.ActualWidth;
        if (avail <= 0) return;   // まだ配置されていない（最初の配置の SizeChanged で決める）
        var inf = new Size(double.PositiveInfinity, double.PositiveInfinity);
        FrontMarkCheck.Measure(inf);
        var cb = FrontMarkCheck.DesiredSize;   // 余白を含む
        var radios = ModelButtons.Children.OfType<UIElement>().ToList();
        foreach (var r in radios) r.Measure(inf);
        var widths = radios.Select(r => r.DesiredSize.Width).ToList();
        bool inModelRow = radios.Count > 0
            && WrapLineCount(widths, avail - cb.Width) == WrapLineCount(widths, avail)
            && cb.Height <= radios.Max(r => r.DesiredSize.Height) + 0.5;
        Panel target = inModelRow ? ModelRow : ViewRow;
        if (ReferenceEquals(FrontMarkCheck.Parent, target)) return;
        (FrontMarkCheck.Parent as Panel)?.Children.Remove(FrontMarkCheck);
        target.Children.Add(FrontMarkCheck);   // ModelRow では Grid.Column="1"（右の端）。WrapPanel では Grid.Column は効かない
    }

    /// <summary>WrapPanel（横）と同じ折り返しで、幅 widths の要素を幅 avail に並べたときの行数。</summary>
    private static int WrapLineCount(IReadOnlyList<double> widths, double avail)
    {
        int lines = 0;
        double x = 0;
        foreach (var w in widths)
        {
            if (lines == 0 || x + w > avail + 1e-6) { lines++; x = w; }
            else x += w;
        }
        return lines;
    }
}
