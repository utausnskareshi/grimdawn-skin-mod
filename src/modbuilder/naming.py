"""レコードのパス・タグ名・アセットの置き場所の規則と、試作MOD 1〜3 で確かめた定数。

装備ごとの名前は内部ID（:func:`new_equipment_id`）だけから決める（表示名は使わない。名前が重複しても衝突しない）。
決めた名前は装備の作成時に ``equipment.json`` の ``naming`` に固定して保存する。規則を将来変えても既存の装備の
レコードのパスは変わらない（ゲーム内の収集（transmutes）はレコードのパスで覚えているため、変わると収集が壊れる [推定]）。

規則（scheme ``id-v1``。``<id>`` は ``eq`` + 小文字の16進 10 桁）:

  頭装備レコード        records/items/aiskin/<id>/head.dbr
  イリュージョン消耗品  records/items/aiskin/<id>/illusion_set.dbr
  アセット              items/aiskin/<id>/<id>_m.msh, <id>_f.msh, <id>_dif.tex（mods/Items.arc の aiskin/<id>/）
  タグ                  tagAISkin_<id>_Head, tagAISkin_<id>_Set, tagAISkin_<id>_SetDesc
                        （ビルドのときに set_tag から tagAISkin_<id>_SetName も作る。:func:`set_name_tag`）

共通の透明レコード（胴・脚・手・足）は試作MOD 1 と同じパス・タグを使う（ModTest の収集をそのまま使えるように）。
"""
from __future__ import annotations

import re
import secrets

from ._i18n import tr

__all__ = [
    "SCHEME", "ID_RE", "new_equipment_id", "derive_naming", "set_name_tag", "is_valid_tag", "is_valid_record_path",
    "NAMESPACE", "INVISIBLE_SLOTS", "INVISIBLE_RECORDS", "OFFICIAL_INVISIBLE_SHOULDERS", "OFFICIAL_INVISIBLE_MEDAL",
    "HEAD_INVISIBLE_M", "HEAD_INVISIBLE_F", "SYSTEM_INVISIBLE_MESH", "MERCHANT_RECORDS", "TEMPLATE_HEAD",
    "TEMPLATE_SET", "TEMPLATE_INVISIBLE", "DB_PRIORITY", "HEAD_TEXTURE_FIELDS", "PLAYER_SKELETONS", "ITEM_COST",
    "INVISIBLE_TAGS_JA", "ICON_SET", "ICON_HEAD", "ICON_INVISIBLE", "ICON_INVISIBLE_MEDAL", "ICON_PREFIX", "LEGACY_BITMAPS",
    "INVISIBLE_MEDAL_RECORD", "INVISIBLE_SOURCE", "INVISIBLE_ICON", "OFFICIAL_INVISIBLE_TEX", "INVISIBLE_LOOK_PREFIX",
    "INVISIBLE_LOOK_TEX", "INVISIBLE_LOOK_MSH", "INVISIBLE_LOOK_MSH_TEXTURE", "INVISIBLE_LOOK_RECORDS", "INVISIBLE_LOOK_FIELDS",
    "LEGACY_LOOK_FIELDS",
]

SCHEME = "id-v1"
ID_RE = re.compile(r"^eq[0-9a-f]{10}$")
NAMESPACE = "aiskin"                       # records/items/aiskin/、Items.arc の aiskin/
TAG_RE = re.compile(r"^tag[A-Za-z0-9_]+$")
RECORD_RE = re.compile(r"^records/[a-z0-9_/&.-]+\.dbr$")

