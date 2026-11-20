using System.Text.Json.Nodes;
using System.Windows.Input;
using System.Windows.Threading;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;
using GDSkinStudio.Views;

namespace GDSkinStudio.ViewModels;

/// <summary>
/// テキスト入力の補助（SF07。2026-10-17 U-A4）: ひな形の例文、生成 AI（CLIP）が読む長さ（77 トークン）の目安、英語以外の語・
/// 全角の英数字の注意。生成の処理・生成に渡す文章は変えない（ひな形は利用者が「文章に入れる」を押したときだけ文章を変える）。
/// トークン数はバックエンドの POST /prompt/estimate（C17 の prompt_fit と同じ「武器なし」の足し方・切り方。CLIP を取得して
/// 同意済みなら取得済みのトークナイザーで正確に、それ以外は概算）。
/// </summary>
public sealed partial class GenerateViewModel
{
    // --- ひな形 ----------------------------------------------------------------------------------------------
    /// <summary>ひな形（cond = 条件だけを前に付ける。他は例文で置き換える）。例文は docs/trellis_text_and_samples.md 2 章の検証済みの文。</summary>
    public IReadOnlyList<Choice<string>> PromptTemplates { get; } =
    [
        new("cond", L.T("UA4_TplCond")), new("knight", L.T("UA4_TplKnight")), new("mage", L.T("UA4_TplMage")),
        new("barbarian", L.T("UA4_TplBarbarian")), new("winged", L.T("UA4_TplWinged")),
    ];
    private string _selectedPromptTemplate = "cond";
    public string SelectedPromptTemplate { get => _selectedPromptTemplate; set => Set(ref _selectedPromptTemplate, value ?? "cond"); }
    private ICommand? _insertTemplateCommand;
    public ICommand InsertTemplateCommand => _insertTemplateCommand ??= new RelayCommand(InsertTemplate, () => !IsBusy);

    /// <summary>ひな形の英語の文（cond は条件だけ）。</summary>
    internal static string TemplateText(string id)
    {
        var prefix = L.T("UA4_TplPrefix");
        string? desc = id switch
        {
            "knight" => L.T("UA4_TplDescKnight"), "mage" => L.T("UA4_TplDescMage"),
            "barbarian" => L.T("UA4_TplDescBarbarian"), "winged" => L.T("UA4_TplDescWinged"), _ => null,
        };
        return desc == null ? prefix : prefix + ", " + desc;
    }

    private void InsertTemplate()
    {
        var id = SelectedPromptTemplate;
        var current = PromptText.Trim();
        if (id == "cond")
        {
            var prefix = TemplateText("cond");
            if (current.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return;     // もう付いている
            PromptText = current.Length == 0 ? prefix + ", " : prefix + ", " + current;
            AppLog.Info(L.T("Log_Gen_TemplateConditions"));
            return;
        }
        var text = TemplateText(id);
        if (current == text) return;
        if (current.Length > 0 && !Dialogs.Confirm(L.T("UA4_TplReplaceTitle"), L.T("UA4_TplReplaceMessage"), L.T("UA4_TplReplace"),
                                                   kind: DialogKind.Warning)) return;
        PromptText = text;
        AppLog.Info(L.F("Log_Gen_TemplateExample", id));
    }

    // --- 生成 AI が読む長さ（トークン数）の目安 ---------------------------------------------------------------------------
    private DispatcherTimer? _estimateTimer;
    private int _estimateSeq;
    private JsonNode? _estimate;

    /// <summary>文章・「武器あり」・同意が変わったら、少し待ってから数え直す（入力の 1 文字ごとには問い合わせない）。</summary>
    private void SchedulePromptEstimate()
    {
        if (_estimateTimer == null)
        {
            _estimateTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(350) };
            _estimateTimer.Tick += async (_, _) => { _estimateTimer!.Stop(); await EstimatePromptAsync(); };
        }
        _estimateTimer.Stop();
        if (string.IsNullOrWhiteSpace(PromptText))
        {
            ++_estimateSeq;
            SetEstimate(null);
            return;
        }
        _estimateTimer.Start();
    }

