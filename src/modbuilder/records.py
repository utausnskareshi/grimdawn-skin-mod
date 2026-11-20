"""DBR レコードと表示名タグの生成（試作MOD 1〜3 の make_records.py の一般化）。

ライブラリの全装備から、次を作る:

* 装備ごとの頭装備レコード（armor_head.tpl）: 雛形は公式の透明ヘルム ``invisible_head.dbr``（gdx1）。
  armorMaleMesh / mesh = 男性用メッシュ、armorFemaleMesh = 女性用メッシュ、armor*Base/BumpTexture は削除
  （メッシュ内蔵のマテリアルを使う。``assets.head_textures`` があればそれを入れる）、headMale/FemaleMesh = 公式の
  透明プレイヤーメッシュ、bitmap = MOD の頭装備のアイコン（:mod:`modbuilder.icons`。2026-11-11 まで公式の胴のアイコン）、
  castsShadows 1、itemLevel 0、levelRequirement 0、itemNameTag。
* 消耗品ごとのイリュージョン消耗品（itemtransmuterset.tpl）: 雛形は ``transmute_invisible_head.dbr``（gdx1）。
  transmuteDbrs = 頭装備（消耗品を共有する装備はすべて）+（include_invisible なら）自前の透明な胴・脚・手・足・肩・メダル
  （メダルは 2026-10-17 利用者の判断 C125。ゲーム内では未確認。2026-11-11 まで肩・メダルは公式の透明レコードだった。D-078）。
  description = 消耗品の名前タグ（「Illusion: <名前>」）、transmuteTag = 接頭辞の無い名前のタグ
  （:func:`~modbuilder.naming.set_name_tag`。公式と同じ形。点検 C73）、itemText = 説明タグ、itemCost 10、
  fullBitmap = MOD の消耗品のアイコン（2026-11-11 まで雛形の公式のアイコン）。
* 共通の透明レコード（胴・脚・手・足・肩・メダル）: 雛形は公式 ``invisible_shoulders.dbr``（メダルは ``invisible_medal.dbr``）。
  templateName・Class・itemNameTag と、bitmap = MOD の透明の部位のアイコン（メダルは 32x32 の小さなアイコン）を変更。
  肩・メダルは mesh・armorMale/FemaleMesh・baseTexture も、公式の透明と同じ見た目の自前のファイル
  （:mod:`modbuilder.invisible_look`。2026-11-11 D-079）に替える。
* Devil's Crossing 補給官の全 4 ティアの差分コピー（最優先版の公式レコード）: 元の marketStaticItems を保持し、
  末尾に消耗品を作成順に追加。
* 表示名タグ: v1.3.1.1 の 13 言語（:data:`LANGUAGES`）すべての Text_XX を作る。値は Text_JA だけ日本語名（日本語名が
  無ければ英語）、ほかは英語（2026-10-17 利用者の判断 C124。英語以外の設定のゲームは、その言語の Text_XX.arc が無いと
  「Tag not found」になる見込みのため。日本語の設定で mods\\Text_JA.arc が読まれることは 2026-11-18 にゲーム内で確かめた）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ._i18n import tr
from .arz import norm_path, split_array
from .errors import ModBuilderError
from .library import Equipment
from .naming import (HEAD_INVISIBLE_F, HEAD_INVISIBLE_M, HEAD_TEXTURE_FIELDS, ICON_HEAD, ICON_SET, INVISIBLE_ICON,
                     INVISIBLE_LOOK_FIELDS, INVISIBLE_LOOK_RECORDS, INVISIBLE_RECORDS, INVISIBLE_SLOTS, INVISIBLE_SOURCE,
                     INVISIBLE_TAGS_JA, ITEM_COST, MERCHANT_RECORDS, TEMPLATE_HEAD, TEMPLATE_SET, set_name_tag)

__all__ = ["RecordPlan", "plan_records", "default_texts", "tags_file_text", "LANGUAGES", "LOCALIZED_LANGUAGES"]

# Grim Dawn v1.3.1.1 の言語（ゲームの resources\Text_*.arc の 13 個。2026-10-17 にこのPCで確認。localization\ は空）。
# ビルドする PC のゲームに依らず、常にこの 13 個の Text_XX.arc を作る（受け取り側の許可の一覧・決定的なビルドのため）
LANGUAGES = ("CS", "DE", "EN", "ES", "FR", "IT", "JA", "KO", "PL", "PT", "RU", "VI", "ZH")
LOCALIZED_LANGUAGES = ("JA",)       # 英語以外の表示名を入れられる言語（Text_JA の日本語名）
TAGS_FILE = "tags_aiskin_modbuilder.txt"
# 消耗品の名前（ゲームの Text_EN・Text_JA に入れる文）の接頭辞。ゲームのデータなので表示言語（tr）に依らない
SET_NAME_PREFIXES = ("Illusion: ", "イリュージョン: ")


@dataclass
class RecordPlan:
    records: dict[str, dict[str, str]] = field(default_factory=dict)     # レコード名 → {フィールド: 値}
    sources: dict[str, str] = field(default_factory=dict)                # レコード名 → 雛形 '<db>:<公式レコード>'
    owners: dict[str, list[str]] = field(default_factory=dict)           # レコード名 → 関係する装備ID
    tags: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # 言語 → [(タグ, 値)]（並び順を保つ）
    mesh_genders: dict[str, set[str]] = field(default_factory=dict)      # MOD のメッシュ → 参照する欄の性別
    set_records: list[str] = field(default_factory=list)                 # 補給官に並べる消耗品（順番どおり）
    equipment: list[str] = field(default_factory=list)


def _official(gd, rel: str) -> tuple[str, dict[str, str]]:
    got = gd.official_record(rel)
    if got is None:
        raise ModBuilderError(tr(f"雛形の公式レコードが見つかりません: {rel}（Ashes of Malmouth が必要）",
                                 f"The official base record was not found: {rel} (Ashes of Malmouth is required)"))
    db, fields = got
    return f"{db}:{rel}", dict(fields)


def _pick(eq: Equipment, role: str, lang: str, default_en: str, default_ja: str) -> str:
    """表示文: texts の上書き → 既定値。日本語の上書きも日本語名も無く、英語の上書きがあれば日本語にも英語を使う。"""
    if lang == "en":
        return eq.text(role, "en") or default_en
    v = eq.text(role, "ja")
    if v:
        return v
    if eq.text(role, "en") and not eq.name_ja:
        return eq.text(role, "en")
    return default_ja


def default_texts(owner: Equipment, members: list[Equipment], include_invisible: bool) -> dict:
    """消耗品の表示文 {役割: {言語: 文}}（説明文は公式の説明タグと同じく " で囲む）。"""
    # どちらの文もゲームの Text_XX に入れるゲームのデータなので、表示言語（tr）に依らず両方を作る（D-060）
    names_en = ", ".join(m.name_en for m in members)
    # 日本語の説明文は、公式の日本語の説明文と同じく文節の区切りに半角の空白を入れる。ゲームはツールチップの文を半角の空白の
    # 位置でしか折り返さず、空白の無い長い区間は 2 行目の途中（1 行に全角 31 字ほど）で「…」になって切れる（2026-11-18 の
    # ゲーム内の確認で、以前の「…マントの騎士。見えない胴・脚・手・足・肩・メダルも収集に加わります。幻術師で適用してください。」が
    # 「…収集に加わり…」で切れた。公式の Text_JA.arc の引用の説明文 410 件はすべて空白で区切られ、空白の無い区間は中央値 15 字）
    names_ja = "、 ".join(m.name_ja or m.name_en for m in members)
    # 日本語は公式の訳語（幻術師・見えない〜）にそろえる（2026-10-17 点検 C74）。メダルは C125
    extra_en = " Also unlocks invisible chest, legs, hands, feet, shoulders and medal." if include_invisible else ""
    extra_ja = "見えない胴・脚・手・足・肩・メダルも 収集に加わります。 " if include_invisible else ""
    d = {
        "set": (f"Illusion: {owner.name_en}", f"イリュージョン: {owner.name_ja or owner.name_en}"),
        "set_desc": (f'"AI Skin illusion for the head slot (full-body appearance): {names_en}.{extra_en} '
                     f'Apply them at the Illusionist."',
                     f'"AI Skin の イリュージョン（頭スロットに 全身の見た目）: {names_ja}。 {extra_ja}'
                     f'幻術師で 適用してください。"'),
    }
    out = {role: {lang: _pick(owner, role, lang, en, ja) for lang in ("en", "ja")} for role, (en, ja) in d.items()}
    # transmuteTag 用の接頭辞の無い名前（set の文から先頭の「Illusion: 」「イリュージョン: 」を外したもの。点検 C73）
    out["set_name"] = {lang: _strip_set_prefix(v) for lang, v in out["set"].items()}
    return out


