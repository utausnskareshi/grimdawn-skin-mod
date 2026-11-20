using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Threading;
using GDSkinStudio.Help;
using GDSkinStudio.ViewModels;

namespace GDSkinStudio.Views;

public partial class HelpView : UserControl
{
    private HelpViewModel? _vm;
    private bool _renderQueued;
    private string? _shownId;       // 最後に表示した項目の id（項目が変わったら先頭から表示する）
    private Window? _window;        // Ctrl+F を受ける窓

    public HelpView()
    {
        InitializeComponent();
        DataContextChanged += (_, e) =>
        {
            if (_vm != null) _vm.PropertyChanged -= OnVmChanged;
            _vm = e.NewValue as HelpViewModel;
            if (_vm != null) _vm.PropertyChanged += OnVmChanged;
            QueueRender();
        };
        // Ctrl+F: 検索の欄へ。ヘルプの画面を表示している間は、フォーカスが左のナビ・窓・本文にあっても移るように窓で先に受ける
        // （以前はこの画面の KeyBinding だけで、フォーカスが画面の外では効かず、本文では FlowDocumentScrollViewer が Ctrl+F で
        // WPF の検索のツールバーを開いた）。ほかの画面では何もしない（キーを受け取らない）
        Loaded += (_, _) =>
        {
            if (_window != null) _window.PreviewKeyDown -= OnWindowPreviewKeyDown;
            _window = Window.GetWindow(this);
            if (_window != null) _window.PreviewKeyDown += OnWindowPreviewKeyDown;
        };
        // 本文の表示部品の「検索」のコマンド（Ctrl+F）も検索の欄へ（WPF の検索のツールバーを開かせない）
        Viewer.CommandBindings.Add(new CommandBinding(ApplicationCommands.Find, (_, e) =>
        {
            FocusSearch();
            e.Handled = true;
        }));
    }

    private void OnWindowPreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (!IsVisible || e.Key != Key.F || e.KeyboardDevice.Modifiers != ModifierKeys.Control) return;
        FocusSearch();
        e.Handled = true;
    }

    private void FocusSearch()
    {
        SearchBox.Focus();
        SearchBox.SelectAll();
    }

    private void OnVmChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(HelpViewModel.SelectedSection) or nameof(HelpViewModel.Query)) QueueRender();
    }

    /// <summary>項目の切り替えと検索の語の変更が続けて来たら、まとめて 1 回だけ作り直す。</summary>
    private void QueueRender()
    {
        if (_renderQueued) return;
        _renderQueued = true;
        Dispatcher.BeginInvoke(DispatcherPriority.DataBind, () =>
        {
            _renderQueued = false;
            Render();
        });
    }

    private void Render()
    {
        var section = _vm?.SelectedSection?.Section;
        if (_vm == null || section == null)
        {
            Viewer.Document = new FlowDocument();
            _shownId = null;
            return;
        }
        var vm = _vm;
        bool changed = section.Id != _shownId;
        _shownId = section.Id;
        Viewer.Document = HelpRenderer.Build(section, vm.Query, id => vm.Navigate(id), this, out var marks);
        // 項目が変わったら先頭から表示する（FlowDocumentScrollViewer は文書を替えてもスクロールの位置を保つので、目次やリンクで
        // 移ると前の項目の位置のまま途中や末尾が出て、見出しが見えなかった）。同じ項目で検索の語だけが変わったときは今の位置のまま。
        // 先頭に戻すのはスクロールの部品の次の配置で済むので、下の強調へのスクロール（配置の後）より前になる
        if (changed) (Viewer.Template?.FindName("PART_ContentHost", Viewer) as ScrollViewer)?.ScrollToHome();
        // 検索の語があれば最初の 1 つまでスクロールする
        if (marks.Count > 0)
            Dispatcher.BeginInvoke(DispatcherPriority.Loaded, () => marks[0].BringIntoView());
    }
}
