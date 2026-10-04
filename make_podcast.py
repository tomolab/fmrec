#!/usr/bin/env python3
"""
make_podcast.py
録音済みの m4a をスキャンして Podcast の RSS フィード(feed.xml)を生成する。
録音完了後やcronで定期実行する。生成したfeed.xmlと録音ファイルを
Webサーバー(またはTailscale経由)で配信し、スマホのPodcastアプリで購読する。

設定は fmrec.conf から読み込む:
  OUTPUT_DIR       : 録音ファイルの場所(= 配信ディレクトリ)
  PODCAST_BASE_URL : 配信URLのベース (例: http://raspberrypi.local:8080)
  PODCAST_TITLE    : フィードのタイトル(任意)

局ごとに分けたい場合は PODCAST_SPLIT_BY_STATION="yes" で
局名ごとの feed_<局名>.xml も生成する。
"""

import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

BASE_DIR = Path(__file__).resolve().parent
CONF_PATH = Path(os.environ.get("FMREC_CONF", BASE_DIR / "fmrec.conf"))
# メタデータ読み取りにffprobeを使う
import subprocess
import json


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def load_conf(path):
    if not path.exists():
        die(f"設定ファイルがありません: {path}")
    conf = {}
    pat = re.compile(
        r'^([A-Z_][A-Z0-9_]*)=(?:"([^"]*)"|(\S*))\s*(?:#.*)?$')
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = pat.match(line)
        if m:
            conf[m.group(1)] = m.group(2) if m.group(2) is not None \
                else m.group(3)
    return conf