# --- 試作MOD 1〜3 で使い、ゲーム内で確かめた値 ---------------------------------------------
# 雛形（gdx1 の公式レコード。最優先版を差分コピーする）
TEMPLATE_HEAD = "records/items/transmutes/invisible_head.dbr"            # armor_head.tpl（公式の透明ヘルム）
TEMPLATE_SET = "records/items/transmutes/transmute_invisible_head.dbr"   # itemtransmuterset.tpl
TEMPLATE_INVISIBLE = "records/items/transmutes/invisible_shoulders.dbr"  # 透明レコードの雛形（胴・脚・手・足・肩）
# 2026-11-11 までは、肩とメダルは公式の透明レコードをそのまま消耗品に入れていた（以前の版の MOD を受け取ったときは認める）。
# 今は肩・メダルも自前の透明レコード（下の INVISIBLE_SLOTS）にして、MOD のアイコンを付ける（利用者の確認で肩のアイコンが
# 公式のままだったため。D-078）。メダルは、全身のイリュージョンで装備中のメダル（バッジ）が生成モデルの胸に重なって出ない
# ように入れている（2026-10-17 利用者の判断 C125。メダルを防具と同じ消耗品に混ぜた公式の例は無く、ゲーム内では未確認）
OFFICIAL_INVISIBLE_SHOULDERS = TEMPLATE_INVISIBLE
OFFICIAL_INVISIBLE_MEDAL = "records/items/transmutes/invisible_medal.dbr"   # jewelry_medal.tpl（自前の透明のメダルの雛形）
# headMale/FemaleMesh: 骨格とアタッチポイント（武器の手など）を持つ公式の透明プレイヤーメッシュ。
# system/meshes/invisible01.msh を入れると武器が足元に出て移動方向も壊れる（試作MOD 1、2026-10-16 ゲーム内で確認）
HEAD_INVISIBLE_M = "creatures/pc/hero_00_invisible.msh"
HEAD_INVISIBLE_F = "creatures/pc/heroine_00_invisible.msh"
SYSTEM_INVISIBLE_MESH = "system/meshes/invisible01.msh"                 # 胴・脚・手・足の透明レコード（armor*Mesh 用）
# MOD のアイテムのアイコン（2026-11-11。利用者の依頼「ひとめでMODだとわかるような画像」。modbuilder.icons がビルドのときに
# 描いて Items.arc の aiskin/icons/ に入れる。ゲーム内での見え方は未確認）
ICON_PREFIX = "items/aiskin/icons/"
ICON_SET = ICON_PREFIX + "illusion.tex"          # 補給官で買う消耗品（itemtransmuterset の fullBitmap）
ICON_HEAD = ICON_PREFIX + "head.tex"             # 頭装備（armor_head の bitmap。幻術師のイリュージョンの一覧）
ICON_INVISIBLE = ICON_PREFIX + "invisible.tex"   # 透明の胴・脚・手・足・肩（bitmap。64x64）
ICON_INVISIBLE_MEDAL = ICON_PREFIX + "invisible_medal.tex"   # 透明のメダル（bitmap。公式のメダルのアイコンと同じ 32x32）
# 以前の版が書いた値（公式のアイコン）。受け取った MOD の確かめでは、今のアイコンと同じく認める。set・invisible・medal は
# 雛形の公式レコードの値のまま（2026-11-11 にこの PC のゲームデータで確認）
LEGACY_BITMAPS = {"head": "items/geartorso/bitmaps/a00_torso001.tex",
                  "set": "items/misc/bitmaps/invisible_head.tex",
                  "invisible": "items/transmutes/shoulders_invisible01a.tex",
                  "medal": "items/transmutes/medal_invisible01a.tex"}
