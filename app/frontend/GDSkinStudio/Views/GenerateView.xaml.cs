using System.Windows.Controls;
using GDSkinStudio.ViewModels;

namespace GDSkinStudio.Views;

public partial class GenerateView : UserControl
{
    public GenerateView()
    {
        InitializeComponent();
        DataContextChanged += (_, e) =>
        {
            if (e.OldValue is GenerateViewModel o) o.PreviewSelectRequested -= OnPreviewSelect;
            if (e.NewValue is GenerateViewModel n) n.PreviewSelectRequested += OnPreviewSelect;
        };
    }

    private void OnPreviewSelect(string key) => Preview.SelectModel(key);
}