def ffprobe_meta(path):
    """m4aから長さ・タイトル・アーティスト・アルバムを取得"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, timeout=30,
        ).stdout
        data = json.loads(out)
        fmt = data.get("format", {})
        tags = fmt.get("tags", {})
        # タグはキーの大小が環境で異なるので正規化
        tags = {k.lower(): v for k, v in tags.items()}
        return {
            "duration": float(fmt.get("duration", 0) or 0),
            "title": tags.get("title", ""),
            "artist": tags.get("artist", ""),
            "album": tags.get("album", ""),
            "comment": tags.get("comment", ""),
        }
    except Exception:
        return {"duration": 0, "title": "", "artist": "", "album": "",
                "comment": ""}


def fmt_duration(sec):
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def build_feed(title, base_url, items, image_url=""):
    """items: list of dict(title, url, guid, pubdate, length, duration,
       author, album, desc)
       image_url: カバーアート(Podcastアイコン)のURL。空なら省略。"""
    now = format_datetime(datetime.now(timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        '<channel>',
        f'<title>{escape(title)}</title>',
        f'<link>{escape(base_url)}</link>',
        '<description>FM/radiko 録音アーカイブ</description>',
        '<language>ja</language>',
        f'<lastBuildDate>{now}</lastBuildDate>',
        '<itunes:explicit>false</itunes:explicit>',
    ]
    # カバーアート(アプリの番組一覧・再生画面に表示される)
    if image_url:
        parts += [
            f'<itunes:image href="{escape(image_url)}"/>',
            '<image>',
            f'<url>{escape(image_url)}</url>',
            f'<title>{escape(title)}</title>',
            f'<link>{escape(base_url)}</link>',
            '</image>',
        ]
    for it in items:
        parts += [
            '<item>',
            f'<title>{escape(it["title"])}</title>',
            f'<itunes:author>{escape(it["author"])}</itunes:author>',
            f'<itunes:subtitle>{escape(it["album"])}</itunes:subtitle>',
            # 改行を <br> に変換して HTML として入れる(CDATA)。生の改行は
            # 多くの Podcast アプリで無視され1行につながってしまうため。
            # 連結録音では各パートの説明が並ぶので、行が分かれると読みやすい
            f'<description><![CDATA[{escape(it["desc"]).replace(chr(10), "<br><br>")}]]></description>',
        ]
        if it.get("art"):
            # エピソード個別アートワーク(番組画像)。iOS純正Podcast等は
            # m4a埋め込みでなくこのタグを表示に使う
            parts.append(f'<itunes:image href="{escape(it["art"])}"/>')
        parts += [
            f'<enclosure url="{escape(it["url"])}" '
            f'length="{it["length"]}" type="audio/mp4"/>',
            f'<guid isPermaLink="false">{escape(it["guid"])}</guid>',
            f'<pubDate>{it["pubdate"]}</pubDate>',
            f'<itunes:duration>{fmt_duration(it["duration"])}'
            '</itunes:duration>',
            '</item>',
        ]
    parts += ['</channel>', '</rss>']
    return "\n".join(parts)


def parse_air_datetime(name):
    """ファイル名から放送日時(datetime)を抽出する。
    「...2026年08月28日(金)19時17分...」「...2026年08月28日17時00分...」
    のような表記に対応。曜日カッコの有無は問わない。
    抽出できない場合は None を返す(呼び出し側でmtimeにフォールバック)。"""
    m = re.search(
        r"(\d{4})年(\d{2})月(\d{2})日(?:\([^)]*\))?(\d{2})時(\d{2})分", name)
    if not m:
        # 時分が無い場合は日付だけでも拾う
        m2 = re.search(r"(\d{4})年(\d{2})月(\d{2})日", name)
        if not m2:
            return None
        y, mo, d = map(int, m2.groups())
        try:
            return datetime(y, mo, d)
        except ValueError:
            return None
    y, mo, d, h, mi = map(int, m.groups())
    try:
        return datetime(y, mo, d, h, mi)
    except ValueError:
        return None


def url_quote(name):
    from urllib.parse import quote
    return quote(name)


def parse_filename(name):
    """録音ファイル名から (録音方式, 番組キー) を推定する。
    ファイル名は「番組名_局名_(FM周波数MHz|radiko)_日時.m4a」形式。
    録音方式: 'fm' か 'radiko'。番組キー: FM/radikoで同じ値になるよう
    番組名と放送日時(日+時)だけで構成する(局名・方式・分を除去)。"""
    stem = name[:-4] if name.lower().endswith(".m4a") else name
    # 連番ガードの _2, _3 を除去(同名回避で付く末尾)
    stem = re.sub(r"_\d+$", "", stem)

    if "_radiko_" in stem:
        method = "radiko"
        prog = stem.split("_")[0]           # 番組名(先頭要素)
        rest = stem
    else:
        m = re.search(r"_FM[\d.]+MHz_", stem)
        if m:
            method = "fm"
            prog = stem.split("_")[0]
            rest = stem
        else:
            return ("other", stem)

    # 日時部分を抽出(YYYY年MM月DD日 ... HH時)。曜日カッコの有無は無視。
    dt = re.search(r"(\d{4}年\d{2}月\d{2}日).*?(\d{2}時)", rest)
    when = f"{dt.group(1)}{dt.group(2)}" if dt else ""
    # キー = 番組名 + 放送日+時(局名・方式・分・曜日を含めない)
    key = f"{prog}|{when}"
    return (method, key)


def program_stem(title):
    """番組名(またはファイル名の番組部分)から分割の可変部分を除いた幹。
    make_podcast側では簡易版。schedule_programs.pyの同名関数に準じる。"""
    import re as _re
    t = (title or "").strip()
    # schedule_programs.py の program_stem と同じ除去パターン。
    # ファイル名(空白が _ に置換済み)から呼ばれることもあるため、
    # 区切りは空白と _ の両方を許容する
    sp = r'[\s_]*'
    pats = [
        r'[（(]\s*(?:PART\s*)?\d+\s*[）)]\s*$',
        sp + r'(?:PART|Part|part)\s*\.?\s*\d+\s*$',
        sp + r'(?:HOUR|Hour|hour)\s*\.?\s*\d+\s*$',
        sp + r'[①-⑳]\s*$',
        sp + r'[０-９\d]{1,2}時(?:[０-９\d]{1,2}分)?' + sp + r'[～~].*$',  # 8時～9時
        sp + r'[０-９\d]{1,2}時台\s*$',                              # 1時台
        sp + r'[０-９\d]{1,2}時(?:[０-９\d]{1,2}分)?\s*$',           # 末尾 17時50
        sp + r'[（(].*?[）)]\s*$',                                    # 末尾括弧
        r'_\d+$',   # ファイル名末尾の連番/枝番(_1 _2)
        # 録音スクリプトがtitleメタデータに付ける放送日時
        # 「2026年09月01日(火)17時30分」(曜日あり/なし両対応)
        sp + r'\d{4}年\d{2}月\d{2}日.*$',   # 他パターンで一部削られた後でも残骸ごと除去
        # NHK系の表記: 「▽サブタイトル」以降、「(曜日)N時台(前半/後半)…」以降
        # (マイあさ！は「５時台ニュース・気象情報／…」のように時台の後に
        #  その日の内容が続くため、時台以降を丸ごと除去する)
        r'[\s_　]*▽.*$',
        r'[\s_　]*(?:[月火水木金土日]曜(?:日)?)?[\s_　]*[０-９\d]{1,2}時台.*$',
        sp + r'[月火水木金土日]曜(?:日)?' + sp + r'[０-９\d]{1,2}時台\s*$',
        sp + r'[月火水木金土日]曜(?:日)?\s*$',
    ]
    prev = None
    while prev != t:
        prev = t
        for pat in pats:
            t = _re.sub(pat, '', t).strip()
    return t.rstrip('　_').strip()


def prog_key(name):
    """ファイル名から番組の識別キー(幹)を返す。局名・方式・日時は除く。"""
    stem = name[:-4] if name.lower().endswith(".m4a") else name
    prog = stem.split("_")[0]
    return program_stem(prog)


def dedup_fm_priority(files_meta):
    """FM優先の重複排除。2段階で行う:
    (1) FM録音がカバーする時間帯に含まれる同一番組のradiko録音を除外
        (FM連結版1本 と radiko分割版3本 のような粒度差に対応)
    (2) 残ったFM/radikoで、番組名+開始時が一致するペアはFM優先
        (FMが途中切れなら尺の長いradikoを採用)
    files_meta: list of (Path, meta_dict)。返り値も同形式。
    parse_air_datetime と duration から各録音の [開始, 終了] を求める。"""
    def span(p, meta):
        air = parse_air_datetime(p.name)
        if air is None:
            return None
        dur = meta.get("duration", 0) or 0
        return (air, air + timedelta(seconds=dur))

    fm_list = []
    rk_list = []
    other = []
    for p, meta in files_meta:
        method, _ = parse_filename(p.name)
        if method == "fm":
            fm_list.append((p, meta))
        elif method == "radiko":
            rk_list.append((p, meta))
        else:
            other.append((p, meta))

    # (1) FMがカバーする時間帯内の同一番組radikoを除外
    fm_spans = []
    for p, meta in fm_list:
        s = span(p, meta)
        if s:
            fm_spans.append((prog_key(p.name), s[0], s[1]))

    kept_rk = []
    for p, meta in rk_list:
        s = span(p, meta)
        covered = False
        if s:
            rk_start, rk_end = s
            rkey = prog_key(p.name)
            margin = timedelta(minutes=5)
            for fkey, fstart, fend in fm_spans:
                # 同一番組(幹一致) かつ radikoの時間帯がFMに実際に含まれる。
                # FMが途中切れ(fendがradiko終了まで届かない)場合は除外せず、
                # 完全なradikoを残す。
                if fkey and rkey and fkey == rkey \
                        and fstart - margin <= rk_start \
                        and rk_end <= fend + margin:
                    covered = True
                    break
        if not covered:
            kept_rk.append((p, meta))

    # (2) 残りで番組名+開始時が一致するFM/radikoペアはFM優先
    def keyf(p):
        method, k = parse_filename(p.name)
        return k
    groups = {}
    for p, meta in fm_list + kept_rk:
        groups.setdefault(keyf(p), {"fm": [], "radiko": []})
        method, _ = parse_filename(p.name)
        groups[keyf(p)][method if method in ("fm", "radiko")
                         else "fm"].append((p, meta))

    chosen = []
    for k, bm in groups.items():
        fm = bm["fm"]
        radiko = bm["radiko"]
        if fm and not radiko:
            chosen.extend(fm)
        elif radiko and not fm:
            chosen.extend(radiko)
        elif fm and radiko:
            fm_best = max(fm, key=lambda x: x[1]["duration"])
            rk_best = max(radiko, key=lambda x: x[1]["duration"])
            if rk_best[1]["duration"] > 0 \
                    and fm_best[1]["duration"] < rk_best[1]["duration"] * 0.85:
                chosen.append(rk_best)
            else:
                chosen.append(fm_best)
    chosen.extend(other)
    return chosen


def main():
    conf = load_conf(CONF_PATH)
    # 録音スクリプト群と同様、EXTRA_PATH のツール(自前ビルドの
    # ffmpeg/ffprobe等)を優先する。systemdやcron経由の実行では
    # ~/bin 等がPATHに入らないため、ここで揃える。
    extra_path = conf.get("EXTRA_PATH", "")
    if extra_path:
        os.environ["PATH"] = f"{extra_path}:{os.environ.get('PATH', '')}"
    out_dir = Path(conf.get("OUTPUT_DIR", ""))
    if not out_dir.is_dir():
        die(f"OUTPUT_DIR が存在しません: {out_dir}")
    base_url = conf.get("PODCAST_BASE_URL", "").rstrip("/")
    if not base_url:
        die("PODCAST_BASE_URL が未設定です (例: http://raspberrypi.local:8080)")
    feed_title = conf.get("PODCAST_TITLE", "FM/radiko 録音")
    split = conf.get("PODCAST_SPLIT_BY_STATION", "no").lower() == "yes"
    # FM/radiko の二重録音を1つにまとめるか(FM優先)
    dedup = conf.get("PODCAST_DEDUP", "yes").lower() == "yes"

    # カバーアート(Podcastアイコン)。配信フォルダに置いた画像を使う。
    # フィード種別ごとに対応ファイルがあればそれを、無ければ統合用、
    # それも無ければ conf の PODCAST_IMAGE を使う。
    def icon_url(*candidates):
        for name in candidates:
            if name and (out_dir / name).is_file():
                return f"{base_url}/{url_quote(name)}"
        conf_img = conf.get("PODCAST_IMAGE", "")
        if conf_img:
            # 絶対URLならそのまま、ファイル名なら配信URL基準
            if conf_img.startswith("http"):
                return conf_img
            if (out_dir / conf_img).is_file():
                return f"{base_url}/{url_quote(conf_img)}"
        return ""

    img_main = icon_url("podcast.png")
    img_fm = icon_url("podcast_fm.png", "podcast.png")
    img_radiko = icon_url("podcast_radiko.png", "podcast.png")

    # 完成した m4a のみ対象
    # 除外: 録音中(.recording.*)、一時ファイル(.tmp.*)、
    #       macのAppleDoubleファイル(._*)、隠しファイル(先頭ドット)
    def sort_key(p):
        # 放送日時(ファイル名)優先。取れなければファイル更新時刻。
        air = parse_air_datetime(p.name)
        return air.timestamp() if air else p.stat().st_mtime

    files = sorted(
        [p for p in out_dir.glob("*.m4a")
         if not p.name.startswith(".")           # 隠し/._ ファイル
         and not p.name.endswith(".recording.m4a")
         and ".tmp." not in p.name],
        key=sort_key,
        reverse=True,   # 新しい放送が先
    )

    # メタデータを先に取得
    files_meta_all = [(p, ffprobe_meta(p)) for p in files]

    # FM/radiko の二重録音を1つにまとめる(FM優先、FM途中切れ時radiko)
    if dedup:
        keep = {id(p) for p, _ in dedup_fm_priority(files_meta_all)}
        files_meta = [(p, m) for p, m in files_meta_all if id(p) in keep]
    else:
        files_meta = files_meta_all

    def to_item(p, meta):
        st = p.stat()
        # pubDate は放送日時(ファイル名)を優先。取れなければ更新時刻。
        air = parse_air_datetime(p.name)
        if air:
            # ファイル名の日時はローカル時刻(JST等)。ローカルTZを付与する。
            local_tz = datetime.now().astimezone().tzinfo
            pub_dt = air.replace(tzinfo=local_tz)
        else:
            pub_dt = datetime.fromtimestamp(st.st_mtime, timezone.utc)
        pubdate = format_datetime(pub_dt)
        desc_parts = []
        if meta["artist"]:
            desc_parts.append(f"出演: {meta['artist']}")
        if meta["comment"]:
            desc_parts.append(meta["comment"])
        description = "\n".join(desc_parts) or (meta["title"] or p.stem)
        # 録音と同名の .jpg(番組画像、録音スクリプトが保存)があれば
        # エピソード個別のアートワークとして使う
        art = p.with_suffix(".jpg")
        art_url = (f"{base_url}/{url_quote(art.name)}"
                   if art.is_file() else "")
        return {
            "title": meta["title"] or p.stem,
            "url": f"{base_url}/{url_quote(p.name)}",
            "guid": p.name,
            "pubdate": pubdate,
            "length": st.st_size,
            "duration": meta["duration"],
            "author": meta["artist"],
            "album": meta["album"],
            "desc": description,
            "art": art_url,
        }

    all_items = []
    by_station = {}
    for p, meta in files_meta:
        item = to_item(p, meta)
        all_items.append(item)
        station = meta["album"] or "その他"
        by_station.setdefault(station, []).append(item)

    # FM録音のみ / radiko録音のみ のフィード(重複排除せず全ファイルから)
    fm_items = []
    radiko_items = []
    for p, meta in files_meta_all:
        method, _ = parse_filename(p.name)
        if method == "fm":
            fm_items.append(to_item(p, meta))
        elif method == "radiko":
            radiko_items.append(to_item(p, meta))

    # 全体フィード
    feed_path = out_dir / "feed.xml"
    feed_path.write_text(
        build_feed(feed_title, base_url, all_items, img_main),
        encoding="utf-8")
    print(f"生成: {feed_path} ({len(all_items)}件)")
    print(f"購読URL: {base_url}/feed.xml")

    # FM録音のみ / radiko録音のみ のフィード
    if fm_items:
        fp = out_dir / "feed_fm.xml"
        fp.write_text(
            build_feed(f"{feed_title} (FM)", base_url, fm_items, img_fm),
            encoding="utf-8")
        print(f"生成: {fp} ({len(fm_items)}件) "
              f"購読URL: {base_url}/feed_fm.xml")
    if radiko_items:
        fp = out_dir / "feed_radiko.xml"
        fp.write_text(
            build_feed(f"{feed_title} (radiko)", base_url, radiko_items,
                       img_radiko),
            encoding="utf-8")
        print(f"生成: {fp} ({len(radiko_items)}件) "
              f"購読URL: {base_url}/feed_radiko.xml")

    # 局別フィード
    station_feeds = []
    if split:
        for station, items in by_station.items():
            safe = re.sub(r'[^\w\-]', '_', station)
            # 局ロゴ(fetch_logos.pyで取得したもの)があれば使う。
            # 無ければ統合用アイコンにフォールバック。
            img_station = icon_url(f"logo_{safe}.png") or img_main
            fp = out_dir / f"feed_{safe}.xml"
            fp.write_text(
                build_feed(f"{feed_title} - {station}", base_url, items,
                           img_station),
                encoding="utf-8")
            print(f"生成: {fp} ({len(items)}件) "
                  f"購読URL: {base_url}/feed_{safe}.xml")
            station_feeds.append((station, f"feed_{safe}.xml", len(items)))

    # 番組別フィード(自動生成・設定不要)
    # 番組名の幹(program_stem: 時刻範囲や分割番号を除いたもの)で
    # グルーピングし、録音が存在する番組すべてに feed_p_<番組名>.xml を作る。
    # カバーは その番組の最新エピソードの番組画像 → 局ロゴ → 統合アイコン。
    # 新しいキーワードで新番組を録れば、次回生成時に自動で現れる。
    program_feeds = []
    by_program = {}      # 正規化キー -> items
    display_name = {}    # 正規化キー -> 表示名(最新エピソード由来を優先)
    for it in all_items:
        stem = program_stem(it["title"]) or it["title"]
        # NHKのニュース系(「ニュース　正午のＮＨＫニュース」「ニュース・気象情報・
        # 交通情報（関東甲信越）」等)は内容違いで別タイトルだが、1つの
        # 「NHKニュース」フィードにまとめる。他局の「ニュース」は別のまま
        if "NHK" in (it["album"] or "").upper() and stem.startswith("ニュース"):
            stem = "NHKニュース"
        # 古い録音はメタデータの空白が _ になっている(ファイル名用の表記を
        # そのまま使っていた)ため、空白と _ を同一視してまとめる。
        # 表示名は先に来た(=新しい)エピソードの表記を使う
        key = re.sub(r"[\s_　]+", "_", stem).strip("_")
        by_program.setdefault(key, []).append(it)
        display_name.setdefault(key, stem)
    for key, items in by_program.items():
        stem = display_name[key]
        safe = re.sub(r'[^\w\-]', '_', stem).strip("_")[:80] or "program"
        # カバー: 最新エピソードの番組画像
        img_prog = ""
        for it in items:  # itemsは放送日時の降順(新しい順)
            if it.get("art"):
                img_prog = it["art"]
                break
        if not img_prog:
            st_safe = re.sub(r'[^\w\-]', '_', items[0]["album"] or "")
            img_prog = icon_url(f"logo_{st_safe}.png") or img_main
        fp = out_dir / f"feed_p_{safe}.xml"
        fp.write_text(
            build_feed(stem, base_url, items, img_prog), encoding="utf-8")
        program_feeds.append((stem, f"feed_p_{safe}.xml", len(items)))
    if program_feeds:
        print(f"番組別フィード: {len(program_feeds)}番組 "
              f"(一覧は {base_url}/index.html)")

    # 購読用の一覧ページ(index.html)。スマホのブラウザで開き、
    # 「Podcastで購読」リンクをタップすると純正Podcastアプリに登録できる
    write_index(out_dir, base_url, feed_title,
                [("すべて", "feed.xml", len(all_items)),
                 ("FM録音のみ", "feed_fm.xml", len(fm_items)),
                 ("radiko録音のみ", "feed_radiko.xml", len(radiko_items))],
                station_feeds, program_feeds)


def write_index(out_dir, base_url, feed_title, main_feeds, station_feeds,
                program_feeds):
    """フィード一覧の index.html を生成する。"""
    def podcast_link(fname):
        # podcast:// スキームは iOS/macOS の純正Podcastアプリを直接開く
        http = f"{base_url}/{fname}"
        pod = re.sub(r"^https?://", "podcast://", http)
        return (f'<a class="sub" href="{escape(pod)}">Podcastで購読</a> '
                f'<a class="url" href="{escape(http)}">{escape(fname)}</a>')

    def section(title, feeds):
        if not feeds:
            return ""
        rows = "".join(
            f"<li><span class=\"name\">{escape(name)}</span>"
            f"<span class=\"n\">{n}件</span> {podcast_link(fname)}</li>"
            for name, fname, n in feeds if n > 0)
        return f"<h2>{escape(title)}</h2><ul>{rows}</ul>"

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(feed_title)} - フィード一覧</title>
<style>
body{{font-family:-apple-system,sans-serif;margin:1.2em;line-height:1.7;
     background:#fafafa;color:#222}}
h1{{font-size:1.3em}} h2{{font-size:1.05em;margin-top:1.4em;color:#555;
     border-bottom:1px solid #ddd}}
ul{{list-style:none;padding:0}} li{{padding:.45em 0;border-bottom:1px solid #eee}}
.name{{font-weight:600}} .n{{color:#888;font-size:.85em;margin:0 .6em}}
.sub{{display:inline-block;background:#6b46c1;color:#fff;padding:.15em .7em;
     border-radius:1em;text-decoration:none;font-size:.85em}}
.url{{color:#999;font-size:.75em;text-decoration:none;margin-left:.5em}}
#kw-box textarea{{width:100%;box-sizing:border-box;font:.95em/1.5 ui-monospace,monospace;
     padding:.5em;border:1px solid #ccc;border-radius:.4em}}
.btns{{margin:.6em 0}} .btns button{{background:#6b46c1;color:#fff;border:0;
     padding:.5em 1em;border-radius:.4em;font-size:.95em;margin-right:.5em}}
.btns button:disabled{{background:#bbb}} #kw-msg{{font-size:.9em}}
#kw-out,#pc-out{{background:#222;color:#ddd;font-size:.75em;padding:.6em;border-radius:.4em;
     white-space:pre-wrap;max-height:16em;overflow:auto}} #kw-out:empty,#pc-out:empty{{display:none}}
.hint{{font-size:.85em;color:#666}} code{{background:#eee;padding:0 .3em;border-radius:.2em}}
#st-out{{background:#f4f4f8;color:#222;font-size:.78em;padding:.6em;border-radius:.4em;
     white-space:pre-wrap;line-height:1.45;border:1px solid #e0e0e8}}
#st-out.warn{{background:#fff3f3;border-color:#e88;border-width:2px}}
button.mini{{background:#888;color:#fff;border:0;padding:.15em .6em;border-radius:.8em;
     font-size:.7em;vertical-align:middle;margin-left:.5em}}
</style></head><body>
<h1>{escape(feed_title)}</h1>
<p>「Podcastで購読」をタップすると純正Podcastアプリに登録されます。</p>
{section("まとめて聴く", main_feeds)}
{section("番組別", sorted(program_feeds))}
{section("放送局別", sorted(station_feeds))}

<h2>録音・予約の状況 <button id="st-reload" class="mini">更新</button></h2>
<pre id="st-out">読み込み中…</pre>

<h2>Podcastフィード</h2>
<div class="btns">
<button id="pc-run">フィードを再生成</button>
<span id="pc-msg"></span>
</div>
<p class="hint">録音完了時に自動更新されますが、局ロゴやアイコンを変えたとき、
ファイルを手で消したときなどに手動で作り直せます。生成後はこのページを
再読み込みしてください。</p>
<pre id="pc-out"></pre>

<h2>録音キーワード</h2>
<div id="kw-box">
<p class="hint">1行1キーワード(番組名・出演者・番組内容に部分一致)。
<code>@局ID</code> で局限定、<code>-語</code> で除外、行頭 <code>!</code> は全体除外、
<code>once</code> で1日1回(再放送除外)、<code>time=06:00-07:00</code> で時間帯、
行頭 <code>#</code> はコメント。</p>
<textarea id="kw" rows="12" spellcheck="false" placeholder="読み込み中…"></textarea>
<div class="btns">
<button id="kw-save">保存</button>
<button id="kw-sched">保存して予約を更新</button>
<span id="kw-msg"></span>
</div>
<pre id="kw-out"></pre>
</div>
<script>
(function(){{
  const ta=document.getElementById('kw'), msg=document.getElementById('kw-msg'),
        out=document.getElementById('kw-out');
  const api=p=>fetch('api/'+p, {{credentials:'include'}});
  function say(t,err){{ msg.textContent=t; msg.style.color=err?'#c33':'#393'; }}
  const st=document.getElementById('st-out');
  async function loadStatus(){{
    st.textContent='読み込み中…';
    try{{ const r=await api('status'); const d=await r.json();
      st.textContent=d.error||d.text||'(出力なし)';
      // 録音失敗の警告行(!!!)があれば目立たせる
      st.classList.toggle('warn', /!!!/.test(st.textContent)); }}
    catch(e){{ st.textContent='取得失敗: '+e; }}
  }}
  document.getElementById('st-reload').onclick=loadStatus;
  loadStatus();
  const pmsg=document.getElementById('pc-msg'), pout=document.getElementById('pc-out');
  document.getElementById('pc-run').onclick=async()=>{{
    pmsg.textContent='生成中…'; pmsg.style.color='#393'; pout.textContent='';
    try{{
      const r=await fetch('api/podcast',{{method:'POST',credentials:'include'}});
      const d=await r.json();
      if(!r.ok||d.error){{ pmsg.textContent=d.error||('HTTP '+r.status); pmsg.style.color='#c33'; return; }}
      pout.textContent=d.output||'(出力なし)';
      pmsg.textContent=d.ok?'生成しました(ページを再読み込みすると一覧が更新されます)':'生成でエラー';
      pmsg.style.color=d.ok?'#393':'#c33';
    }}catch(e){{ pmsg.textContent='失敗: '+e; pmsg.style.color='#c33'; }}
  }};
  api('keywords').then(r=>r.json()).then(d=>{{
    if(d.error){{ ta.value=''; ta.placeholder=d.error; ta.disabled=true;
      document.querySelectorAll('.btns button').forEach(b=>b.disabled=true); return; }}
    ta.value=d.text||'';
  }}).catch(e=>say('読み込み失敗: '+e,true));
  async function save(){{
    say('保存中…');
    const r=await fetch('api/keywords',{{method:'POST',credentials:'include',
      headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{text:ta.value}})}});
    const d=await r.json();
    if(!r.ok||d.error){{ say(d.error||('HTTP '+r.status),true); return false; }}
    say('保存しました'); return true;
  }}
  document.getElementById('kw-save').onclick=save;
  document.getElementById('kw-sched').onclick=async()=>{{
    if(!await save()) return;
    say('予約を更新中(番組表取得に数十秒かかります)…'); out.textContent='';
    const r=await fetch('api/schedule',{{method:'POST',credentials:'include'}});
    const d=await r.json();
    if(!r.ok||d.error){{ say(d.error||('HTTP '+r.status),true); return; }}
    out.textContent=d.output||'(出力なし)'; say(d.ok?'予約を更新しました':'予約処理でエラー',!d.ok);
    loadStatus();
  }};
}})();
</script>
</body></html>"""
    (out_dir / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