ITEM_COST = "10"                                                         # 0 だと補給官の在庫に出なかった（試作MOD 1）
HEAD_TEXTURE_FIELDS = ("armorMaleBaseTexture", "armorMaleBumpTexture", "armorFemaleBaseTexture", "armorFemaleBumpTexture")
# 透明レコード: (部位, テンプレート, Class, レコード, タグ, 英語の表示名)
INVISIBLE_SLOTS = [
    ("chest", "armor_chest.tpl", "ArmorProtective_Chest", "records/items/aiskin/invisible_chest.dbr",
     "tagAISkinInvisibleChest", "AI Skin: Invisible Chest"),
    ("legs", "armor_legs.tpl", "ArmorProtective_Legs", "records/items/aiskin/invisible_legs.dbr",
     "tagAISkinInvisibleLegs", "AI Skin: Invisible Legs"),
    ("hands", "armor_hands.tpl", "ArmorProtective_Hands", "records/items/aiskin/invisible_hands.dbr",
     "tagAISkinInvisibleHands", "AI Skin: Invisible Hands"),
    ("feet", "armor_feet.tpl", "ArmorProtective_Feet", "records/items/aiskin/invisible_feet.dbr",
     "tagAISkinInvisibleFeet", "AI Skin: Invisible Feet"),
    # 肩・メダル（2026-11-11 から自前のレコード。D-078）。メダルの雛形は公式の透明のメダル（下の INVISIBLE_SOURCE）
    ("shoulders", "armor_shoulders.tpl", "ArmorProtective_Shoulders", "records/items/aiskin/invisible_shoulders.dbr",
     "tagAISkinInvisibleShoulders", "AI Skin: Invisible Shoulders"),
    ("medal", "jewelry_medal.tpl", "ArmorJewelry_Medal", "records/items/aiskin/invisible_medal.dbr",
     "tagAISkinInvisibleMedal", "AI Skin: Invisible Medal"),
]
INVISIBLE_RECORDS = [s[3] for s in INVISIBLE_SLOTS]
INVISIBLE_MEDAL_RECORD = "records/items/aiskin/invisible_medal.dbr"
# 透明レコードの雛形（公式レコード）。メダルだけ公式の透明のメダル、ほかは公式の透明の肩
INVISIBLE_SOURCE = {s[3]: (OFFICIAL_INVISIBLE_MEDAL if s[0] == "medal" else TEMPLATE_INVISIBLE) for s in INVISIBLE_SLOTS}
# 透明レコードのアイコン（メダルは小さい欄に合わせた 32x32）
INVISIBLE_ICON = {s[3]: (ICON_INVISIBLE_MEDAL if s[0] == "medal" else ICON_INVISIBLE) for s in INVISIBLE_SLOTS}
# 自前の透明の見た目（2026-11-11。D-079）: 自前の透明の肩・メダルは、公式の透明と同じ見た目で、パスだけが違うメッシュと画像を
# 使う。公式の透明の肩・メダル（同じメッシュと画像）を収集済みのセーブでは、自前の肩・メダルが消耗品を使っても収集に入らなかった
# （利用者のゲーム内の確認とセーブの記録。ゲームが同じ見た目のイリュージョンを足さないためと見ている [推定]）。
# 公式の透明: メッシュ system/meshes/invisible01.msh（貼る画像 System/textures/invisible.tex）、画像は全透明の 32x32
OFFICIAL_INVISIBLE_TEX = "system/textures/invisible.tex"
INVISIBLE_LOOK_PREFIX = "items/aiskin/invisible/"                      # アプリが作るファイルの置き場所（装備のファイルは置けない）
INVISIBLE_LOOK_TEX = INVISIBLE_LOOK_PREFIX + "invisible.tex"           # 全透明の 32x32（アプリが描く。公式の画像と同じバイト列）
INVISIBLE_LOOK_MSH = INVISIBLE_LOOK_PREFIX + "invisible.msh"           # 公式の透明メッシュの貼る画像を上の画像に替えたもの
INVISIBLE_LOOK_MSH_TEXTURE = "Items/aiskin/invisible/invisible.tex"    # メッシュのマテリアルに書くパス（装備の .msh と同じ書き方）
# 自前の見た目を使う透明レコード（肩・メダル。胴・脚・手・足は公式に同じ見た目のものが無いので今までどおり）と、変える項目
INVISIBLE_LOOK_RECORDS = [s[3] for s in INVISIBLE_SLOTS if s[0] in ("shoulders", "medal")]
INVISIBLE_LOOK_FIELDS = {"mesh": INVISIBLE_LOOK_MSH, "armorMaleMesh": INVISIBLE_LOOK_MSH,
                         "armorFemaleMesh": INVISIBLE_LOOK_MSH, "baseTexture": INVISIBLE_LOOK_TEX}
# 以前の版（D-079 の前）が書いた値（公式の透明のメッシュと画像）。受け取った MOD の確かめでは今の値と同じく認める
LEGACY_LOOK_FIELDS = {"mesh": SYSTEM_INVISIBLE_MESH, "armorMaleMesh": SYSTEM_INVISIBLE_MESH,
                      "armorFemaleMesh": SYSTEM_INVISIBLE_MESH, "baseTexture": OFFICIAL_INVISIBLE_TEX}
