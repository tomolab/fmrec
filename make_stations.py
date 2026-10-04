#!/usr/bin/env python3
"""
make_stations.py : stations.conf の雛形を自動生成する
radikoの局リストAPIから正確な局IDと局名を取得し、
既知の親局周波数テーブルと組み合わせて stations.conf 形式で出力する。

使い方:
  ./make_stations.py JP13            # 東京エリア
  ./make_stations.py JP13 JP14      # 複数エリア
  ./make_stations.py auto            # 接続元IPからエリア自動判定
  ./make_stations.py JP13 > stations.conf

同一局に複数の周波数(中継局)がある場合は、1行目を有効行、
2行目以降を「#」付きの選択肢として出力する。
自分の受信環境に合う行だけを残す(コメントを外す/付ける)こと。
"""

import re
import sys
import urllib.request
from xml.etree import ElementTree

# 既知の周波数テーブル(MHz)
# 書式: 局ID: [(周波数, 送信所ラベル), ...] 先頭が既定値
# ここに無い局・地域は ??.? で出力されるので手で記入する。
# 中継局を使っている場合も、実際の受信周波数に書き換えること。
FREQ_TABLE = {
    # radiko局ID: [(周波数MHz, 送信所メモ), ...] 親局(主要周波数)のみ。
    # 中継局を使う場合は生成後に stations.conf を手で直す。
    # --- 北海道 ---
    "AIR-G":     [(80.4, "手稲山")],
    "NORTHWAVE": [(82.5, "手稲山")],
    "HBC":       [(91.5, "手稲山 ワイドFM")],
    "STV":       [(90.4, "手稲山 ワイドFM")],
    # --- 東北 ---
    "AFB":       [(80.0, "馬ノ神山 エフエム青森")],
    "RAB":       [(91.7, "ワイドFM")],
    "FMI":       [(76.1, "新山 エフエム岩手")],
    "IBC":       [(90.6, "ワイドFM")],
    "DATEFM":    [(77.1, "大年寺山 Date fm")],
    "TBC":       [(93.5, "ワイドFM")],
    "AFM":       [(82.8, "大森山 エフエム秋田")],
    "ABS":       [(90.1, "ワイドFM")],
    "RFM":       [(80.4, "西蔵王 Rhythm Station")],
    "YBC":       [(92.4, "ワイドFM")],
    "FMF":       [(81.8, "笹森山 ふくしまFM")],
    "RFC":       [(90.8, "ワイドFM ラジオ福島")],
    # --- 関東 ---
    "FMT":       [(80.0, "東京タワー TOKYO FM")],
    "FMJ":       [(81.3, "スカイツリー J-WAVE")],
    "JOAK-FM":   [(82.5, "スカイツリー NHK-FM東京")],
    "INT":       [(89.7, "スカイツリー interfm")],
    "BAYFM78":   [(78.0, "船橋三山")],
    "NACK5":     [(79.5, "飯盛峠")],
    "YFM":       [(84.7, "大山 FMヨコハマ")],
    "TBS":       [(90.5, "スカイツリー ワイドFM")],
    "QRR":       [(91.6, "スカイツリー ワイドFM 文化放送")],
    "JORF":      [(92.4, "みなとみらい ワイドFM ラジオ日本")],
    "LFR":       [(93.0, "スカイツリー ワイドFM ニッポン放送")],
    "IBS":       [(94.6, "LuckyFM茨城 ワイドFM")],
    "CRT":       [(94.1, "栃木放送 ワイドFM")],
    "RADIOBERRY":[(76.4, "宇都宮 RADIO BERRY")],
    "FMGUNMA":   [(86.3, "赤城山 FM GUNMA")],
    # --- 甲信越 ---
    "FMNIIGATA": [(77.5, "弥彦山 FM NIIGATA")],
    "BSN":       [(92.7, "ワイドFM")],
    "FM-FUJI":   [(83.0, "坊ヶ峯 FM FUJI")],
    "YBS":       [(90.9, "ワイドFM 山梨放送")],
    "FMN":       [(79.7, "美ヶ原 FM長野")],
    "SBC":       [(92.2, "ワイドFM 信越放送")],
    # --- 北陸 ---
    "FMTOYAMA":  [(82.7, "呉羽山 FMとやま")],
    "KNB":       [(90.2, "ワイドFM 北日本放送")],
    "HELLOFIVE": [(80.5, "観音堂 エフエム石川")],
    "MRO":       [(94.0, "ワイドFM 北陸放送")],
    "FMFUKUI":   [(76.1, "足羽山 FM福井")],
    "FBC":       [(94.6, "ワイドFM 福井放送")],
    # --- 東海 ---
    "K-MIX":     [(79.2, "日本平 K-MIX静岡")],
    "SBS":       [(93.9, "ワイドFM 静岡放送")],
    "ZIP-FM":    [(77.8, "東山タワー")],
    "FMAICHI":   [(80.7, "東山タワー FM AICHI")],
    "CBC":       [(93.7, "東山タワー ワイドFM")],
    "TOKAIRADIO":[(92.9, "東山タワー ワイドFM 東海ラジオ")],
    "FMGIFU":    [(80.0, "金華山 FM GIFU")],
    "GBS":       [(90.4, "ワイドFM ぎふチャン")],
    "FMMIE":     [(78.9, "青山高原 レディオキューブFM三重")],
    # --- 関西 ---
    "E-RADIO":   [(77.0, "宇佐山 e-radio FM滋賀")],
    "ALPHA-STATION": [(89.4, "比叡山 α-STATION FM京都")],
    "KBS":       [(94.9, "ワイドFM KBS京都")],
    "802":       [(80.2, "飯盛山 FM802")],
    "FMO":       [(85.1, "飯盛山 FM大阪")],
    "CCL":       [(76.5, "飯盛山 FM COCOLO")],
    "ABC":       [(93.3, "生駒山 ワイドFM")],
    "MBS":       [(90.6, "生駒山 ワイドFM")],
    "OBC":       [(91.9, "生駒山 ワイドFM ラジオ大阪")],
    "KISSFMKOBE":[(89.9, "摩耶山 Kiss FM KOBE")],
    "CRK":       [(91.1, "ワイドFM ラジオ関西")],
    "WBS":       [(94.2, "ワイドFM 和歌山放送")],
    # --- 中国 ---
    "FM-SANIN":  [(77.4, "毛無山 V-air エフエム山陰")],
    "BSS":       [(92.2, "ワイドFM 山陰放送")],
    "FM-OKAYAMA":[(76.8, "金甲山 FM岡山")],
    "RSK":       [(91.4, "ワイドFM RSK山陽放送")],
    "HFM":       [(78.2, "黄金山 広島FM")],
    "RCC":       [(94.6, "ワイドFM 中国放送")],
    "FMY":       [(79.2, "大平山 FM山口")],
    "KRY":       [(92.3, "ワイドFM 山口放送")],
    # --- 四国 ---
    "JRT":       [(93.0, "ワイドFM 四国放送")],
    "FMKAGAWA":  [(78.6, "前田山 FM香川")],
    "RNC":       [(90.3, "ワイドFM 西日本放送")],
    "JOEU-FM":   [(79.7, "行道山 FM愛媛")],
    "RNB":       [(91.7, "ワイドFM 南海放送")],
    "HI-SIX":    [(81.6, "五台山 エフエム高知")],
    "RKC":       [(90.1, "ワイドFM 高知放送")],
    # --- 九州・沖縄 ---
    "FMFUKUOKA": [(80.7, "福岡タワー FM FUKUOKA")],
    "CROSSFM":   [(78.7, "皿倉山")],
    "LOVEFM":    [(76.1, "福岡タワー")],
    "RKB":       [(91.0, "福岡タワー ワイドFM")],
    "KBC":       [(90.2, "福岡タワー ワイドFM 九州朝日放送")],
    "FMS":       [(77.9, "九千部山 エフエム佐賀")],
    "NBC":       [(92.6, "ワイドFM 長崎放送")],
    "FMNAGASAKI":[(79.5, "稲佐山 FM長崎")],
    "FMK":       [(77.4, "金峰山 FM熊本")],
    "RKK":       [(91.4, "ワイドFM 熊本放送")],
    "FM_OITA":   [(88.0, "十文字原 FM大分")],
    "OBS":       [(93.3, "ワイドFM 大分放送")],
    "JOYFM":     [(83.2, "鰐塚山 FM宮崎 JOY FM")],
    "MRT":       [(90.4, "ワイドFM 宮崎放送")],
    "MYUFM":     [(79.8, "紫尾山 μFM 鹿児島")],
    "MBC":       [(92.8, "ワイドFM 南日本放送")],
    "FM_OKINAWA":[(87.3, "豊見城高安 FM沖縄")],
    "RBC":       [(92.1, "ワイドFM 琉球放送")],
    "ROK":       [(93.1, "ワイドFM ラジオ沖縄")],
}

