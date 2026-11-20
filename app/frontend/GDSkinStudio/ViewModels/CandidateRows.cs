using System.Text.Json.Nodes;
using System.Windows.Input;
using GDSkinStudio.Localization;
using GDSkinStudio.Services;

namespace GDSkinStudio.ViewModels;

/// <summary>
/// 候補の一覧の 1 行（プレビューの「候補」のタブ。2026-10-17 追加機能 SF06・SF14、U-A2）。下書きの要約の candidates.items から作る。
/// 「この候補で作る」は今の体以外の、空でない候補に出す。生成結果が保存してあれば 3D 生成なしで、無ければそのシードで生成し直す。
/// </summary>
public sealed class CandidateRow
{
    public long Seed { get; init; }
    public string Title { get; init; } = "";
    public string Detail { get; init; } = "";
    public bool IsCurrent { get; init; }
    public bool IsAuto { get; init; }
    public bool HasSample { get; init; }
    public bool CanPick { get; init; }
    public ICommand? PickCommand { get; init; }
    public string PickText => L.T("UA2_PickButton");
    /// <summary>読み上げの名前（ボタンが何をするか。行の見出しではない）。</summary>
    public string PickName => IsCurrent ? L.F("UA2_PickNameReconvert", Seed) : L.F("UA2_PickName", Seed);
    public string PickToolTip => IsCurrent ? L.T("UA2_PickTipReconvert")
        : HasSample ? L.T("UA2_PickTipSaved") : L.T("UA2_PickTipRegenerate");
    public string PickId => $"Cand_Pick_{Seed}";
    public string RowId => $"Cand_Row_{Seed}";
    public string DetailId => $"Cand_Detail_{Seed}";

    /// <summary>
    /// 下書きの要約の candidates → 行（候補の記録が無ければ空）。pick は「この候補で作る」の処理（シードを渡す）。
    /// draftFailed（変換の途中で止まった・失敗した下書き）のときは、今の体の行にも出す（変換し直す）。
    /// </summary>
    public static IReadOnlyList<CandidateRow> Build(JsonObject? candidates, ICommand pick, bool draftFailed = false)
    {
        var list = new List<CandidateRow>();
        if (candidates == null) return list;
        foreach (var it in candidates.Arr("items"))
        {
            if (it.Long("seed") is not long seed) continue;
            bool current = it.Bool("current"), auto = it.Bool("auto"), sample = it.Bool("sample");
            bool selectable = it.Bool("selectable", true);
            var marks = new List<string>();
            if (auto) marks.Add(L.T("UA2_CandAuto"));
            if (current) marks.Add(L.T("UA2_CandCurrent"));
            var title = marks.Count > 0 ? L.F("Common_ParenFormat", L.F("Gen_DraftSeed", seed), string.Join(L.T("Common_ListSepDot"), marks))
                                        : L.F("Gen_DraftSeed", seed);
            var parts = new List<string>();
            if (!selectable) parts.Add(L.T("UA2_CandEmpty"));
            else
            {
                if (it.Dbl("arm_angle_deg") is double a) parts.Add(L.F("Gen_DraftArm", a));
                else parts.Add(L.T("UA2_CandNoArm"));
                // 候補の図から測った色の付き方（B-040。バックエンドの candidate_colors。判定できない候補は何も出さない）
                if (it.Str("color") == "colored") parts.Add(L.T("UA2_CandColored"));
                else if (it.Str("color") == "colorless") parts.Add(L.T("UA2_CandColorless"));
                if (it.Bool("weapon_suspected")) parts.Add(L.T("UA2_CandWeapon"));
                var reasons = it.Strings("excluded").Select(Reason).Distinct().ToList();
                if (reasons.Count > 0) parts.Add(L.F("UA2_CandExcluded", string.Join(L.T("Common_ListSepDot"), reasons)));
                parts.Add(sample ? L.T("UA2_CandSaved") : current ? L.T("UA2_CandNotSavedCurrent") : L.T("UA2_CandNotSaved"));
            }
            list.Add(new CandidateRow
            {
                Seed = seed, Title = title, Detail = string.Join(L.T("Common_Gap"), parts), IsCurrent = current, IsAuto = auto,
                HasSample = sample, CanPick = selectable && (!current || draftFailed), PickCommand = pick,
            });
        }
        return list;
    }

    /// <summary>生成スクリプトの除外の理由（gen_helpers.exclusions。英語）→ 画面の文。</summary>
    private static string Reason(string r) =>
        r.StartsWith("weapon-like object") ? L.T("UA2_ReasonWeapon")
        : r.StartsWith("no reliable arm estimate") ? L.T("UA2_ReasonNoArm")
        : r.StartsWith("empty result") ? L.T("UA2_CandEmpty")
        : r;
}
