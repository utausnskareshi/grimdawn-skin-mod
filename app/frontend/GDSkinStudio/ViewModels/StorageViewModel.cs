using System.Collections.ObjectModel;
using System.Text.Json.Nodes;
using System.Windows.Input;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using GDSkinStudio.Views;

namespace GDSkinStudio.ViewModels;

/// <summary>「データの容量と整理」の 1 行（項目・大きさ・整理で消せるもの）。ToString は UI Automation の項目の名前
/// （ItemsControl の項目は record の ToString として読み上げられるため。U-D1 のレビュー UD1-R6）。</summary>
public sealed record StorageRow(string Id, string Label, string SizeText, string Detail)
{
    public override string ToString() => string.Join(" ", new[] { Label, SizeText, Detail }.Where(s => !string.IsNullOrEmpty(s)));
}

/// <summary>
/// 設定画面の「データの容量と整理」（2026-10-17 機能 SF03。監査 C88・C104 の残り）。
/// 押したときだけ消す（自動では消さない）。バックエンドの storage/plan で消すもの・残すもの（理由つき）を確認のダイアログに
/// 出し、同意（チェック）の後に storage/cleanup へ確認した ID だけを送る。導入中・最新のビルド、アンインストールと
/// 「前の状態に戻す」が使う退避、表示中・処理中の下書きはバックエンドが残す。
/// </summary>
public sealed class StorageViewModel : ObservableObject
{
    private readonly MainViewModel _main;
    private bool _loaded;
    private const int MaxLines = 30;

    public StorageViewModel(MainViewModel main)
    {
        _main = main;
        RefreshCommand = new AsyncCommand(RefreshAsync, () => _main.IsReady && !Busy);
        CleanBuildsCommand = new AsyncCommand(() => CleanAsync("builds"), () => _main.IsReady && !Busy);
        CleanBackupsCommand = new AsyncCommand(() => CleanAsync("backups"), () => _main.IsReady && !Busy);
        CleanTrashCommand = new AsyncCommand(() => CleanAsync("trash"), () => _main.IsReady && !Busy);
        CleanInputsCommand = new AsyncCommand(() => CleanAsync("input_images"), () => _main.IsReady && !Busy);
        CleanDraftsCommand = new AsyncCommand(() => CleanAsync("drafts"), () => _main.IsReady && !Busy);
    }

    public ICommand RefreshCommand { get; }
    public ICommand CleanBuildsCommand { get; }
    public ICommand CleanBackupsCommand { get; }
    public ICommand CleanTrashCommand { get; }
    public ICommand CleanInputsCommand { get; }
    public ICommand CleanDraftsCommand { get; }

    public ObservableCollection<StorageRow> Rows { get; } = new();
    private string _summary = "";
    public string Summary { get => _summary; private set => Set(ref _summary, value); }
    private string _statusText = "";
    public string StatusText { get => _statusText; private set => Set(ref _statusText, value); }
    private bool _busy;
    public bool Busy { get => _busy; private set { if (Set(ref _busy, value)) CommandManager.InvalidateRequerySuggested(); } }

    private bool _expanded;
    /// <summary>開いたときに初めて容量を調べる（データフォルダーを数えるので、設定画面を開くたびには行わない）。</summary>
    public bool Expanded
    {
        get => _expanded;
        set
        {
            if (Set(ref _expanded, value) && value && !_loaded && _main.IsReady)
                RefreshCommand.Execute(null);
        }
    }

    private static string Fmt(long bytes) => SetupViewModel.Fmt(bytes);

