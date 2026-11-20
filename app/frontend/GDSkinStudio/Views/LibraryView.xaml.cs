using System.Windows.Controls;
using GDSkinStudio.ViewModels;

namespace GDSkinStudio.Views;

public partial class LibraryView : UserControl
{
    public LibraryView()
    {
        InitializeComponent();
    }

    /// <summary>複数選択（Ctrl・Shift + クリック）を ViewModel に伝える（装備パッケージの書き出しに使う）。</summary>
    private void OnSelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (DataContext is LibraryViewModel vm && sender is ListView lv)
            vm.SetMultiSelection(lv.SelectedItems.OfType<EquipmentItem>().ToList());
    }
}
