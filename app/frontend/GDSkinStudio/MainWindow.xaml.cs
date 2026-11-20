using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using GDSkinStudio.ViewModels;

namespace GDSkinStudio;

public partial class MainWindow : Window
{
    private bool _closing;          // 確認とバックエンドの終了が済み、本当に閉じてよい
    private bool _closeInProgress;  // 確認・待ち・バックエンドの終了の途中（その間の閉じる操作は受け付けない）

    private bool _restoreMaximized; // 最小化の前が最大化だった（最小化したまま閉じたときに最大化を残すため）

    public MainWindow()
    {
        InitializeComponent();
    }

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        // 最大化で起動したとき（App が表示の前に WindowState を設定する）は StateChanged が出ないので、ここで覚える
        _restoreMaximized = WindowState == WindowState.Maximized;
    }

    /// <summary>
    /// 選択中の項目の Ctrl+クリック（Ctrl+Space、UI Automation の選択の解除）で左のナビの選択が外れたら、今のページの項目を
    /// 選び直す（MainViewModel.SelectedPage は null を受け付けないので、ページはそのまま。2026-10-20 レビュー R17A-02）。
    /// バインディングが SelectedItem を元の項目に戻しても、項目の選択の表示は外れたままになるので、項目の側を選び直す。
    /// </summary>
    private void OnNavSelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (sender is not ListBox lb || e.AddedItems.Count > 0) return;
        Dispatcher.BeginInvoke(() =>
        {
            if (lb.SelectedItems.Count == 0 && DataContext is MainViewModel main &&
                lb.ItemContainerGenerator.ContainerFromItem(main.SelectedPage) is ListBoxItem item)
                item.IsSelected = true;
        });
    }

    protected override void OnStateChanged(EventArgs e)
    {
        base.OnStateChanged(e);
        if (WindowState != WindowState.Minimized) _restoreMaximized = WindowState == WindowState.Maximized;
    }

    protected override async void OnClosing(CancelEventArgs e)
    {
        base.OnClosing(e);
        // Windows の終了では取り消しが無視され、await の後は実行されない（UI 設定の保存は App.OnSessionEnding が済ませた）
        if (_closing || App.IsSessionEnding) return;
        // 確認（ジョブの状態はバックエンドに問い合わせる）とバックエンドの終了が済んでから閉じる。await の前に取り消しておく
        e.Cancel = true;
        if (_closeInProgress) return;
        _closeInProgress = true;
        var main = (MainViewModel)DataContext;
        try
        {
            // 取り消せない処理（導入など）の実行中は、終わるのを待ってから閉じる（2026-10-17 監査 C99）
            var (close, force) = await main.PrepareCloseAsync(enabled => IsEnabled = enabled);
            if (!close)
            {
                IsEnabled = true;
                return;
            }
            CaptureBounds();
            App.Ui.Save(App.Paths.UiSettingsPath);
            IsEnabled = false;
            await main.ShutdownAsync(force);
            _closing = true;
            Application.Current.Shutdown();
        }
        catch (Exception ex)
        {
            // 確認の途中の想定外のエラーでも、閉じられない状態にはしない（次の閉じる操作で、もう一度確かめる）
            AppLog.Error(L.F("Log_App_CloseError", ex));
            IsEnabled = true;
        }
        finally
        {
            if (!_closing) _closeInProgress = false;
        }
    }

    /// <summary>窓の状態と通常の大きさ・位置を App.Ui に入れる（閉じるとき・Windows の終了のとき）。</summary>
    internal void CaptureBounds()
    {
        // 最小化したまま閉じたときは、最小化の前の状態を残す。通常の大きさと位置は、最大化・最小化しているときは
        // 元に戻したときの値（RestoreBounds）を残す（最大化中の Left・Top・Width・Height は最大化の値になるので使わない。
        // 以前は最小化で閉じると最大化を忘れ、最大化・最小化の前に変えた大きさも失った。2026-10-20 レビュー R17A-04）
        App.Ui.WindowMaximized = WindowState == WindowState.Maximized || (WindowState == WindowState.Minimized && _restoreMaximized);
        var rb = WindowState == WindowState.Normal ? new Rect(Left, Top, Width, Height) : RestoreBounds;
        if (!rb.IsEmpty && rb.Width > 0 && rb.Height > 0 && double.IsFinite(rb.Left) && double.IsFinite(rb.Top) &&
            double.IsFinite(rb.Width) && double.IsFinite(rb.Height))
        {
            App.Ui.WindowLeft = rb.Left;
            App.Ui.WindowTop = rb.Top;
            App.Ui.WindowWidth = rb.Width;
            App.Ui.WindowHeight = rb.Height;
        }
    }

    // --- Windows の終了を止める（App.OnSessionEnding。2026-10-27 R17B-02） ---------------------------------------------
    private bool _sessionEndBlocked;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ShutdownBlockReasonCreate(IntPtr hWnd, string pwszReason);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ShutdownBlockReasonDestroy(IntPtr hWnd);

    /// <summary>
    /// 実行中の取り消せないジョブが終わるまで、Windows の終了の画面に止めている理由を出す。終わったら理由を消す（その後の
    /// Windows の終了は止めない。アプリは閉じない）。
    /// </summary>
    internal async Task BlockSessionEndAsync(IReadOnlyList<string> jobs)
    {
        if (_sessionEndBlocked) return;
        _sessionEndBlocked = true;
        var hwnd = new WindowInteropHelper(this).Handle;
        try
        {
            ShutdownBlockReasonCreate(hwnd, L.F("App_SessionEndBlocked", string.Join(L.T("Common_ListSep"), jobs)));
            await ((MainViewModel)DataContext).WaitUncancellableForSessionEndAsync();
        }
        catch (Exception ex) { AppLog.Error(L.F("Log_App_CloseError", ex)); }
        finally
        {
            ShutdownBlockReasonDestroy(hwnd);
            _sessionEndBlocked = false;
            AppLog.Info(L.T("Log_App_SessionEndUnblocked"));
        }
    }
}