def _strip_set_prefix(text: str) -> str:
    for p in SET_NAME_PREFIXES:
        if text.startswith(p) and text[len(p):].strip():
            return text[len(p):]
    return text


def head_text(eq: Equipment, lang: str) -> str:
    return _pick(eq, "head", lang, eq.name_en, eq.name_ja or eq.name_en)


def plan_records(gd, equipment: list[Equipment], languages=("EN", "JA")) -> RecordPlan:
    """ライブラリの装備（作成順）からレコードとタグを作る。

    タグは常に :data:`LANGUAGES` の 13 言語分作る。languages は表示名を訳す言語の指定で、"JA" を含めば Text_JA に
    日本語名（日本語名が無ければ英語）を入れ、含まなければ Text_JA も英語にする（2026-10-17 点検 C124）。"""
    if not equipment:
        raise ModBuilderError(tr("装備がありません（ライブラリに装備を追加してからビルドする）",
                                 "There is no equipment (add equipment to the Library before building)"))
    plan = RecordPlan()
    ids = {e.id for e in equipment}
    by_id = {e.id: e for e in equipment}
    langs = [l.upper() for l in languages]
    for l in langs:
        if l not in LANGUAGES:
            raise ModBuilderError(tr(f"未対応の言語: {l}（{', '.join(LANGUAGES)}）",
                                     f"Unsupported language: {l} (supported: {', '.join(LANGUAGES)})"))
    localized = {l for l in langs if l in LOCALIZED_LANGUAGES}
    tags = {l: [] for l in LANGUAGES}

    def add_tag(key: str, en: str, ja: str | None = None):
        # タグファイルは 1 行 1 タグ。検証・受け取り検査の parse_tags（str.splitlines）が区切りにする文字（U+2028 など）が
        # 値にあると行が割れ、どの装備が原因か分からない検証のエラーになるので、タグ名（装備IDを含む）を示して止める
        # （2026-10-20 レビュー R05B-03）
        for v in (en, ja):
            if v and "".join(v.splitlines()) != v:
                raise ModBuilderError(tr(f"タグ {key} の値に改行の文字があります（表示名・説明文を直してください）: {v!r}",
                                         f"The value of tag {key} contains a line break character (fix the name or "
                                         f"description): {v!r}"))
        for l in LANGUAGES:
            tags[l].append((key, (ja or en) if l in localized else en))

    # --- 共通の透明レコード ----------------------------------------------------------
    for slot, tpl, cls, rel, tag, en in INVISIBLE_SLOTS:
        src, f = _official(gd, INVISIBLE_SOURCE[rel])     # 透明の肩（メダルは透明のメダル）の公式レコード
        f["templateName"] = "database/templates/" + tpl
        f["Class"] = cls
        f["itemNameTag"] = tag
        f["bitmap"] = INVISIBLE_ICON[rel]   # 幻術師の一覧で MOD の品と分かるアイコン（2026-11-11）
        if rel in INVISIBLE_LOOK_RECORDS:
            # 肩・メダル: 公式の透明と同じ見た目で、パスだけが違う自前のメッシュと画像（D-079。modbuilder.invisible_look）
            f.update(INVISIBLE_LOOK_FIELDS)
        plan.records[rel] = f
        plan.sources[rel] = src
        plan.owners[rel] = []
        add_tag(tag, en, INVISIBLE_TAGS_JA.get(tag))

    # --- 装備ごとの頭装備レコード -----------------------------------------------------
    for eq in equipment:
        a = eq.data["assets"]
        if not a.get("male_mesh") or not a.get("female_mesh"):
            raise ModBuilderError(tr(f"装備 {eq.id}（{eq.name_en}）にメッシュがありません（変換結果を取り込んでいない）",
                                     f"Equipment {eq.id} ({eq.name_en}) has no mesh (the conversion result has not been "
                                     "imported)"))
        if eq.set_owner not in ids:
            raise ModBuilderError(tr(f"装備 {eq.id} の消耗品の持ち主 {eq.set_owner} がライブラリにありません",
                                     f"The consumable owner {eq.set_owner} of equipment {eq.id} is not in the Library"))
        n = eq.naming
        src, f = _official(gd, TEMPLATE_HEAD)
        # file_* の欄は compile で小文字・'/' 区切りになるので、作業フォルダーの .dbr にも正規化した値を書く（大文字や '\\'
        # があると往復検証 V1/V2 で原因の分かりにくい不一致になる。2026-10-17 点検 C78）
        f["armorMaleMesh"] = norm_path(a["male_mesh"])
        f["mesh"] = norm_path(a["male_mesh"])
        f["armorFemaleMesh"] = norm_path(a["female_mesh"])
        for k in HEAD_TEXTURE_FIELDS:
            f.pop(k, None)
        f.update({k: norm_path(v) for k, v in (a.get("head_textures") or {}).items()})
        f["headMaleMesh"] = HEAD_INVISIBLE_M
        f["headFemaleMesh"] = HEAD_INVISIBLE_F
        f["bitmap"] = ICON_HEAD             # 幻術師のイリュージョンの一覧のアイコン（2026-11-11 から MOD のアイコン）
        f["castsShadows"] = "1"
        f["itemNameTag"] = n["head_tag"]
        f["itemLevel"] = "0"
        f["levelRequirement"] = "0"
        rel = n["head_record"]
        if rel in plan.records:
            raise ModBuilderError(tr(f"レコードのパスが重なっています: {rel}", f"Duplicate record path: {rel}"))
        plan.records[rel] = f
        plan.sources[rel] = src
        plan.owners[rel] = [eq.id]
        add_tag(n["head_tag"], head_text(eq, "en"), head_text(eq, "ja"))
        for g, key in (("m", "male_mesh"), ("f", "female_mesh")):
            plan.mesh_genders.setdefault(norm_path(a[key]), set()).add(g)
        plan.equipment.append(eq.id)

    # --- 消耗品（作成順。共有する装備は持ち主の消耗品にまとめる） ---------------------------
    for owner in equipment:
        if not owner.owns_set:
            continue
        members = [e for e in equipment if e.set_owner == owner.id]
        include = bool(owner.data["illusion_set"].get("include_invisible", True))
        n = owner.naming
        if not n.get("set_record"):
            raise ModBuilderError(tr(f"装備 {owner.id} は消耗品の持ち主ですが、消耗品のレコード名がありません",
                                     f"Equipment {owner.id} owns a consumable but has no consumable record name"))
        src, f = _official(gd, TEMPLATE_SET)
        dbrs = [m.naming["head_record"] for m in members]
        if include:
            # 透明の胴・脚・手・足・肩・メダル（2026-11-11 から肩・メダルも自前のレコード。それまでは公式の透明の肩・メダル）
            dbrs += INVISIBLE_RECORDS
        f["transmuteDbrs"] = ";".join(dbrs)
        f["transmuteTag"] = set_name_tag(n["set_tag"])      # 接頭辞の無い名前（公式と同じ形。点検 C73）
        f["description"] = n["set_tag"]                     # 「Illusion: <名前>」（品物の名前）
        f["itemText"] = n["set_desc_tag"]
        f["itemCost"] = ITEM_COST
        f["fullBitmap"] = ICON_SET          # 補給官の品ぞろえ・持ち物のアイコン（2026-11-11 から MOD のアイコン）
        rel = n["set_record"]
        if rel in plan.records:
            raise ModBuilderError(tr(f"レコードのパスが重なっています: {rel}", f"Duplicate record path: {rel}"))
        plan.records[rel] = f
        plan.sources[rel] = src
        plan.owners[rel] = [m.id for m in members]
        plan.set_records.append(rel)
        t = default_texts(owner, members, include)
        add_tag(n["set_tag"], t["set"]["en"], t["set"]["ja"])
        add_tag(set_name_tag(n["set_tag"]), t["set_name"]["en"], t["set_name"]["ja"])
        add_tag(n["set_desc_tag"], t["set_desc"]["en"], t["set_desc"]["ja"])
    for e in equipment:
        if e.set_owner in by_id and not by_id[e.set_owner].owns_set:
            raise ModBuilderError(tr(f"装備 {e.id} の消耗品の持ち主 {e.set_owner} 自身が別の消耗品に属しています",
                                     f"The consumable owner {e.set_owner} of equipment {e.id} itself belongs to another "
                                     "consumable"))

    # --- 補給官の全ティア（差分コピー） ---------------------------------------------------------
    for rel in MERCHANT_RECORDS:
        src, f = _official(gd, rel)
        items = split_array(f.get("marketStaticItems", ""))
        for it in plan.set_records:
            if it not in items:
                items.append(it)
        f["marketStaticItems"] = ";".join(items)
        plan.records[rel] = f
        plan.sources[rel] = src
        plan.owners[rel] = list(plan.equipment)

    plan.tags = tags
    return plan


def tags_file_text(tags: list[tuple[str, str]], lang: str) -> str:
    """タグファイル（改行 CRLF、UTF-8 BOM なし。公式の Text_EN / Text_JA と同じ）。"""
    lines = [f"# AI Skin MOD (generated by ModBuilder). Language {lang}. Keys are referenced by records/items/aiskin/",
             ""]
    lines += [f"{k}={v}" for k, v in tags]
    return "\r\n".join(lines) + "\r\n"
