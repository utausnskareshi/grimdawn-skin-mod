using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using GDSkinStudio.Localization;
using GDSkinStudio.ViewModels;

namespace GDSkinStudio.Views;

/// <summary>
/// 削除した装備（trash）の一覧（2026-10-17 機能 SF02）。選んだ 1 件を返すだけで、元に戻す処理は呼び出し側（LibraryViewModel）が行う。
/// 戻せない項目（同じ内部IDの装備がある・レコードの重なり・消耗品の持ち主が無いなど）は理由を表示し、「元に戻す」を押せない。
/// </summary>
public partial class TrashDialog : Window
{
    public TrashEntry? Picked { get; private set; }

    public TrashDialog(IReadOnlyList<TrashEntry> entries)
    {
        InitializeComponent();
        TrashList.ItemsSource = entries;
        EmptyText.Visibility = entries.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        TrashList.SelectionChanged += (_, _) => Update();
        TrashList.SelectedItem = entries.FirstOrDefault(e => e.Restorable) ?? entries.FirstOrDefault();
        Update();
        Loaded += (_, _) => WorkArea.KeepInside(this);
    }

    private TrashEntry? Current => TrashList.SelectedItem as TrashEntry;

    private void Update()
    {
        var e = Current;
        RestoreButton.IsEnabled = e?.Restorable == true;
        SelectedNote.Text = e == null ? "" : e.Restorable ? L.F("LibC_TrashSelected", e.Title) : L.T("LibC_TrashSelectedCannot");
    }

    private void OnRestore(object sender, RoutedEventArgs e)
    {
        if (Current is not { Restorable: true } c) return;
        Picked = c;
        DialogResult = true;
    }

    /// <summary>戻せる項目を左ボタンでダブルクリックしたときだけ戻す（スクロールバー・空白・右ボタンでは戻さない。R19B-03）。</summary>
    private void OnItemDoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (e.ChangedButton != MouseButton.Left || sender is not ListViewItem { DataContext: TrashEntry { Restorable: true } entry }) return;
        TrashList.SelectedItem = entry;
        e.Handled = true;
        OnRestore(sender, e);
    }

    private void OnClose(object sender, RoutedEventArgs e) => DialogResult = false;

    /// <summary>一覧を表示し、「元に戻す」を押した項目を返す（閉じた・取り消したときは null）。</summary>
    public static TrashEntry? Pick(IReadOnlyList<TrashEntry> entries)
    {
        var owner = Application.Current?.Windows.OfType<Window>().FirstOrDefault(w => w.IsActive) ?? Application.Current?.MainWindow;
        var d = new TrashDialog(entries);
        if (owner != null && owner.IsLoaded && owner != d) d.Owner = owner;
        else d.WindowStartupLocation = WindowStartupLocation.CenterScreen;
        return d.ShowDialog() == true ? d.Picked : null;
    }
}