UA = {"User-Agent": "Mozilla/5.0"}


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def detect_area():
    with urllib.request.urlopen(
        urllib.request.Request("https://radiko.jp/area", headers=UA),
        timeout=15,
    ) as res:
        body = res.read().decode("utf-8", "replace")
    m = re.search(r'class="(JP\d{1,2})"', body)
    if not m:
        die("エリア自動判定に失敗しました。JP13 のように指定してください")
    return m.group(1)


def sanitize_name(name):
    """局名をファイル名安全な表記へ(空白→ハイフン)"""
    name = re.sub(r'[/\\:*?"<>|$`!;&()\[\]{}\s\']+', "-", name)
    return name.strip("-") or "station"


def fetch_stations(area):
    url = f"https://radiko.jp/v3/station/list/{area}.xml"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=30
        ) as res:
            root = ElementTree.parse(res).getroot()
    except Exception as e:
        die(f"局リスト取得失敗 ({area}): {e}")
    out = []
    for st in root.iter("station"):
        sid = st.findtext("id", "")
        name = st.findtext("name", "")
        ascii_name = st.findtext("ascii_name", "") or name
        if sid:
            out.append((sid, name, ascii_name))
    return out


def main():
    args = sys.argv[1:]
    if not args:
        die(__doc__)
    areas = []
    for a in args:
        if a.lower() == "auto":
            areas.append(detect_area())
        elif re.fullmatch(r"JP([1-9]|[1-3][0-9]|4[0-7])", a):
            areas.append(a)
        else:
            die(f"不正なエリアコード: {a}")

    print("# stations.conf (make_stations.py により生成)")
    print("# 書式: <radiko局ID> <周波数MHz|-> <局名> [モード]")
    print("# モード(4列目):")
    print("#   both   = 電波録音 + radikoでも録音(二重化・既定で出力)")
    print("#   fm     = 電波録音のみ(radikoを使わない局はこれに変更)")
    print("#   radiko = radiko録音のみ(周波数は - と書く)")
    print("# ※ both はfmrec.confのradiko連携設定時のみ二重化されます。")
    print("#   未設定の場合は電波録音のみ動作します(警告が出ます)")
    print("# ・周波数が ??.? の行は、自分の受信周波数を記入して有効化")
    print("#   (電波で受信しない局は、周波数を - にして radiko モードでも可)")
    print("# ・複数周波数の候補がある局は、受信できる行だけ残す")
    print("# ・録音しない局は行頭に # を付ける")

    seen = set()
    for area in areas:
        print(f"\n# ===== {area} =====")
        for sid, name, ascii_name in fetch_stations(area):
            if sid in seen:
                continue  # 複数エリア指定時の重複(広域局)を除去
            seen.add(sid)
            # 日本語の正式局名を優先(ファイル名・Podcast表示が自然になる)。
            # 空白や記号はハイフンに置換される
            label = sanitize_name(name or ascii_name)
            if sid in ("RN1", "RN2"):
                # 短波局はFM受信不可。radiko録音でなら録れるため、
                # radikoモードの行をコメント状態で出力する
                print(f"#{sid:10s} -      {label}  radiko   "
                      f"# {name} (短波。radiko設定済みなら#を外す)")
                continue
            freqs = FREQ_TABLE.get(sid)
            if freqs:
                f0, loc0 = freqs[0]
                print(f"{sid:10s} {f0:5.1f}  {label}  both   "
                      f"# {name} ({loc0})")
                for f, loc in freqs[1:]:
                    print(f"#{sid:10s} {f:5.1f}  {label}  both   "
                          f"# 別候補: {loc}")
            else:
                print(f"#{sid:10s} ??.?   {label}  both   "
                      f"# {name} : 周波数を記入して#を外す "
                      f"(電波不可なら周波数を - に、モードを radiko に)")


if __name__ == "__main__":
    main()