    public async Task RefreshAsync()
    {
        Summary = L.T("UD1_Storage_Counting");
        JsonNode? u;
        try { u = await _main.RequireClient().GetAsync("storage/usage"); }
        catch
        {
            Summary = L.T("UD1_Storage_CountFailed");     // 「調べています…」のまま残さない（UD1-R5）。エラーは呼び出し元が出す
            throw;
        }
        var clean = u.Obj("cleanable");
        Rows.Clear();
        foreach (var c in u.Arr("categories"))
        {
            var id = c.Str("id") ?? "";
            var detail = "";
            if (clean.Obj(id) is JsonObject k)
                detail = k.Str("error") != null ? L.T("UD1_Storage_CleanUnknown")
                    : (k.Long("count") ?? 0) > 0 ? L.F("UD1_Storage_Cleanable", k.Long("count") ?? 0, Fmt(k.Long("bytes") ?? 0))
                    : L.T("UD1_Storage_NothingToClean");
            Rows.Add(new StorageRow(id, L.TryT("UD1_Cat_" + id) ?? id, Fmt(c.Long("bytes") ?? 0), detail));
        }
        foreach (var s in u.Arr("skipped"))
            Rows.Add(new StorageRow(s.Str("id") ?? "", L.TryT("UD1_Cat_" + s.Str("id")) ?? s.Str("id") ?? "", "—", L.T("UD1_Storage_TrellisSkipped")));
        Summary = L.F("UD1_Storage_Total", Fmt(u.Long("total_bytes") ?? 0), u.Long("free_bytes") is long f ? Fmt(f) : "—");
        _loaded = true;
    }

    /// <summary>計画の 1 項目の表示（種類ごと）。keep があれば残す理由を添える。</summary>
    private static string Line(string kind, JsonNode? i)
    {
        var size = Fmt(i.Long("bytes") ?? 0);
        var when = EquipmentItem.FormatTime(i.Str("created_at") ?? i.Str("removed_at"));
        // ビルド・退避の ID は作った日時（yyyyMMdd-HHmmss）なので、日時は重ねて出さない
        string text = kind switch
        {
            "builds" => L.F("UD1_Line_build", i.Str("id"), L.TryT("UD1_BuildStatus_" + i.Str("status")) ?? i.Str("status"), i.Long("equipment") ?? 0, size),
            "backups" => L.F("UD1_Line_backup", i.Str("id"), L.TryT("UD1_BackupReason_" + (i.Str("reason") ?? "").Split(' ')[0]) ?? i.Str("reason"),
                             i.Arr("files").Count),
            "trash" => L.F("UD1_Line_trash", i.Str("name") ?? i.Str("id"), when, size),
            "input_images" => L.F("UD1_Line_input", i.Str("name") ?? i.Str("id"), i.Long("files") ?? 0, size),
            // saved: 装備として保存済みで keep_drafts により残した下書き（R18A-02）
            _ => L.F("UD1_Line_draft", i.Str("id"), when,
                     i.Bool("saved") ? L.T("GX_DraftSaved")
                     : i.Bool("interrupted") ? L.T("UD1_DraftInterrupted") : L.F("UD1_DraftFailed", i.Str("error_code") ?? "-"), size),
        };
        return i.Str("keep") is string keep ? text + " — " + (L.TryT("UD1_Keep_" + keep) ?? keep) : text;
    }

    private static List<string> Lines(string kind, List<JsonNode?> items)
    {
        var lines = items.Take(MaxLines).Select(i => Line(kind, i)).ToList();
        if (items.Count > MaxLines) lines.Add(L.F("UD1_Storage_More", items.Count - MaxLines));
        return lines;
    }

    private JsonArray Exclude(string kind)
    {
        // 生成の画面に表示中の下書きは消さない（失敗した下書きを見ている途中など）
        var a = new JsonArray();
        if (kind == "drafts" && _main.Generate.Draft.Str("id") is string did) a.Add(did);
        // 生成の画面の画像の欄が指す保存済みの入力画像（再生成で library\equipment\<ID>\source\input_n.png を欄に戻している）の
        // 装備も消さない（消すと欄が無いファイルを指し、生成を始めたときに失敗する。UD1-R4）
        if (kind == "input_images")
            foreach (var id in _main.Generate.SlotPaths.Select(EquipmentIdOfInput).OfType<string>().Distinct()) a.Add(id);
        return a;
    }

