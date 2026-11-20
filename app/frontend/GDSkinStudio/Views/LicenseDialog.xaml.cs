using System.Text;
using System.Text.Json.Nodes;
using System.Windows;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;

namespace GDSkinStudio.Views;

/// <summary>ライセンスの全文（GET /setup/license/{id} の結果）を表示する。</summary>
public partial class LicenseDialog : Window
{
    public LicenseDialog(JsonNode t)
    {
        InitializeComponent();
        Title = L.F("License_Title", t.Str("name"));
        NameText.Text = t.Str("name");
        LicenseText.Text = L.F("License_Name", t.Str("license"));
        SourceText.Text = L.F("License_Source", t.Obj("source").Str("name"), t.Obj("source").Str("url"));
        var links = t.Strings("links").ToList();
        LinksText.Text = links.Count == 0 ? "" : L.F("License_Links", string.Join("  ", links));
        var flags = t.Arr("flags").Select(f => f.Str("text") ?? "").Where(s => s.Length > 0).ToList();
        if (flags.Count > 0)
        {
            FlagsText.Text = string.Join("\n", flags);
            FlagsBox.Visibility = Visibility.Visible;
        }
        var sb = new StringBuilder();
        foreach (var f in t.Arr("files"))
        {
            sb.Append("==== ").Append(f.Str("file")).Append(" ====").Append('\n');
            sb.Append((f.Str("text") ?? "").Replace("\r\n", "\n").Replace("\n", Environment.NewLine)).Append(Environment.NewLine).Append(Environment.NewLine);
        }
        BodyText.Text = sb.ToString();
    }

    /// <summary>ファイルの文章をそのまま表示する（ツール自体の LICENSE、THIRD_PARTY_NOTICES）。</summary>
    public LicenseDialog(string title, string heading, string sub, string body)
    {
        InitializeComponent();
        Title = title;
        NameText.Text = heading;
        LicenseText.Text = sub;
        BodyText.Text = body.Replace("\r\n", "\n").Replace("\n", Environment.NewLine);
    }

    public static void ShowText(string title, string heading, string sub, string body) => ShowWindow(new LicenseDialog(title, heading, sub, body));

    private void OnClose(object sender, RoutedEventArgs e) => Close();

    public static void Show(JsonNode t) => ShowWindow(new LicenseDialog(t));

    private static void ShowWindow(LicenseDialog d)
    {
        var owner = Application.Current?.Windows.OfType<Window>().FirstOrDefault(w => w.IsActive) ?? Application.Current?.MainWindow;
        if (owner != null && owner != d && owner.IsLoaded) d.Owner = owner;
        else d.WindowStartupLocation = WindowStartupLocation.CenterScreen;
        // 固定の高さ（720 DIP）は、表示するモニターの作業領域に収める（作業領域の低い画面で上下にはみ出さないように。
        // 2026-10-20 レビュー R17A-03。TrashDialog と同じ）
        d.Loaded += (_, _) => WorkArea.KeepInside(d);
        d.ShowDialog();
    }
}
