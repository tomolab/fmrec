#!/usr/bin/env python3
"""
fetch_logos.py : radikoの局ロゴをダウンロードして配信フォルダに保存する。
局別フィード(feed_<局名>.xml)のアイコンに使われる。

stations.conf に定義された局のロゴを取得し、OUTPUT_DIR に
logo_<局名>.png として保存する。<局名> は stations.conf の局名
(make_podcast.py が feed_<局名>.xml を作るときと同じ正規化)。

使い方:
  ./fetch_logos.py            # fmrec.conf の設定に従って取得
  ./fetch_logos.py --area JP13  # エリアを明示

必要環境: curl または Python の urllib(radiko.jp へのアクセス)。
"""

import argparse
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONF_PATH = Path(os.environ.get("FMREC_CONF", BASE_DIR / "fmrec.conf"))
UA = {"User-Agent": "Mozilla/5.0"}


def load_conf(path):
    conf = {}
    if not path.exists():
        return conf
    pat = re.compile(r'^([A-Z_][A-Z0-9_]*)=(?:"([^"]*)"|(\S*))\s*(?:#.*)?$')
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = pat.match(line)
        if m:
            conf[m.group(1)] = m.group(2) if m.group(2) is not None \
                else m.group(3)
    return conf


def load_stations(path):
    """stations.conf を読み、{局ID: 局名} を返す。"""
    stations = {}
    if not path.exists():
        return stations
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 3:
            sid, _freq, name = parts[0], parts[1], parts[2]
            stations[sid] = name
    return stations


def safe_station(name):
    """make_podcast.py の feed_<局名>.xml と同じ正規化。"""
    return re.sub(r'[^\w\-]', '_', name)


def fetch_logo_map(area):
    """radiko局リストから {局ID: ロゴURL} を返す。最大サイズのロゴを選ぶ。"""
    url = f"https://radiko.jp/v3/station/list/{area}.xml"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as res:
        data = res.read()
    if data[:2] == b'\x1f\x8b':
        import gzip
        data = gzip.decompress(data)
    root = ET.fromstring(data)
    logos = {}
    for st in root.iter("station"):
        sid = st.findtext("id", "")
        if not sid:
            continue
        # 複数サイズのロゴから最大(width最大)を選ぶ。無ければ<logo>テキスト
        best = None
        best_w = -1
        for logo in st.iter("logo"):
            u = (logo.text or "").strip()
            if not u:
                continue
            try:
                w = int(logo.get("width", "0"))
            except ValueError:
                w = 0
            if w > best_w:
                best_w, best = w, u
        # 旧形式: <logo_xsmall> 等の個別タグにも対応
        if best is None:
            for tag in ("logo_large", "logo_medium", "logo_small",
                        "banner"):
                v = st.findtext(tag, "")
                if v:
                    best = v.strip()
                    break
        if best:
            logos[sid] = best
    return logos


def download(url, dest):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as res:
        data = res.read()
    dest.write_bytes(data)
    return len(data)


def squarify(path, base_icon=None, size=1400):
    """局ロゴをPodcast用の正方形カバーアートに加工する。
    base_icon(make_icons.py が生成する podcast_base.png)があれば、
    ラジオのイラストの下部に白パネル+局ロゴを合成して統一デザインにする。
    無ければ白背景の正方形キャンバス中央配置(従来動作)。
    Pillow が無い場合は何もしない(横長のまま使われる)。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  (Pillow が無いため正方形化をスキップ。"
              "pip install pillow --break-system-packages で有効化)")
        return False
    try:
        logo = Image.open(path).convert("RGBA")

        if base_icon is not None and base_icon.is_file():
            # ベース(ラジオイラスト)の下部ラベル領域にロゴを合成
            canvas = Image.open(base_icon).convert("RGBA")
            if canvas.size != (size, size):
                canvas = canvas.resize((size, size), Image.LANCZOS)
            # 白の角丸パネル(下部)。紫背景の上でロゴを見やすくする
            panel_x0, panel_y0 = int(size * 0.13), int(size * 0.72)
            panel_x1, panel_y1 = int(size * 0.87), int(size * 0.935)
            draw = ImageDraw.Draw(canvas)
            try:
                draw.rounded_rectangle(
                    [panel_x0, panel_y0, panel_x1, panel_y1],
                    radius=int(size * 0.03), fill=(255, 255, 255, 255))
            except AttributeError:
                # 古いPillow(rounded_rectangle無し)は普通の矩形
                draw.rectangle([panel_x0, panel_y0, panel_x1, panel_y1],
                               fill=(255, 255, 255, 255))
            # パネル内(9割)に収まるようロゴをリサイズして中央配置
            avail_w = int((panel_x1 - panel_x0) * 0.9)
            avail_h = int((panel_y1 - panel_y0) * 0.82)
            w, h = logo.size
            scale = min(avail_w / w, avail_h / h)
            new_w, new_h = int(w * scale), int(h * scale)
            logo = logo.resize((new_w, new_h), Image.LANCZOS)
            cx = (panel_x0 + panel_x1) // 2
            cy = (panel_y0 + panel_y1) // 2
            canvas.paste(logo, (cx - new_w // 2, cy - new_h // 2), logo)
            canvas.convert("RGB").save(path, "PNG")
            return True

        # ベース無し: 白背景の正方形中央配置(従来動作)
        target = int(size * 0.8)
        w, h = logo.size
        scale = min(target / w, target / h)
        new_w, new_h = int(w * scale), int(h * scale)
        logo = logo.resize((new_w, new_h), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        canvas.paste(logo, ((size - new_w) // 2, (size - new_h) // 2),
                     logo)
        canvas.convert("RGB").save(path, "PNG")
        return True
    except Exception as e:
        print(f"  正方形化失敗(元画像のまま使用): {e}")
        return False


def main():
    conf = load_conf(CONF_PATH)
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", default=conf.get("AREA", "JP13"))
    ap.add_argument("--out", default=conf.get("OUTPUT_DIR", "."))
    ap.add_argument("--stations",
                    default=str(BASE_DIR / "stations.conf"))
    args = ap.parse_args()

    if args.area.lower() == "auto":
        print("AREA=auto は fetch_logos では未対応です。"
              "--area JP13 のように明示してください。", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out)
    if not out_dir.is_dir():
        print(f"OUTPUT_DIR がありません: {out_dir}", file=sys.stderr)
        sys.exit(1)

    stations = load_stations(Path(args.stations))
    if not stations:
        print(f"stations.conf に局がありません: {args.stations}",
              file=sys.stderr)
        sys.exit(1)

    try:
        logo_map = fetch_logo_map(args.area)
    except Exception as e:
        print(f"局リスト取得失敗 ({args.area}): {e}", file=sys.stderr)
        sys.exit(1)

    ok = 0
    for sid, name in stations.items():
        url = logo_map.get(sid)
        if not url:
            print(f"  ロゴ無し: {sid} ({name})")
            continue
        dest = out_dir / f"logo_{safe_station(name)}.png"
        try:
            n = download(url, dest)
            base = out_dir / "podcast_base.png"
            sq = squarify(dest, base_icon=base)
            note = "(ラジオアイコンに合成)" if sq and base.is_file() \
                else ("(正方形化済)" if sq else "")
            print(f"  取得: {dest.name} <- {url} ({n}バイト){note}")
            ok += 1
        except Exception as e:
            print(f"  取得失敗: {sid} ({name}): {e}")
    print(f"\n完了: {ok}/{len(stations)} 局のロゴを取得しました。")
    print("make_podcast.py を実行するとロゴが局別フィードに反映されます。")


if __name__ == "__main__":
    main()