# 日本語名は公式の訳語にそろえる（公式は「見えないヘルム」「見えないショルダーガード」「見えないメダル」（2026-11-11 にゲームの
# Text_JA で確認）、部位は (胴) (脚) (手) (足)。2026-10-17 点検 C74。変わるのは Text_JA だけで、database.arz は変わらない）。
# ゲームのデータなので、画面の表示言語（tr）によらず日本語のまま
INVISIBLE_TAGS_JA = {
    "tagAISkinInvisibleChest": "AI Skin: 見えない胴",
    "tagAISkinInvisibleLegs": "AI Skin: 見えない脚",
    "tagAISkinInvisibleHands": "AI Skin: 見えない手",
    "tagAISkinInvisibleFeet": "AI Skin: 見えない足",
    "tagAISkinInvisibleShoulders": "AI Skin: 見えないショルダーガード",
    "tagAISkinInvisibleMedal": "AI Skin: 見えないメダル",
}
# Devil's Crossing 補給官の 4 ティア。固定在庫はティアごとに独立（上位の評判で下位ティアの在庫は出ない。
# 試作MOD 1、2026-10-16 ゲーム内で確認）なので全ティアに追加する。
MERCHANT_RECORDS = ["records/creatures/npcs/merchants/factiontables/devilscrossing_%s_01.dbr" % t
                    for t in ("friendly", "respected", "honored", "revered")]
# 公式データベースの優先順（後から読まれるものが先）。差分コピーの雛形は最優先版を使う
DB_PRIORITY = ["gdx3", "gdx2", "gdx1", "base"]
# ボーン名の照合に使うプレイヤー骨格（男性 56 本 / 女性 49 本）
PLAYER_SKELETONS = {"m": "creatures/pc/hero_00.msh", "f": "creatures/pc/heroine_00.msh"}


def new_equipment_id(existing: set[str] | None = None) -> str:
    """``eq`` + 16進 10 桁（約 1.1 兆通り）。既存の ID と重ならないものを返す。"""
    existing = existing or set()
    while True:
        i = "eq" + secrets.token_hex(5)
        if i not in existing:
            return i


def derive_naming(equipment_id: str) -> dict:
    """内部ID → レコードのパス・タグ名・アセットのフォルダー（scheme id-v1）。"""
    if not ID_RE.match(equipment_id):
        raise ValueError(tr(f"装備IDの形式が不正: {equipment_id!r}（eq + 小文字の16進 10 桁）",
                            f"Invalid equipment ID format: {equipment_id!r} (eq + 10 lowercase hex digits)"))
    base = f"records/items/{NAMESPACE}/{equipment_id}/"
    return {
        "scheme": SCHEME,
        "head_record": base + "head.dbr",
        "set_record": base + "illusion_set.dbr",
        "head_tag": f"tagAISkin_{equipment_id}_Head",
        "set_tag": f"tagAISkin_{equipment_id}_Set",
        "set_desc_tag": f"tagAISkin_{equipment_id}_SetDesc",
        "asset_dir": f"items/{NAMESPACE}/{equipment_id}",
        "asset_prefix": equipment_id,
    }


def set_name_tag(set_tag: str) -> str:
    """消耗品の transmuteTag に入れる、接頭辞（Illusion: ）の無い名前のタグ（消耗品の名前タグ set_tag から導く。
    equipment.json の naming には保存しない）。公式の消耗品は transmuteTag に接頭辞の無い名前を入れ、使ったときの通知
    は「Illusion: 」を付けて出す見込みなので、接頭辞付きの set_tag を入れると「Illusion: Illusion: …」と重なる
    （2026-10-17 点検 C73。通知の文はゲーム内で未確認）。例: tagAISkin_eq0123456789_Set → tagAISkin_eq0123456789_SetName"""
    return set_tag + "Name"


def is_valid_tag(tag: str) -> bool:
    return bool(TAG_RE.match(tag))


def is_valid_record_path(path: str) -> bool:
    return bool(RECORD_RE.match(path))