    /// <summary>…\equipment\&lt;ID&gt;\source\&lt;ファイル&gt; の形のパスなら装備の ID、そうでなければ null。</summary>
    internal static string? EquipmentIdOfInput(string path)
    {
        try
        {
            var src = System.IO.Path.GetDirectoryName(System.IO.Path.GetFullPath(path));
            var eqDir = src == null ? null : System.IO.Path.GetDirectoryName(src);
            var root = eqDir == null ? null : System.IO.Path.GetDirectoryName(eqDir);
            if (src == null || eqDir == null || root == null) return null;
            if (!string.Equals(System.IO.Path.GetFileName(src), "source", StringComparison.OrdinalIgnoreCase)) return null;
            if (!string.Equals(System.IO.Path.GetFileName(root), "equipment", StringComparison.OrdinalIgnoreCase)) return null;
            var id = System.IO.Path.GetFileName(eqDir);
            return string.IsNullOrEmpty(id) ? null : id;
        }
        catch (Exception) { return null; }
    }

    private async Task CleanAsync(string kind)
    {
        var c = _main.RequireClient();
        var plan = await c.PostAsync("storage/plan", new JsonObject { ["kind"] = kind, ["exclude"] = Exclude(kind) });
        var items = plan.Arr("items").ToList();
        var remove = items.Where(i => i.Str("keep") == null).ToList();
        var kept = items.Where(i => i.Str("keep") != null).ToList();
        var title = L.T("UD1_Storage_Title_" + kind);
        if (remove.Count == 0)
        {
            var msg = L.T("UD1_Storage_Nothing_" + kind);
            if (kept.Count > 0) msg += "\n" + L.F("UD1_Storage_KeptCount", kept.Count);
            Dialogs.Info(title, msg, L.T("UD1_Storage_Hint_" + kind));
            return;
        }
        var sections = new List<DialogSection> { new() { Header = L.F("UD1_Storage_RemoveHeader", remove.Count), Items = Lines(kind, remove) } };
        if (kept.Count > 0) sections.Add(new() { Header = L.F("UD1_Storage_KeepHeader", kept.Count), Items = Lines(kind, kept) });
        var ok = Dialogs.Show(new DialogSpec
        {
            Kind = DialogKind.Warning, Title = title,
            Message = L.F("UD1_Storage_Confirm_" + kind, remove.Count, Fmt(plan.Long("bytes") ?? 0)),
            Hint = L.T("UD1_Storage_Hint_" + kind), Sections = sections,
            CheckText = L.T("UD1_Storage_Check"), CheckRequired = true,
            PrimaryText = L.T("UD1_Storage_Do"), SecondaryText = L.T("Dialog_Cancel"),
        });
        if (!(ok.Accepted && ok.Checked)) return;
        Busy = true;
        StatusText = L.T("UD1_Storage_Running");
        try
        {
            var ids = new JsonArray(remove.Select(i => (JsonNode?)JsonValue.Create(i.Str("id"))).ToArray());
            var r = await c.PostAsync("storage/cleanup", new JsonObject { ["kind"] = kind, ["items"] = ids, ["exclude"] = Exclude(kind) });
            var removed = r.Arr("removed").Count;
            var lines = new List<string> { L.F("UD1_Storage_Done", removed, Fmt(r.Long("freed_bytes") ?? 0)) };
            if (r.Arr("failed").Count > 0) lines.Add(L.F("UD1_Storage_Failed", r.Arr("failed").Count));
            if (r.Arr("skipped").Count > 0) lines.Add(L.F("UD1_Storage_Skipped", r.Arr("skipped").Count));
            StatusText = string.Join(" ", lines);
            AppLog.Info(L.F("Log_Storage_Removed", kind, removed));
            Dialogs.Info(L.T("UD1_Storage_DoneTitle"), string.Join("\n", lines));
        }
        catch (Exception e) { StatusText = L.T("UD1_Storage_FailedAll"); App.ShowError(e); }
        finally
        {
            Busy = false;
            try
            {
                await RefreshAsync();
                if (kind == "backups" || kind == "builds") await _main.Mod.RefreshAsync();
                if (kind == "trash" || kind == "input_images") await _main.Library.RefreshAsync();
            }
            catch (Exception e) { AppLog.Info(L.F("Log_Storage_ReloadFailed", e.Message)); }
        }
    }
}
