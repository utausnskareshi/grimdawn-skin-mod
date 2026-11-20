using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;

namespace GDSkinStudio.Views;

public enum DialogKind { Info, Warning, Error, Question }

public sealed class DialogSection
{
    public string Header { get; init; } = "";
    public IReadOnlyList<string> Items { get; init; } = [];
    public Brush Brush { get; init; } = (Brush)Application.Current.Resources["CardBorder"];
}

public sealed class DialogSpec
{
    public string Title { get; init; } = "";
    public string Message { get; init; } = "";
    public string? Hint { get; init; }
    public DialogKind Kind { get; init; } = DialogKind.Info;
    public IReadOnlyList<DialogSection> Sections { get; init; } = [];
    public string? CheckText { get; init; }
    public bool CheckDefault { get; init; }
    /// <summary>チェックを入れないと主ボタンを押せない（同意の確認など）。</summary>
    public bool CheckRequired { get; init; }
    public string? Detail { get; init; }
    public string PrimaryText { get; init; } = L.T("Dialog_OK");
    public string? SecondaryText { get; init; }
    public bool ShowOpenLogs { get; init; }
    public bool PrimaryEnabled { get; init; } = true;
}

public sealed record DialogOutcome(bool Accepted, bool Checked);

public partial class MessageDialog : Window
{
    private readonly DialogSpec _spec;
    public bool Accepted { get; private set; }

    public MessageDialog(DialogSpec spec)
    {
        InitializeComponent();
        _spec = spec;
        Title = spec.Title;
        TitleText.Text = spec.Title;
        MessageText.Text = spec.Message;
        (IconText.Text, IconText.Foreground) = spec.Kind switch
        {
            DialogKind.Error => ("", (Brush)FindResource("ErrBrush")),
            DialogKind.Warning => ("", (Brush)FindResource("WarnBrush")),
            DialogKind.Question => ("", (Brush)FindResource("Accent")),
            _ => ("", (Brush)FindResource("Accent")),
        };
        if (!string.IsNullOrWhiteSpace(spec.Hint))
        {
            HintText.Text = spec.Hint;
            HintBox.Visibility = Visibility.Visible;
        }
        SectionList.ItemsSource = spec.Sections;
        if (spec.CheckText != null)
        {
            CheckText.Text = spec.CheckText;
            Check.IsChecked = spec.CheckDefault;
            Check.Visibility = Visibility.Visible;
            Check.Checked += (_, _) => UpdatePrimary();
            Check.Unchecked += (_, _) => UpdatePrimary();
        }
        if (!string.IsNullOrWhiteSpace(spec.Detail))
        {
            DetailText.Text = spec.Detail;
            System.Windows.Automation.AutomationProperties.SetName(DetailText, L.T("Dialog_Details"));   // 読み上げ用の名前
            DetailExpander.Visibility = Visibility.Visible;
        }
        PrimaryButton.Content = spec.PrimaryText;
        if (spec.SecondaryText != null) SecondaryButton.Content = spec.SecondaryText;
        else SecondaryButton.Visibility = Visibility.Collapsed;
        LogsButton.Visibility = spec.ShowOpenLogs ? Visibility.Visible : Visibility.Collapsed;
        UpdatePrimary();
        // 中央に置いた結果が作業領域の上下にはみ出したら中に戻す（下のボタンがタスクバーに隠れないように）
        Loaded += (_, _) => WorkArea.KeepInside(this);
    }

    private void UpdatePrimary() =>
        PrimaryButton.IsEnabled = _spec.PrimaryEnabled && (!_spec.CheckRequired || Check.IsChecked == true);

    public bool IsChecked => Check.IsChecked == true;

    private void OnPrimary(object sender, RoutedEventArgs e)
    {
        Accepted = true;
        DialogResult = true;
    }

    private void OnSecondary(object sender, RoutedEventArgs e) => DialogResult = false;

    private void OnOpenLogs(object sender, RoutedEventArgs e) => Dialogs.OpenFolder(App.Paths.LogsDir);
}

