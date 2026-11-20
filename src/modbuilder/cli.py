"""ModBuilder のコマンドライン（``python -I scripts/modbuilder.py <コマンド>`` または ``python -m modbuilder``）。

  library init|list|show|add-converted|update-converted|rename|set-text|remove|import-prototype
  build      装備ライブラリの全装備から統合ビルドを作り、検証する
  verify     既存のビルドをもう一度検証する（ハッシュ・往復）
  compare    2 つのビルド（または .arz）をレコード単位で比べる
  detect     Grim Dawn のインストール先・起動オプション・ゲームの言語・mods/ の状態
  status     導入の記録と mods/ の状態
  install    ビルドを mods/ に導入する（--yes が無ければ確認のみ）
  uninstall  導入したファイルを取り除き、最初の導入前の状態に戻す（--yes が無ければ確認のみ）
  reinstall  導入済みのビルド（または指定のビルド）を入れ直す
  restore    退避（backups）の時点に戻す
  backups    退避の一覧（--prune で古い退避を消す。アンインストールと前の状態に戻すのに使う退避は残す）

終了コード: 0 成功（確認のみを含む）、1 検証・導入の失敗、2 指定の誤り。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import __version__, paths, steam
from .errors import LibraryError, ModBuilderError, ValidationFailed

__all__ = ["main"]


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _log(args):
    """進行状況の行の出し先。--json のときは標準エラーに出す（標準出力を JSON だけにする。2026-10-20 R08A-03）。"""
    if getattr(args, "json", False):
        return lambda *a: print(*a, file=sys.stderr)
    return print


def _pair(s: str) -> tuple[str, str]:
    """--rename・--value-map の「A=B」→ (A, B)。形が違えば argparse の指定の誤り（終了コード 2。2026-10-20 R08A-07）。"""
    a, sep, b = s.partition("=")
    if not sep or not a:
        raise argparse.ArgumentTypeError(f"A=B の形で指定してください: {s!r}")
    return a, b


def _nonneg_int(s: str) -> int:
    """backups の --keep。0 以上の整数だけを受け付ける（負の値は 0 と同じに扱われ「新しい -1 件は残す」と出て最新の
    退避まで消す対象になっていた。指定の誤りとして終了コード 2。R08A-06）。"""
    try:
        n = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"0 以上の整数を指定してください: {s!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"0 以上の整数を指定してください: {s!r}")
    return n


def _resolve_gd(args) -> str:
    gd = args.gd or os.environ.get("GRIMDAWN_DIR")
    if gd:
        return os.path.abspath(gd)
    found = steam.find_grim_dawn()
    if not found:
        raise ModBuilderError("Grim Dawn のインストール先が見つかりません（--gd で指定）")
    return found[0]["path"]


def _library(args, create=False):
    from .library import Library
    return Library.open(args.library, create=create)


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------
def cmd_library(args) -> int:
    from .importers import import_converted, import_prototype, replace_converted
    from .library import sha256_file
    if args.lcmd == "init":
        lib = _library(args, create=True)
        print(f"装備ライブラリ: {lib.root}")
        return 0
    lib = _library(args)
    if args.lcmd == "list":
        items = [e.summary() for e in lib.list()]
        if args.json:
            _print_json(items)
        else:
            print(f"装備ライブラリ: {lib.root}（{len(items)} 件）")
            for s in items:
                print(f"  {s['id']}  {s['name']}{' / ' + s['name_ja'] if s['name_ja'] else ''}  "
                      f"[生成 {s['method']} / 変換 {s['conversion']} / MOD {s['mod']}{' / 開発用' if s['dev_only'] else ''}]"
                      f"  {s['head_record']}")
        return 0
    if args.lcmd == "show":
        eq = lib.find(args.key)
        _print_json(eq.data) if args.json else _print_equipment(eq)
        return 0
    if args.lcmd == "add-converted":
        gen = {"method": args.method} if args.method else {}
        if args.input_text:
            gen["input_text"] = args.input_text
        if args.model_id:
            gen["model_id"] = args.model_id
        if args.seed is not None:
            gen["seed"] = args.seed
        if args.input_image:
            if not os.path.isfile(args.input_image):
                # 以前は読み取りの失敗が「ファイルを書き込めませんでした」「書き込みの権限がありません」と出た（R08B-02）
                raise LibraryError(f"入力画像がありません（ファイルを指定してください）: {os.path.abspath(args.input_image)}")
            gen["input_image"] = {"original_path": os.path.abspath(args.input_image),
                                  "sha256": sha256_file(args.input_image), "stored_path": None}
        eq = import_converted(lib, args.src, args.name, args.name_ja, args.source_name, gen)
        if args.input_image and args.keep_image:
            ext = os.path.splitext(args.input_image)[1].lower()
            shutil.copyfile(args.input_image, os.path.join(eq.dir, "source", "input" + ext))
            eq.data["generation"]["input_image"]["stored_path"] = "source/input" + ext
            lib.save(eq)
        print(f"追加: {eq.id}  {eq.name_en}  → {eq.naming['head_record']}")
        for f in eq.data["assets"]["files"]:
            print(f"  {f['game_path']}  {f['size']:,} B")
        for w in eq.data["conversion"].get("warnings") or []:
            print(f"  変換の注意: {w}（確認画像 {eq.data['conversion'].get('preview')} を見てから使う）")
        return 0
    if args.lcmd == "update-converted":
        eq = replace_converted(lib, args.key, args.src, args.source_name)
        print(f"差し替え: {eq.id}  {eq.name_en}（MOD は再ビルドが必要）")
        return 0
    if args.lcmd == "rename":
        if args.name is None and args.name_ja is None:
            # 何も変えない指定でも「変更: …（MOD は再ビルドが必要）」と出ていた（指定の誤り。R08A-07）
            print("エラー: --name か --name-ja を指定してください", file=sys.stderr)
            return 2
        old = lib.find(args.key).data
        before = json.dumps((old["display_name"], old.get("texts")), sort_keys=True)
        eq = lib.rename(args.key, args.name, args.name_ja)
        names = f"{eq.name_en}{' / ' + eq.name_ja if eq.name_ja else ''}"
        if json.dumps((eq.data["display_name"], eq.data.get("texts")), sort_keys=True) == before:
            # 名前が今と同じなら Library.rename は記録も MOD の状態も変えない（R19A-04）。表示もそれに合わせる（R08A-07）
            print(f"変更はありません: {eq.id}  {names}（MOD の状態は変わりません）")
            return 0
        print(f"変更: {eq.id}  {names}（MOD は再ビルドが必要）")
        return 0
    if args.lcmd == "set-text":
        eq = lib.set_texts(args.key, args.role, args.lang, None if args.clear else args.value)
        print(f"変更: {eq.id} texts.{args.role}.{args.lang}（MOD は再ビルドが必要）")
        return 0
    if args.lcmd == "remove":
        dst = lib.remove(args.key)
        print(f"ライブラリから外しました（{dst} に移動。MOD は再ビルド・再導入が必要）")
        return 0
    if args.lcmd == "import-library":
        from .importers import import_from_library
        from .library import Library
        src = Library.open(args.source)
        done, skipped = import_from_library(lib, src, args.keys or None)
        for e in done:
            print(f"取り込み: {e.id}  {e.name_en}  {e.naming['head_record']}  {e.naming['head_tag']}")
        for s in skipped:
            print(f"飛ばした: {s}")
        print(f"{len(done)} 件を {lib.root} に取り込みました（ID・レコードのパス・タグ・アセットは取り込み元のまま。MOD は再ビルドが必要）")
        return 0
    if args.lcmd == "import-prototype":
        eqs = import_prototype(lib, args.mod_dir)
        for e in eqs:
            print(f"取り込み: {e.id}  {e.name_en}  {e.naming['head_record']}")
        return 0
    raise ModBuilderError(f"不明なコマンド: library {args.lcmd}")


def _print_equipment(eq) -> None:
    d = eq.data
    print(f"{d['id']}  {d['display_name'].get('en')}" + (f" / {d['display_name']['ja']}" if d['display_name'].get('ja') else ""))
    print(f"  作成 {d['created_at']}  更新 {d['updated_at']}")
    g = d["generation"]
    print(f"  生成: {g.get('method')}  model={g.get('model_id')}  seed={g.get('seed')}  text={g.get('input_text')!r}")
    if g.get("input_image"):
        print(f"        画像 {g['input_image']}")
    if g.get("source_glb"):
        print(f"        GLB {g['source_glb'].get('path')}")
    n = d["naming"]
    print(f"  レコード: {n['head_record']}  消耗品: {n.get('set_record') or '（' + d['illusion_set']['owner'] + ' と共有）'}")
    print(f"  タグ: {n['head_tag']} {n.get('set_tag') or ''} {n.get('set_desc_tag') or ''}")
    a = d["assets"]
    print(f"  メッシュ: 男性 {a['male_mesh']}  女性 {a['female_mesh']}  DBR テクスチャ {a.get('head_textures') or '-'}")
    for f in a["files"]:
        print(f"    {f['game_path']}  {f['size']:,} B  sha256 {f['sha256'][:16]}…")
    c = d["conversion"]
    print(f"  変換: {c.get('status')}  {c.get('tool') or ''}  {c.get('source_dir') or ''}"
          + (f"  注意 {c['warnings']}" if c.get("warnings") else ""))
    m = d["mod"]
    print(f"  MOD: {m.get('status')}  ビルド {m.get('last_build_id')}  導入 {m.get('installed_build_id')}")
    for e in d.get("errors", [])[:5]:
        print(f"  エラー {e['at']} [{e['stage']}] {e['message']}")


# ---------------------------------------------------------------------------
# build / verify / compare
# ---------------------------------------------------------------------------
def cmd_build(args) -> int:
    from .builder import build
    from .gamedata import GameData
    lib = _library(args)
    gd_dir = _resolve_gd(args)
    gd = GameData(gd_dir)
    langs = [l.strip().upper() for l in args.languages.split(",") if l.strip()]
    try:
        m = build(lib, gd, args.out, langs, args.custom_game, use_archivetool_check=not args.no_archivetool_check,
                  include_dev=args.dev, log=_log(args))
    except ValidationFailed as e:
        print(str(e), file=sys.stderr)
        return 1
    if args.json:
        _print_json(m)
    else:
        print(f"\nビルド {m['build_id']}（{'開発用' if m['profile'] == 'dev' else '製品版'}）: {m['build_dir']}")
        if m["excluded"]:
            print(f"  除外した開発用の装備 {len(m['excluded'])} 件（--dev で含める）: "
                  + ", ".join(e["name"] for e in m["excluded"]))
        for kind, files in m["files"].items():
            for n, v in files.items():
                print(f"  [{kind}] {n:28s} {v['size']:>10,} B  md5 {v['md5']}")
        # --library で作ったビルドは、導入でもそのライブラリの装備の状態を更新するように同じ指定を案内する（R08A-04）
        lib_opt = f" --library \"{lib.root}\"" if args.library else ""
        # --gd で作ったビルドも同じ Grim Dawn に導入するように指定を案内する（無いと GRIMDAWN_DIR・Steam の検出に戻り、
        # 見つからないか別のインストール先が対象になる。R08A-04）
        gd_opt = f" --gd \"{gd_dir}\"" if args.gd else ""
        print(f"\n導入（確認のみ）: python -I scripts/modbuilder.py{lib_opt}{gd_opt} install \"{m['build_dir']}\"")
    return 0


def _find_outputs(p: str) -> dict:
    """ビルドのフォルダー・mod/ フォルダー・.arz のどれかから {database.arz, *.arc} を探す。

    キーは小文字にそろえる（Windows とゲームはファイル名の大文字・小文字を区別しない。綴りだけ違う Items.arc と
    items.arc を別のファイル、DATABASE.ARZ を「無い」としていた。R08B-04・R08A-02）。"""
    if os.path.isfile(p):
        if not p.lower().endswith(".arz"):
            # 以前は .arc なども database.arz とみなして読み、ヘッダー不正のトレースバックになった（R08B-03）
            raise ModBuilderError(f".arz ファイルかビルドのフォルダーを指定してください: {p}")
        return {"database.arz": p}
    for d in (os.path.join(p, "mod"), os.path.join(p, "build"), p):
        if not os.path.isdir(d):
            continue
        found = {f.lower(): os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith((".arz", ".arc"))}
        if os.path.isfile(found.get("database.arz", "")):
            return found
    raise ModBuilderError(f"database.arz が見つかりません: {p}")


def _read_arz(path: str):
    """.arz を開いて全レコードを展開できるか確かめる（壊れていれば ValueError。R08B-03）。"""
    from .arz import ArzFile
    arz = ArzFile(path)
    for r in arz.records:
        arz.decode(r)
    return arz


def cmd_compare(args) -> int:
    from .arc import ArcFile
    from .arz import diff
    from .gamedata import parse_tags
    a, b = _find_outputs(args.a), _find_outputs(args.b)
    rename = dict(args.rename)          # 「A=B」の検査は argparse（_pair）で済んでいる
    vmap = dict(args.value_map)
    # 壊れた（短い・ヘッダー不正・展開できない）.arz・.arc はトレースバックにせず「壊れています」・差異ありにする
    # （比べたい片方が壊れているのは compare で見つけたい状況。R08A-02・R08B-03）
    ok = True
    arz = {}
    for side, outs in (("A", a), ("B", b)):
        try:
            arz[side] = _read_arz(outs["database.arz"])
        except ValueError as e:
            print(f"{side} の database.arz を読めません（壊れています）: {e}")
            ok = False
    if len(arz) == 2:
        d = diff(arz["A"], arz["B"], ignore_time=not args.with_time, rename=rename, value_map=vmap)
        ok = not d["differences"] and not d["only_a"] and not d["only_b"]
        print(f"database.arz: 共通 {d['common']}（一致 {d['same']} / 差異 {d['common'] - d['same']}）、"
              f"A のみ {len(d['only_a'])}、B のみ {len(d['only_b'])}、末尾の Adler32 A {d['a_trailer_ok']} B {d['b_trailer_ok']}")
        # 末尾が内容と合わない .arz は壊れている（validate の V1 と同じ扱い。以前はレコードが同じなら「同等」。R08A-06）
        for side, good in (("A", d["a_trailer_ok"]), ("B", d["b_trailer_ok"])):
            if not good:
                print(f"  {side} の database.arz の末尾（Adler32）が内容と一致しません（壊れています）")
                ok = False
        for x in d["only_a"]:
            print("  A のみ: " + x)
        for x in d["only_b"]:
            print("  B のみ: " + x)
        for x in d["differences"][:args.max]:
            print("  " + x)
    for key in sorted(set(a) | set(b)):
        if not key.endswith(".arc"):
            continue
        name = os.path.basename(a[key] if key in a else b[key])     # 表示は元の綴り（A を優先）
        if key not in a or key not in b:
            print(f"{name}: {'A' if key in a else 'B'} にだけあります")
            ok = False
            continue
        xs = {}
        for side, outs in (("A", a), ("B", b)):
            try:
                xs[side] = ArcFile(outs[key]).read_all()
            except ValueError as e:
                print(f"{name}: {side} を読めません（壊れています）: {e}")
                ok = False
        if len(xs) != 2:
            continue
        xa, xb = xs["A"], xs["B"]
        if key.startswith("text_"):
            ta, tb = {}, {}
            for v in xa.values():
                ta.update(parse_tags(v))
            for v in xb.values():
                tb.update(parse_tags(v))
            ta = {vmap.get(k, k): vmap.get(v, v) for k, v in ta.items()}
            same = ta == tb
            print(f"{name}: タグ A {len(ta)} / B {len(tb)}、キーと値が{'一致' if same else '不一致'}"
                  f"（ファイル A {sorted(xa)} / B {sorted(xb)}）")
            if not same:
                ok = False
                only_a, only_b = sorted(set(ta) - set(tb)), sorted(set(tb) - set(ta))
                if only_a:
                    print(f"  A のみ {len(only_a)}: {only_a[:args.max]}")
                if only_b:
                    print(f"  B のみ {len(only_b)}: {only_b[:args.max]}")
                for k in sorted(set(ta) & set(tb)):
                    if ta[k] != tb[k]:
                        print(f"  値が違う {k}: A={ta[k]!r} B={tb[k]!r}")
        else:
            la = {k.lower(): v for k, v in xa.items()}
            lb = {k.lower(): v for k, v in xb.items()}
            same_names = sorted(la) == sorted(lb)
            same = same_names and all(la[k] == lb[k] for k in la)
            print(f"{name}: ファイル A {len(la)} / B {len(lb)}、{'全てバイト一致' if same else '不一致'}")
            if not same:
                ok = False
                only_a, only_b = sorted(set(la) - set(lb)), sorted(set(lb) - set(la))
                if only_a:
                    print(f"  A のみ {len(only_a)}: {only_a[:args.max]}")
                if only_b:
                    print(f"  B のみ {len(only_b)}: {only_b[:args.max]}")
                common = sorted(set(la) & set(lb))
                for k in common:
                    if la[k] != lb[k]:
                        print(f"  中身が違う: {k}")
                print(f"  共通 {len(common)} ファイルのうちバイト一致 {sum(la[k] == lb[k] for k in common)}")
    print("結果: " + ("同等" if ok else "差異あり"))
    return 0 if ok else 1


def cmd_verify(args) -> int:
    from .arc import ArchiveTool
    from .builder import load_manifest
    from .library import sha256_file
    from .validate import verify_outputs
    m = load_manifest(args.build_dir)
    _check_manifest_shape(m, args.build_dir)
    problems = []
    # 失敗したビルドは files が空で、検査するものが 0 件のまま「問題なし」になっていた（install の _build_files と
    # 同じく status を見る。2026-10-20 R08A-01）
    if m.get("status") != "ok":
        err = (m.get("error") or {}).get("message")
        problems.append(f"ビルドが成功していません（status {m.get('status')}）" + (f": {err}" if err else ""))
    elif "database.arz" not in (m.get("files", {}).get("basemods") or {}):
        problems.append("検査するビルドの出力（database.arz）が記録にありません")
    outputs = {}
    for kind, files in m.get("files", {}).items():
        for n, v in files.items():
            p = os.path.join(args.build_dir, v["path"].replace("/", os.sep))
            if not os.path.isfile(p):
                problems.append(f"ファイルがありません: {p}")
            elif sha256_file(p) != v["sha256"]:
                problems.append(f"記録とハッシュが一致しません: {p}")
            elif kind == "basemods":
                outputs[n] = p
    # 記録とハッシュが合う .arz でも読めないものは、V1 のエラーにする（verify_outputs の中で ValueError の
    # トレースバックになっていた。.arc は V3 で捕まえている。R08B-03）
    if "database.arz" in outputs:
        try:
            _read_arz(outputs["database.arz"])
        except ValueError as e:
            problems.append(f"V1 database.arz を読めません（壊れています）: {e}")
            del outputs["database.arz"]
    staging = os.path.join(args.build_dir, "staging")
    if not os.path.isdir(os.path.join(staging, "database")):
        # staging（元）の無いビルド（受け取った MOD の shared\received\<ID>、staging を除いたコピーなど）は元との照合が
        # できない。以前は元が 0 件のまま照合して、全レコード・全ファイルを「元に無い…が入っています」のエラーにしていた。
        # 元に依存しない検査（ハッシュの照合・.arz の末尾と展開・.arc の読み取り）だけを行い、省いたことを警告する（R08A-01）
        rep = _verify_without_staging(outputs)
        problems += rep.errors
        _print_verify(m, rep, problems)
        return 0 if not problems else 1
    tool, no_tool = None, None
    if not args.no_archivetool_check:
        try:
            tool = ArchiveTool(_resolve_gd(args))
        except (FileNotFoundError, ModBuilderError) as e:
            tool, no_tool = None, str(e)
    rep = verify_outputs(staging, outputs, tool)
    if no_tool is not None:
        # 黙って省かない（--no-archivetool-check を付けたときと見分けが付かなかった。2026-10-20 R08A-05）
        rep.warnings.append(f"ArchiveTool を使えないため、V2（-database）・V4（-list）の照合を省きました: {no_tool}"
                            "（--gd で Grim Dawn のフォルダーを指定するか、--no-archivetool-check で明示的に省けます）。"
                            "自前の読み取りでの照合（V1・V3）は行っています")
        if "database.arz" in outputs:
            rep.checks["arz_archivetool"] = "skipped: no_tool"
    problems += rep.errors
    _print_verify(m, rep, problems)
    return 0 if not problems else 1


def _print_verify(m: dict, rep, problems: list) -> None:
    print(f"ビルド {m.get('build_id')}（{m.get('status')}）: 検査 {rep.checks}")
    for w in rep.warnings:          # ArchiveTool の照合（V2）を省いたときなど（2026-10-17 点検 C02）
        print("  警告: " + w)
    for p in problems:
        print("  エラー: " + p)
    print("結果: " + ("問題なし" if not problems else f"問題 {len(problems)} 件"))


def _check_manifest_shape(m, build_dir: str) -> None:
    """verify で読むビルドの記録の形を確かめる（[] や files がリストの記録で AttributeError のトレースバックに
    なっていた。R08B-03）。"""
    files = m.get("files", {}) if isinstance(m, dict) else None
    ok = isinstance(files, dict) and all(
        isinstance(fs, dict) and all(isinstance(v, dict) and isinstance(v.get("path"), str)
                                     and isinstance(v.get("sha256"), str) for v in fs.values())
        for fs in files.values())
    if not ok or not isinstance(m.get("error") or {}, dict):
        from .builder import MANIFEST
        raise ModBuilderError(f"ビルドの記録の形が不正です（壊れています）: {os.path.join(build_dir, MANIFEST)}")


def _verify_without_staging(outputs: dict):
    """staging の無いビルドの、元に依存しない検査（.arz の末尾（Adler32）と全レコードの展開、.arc の読み取り）。"""
    from .arc import ArcFile
    from .validate import Report
    rep = Report()
    rep.warnings.append("作業フォルダー（staging）が無いため、元との照合（V1〜V4）を省きました（受け取った MOD・staging を"
                        "除いたコピーなど）。記録とのハッシュの照合と、database.arz の末尾（Adler32）・全レコードの展開、"
                        ".arc の読み取りの検査は行いました")
    rep.checks["source"] = "skipped: no_staging"
    for name, path in outputs.items():
        if name == "database.arz":
            a = _read_arz(path)         # 読めることは cmd_verify で確かめてある
            if not a.trailer_ok():
                rep.error(name, "V1 database.arz の末尾（Adler32）が内容と一致しません")
            rep.checks["arz_records"] = len(a.records)
        elif name.lower().endswith(".arc"):
            try:
                rep.checks[name] = len(ArcFile(path).read_all())
            except ValueError as e:
                rep.error(name, f"V3 {name} を読めません: {e}")
    return rep


# ---------------------------------------------------------------------------
# detect / status / install
# ---------------------------------------------------------------------------
def _running_text(v) -> str:
    return {True: "はい", False: "いいえ"}.get(v, "判定できません")


def _print_target(info: dict) -> None:
    print(f"  {info['root']}")
    for rel, f in info["files"].items():
        if f["kind"] != "absent":
            print(f"    {rel:24s} {f.get('size', 0):>10,} B  [{f['kind']}] {f['detail']}")
    if info.get("basemods_others"):
        print("    他の .arc・.arz（本ツールは触らない。/basemods で読み込まれ続ける）: "
              + ", ".join(f"{o['name']} {o['size'] or 0:,} B" for o in info["basemods_others"]))
    if info.get("others"):
        print(f"    その他（触らない）: {', '.join(info['others'])}")


def cmd_detect(args) -> int:
    from .gamedata import looks_like_gd
    from .install import inspect_target
    root = steam.steam_root()
    found = steam.find_grim_dawn(root) if root else []
    # --gd・GRIMDAWN_DIR を Steam の検出より優先する（_resolve_gd と同じ順。Steam が無くても mods/ を表示する）
    gd = args.gd or os.environ.get("GRIMDAWN_DIR") or (found[0]["path"] if found else None)
    out = {"steam_root": root, "libraries": steam.library_folders(root) if root else [], "grim_dawn": found,
           "launch_options": steam.launch_options(root) if root else [], "game_language": steam.game_language(),
           "game_running": steam.game_running(), "gd": os.path.abspath(gd) if gd else None}
    if args.json:
        _print_json(out)
        return 0
    print(f"Steam: {root}")
    for lib in out["libraries"]:
        print(f"  ライブラリ: {lib}")
    for g in found:
        print(f"Grim Dawn: {g['path']}（{g['source']}）")
    if not found:
        print("Grim Dawn: Steam からは見つかりません（--gd で指定）")
    print(f"ゲームの言語: {out['game_language']}　起動中: {_running_text(out['game_running'])}")
    print(steam.basemods_guide(out["launch_options"]))
    if gd and looks_like_gd(gd):
        print("mods/ の状態:")
        _print_target(inspect_target(os.path.abspath(gd)))
    elif gd:
        print(f"mods/ の状態: Grim Dawn のインストール先ではありません: {gd}")
    return 0


def cmd_status(args) -> int:
    from .gamedata import looks_like_gd
    from .install import status
    gd = _resolve_gd(args)
    s = status(gd)
    # --gd・GRIMDAWN_DIR の誤りでも空の mods/ を正常に表示して、MOD が導入されていないように見えた（detect と同じ判定。
    # ゲームを移した・消した後に古いパスの導入の記録を見られるように、エラーにはせず注意にする。R08A-05）
    s["gd_ok"] = looks_like_gd(gd)
    if args.json:
        _print_json(s)
        return 0
    print(f"Grim Dawn: {s['gd']}（起動中: {_running_text(s['game_running'])}、言語 {s['game_language']}）")
    if not s["gd_ok"]:
        print(f"  注意: Grim Dawn のインストール先ではありません（database\\database.arz がありません）: {gd}。"
              "--gd・GRIMDAWN_DIR を確かめてください")
    print("mods/（/basemods 形式）:")
    _print_target(s["basemods"])
    if s.get("backups"):
        print(f"導入の退避: {s['backups']['count']} 件、{s['backups']['bytes'] / 1024 / 1024:,.1f} MB"
              "（古いものは backups --prune で消せる）")
    for t in s["targets"]:
        inst = t.get("installed")
        print(f"導入の記録 [{t['format']}{' ' + t['name'] if t.get('name') else ''}]: "
              + (f"ビルド {inst['build_id']}（{inst['at']}）装備 {len(inst['equipment'])} 件" if inst else "なし"))
        if t.get("baseline"):
            print(f"  導入前の状態の退避: {t['baseline']['backup_id']}（あった: {sorted(t['baseline']['files'])}、"
                  f"無かった: {t['baseline']['absent']}）")
    return 0


def _print_plan(plan: dict) -> None:
    print(f"[{plan['action']}] {plan['root']}（{plan['format']}{' ' + plan['name'] if plan.get('name') else ''}）")
    if plan.get("build_id"):
        print(f"  ビルド: {plan['build_id']}  {plan.get('build_dir', '')}")
    for rel, f in plan.get("existing", {}).items():
        if f["kind"] != "absent":
            print(f"  現在: {rel}  [{f['kind']}] {f['detail']}")
    for c in plan.get("conflicts", []):
        print(f"  競合（退避して置き換える）: {c['file']}  [{c['kind']}]")
    for c in plan.get("kept_foreign", []):
        print(f"  触らない他の MOD のファイル: {c['file']}  [{c['kind']}]")
    for o in plan.get("basemods_others", []):
        print(f"  mods/ に残り /basemods で読み込まれ続けるファイル: {o['name']}")
    for rel in plan.get("backup", []):
        print(f"  退避: {rel}")
    for rel, sha in plan.get("copy", {}).items():
        print(f"  配置: {rel}  sha256 {sha[:16]}…")
    for rel in plan.get("restore", []):
        print(f"  戻す: {rel}")
    for rel in plan.get("remove", []):
        print(f"  削除: {rel}（退避してから）")
    for w in plan.get("warnings", []):
        print(f"  警告: {w}")
    if plan.get("executed"):
        print(f"完了（退避 {plan.get('backup_id')}。restore {plan.get('backup_id')} でこの操作の前に戻せる）")
    else:
        print("確認のみ（何も変更していません）。実行するには同じコマンドに --yes を付ける。ゲームは終了しておくこと。")
    if plan.get("guide"):
        print("\n" + plan["guide"])


def cmd_install(args) -> int:
    from .install import install, reinstall, restore, uninstall
    gd = _resolve_gd(args)
    lib = None
    try:
        lib = _library(args)
    except ModBuilderError:
        pass
    log = _log(args)
    if args.cmd == "install":
        plan = install(gd, args.build_dir, args.format, yes=args.yes, force=args.force,
                       replace_foreign=args.replace_foreign, library=lib, log=log)
    elif args.cmd == "reinstall":
        plan = reinstall(gd, args.build_dir, args.format, args.name, yes=args.yes, force=args.force, library=lib, log=log)
    elif args.cmd == "uninstall":
        plan = uninstall(gd, args.format, args.name, yes=args.yes, force=args.force, library=lib, log=log)
    else:
        plan = restore(gd, args.backup_id, yes=args.yes, force=args.force, library=lib, log=log)
    _print_json(plan) if args.json else _print_plan(plan)
    return 0


def cmd_backups(args) -> int:
    from .install import backups_usage, list_backups, protected_backups, prune_backups
    if args.prune:
        plan = prune_backups(keep=args.keep, yes=args.yes, log=_log(args))
        if args.json:
            _print_json(plan)
            return 0
        print(f"退避 {plan['count']} 件のうち、消すもの {len(plan['remove'])} 件（新しい {plan['keep']} 件と、"
              f"アンインストール・前の状態に戻すのに使う {len(plan['protected'])} 件は残す）")
        for bid in plan["remove"]:
            print(f"  消す: {bid}")
        if plan["executed"]:
            if plan.get("failed"):
                print(f"消せなかった退避: {plan['failed']}")
            print(f"完了。退避の大きさ: {plan['usage']['bytes'] / 1024 / 1024:,.1f} MB")
        else:
            print("確認のみ（何も消していません）。消すには同じコマンドに --yes を付ける。")
        return 0
    items = list_backups()
    if args.json:
        _print_json(items)
    else:
        prot = protected_backups()
        for b in items:
            if b.get("broken"):         # 記録（backup.json）が読めない退避（R07B-03）
                print(f"{b['id']}  （記録が壊れています" + (f"）  （残す: {prot[b['id']]}）" if b["id"] in prot
                                                          else "。backups --prune で消せます）"))
                continue
            print(f"{b['id']}  {b['created_at']}  [{b['reason']}] {b['format']}{' ' + b['name'] if b.get('name') else ''}  "
                  f"あった: {b['files']}  無かった: {b['absent']}" + (f"  （残す: {prot[b['id']]}）" if b["id"] in prot else ""))
        u = backups_usage()
        print(f"合計 {u['count']} 件、{u['bytes'] / 1024 / 1024:,.1f} MB（同じ中身は共有して 1 回だけ数える）")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="modbuilder", description="装備ライブラリから Grim Dawn の MOD を生成・検証・導入する")
    ap.add_argument("--version", action="version", version=f"modbuilder {__version__}")
    ap.add_argument("--library", help=f"装備ライブラリ（既定 {paths.library_dir()}、環境変数 GDSKIN_LIBRARY）")
    ap.add_argument("--gd", help="Grim Dawn のインストール先（既定: Steam から検出。環境変数 GRIMDAWN_DIR）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("library", help="装備ライブラリの操作")
    ls = lp.add_subparsers(dest="lcmd", required=True)
    ls.add_parser("init", help="ライブラリを作る")
    p = ls.add_parser("list", help="装備の一覧")
    p.add_argument("--json", action="store_true")
    p = ls.add_parser("show", help="装備の詳細")
    p.add_argument("key", help="装備ID または表示名")
    p.add_argument("--json", action="store_true")
    p = ls.add_parser("add-converted", help="gdconvert の変換結果を装備として追加")
    p.add_argument("src", help="変換結果のフォルダー（<名前>_m.msh, _f.msh, _dif.tex, _report.json）")
    p.add_argument("--name", required=True, help="ゲーム内表示名（英語）")
    p.add_argument("--name-ja", help="ゲーム内表示名（日本語。Text_JA 用）")
    p.add_argument("--source-name", help="フォルダーに複数ある場合の変換名")
    p.add_argument("--method", choices=["text", "image", "external"],
                   help="生成方法（既定: manifest.json にプロンプトがあれば text、無ければ external）")
    p.add_argument("--input-text", help="生成に使ったテキスト")
    p.add_argument("--input-image", help="生成に使った画像（参照とハッシュを記録）")
    p.add_argument("--keep-image", action="store_true", help="画像をライブラリにコピーして保存する")
    p.add_argument("--model-id", help="使った TRELLIS のモデルID")
    p.add_argument("--seed", type=int)
    p = ls.add_parser("update-converted", help="装備の変換結果を差し替える（再生成。ID は変わらない）")
    p.add_argument("key")
    p.add_argument("src")
    p.add_argument("--source-name")
    p = ls.add_parser("rename", help="表示名を変える")
    p.add_argument("key")
    p.add_argument("--name")
    p.add_argument("--name-ja", help="空文字で日本語名を消す")
    p = ls.add_parser("set-text", help="表示文（頭装備・消耗品・説明）を上書きする")
    p.add_argument("key")
    p.add_argument("--role", required=True, choices=["head", "set", "set_desc"])
    p.add_argument("--lang", required=True, choices=["en", "ja"])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--value")
    g.add_argument("--clear", action="store_true")
    p = ls.add_parser("remove", help="装備をライブラリから外す（trash/ へ移動）")
    p.add_argument("key")
    p = ls.add_parser("import-library", help="別の装備ライブラリの装備を、ID・レコードのパス・タグ・アセットを保って取り込む"
                                             "（既定は開発用でない全装備。開発用の装備は製品版の既定ライブラリに入れない）")
    p.add_argument("source", help="取り込み元の装備ライブラリ")
    p.add_argument("keys", nargs="*", help="取り込む装備の ID または表示名（省くと開発用でない全装備）")
    p = ls.add_parser("import-prototype", help="試作MOD のソース（prototype/mod03 など）から開発用の装備を復元"
                                               "（製品版の既定ライブラリには入れない。--library で開発用のライブラリを指定。D-018）")
    p.add_argument("mod_dir")

    p = sub.add_parser("build", help="統合ビルド（database.arz, 13 言語の Text_XX.arc, Items.arc）を作って検証する")
    p.add_argument("--out", help="出力先（既定: ユーザーデータ領域の builds/<ビルドID>）")
    p.add_argument("--languages", default="EN,JA",
                   help="表示名を訳す言語（EN,JA = Text_JA.arc に日本語名を入れる（既定）、EN = 英語名）。Text_XX.arc は"
                        "常に 13 言語分を作る。英語と日本語以外の設定のゲームで読まれるかは未確認")
    p.add_argument("--custom-game", metavar="NAME", help="Custom Game 形式（mods/<NAME>/）の出力も作る")
    p.add_argument("--no-archivetool-check", action="store_true", help="ArchiveTool での往復検証を省く")
    p.add_argument("--dev", action="store_true",
                   help="開発用のビルド: 開発用の装備（試作の装備）も入れる。既定は製品版で、開発用の装備を除く（D-018）")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify", help="既存のビルドを検証する")
    p.add_argument("build_dir")
    p.add_argument("--no-archivetool-check", action="store_true")
    p = sub.add_parser("compare", help="2 つのビルド（フォルダーか .arz）を比べる")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--rename", action="append", default=[], type=_pair, metavar="A=B",
                   help="A のレコード名 A を B として比べる")
    p.add_argument("--value-map", action="append", default=[], type=_pair, metavar="A=B",
                   help="A の値（パス・タグ名）を B に読み替える")
    p.add_argument("--with-time", action="store_true", help="レコードの更新時刻も比べる")
    p.add_argument("--max", type=int, default=50)
    p = sub.add_parser("detect", help="インストール先・起動オプション・言語・mods/ の状態")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("status", help="導入の記録と mods/ の状態")
    p.add_argument("--json", action="store_true")
    for name, hlp in (("install", "ビルドを mods/ に導入する"), ("reinstall", "導入し直す"),
                      ("uninstall", "導入前の状態に戻す"), ("restore", "退避の時点に戻す")):
        p = sub.add_parser(name, help=hlp + "（--yes が無ければ確認のみ）")
        if name == "install":
            p.add_argument("build_dir")
        elif name == "reinstall":
            p.add_argument("build_dir", nargs="?")
        elif name == "restore":
            p.add_argument("backup_id")
        if name != "restore":
            p.add_argument("--format", default="basemods", choices=["basemods", "custom"])
        if name in ("uninstall", "reinstall"):
            p.add_argument("--name", help="Custom Game の名前")
        p.add_argument("--yes", action="store_true", help="実際に mods/ を変更する")
        p.add_argument("--force", action="store_true", help="導入後に変更されたファイルも退避して置き換える")
        if name == "install":
            p.add_argument("--replace-foreign", action="store_true", help="他の MOD のファイルも退避して置き換える")
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("backups", help="退避の一覧（--prune で古い退避を消す）")
    p.add_argument("--prune", action="store_true",
                   help="古い退避を消す（アンインストールと前の状態に戻すのに使う退避と、新しい --keep 件は残す。--yes が無ければ確認のみ）")
    p.add_argument("--keep", type=_nonneg_int, default=5, help="--prune で残す新しい退避の件数（既定 5）")
    p.add_argument("--yes", action="store_true", help="--prune で実際に消す")
    p.add_argument("--json", action="store_true")
    return ap


def main(argv=None) -> int:
    _utf8()
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "library":
            return cmd_library(args)
        if args.cmd == "build":
            return cmd_build(args)
        if args.cmd == "verify":
            return cmd_verify(args)
        if args.cmd == "compare":
            return cmd_compare(args)
        if args.cmd == "detect":
            return cmd_detect(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd in ("install", "reinstall", "uninstall", "restore"):
            if args.cmd == "restore":
                args.format, args.name = "basemods", None
            return cmd_install(args)
        if args.cmd == "backups":
            return cmd_backups(args)
    except ValidationFailed as e:
        print("エラー: " + str(e), file=sys.stderr)
        return 1
    except ModBuilderError as e:
        print("エラー: " + str(e), file=sys.stderr)
        return 1
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # 記録のファイル（state.json・backup.json・build_manifest.json）や変換結果の manifest.json などが壊れている
        # （UTF-8 として読めないものもトレースバックにしない。R08B-02）
        print(f"エラー: ファイルを読めません（壊れています）: {e}", file=sys.stderr)
        return 1
    except OSError as e:                    # 最後の守り（導入・アンインストール・復元の失敗は InstallFailed になる）
        from .install import CAUSE_TEXT, _os_cause
        if isinstance(e, (FileNotFoundError, NotADirectoryError)):
            # 読み取りの失敗を「ファイルを書き込めませんでした」と出していた（R08B-02）
            print(f"エラー: ファイルまたはフォルダーがありません: {e.filename or e}", file=sys.stderr)
            return 1
        cause = _os_cause(e)
        text = CAUSE_TEXT[cause] if cause != "other" else "ファイルを読み書きできませんでした"
        print(f"エラー: {text}（{e}）", file=sys.stderr)
        return 1
    return 2
