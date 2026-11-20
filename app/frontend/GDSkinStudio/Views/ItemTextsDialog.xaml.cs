using System.Windows;
using System.Windows.Controls;
using GDSkinStudio.Localization;
using GDSkinStudio.ViewModels;

namespace GDSkinStudio.Views;

/// <summary>
/// 消耗品（イリュージョン）の名前と説明文の編集（2026-10-17 機能 SF11）。欄は今の上書き（無ければ空欄）で始まり、各欄の下に
/// 空欄のときに使う既定の文を出す。保存は呼び出し側の save（PUT /library/&lt;id&gt;/texts）で行い、失敗（使えない文字など）の
/// ときはエラーを表示して、入力を残したまま開いておく。
/// </summary>
public partial class ItemTextsDialog : Window
{
    private readonly Func<IReadOnlyDictionary<(string Role, string Lang), string>, Task> _save;
    private readonly Dictionary<(string Role, string Lang), TextBox> _boxes;

    public ItemTextsDialog(EquipmentItem item, Func<IReadOnlyDictionary<(string Role, string Lang), string>, Task> save)
    {
        InitializeComponent();
        _save = save;
        TitleText.Text = L.F("LibC_TextsFor", item.DisplayName);
        _boxes = new()
        {
            [("set", "en")] = SetEn, [("set", "ja")] = SetJa, [("set_desc", "en")] = DescEn, [("set_desc", "ja")] = DescJa,
        };
        foreach (var ((role, lang), box) in _boxes) box.Text = item.ItemText(role, lang) ?? "";
        string Def(string role, string lang) => L.F("LibC_TextsDefault", item.ItemTextDefault(role, lang) ?? "?");
        SetEnDefault.Text = Def("set", "en");
        SetJaDefault.Text = Def("set", "ja") + "\n" + L.T("LibC_TextsJaFallback");
        DescEnDefault.Text = Def("set_desc", "en");
        DescJaDefault.Text = Def("set_desc", "ja") + "\n" + L.T("LibC_TextsJaFallback");
        Loaded += (_, _) => { WorkArea.KeepInside(this); SetEn.Focus(); };
    }

    private void OnClear(object sender, RoutedEventArgs e)
    {
        foreach (var box in _boxes.Values) box.Text = "";
    }

    private bool _saving;

    /// <summary>保存の応答を待つ間は、タイトルバーの × や Alt+F4 でも閉じない（応答の前に閉じると、保存は済んでいるのに
    /// 一覧の表示が古いまま残り、閉じた窓に結果を設定してエラーになるので。U-C-R3）。</summary>
    protected override void OnClosing(System.ComponentModel.CancelEventArgs e)
    {
        if (_saving) e.Cancel = true;
        base.OnClosing(e);
    }

    private async void OnSave(object sender, RoutedEventArgs e)
    {
        var values = _boxes.ToDictionary(kv => kv.Key, kv => kv.Value.Text ?? "");
        _saving = true;
        SaveButton.IsEnabled = CancelButton.IsEnabled = false;
        try
        {
            await _save(values);
            _saving = false;
            if (IsVisible) DialogResult = true;
        }
        catch (Exception ex) { _saving = false; App.ShowError(ex); }          // 使えない文字・長すぎるなど。入力は残す
        finally
        {
            _saving = false;
            if (IsLoaded) SaveButton.IsEnabled = CancelButton.IsEnabled = true;
        }
    }

    private void OnCancel(object sender, RoutedEventArgs e) => DialogResult = false;

    /// <summary>編集のダイアログを開く。保存して閉じたら true（保存できたかは、呼び出し側が save の結果でも確かめる）。</summary>
    public static bool Edit(EquipmentItem item, Func<IReadOnlyDictionary<(string Role, string Lang), string>, Task> save)
    {
        var owner = Application.Current?.Windows.OfType<Window>().FirstOrDefault(w => w.IsActive) ?? Application.Current?.MainWindow;
        var d = new ItemTextsDialog(item, save);
        if (owner != null && owner.IsLoaded && owner != d) d.Owner = owner;
        else d.WindowStartupLocation = WindowStartupLocation.CenterScreen;
        d.MaxHeight = Math.Max(320, Math.Min(d.MaxHeight, WorkArea.Of(d.Owner).Height - 40));
        return d.ShowDialog() == true;
    }
}