/// <summary>モニターの作業領域（タスクバーを除いた範囲。窓の Left・Top と同じ DIP）。</summary>
public static class WorkArea
{
    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public int dwFlags; }

    private const int MONITOR_DEFAULTTONEAREST = 2;

    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromWindow(IntPtr hwnd, int flags);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO info);

    /// <summary>窓のあるモニターの作業領域。窓がまだ作られていないとき（null を含む）は主モニターの作業領域。</summary>
    public static Rect Of(Window? w)
    {
        var hwnd = w != null ? new WindowInteropHelper(w).Handle : IntPtr.Zero;
        if (hwnd != IntPtr.Zero)
        {
            var mi = new MONITORINFO { cbSize = Marshal.SizeOf<MONITORINFO>() };
            var mon = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
            if (mon != IntPtr.Zero && GetMonitorInfo(mon, ref mi))
            {
                var dpi = VisualTreeHelper.GetDpi(w!);   // 画素 → DIP（そのモニターの表示倍率）
                return new Rect(mi.rcWork.Left / dpi.DpiScaleX, mi.rcWork.Top / dpi.DpiScaleY,
                                (mi.rcWork.Right - mi.rcWork.Left) / dpi.DpiScaleX, (mi.rcWork.Bottom - mi.rcWork.Top) / dpi.DpiScaleY);
            }
        }
        return SystemParameters.WorkArea;
    }

    /// <summary>
    /// 窓の大きさと位置を、そのモニターの作業領域に収める。大きさの下限（MinWidth・MinHeight）も作業領域までに下げる。
    /// 大きさ（Width・Height）は最大化・最小化しているときも下げる（元に戻したときの大きさになる）。位置（Left・Top）は
    /// 通常の表示のときだけ直すので、最大化から戻したとき（StateChanged）にもう一度呼ぶ。
    /// SizeToContent の窓は今の大きさ（ActualWidth・ActualHeight）で位置だけを直す。
    /// </summary>
    public static void KeepInside(Window w)
    {
        var wa = Of(w);
        if (wa.Width <= 0 || wa.Height <= 0) return;
        if (w.MinWidth > wa.Width) w.MinWidth = wa.Width;
        if (w.MinHeight > wa.Height) w.MinHeight = wa.Height;
        if (!double.IsNaN(w.Width) && w.Width > wa.Width) w.Width = wa.Width;
        if (!double.IsNaN(w.Height) && w.Height > wa.Height) w.Height = wa.Height;
        if (w.WindowState != WindowState.Normal) return;
        double width = double.IsNaN(w.Width) ? w.ActualWidth : w.Width;
        double height = double.IsNaN(w.Height) ? w.ActualHeight : w.Height;
        if (!double.IsNaN(w.Left) && width > 0)
        {
            if (w.Left + width > wa.Right) w.Left = wa.Right - width;
            if (w.Left < wa.Left) w.Left = wa.Left;
        }
        if (!double.IsNaN(w.Top) && height > 0)
        {
            if (w.Top + height > wa.Bottom) w.Top = wa.Bottom - height;
            if (w.Top < wa.Top) w.Top = wa.Top;
        }
    }
}

public static class Dialogs
{
    public static DialogOutcome Show(DialogSpec spec)
    {
        var owner = Application.Current?.Windows.OfType<Window>().FirstOrDefault(w => w.IsActive) ?? Application.Current?.MainWindow;
        var d = new MessageDialog(spec);
        if (owner != null && owner.IsLoaded && owner != d) d.Owner = owner;
        else d.WindowStartupLocation = WindowStartupLocation.CenterScreen;
        // 最大の高さ（XAML の 820 DIP）は、表示するモニターの作業領域に収める。中身は ScrollViewer なのでボタンは残る
        d.MaxHeight = Math.Max(240, Math.Min(d.MaxHeight, WorkArea.Of(d.Owner).Height - 40));
        d.ShowDialog();
        return new DialogOutcome(d.Accepted, d.IsChecked);
    }

    public static void ShowError(UserError err)
    {
        AppLog.Write("ERROR", "app", $"[{err.Code}] {err.Title}: {err.Message}");
        Show(new DialogSpec
        {
            Kind = DialogKind.Error, Title = err.Title, Message = err.Message, Hint = err.Hint,
            Detail = (string.IsNullOrWhiteSpace(err.Detail) ? "" : err.Detail + "\n\n") + L.F("Dialog_ErrorCode", err.Code),
            ShowOpenLogs = true,
        });
    }

    public static void Info(string title, string message, string? hint = null) =>
        Show(new DialogSpec { Title = title, Message = message, Hint = hint });

    public static bool Confirm(string title, string message, string primary, string? hint = null, DialogKind kind = DialogKind.Question) =>
        Show(new DialogSpec { Title = title, Message = message, Hint = hint, Kind = kind, PrimaryText = primary,
                              SecondaryText = L.T("Dialog_Cancel") }).Accepted;

    public static void OpenFolder(string path)
    {
        try
        {
            System.IO.Directory.CreateDirectory(path);
            Process.Start(new ProcessStartInfo("explorer.exe", $"\"{path}\"") { UseShellExecute = true });
        }
        catch (Exception e) { AppLog.Warn(L.F("Log_App_OpenFolderFailed", e.Message)); }
    }
}
