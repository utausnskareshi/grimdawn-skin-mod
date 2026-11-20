using System.ComponentModel;
using System.Windows.Data;
using System.Windows.Input;
using GDSkinStudio.Help;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;

namespace GDSkinStudio.ViewModels;

/// <summary>目次の 1 項目（検索中は見つかった数も出す）。</summary>
public sealed class HelpSectionItem(HelpSection section) : ObservableObject
{
    public HelpSection Section { get; } = section;
    public string Id => Section.Id;
    public string Title => Section.Title;

    private int _matches;
    public int Matches
    {
        get => _matches;
        set
        {
            if (!Set(ref _matches, value)) return;
            OnPropertyChanged(nameof(MatchText));
            OnPropertyChanged(nameof(HasMatches));
        }
    }
    public bool HasMatches => Matches > 0;
    public string MatchText => Matches > 0 ? L.F("Help_TocMatches", Matches) : "";
}

/// <summary>
/// ヘルプの画面: EXE に埋め込んだヘルプ（表示言語のもの。Help\help_ja.md・help_en.md）の目次・本文・検索。
/// バックエンドを使わないので、バックエンドが動いていなくても読める。
/// </summary>
public sealed class HelpViewModel : ObservableObject
{
    public IReadOnlyList<HelpSectionItem> Sections { get; }
    public ICollectionView SectionsView { get; }
    public string? LoadError { get; }
    public ICommand ClearSearchCommand { get; }

    private HelpSectionItem? _selected;
    public HelpSectionItem? SelectedSection
    {
        get => _selected;
        set
        {
            // 絞り込みで目次から外れたときに ListBox が null にするのは受け付けない（ApplySearch が選び直す）
            if (value == null && Sections.Count > 0) return;
            Set(ref _selected, value);
        }
    }

    private string _searchText = "";
    /// <summary>検索の欄（画面のバインディングが入力の後に少し待ってから渡す）。</summary>
    public string SearchText
    {
        get => _searchText;
        set
        {
            if (!Set(ref _searchText, value ?? "")) return;
            var q = _searchText.Trim();
            if (q == _query) return;
            _query = q;
            ApplySearch();
            OnPropertyChanged(nameof(Query));
        }
    }

    private string _query = "";
    /// <summary>本文の強調に使う語（前後の空白を除いた検索の欄。空なら強調しない）。</summary>
    public string Query => _query;

    private string _searchStatus = "";
    public string SearchStatus { get => _searchStatus; private set => Set(ref _searchStatus, value); }

    public HelpViewModel() : this(Load(out var error), error) { }

    /// <summary>読み込んだヘルプで作る（doc が null なら読み込みの失敗。error を画面に出す）。</summary>
    public HelpViewModel(HelpDocument? doc, string? error)
    {
        LoadError = error;
        Sections = doc?.Sections.Select(s => new HelpSectionItem(s)).ToList() ?? [];
        SectionsView = new CollectionViewSource { Source = Sections }.View;
        SectionsView.Filter = o => _query.Length == 0 || o is HelpSectionItem { Matches: > 0 };
        _selected = Sections.FirstOrDefault();
        ClearSearchCommand = new RelayCommand(() => SearchText = "", () => SearchText.Length > 0);
    }

    private static HelpDocument? Load(out string? error)
    {
        try
        {
            error = null;
            return HelpDocument.Load(L.Language);
        }
        catch (Exception e)
        {
            var why = e is HelpFormatException f ? L.F("Help_FormatError", f.Line, f.Message) : e.Message;
            AppLog.Error(L.F("Log_App_HelpLoadFailed", why));
            error = L.F("Help_LoadFailed", why);
            return null;
        }
    }

    /// <summary>リンクの先の項目を開く。検索で目次から外れている項目なら、検索を解いてから開く。</summary>
    public void Navigate(string id)
    {
        var target = Sections.FirstOrDefault(s => s.Id == id);
        if (target == null) return;
        if (_query.Length > 0 && target.Matches == 0) SearchText = "";
        SelectedSection = target;
    }

    private void ApplySearch()
    {
        int found = 0;
        foreach (var s in Sections)
        {
            s.Matches = HelpSearch.Count(s.Section.PlainText, _query);
            if (s.Matches > 0) found++;
        }
        SectionsView.Refresh();
        if (_query.Length == 0) SearchStatus = "";
        else SearchStatus = found == 0 ? L.T("Help_SearchNone") : L.F("Help_SearchFound", found);
        // 今の項目が見つからなかったら、見つかった最初の項目に移る（無ければ今のまま。本文には強調が出ない）
        if (_query.Length > 0 && found > 0 && _selected is not { Matches: > 0 })
            SelectedSection = Sections.First(s => s.Matches > 0);
        // 絞り込みの間に ListBox が外した選択を、目次に戻った項目で選び直させる
        OnPropertyChanged(nameof(SelectedSection));
        CommandManager.InvalidateRequerySuggested();
    }
}