    private async Task EstimatePromptAsync()
    {
        int seq = ++_estimateSeq;
        var text = PromptText;
        if (string.IsNullOrWhiteSpace(text)) { SetEstimate(null); return; }
        if (!_main.IsReady) return;                 // つながった後（InitAsync の同意の確認）でもう一度
        try
        {
            var r = await _main.RequireClient().PostAsync("prompt/estimate",
                new JsonObject { ["text"] = text, ["allow_weapons"] = AllowWeapons });
            if (seq == _estimateSeq) SetEstimate(r);
        }
        catch (ApiException) { if (seq == _estimateSeq) SetEstimate(null); }      // 長すぎる文章など（生成の開始で知らせる）
        catch (Exception) { }                                                       // 接続の切断など
    }

    private void SetEstimate(JsonNode? r)
    {
        _estimate = r;
        OnPropertyChanged(nameof(PromptTokenText)); OnPropertyChanged(nameof(PromptTokenWarning)); OnPropertyChanged(nameof(PromptForeignNote));
    }

    /// <summary>「生成 AI が読む長さ: 47 / 77 トークン」（概算のときは「約」）。</summary>
    public string? PromptTokenText
    {
        get
        {
            var r = _estimate;
            if (r == null || (r.Long("tokens") ?? 0) == 0) return null;
            long n = r.Long("tokens") ?? 0, lim = r.Long("limit") ?? 77;
            var head = r.Bool("exact") ? L.F("UA4_Tokens", n, lim)
                : r.Str("reason") is "no_consent" or "missing" or "no_root" ? L.F("UA4_TokensApproxSetup", n, lim) : L.F("UA4_TokensApprox", n, lim);
            return r.Bool("suffix_added") ? head + L.T("UA4_TokensSuffix") : head;
        }
    }

    /// <summary>長すぎて後ろが読まれないときの注意（読まれない部分の先頭を見せる）。</summary>
    public string? PromptTokenWarning
    {
        get
        {
            var r = _estimate;
            if (r == null || !r.Bool("truncated")) return null;
            long lim = r.Long("limit") ?? 77;
            var dropped = r.Str("dropped_text");
            var part = string.IsNullOrEmpty(dropped) ? L.T("UA4_DroppedUnknown")
                : L.F("Common_QuoteFormat", dropped.Length > 40 ? dropped[..40] + "…" : dropped);
            return r.Bool("suffix_added")
                ? L.F("UA4_TooLongSuffix", r.Long("with_suffix") ?? r.Long("original") ?? 0, lim, part)
                : L.F("UA4_TooLong", r.Long("original") ?? 0, lim, part);
        }
    }

    /// <summary>
    /// 英語以外の語の注意（止めない）。英語以外の文字が多い文章は既存の注意（PromptNotice、2 割以上）を出すので、ここでは
    /// 少しだけ混じるとき（例: a knight with 赤いマント）に語を示す。全角の英数字・記号（U+FF01〜FF5E。（）～ なども CLIP は
    /// 半角とは別の文字として読む）は、割合にかかわらず半角に直す案内を出す（UA4-R1: 文言は「英数字・記号」）。
    /// </summary>
    public string? PromptForeignNote
    {
        get
        {
            var parts = new List<string>();
            var words = _estimate.Strings("foreign_words").ToList();
            if (PromptNotice == null && words.Count > 0 && !string.IsNullOrWhiteSpace(PromptText))
                parts.Add(L.F("UA4_ForeignWords", string.Join(L.T("Common_ListSep"), words)));
            if (PromptText.Any(c => c is >= '！' and <= '～')) parts.Add(L.T("UA4_FullWidth"));
            return parts.Count == 0 ? null : string.Join(" ", parts);
        }
    }
}
