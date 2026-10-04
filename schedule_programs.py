#!/usr/bin/env python3
"""
schedule_programs.py
radikoの番組表を取得し keywords.txt にマッチする番組を at で録音予約する。

- 局ごとに録音モードを指定できる(stations.conf の4列目):
    fm     : 電波録音のみ(既定)
    both   : 電波 + radikoライブの二重録音
    radiko : radikoライブ録音のみ(短波局など電波で受信できない局向け)
- 全チューナー使用中で電波録音できない番組は、radikoが設定されていれば
  自動的にradiko録音へ振り替える。
- 実行のたびに keywords.txt / stations.conf と予約状態を同期する
  (追加・自動キャンセル・放送済みエントリの掃除)。
"""

import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

BASE_DIR = Path(__file__).resolve().parent
CONF_PATH = Path(os.environ.get("FMREC_CONF", BASE_DIR / "fmrec.conf"))
STATIONS_PATH = Path(
    os.environ.get("FMREC_STATIONS", BASE_DIR / "stations.conf")
)
# 予約台帳(TSV): prog_id, tuner("R"=radiko), ジョブ開始, 終了, atジョブ番号, 番組名
SCHEDULE_LOG = BASE_DIR / "registered.log"
TIME_FMT = "%Y%m%d%H%M%S"
UA = {"User-Agent": "Mozilla/5.0"}
MODES = ("fm", "both", "radiko")


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def load_conf(path):
    if not path.exists():
        die(f"設定ファイルがありません: {path}\n"
            f"{path.name}.example をコピーして作成してください")
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


def load_stations(path):
    """<局ID> <周波数|-> <局名> [モード] [tuner=N] を読む。
    戻り値: sid -> (freq or None, 局名, モード, 専用tuner or None)
    tuner=N を指定した局は、そのチューナーでのみFM録音される
    (例: Airspy等の高音質SDRを特定局の専用機にする)。"""
    if not path.exists():
        die(f"局リストがありません: {path}\n"
            f"{path.name}.example をコピーして作成してください")
    stations = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 行内コメント(空白+#以降)を除去
        line = re.split(r"\s+#", line)[0].rstrip()
        parts = line.split()
        if len(parts) < 3:
            continue
        sid, freq_s, name = parts[0], parts[1], parts[2]
        mode = "fm"
        pin_tuner = None
        prefer_tuner = None
        sdr_conf = ""
        # 第4カラム以降: モード / tuner=N / prefer=N / sdr=... を順不同で受け付ける
        for opt in parts[3:]:
            o = opt.lower()
            if o in MODES:
                mode = o
            elif o.startswith("tuner="):
                try:
                    pin_tuner = int(o.split("=", 1)[1])
                except ValueError:
                    print(f"警告: tuner指定が不正のため無視: {opt} ({line})",
                          file=sys.stderr)
            elif o.startswith("prefer="):
                # 優先チューナー: 空いていればそれを使い、埋まっていれば
                # 他の(専用指定されていない)チューナーへフォールバックする。
                # tuner=(専用・温存)との違いは、他局の割当を妨げないこと
                try:
                    prefer_tuner = int(o.split("=", 1)[1])
                except ValueError:
                    print(f"警告: prefer指定が不正のため無視: {opt} ({line})",
                          file=sys.stderr)
            elif o.startswith("sdr="):
                # この局をSDRで録るときのデバイス設定(ゲイン等)。
                # 例: sdr=lgain=2,mgain=8,vgain=12  → conf の SDR_DEVICE_CONF
                # より優先される。局ごとに強電界/弱電界で使い分ける
                val = opt[4:]
                if re.fullmatch(r"[A-Za-z0-9_=,.]+", val):
                    sdr_conf = val
                else:
                    print(f"警告: sdr指定に使えない文字があるため無視: "
                          f"{opt} ({line})", file=sys.stderr)
            else:
                print(f"警告: 不明なオプション {opt!r} のため無視: {line}",
                      file=sys.stderr)
        if freq_s == "-":
            freq = None
            if mode != "radiko":
                print(f"警告: 周波数 '-' は radiko モード専用のため無視: "
                      f"{line}", file=sys.stderr)
                continue
        else:
            try:
                freq = float(freq_s)
            except ValueError:
                print(f"警告: 周波数が数値でないため無視: {line}",
                      file=sys.stderr)
                continue
        if sid in stations:
            print(f"警告: 局ID {sid} が重複しています。後の行を使用します",
                  file=sys.stderr)
        if pin_tuner is not None and prefer_tuner is not None:
            print(f"警告: tuner= と prefer= の両方は指定できません。"
                  f"tuner=(専用)を採用: {line}", file=sys.stderr)
            prefer_tuner = None
        stations[sid] = (freq, name, mode, pin_tuner, sdr_conf, prefer_tuner)
    return stations


def load_keywords(path):
    """keywords.txt を読む。書式:
      きょうのニュース @JOAK-FM        ← JOAK-FM の番組だけにマッチ
      オールナイトニッポン -MUSIC10    ← MUSIC10を含む番組は除外(行内NOT)
      深夜便 @JOAK,JOAK-FM -休止       ← @局指定と-除外は併用可・順不同
      !放送休止                         ← 全キーワード共通の除外(グローバルNOT)
      基礎英語 once                     ← 同じ放送日に複数回あれば最初の1回だけ
      基礎英語 time=06:00-07:00        ← 開始時刻がこの時間帯の放送だけ
      JUNK                              ← 全局対象(従来通り)
    戻り値: (keywords, global_nots)
      keywords: [(キーワード, 局指定set or None, 除外語list, once,
                  時間帯list[(開始分, 終了分)]), ...]
      global_nots: [除外語, ...]
    マッチ・除外ともタイトル・出演者(pfm)・番組内容(desc)への部分一致。"""
    if not path.exists():
        die(f"keywords.txt がありません: {path}")
    keywords = []
    global_nots = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # グローバルNOT行
        if line.startswith("!"):
            word = line[1:].strip()
            if word:
                global_nots.append(word)
            continue
        # トークンに分解し、@局指定 / -除外語 / キーワード本体に振り分ける。
        # キーワード本体は空白を含み得るので、@でも-でもないトークンを連結する。
        stations = None
        nots = []
        once = False
        times = []
        kw_parts = []
        for tok in line.split():
            if tok.startswith("@") and len(tok) > 1:
                stations = {s.strip() for s in tok[1:].split(",")
                            if s.strip()}
            elif tok.startswith("-") and len(tok) > 1:
                nots.append(tok[1:])
            elif tok.lower() == "once":
                # 1日1回: 同じタイトルの番組が同じ放送日に複数回ある(再放送)
                # 場合、最初の1回だけ予約する
                once = True
            elif tok.lower().startswith("time="):
                # 時間帯指定: 番組の開始時刻がこの範囲に入るものだけ。
                # time=06:00-07:00 / 深夜またぎ time=23:00-01:00 /
                # 24時超え表記 time=24:00-25:00 / 複数 time=06:00-07:00,18:00-19:00
                for rng in tok[5:].split(","):
                    mm = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})",
                                      rng.strip())
                    if not mm:
                        print(f"警告: time指定が不正のため無視: {tok} ({line})",
                              file=sys.stderr)
                        continue
                    h1, m1, h2, m2 = map(int, mm.groups())
                    times.append(((h1 * 60 + m1) % 1440, (h2 * 60 + m2) % 1440))
            else:
                kw_parts.append(tok)
        kw = " ".join(kw_parts).strip()
        if kw:
            keywords.append((kw, stations, nots, once, times))
    return keywords, global_nots


def load_schedule():
    entries = []
    if not SCHEDULE_LOG.exists():
        return entries
    for line in SCHEDULE_LOG.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        entries.append({
            "prog_id": parts[0],
            "tuner": parts[1],  # "0","1",... または "R"(radiko)
            "start": datetime.strptime(parts[2], TIME_FMT),
            "end": datetime.strptime(parts[3], TIME_FMT),
            "at_job": parts[4] if len(parts) > 4 and parts[4] else None,
            "title": parts[5] if len(parts) > 5 else "",
        })
    return entries


def save_schedule(entries):
    lines = []
    for e in entries:
        lines.append("\t".join([
            e["prog_id"], str(e["tuner"]),
            e["start"].strftime(TIME_FMT), e["end"].strftime(TIME_FMT),
            e["at_job"] or "", e["title"],
        ]))
    SCHEDULE_LOG.write_text("\n".join(lines) + ("\n" if lines else ""))


def detect_area():
    url = "https://radiko.jp/area"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as res:
            body = res.read().decode("utf-8", "replace")
    except Exception as e:
        die(f"エリア自動判定に失敗しました: {e}\n"
            f"fmrec.conf の AREA に JP13 のような都道府県コードを"
            f"直接指定してください")
    m = re.search(r'class="(JP\d{1,2})"', body)
    if not m:
        die("エリア自動判定の応答を解釈できませんでした。\n"
            "fmrec.conf の AREA に都道府県コードを直接指定してください")
    return m.group(1)


def fetch_guide(area, date_str, retries=3, wait=5):
    """番組表XMLを取得する。radiko側の一時的な応答異常(空応答・HTMLエラー等)
    に備え、失敗したら wait 秒待って最大 retries 回試す。"""
    url = f"https://radiko.jp/v3/program/date/{date_str}/{area}.xml"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as res:
                data = res.read()
                encoding = res.headers.get("Content-Encoding", "")
            if encoding == "gzip" or data[:2] == b"\x1f\x8b":
                import gzip
                data = gzip.decompress(data)
            try:
                return ElementTree.fromstring(data)
            except ElementTree.ParseError:
                head = data[:200].decode("utf-8", "replace")
                raise RuntimeError(
                    f"XMLとして解釈できない応答でした。応答の先頭: {head!r}")
        except Exception as e:
            last_err = e
            if attempt < retries:
                print(f"番組表取得失敗 ({date_str}, {attempt}回目): {e} "
                      f"→ {wait}秒後に再試行", file=sys.stderr)
                time.sleep(wait)
    raise last_err


def collect_matches(root, stations, keywords, global_nots=(), now=None,
                    registered=None, output_dir=None):
    matches = []
    seen_once = set()   # once指定: この番組表(1放送日)内で採用済みの(局, タイトル)
    for station in root.iter("station"):
        sid = station.get("id")
        if sid not in stations:
            continue
        station_name = stations[sid][1]
        for prog in station.iter("prog"):
            title = prog.findtext("title", "") or ""
            pfm = prog.findtext("pfm", "") or ""
            desc = prog.findtext("desc", "") or ""
            info = prog.findtext("info", "") or ""
            # キーワード判定の対象は従来通り title/pfm/desc
            text = f"{title} {pfm} {desc}"
            # 表示用(Podcast説明欄)は desc と info を結合。
            # 局によって本文を desc でなく info に書く(TBS等)ため
            desc_show = "\n".join(x for x in (desc.strip(), info.strip())
                                  if x)
            # グローバルNOT: どのキーワードにも先立って除外
            if any(ng in text for ng in global_nots):
                continue
            # キーワード判定: @局指定があれば、この局(ID or 局名)が
            # 指定に含まれるキーワードだけを適用。
            # 行内NOT(-語)を含む番組は、そのキーワードではマッチしない。
            # keywords は keywords.txt の行順。最初にマッチした行の番号を
            # 優先度(小さいほど高い)として記録し、チューナー割当の順序に使う
            hit = False
            priority = 0
            hit_once = False
            ft_raw = prog.get("ft") or ""
            start_min = (int(ft_raw[8:10]) * 60 + int(ft_raw[10:12])
                         if len(ft_raw) >= 12 else None)
            for idx, (kw, kw_stations, kw_nots, kw_once,
                      kw_times) in enumerate(keywords):
                if kw_stations is not None \
                        and sid not in kw_stations \
                        and station_name not in kw_stations:
                    continue
                if kw not in text:
                    continue
                if any(ng in text for ng in kw_nots):
                    continue
                if kw_times and start_min is not None:
                    # 開始時刻がいずれかの時間帯に入るか(またぎ対応)
                    def _in(rng, t=start_min):
                        s, e = rng
                        return (s <= t < e) if s <= e else (t >= s or t < e)
                    if not any(_in(r) for r in kw_times):
                        continue
                hit = True
                priority = idx
                hit_once = kw_once
                break
            if not hit:
                continue
            ft, to = prog.get("ft"), prog.get("to")
            ts_ng = any((prog.findtext(k, "") or "").strip() not in ("", "0")
                        for k in ("ts_in_ng", "ts_out_ng"))
            # once: 同じ放送日(この番組表)に同じタイトルが既に出ていれば
            # 2回目以降(再放送)はスキップ。番組表は放送時刻順なので
            # 先の放送(朝など)が採用される。ただし「放送済みでタイムフリー
            # 不可」の回は録音手段が無いので採用せず、次の回(夜の再放送)に
            # 譲る(朝を逃した日は夜で救済される)
            if hit_once:
                key = (sid, title.strip())
                if key in seen_once:
                    continue
                if now is not None and ts_ng:
                    end_dt = datetime.strptime(to, TIME_FMT)
                    if end_dt <= now:
                        # 放送済みでタイムフリー不可 = 今からは録れない回。
                        # ただし既に録音済み(台帳に登録済み or 録音ファイルが
                        # ある)なら「採用済み」として次の回(再放送)は除外する。
                        # 未録音なら seen に入れず次の回に譲る(救済)
                        pid = f"{sid}_{ft}"
                        already = bool(registered) and (
                            pid in registered or pid + "#R" in registered)
                        if not already and output_dir:
                            already = has_good_recording(
                                output_dir, title, station_name,
                                datetime.strptime(ft, TIME_FMT), end_dt)
                        if already:
                            seen_once.add(key)
                        continue
                seen_once.add(key)
            matches.append({
                "prog_id": f"{sid}_{ft}",
                "sid": sid,
                "title": title,
                "pfm": pfm,
                "desc": desc_show,
                "img": (prog.findtext("img", "") or "").strip(),
                "start": datetime.strptime(ft, TIME_FMT),
                "end": datetime.strptime(to, TIME_FMT),
                "priority": priority,
                # タイムフリー配信不可フラグ(権利上の理由で配信されない番組)。
                # ts_in_ng=エリア内、ts_out_ng=エリア外(プレミアム)。値は
                # 0=配信可、それ以外(1, 2 …)=不可(基礎英語は 2 だった)。
                # どちらかが 0 以外なら取得を予約しない(ライブ録音は可能)
                "ts_ng": ts_ng,
            })
    return matches


def program_stem(title):
    """番組名から分割の可変部分(回数・PART・時刻表記)を除いた幹を返す。"""
    t = (title or "").strip()
    patterns = [
        r'[（(]\s*(?:PART\s*)?\d+\s*[）)]\s*$',        # (1) (PART1) （１）
        r'\s*(?:PART|Part|part)\s*\.?\s*\d+\s*$',       # PART1 Part1 part.1
        r'\s*(?:HOUR|Hour|hour)\s*\.?\s*\d+\s*$',       # Hour.1 Hour1
        r'\s*[①-⑳]\s*$',                               # 丸数字 ①②③
        r'\s*[０-９\d]{1,2}時(?:[０-９\d]{1,2}分)?\s*[～~].*$',  # 11時30分～...
        r'\s*[０-９\d]{1,2}時台\s*$',                  # 1時台
        r'\s*[０-９\d]{1,2}時(?:[０-９\d]{1,2}分)?\s*$',  # 末尾が「17時50」等で切れ
        r'\s*[（(].*?[）)]\s*$',                        # その他末尾括弧
    ]
    prev = None
    while prev != t:
        prev = t
        for pat in patterns:
            t = re.sub(pat, '', t).strip()
    # 全角スペースも境界として扱えるよう、末尾の全角スペースを除去
    return t.rstrip('　').strip()


def same_program(a, b):
    """2番組が同じ番組(分割された同一番組)か判定する。
    幹が完全一致、または一方が他方の前方一致で、続く部分が
    分割マーカー(空白/括弧/時刻など)で始まる場合のみ同一とみなす。
    「オールナイトニッポン」と「オールナイトニッポン0」のように
    続きが番組名の一部(数字直結)の場合は別番組として扱う。"""
    sa, sb = program_stem(a), program_stem(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    short, long = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(short) < 6 or not long.startswith(short):
        return False
    # 続く部分が分割マーカーらしき文字で始まるか
    rest = long[len(short):]
    # 空白・全角空白・括弧・ハイフン等の区切りで始まれば同一番組の続きとみなす。
    # 英数字が直接続く場合は別番組(名前の一部)とみなす。
    return bool(re.match(r'^[\s　（(【\[〜～\-–—]', rest))


def merge_consecutive(matches, gap_tolerance=120, bridge_max=600,
                      bridge_min_cur=1800):
    """同じ局で、番組名の幹が一致し時間的に連続する番組を1本にまとめる。
    さらに、同じ幹の番組の間に短い別コーナーが挟まる「箱番組」に対応する:
    連結中の番組の後に、幹の違う短い番組(bridge_max秒以内)を挟んで
    再び同じ幹の番組が続く場合、間の別コーナーごと内包して連結する。
    (例: 「あなたとハッピー」の途中に5分の別コーナーが入る構成)
    ただし内包は、連結中の番組がある程度の長さ(bridge_min_cur秒以上)の
    ときだけ行う。これにより「日経NEWS」のような独立した短い同名番組が
    近接して並ぶ場合に誤って内包・連結されるのを防ぐ。
    gap_tolerance: 直接連結とみなす隙間の上限(秒)
    bridge_max: 箱番組で内包を許す挟まれ区間の長さ上限(秒)
    bridge_min_cur: 内包を許すために連結中番組に必要な最小の長さ(秒)
    """
    if not matches:
        return []
    by_sid = {}
    for m in matches:
        by_sid.setdefault(m["sid"], []).append(m)

    merged = []
    for sid, progs in by_sid.items():
        progs = sorted(progs, key=lambda x: x["start"])
        used = [False] * len(progs)
        for i, m in enumerate(progs):
            if used[i]:
                continue
            cur = dict(m)
            cur["parts"] = [m]
            used[i] = True
            while True:
                extended = False
                for j in range(i + 1, len(progs)):
                    if used[j]:
                        continue
                    nxt = progs[j]
                    gap = (nxt["start"] - cur["end"]).total_seconds()
                    if gap < 0:
                        continue  # 時間が重なる(別系統)。スキップ
                    if gap > bridge_max:
                        break  # これ以上先は内包対象外
                    if not same_program(cur["title"], nxt["title"]):
                        continue  # 幹が違う番組は連結対象外(内包の中身)
                    # 直接隣接なら常に連結
                    if gap <= gap_tolerance:
                        cur["end"] = nxt["end"]
                        cur["parts"].append(nxt)
                        used[j] = True
                        extended = True
                        break
                    # 離れている場合は「箱番組」の内包。
                    # 連結中の番組が十分長いときだけ内包する
                    # (独立した短い同名番組の誤連結を防ぐ)
                    cur_len = (cur["end"] - cur["start"]).total_seconds()
                    if cur_len >= bridge_min_cur:
                        cur["end"] = nxt["end"]
                        cur["parts"].append(nxt)
                        used[j] = True
                        extended = True
                        break
                if not extended:
                    break
            merged.append(cur)
    # 2番組以上を連結したものは、タイトルを幹(時刻範囲・分割番号を除いた
    # 番組名)にする。先頭パートの「8時～9時」のような表記がファイル名や
    # Podcastのエピソード名に残ると、実際の録音範囲(8時～11時30分)と
    # 食い違って紛らわしいため。連結していないものはそのまま
    for cur in merged:
        if len(cur.get("parts", [])) >= 2:
            stem = program_stem(cur["title"])
            if stem:
                cur["title"] = stem
            cur["priority"] = min(p.get("priority", 0) for p in cur["parts"])
            # 各パートの説明をすべて連結する(先頭パートの説明だけだと
            # 連結録音の中身が分からないため)。パートごとに元の番組名を
            # 添えて「番組名: 説明」の形で並べる
            chunks = []
            for part in cur["parts"]:
                d = (part.get("desc") or "").strip()
                if not d:
                    continue
                t = (part.get("title") or "").strip()
                chunks.append(f"{t}: {d}" if t else d)
            if chunks:
                cur["desc"] = "\n".join(chunks)
    return merged


def pick_tuner(busy, candidates, start, end, gap, sid=None,
               adj_tolerance=180, reserved=None, allow_other_sid=False):
    """candidates(チューナー番号のリスト)から、時間帯が空いているものを返す。
    戻り値: (tuner番号 or None, 競合リスト)
    競合リストは全候補が塞がっていた場合の [(tuner, 番組名), ...]。
    sid を渡すと、同一局の予約との重なりが adj_tolerance 秒以下なら衝突と
    みなさない(隣接番組を同じチューナーで連続録音できる)。
    allow_other_sid=True なら別局の隣接も同様に許容する(周波数を切り替えて
    続けて録る。前番組は HANDOFF で早く切り上げ、次番組は遅く始める)。
    どちらも、録り足しと次番組の頭のわずかな重なりを許すためのもの。"""
    conflicts = []
    for t in candidates:
        blocking = []
        for ent in busy.get(t, []):
            s, e = ent[0], ent[1]
            if not (start < e + gap and s < end + gap):
                continue  # 時間帯が重ならない
            ent_sid = ent[3] if len(ent) > 3 else None
            if (sid and ent_sid == sid) or allow_other_sid:
                overlap = (min(end, e) - max(start, s)).total_seconds()
                if overlap <= adj_tolerance:
                    continue  # 隣接。同じチューナーで続けて録れる
            blocking.append(ent)
        # reserved: 専用局(tuner=N)がこれから使う予定の時間帯。指定なし局が
        # 専用チューナーを取るとき、その予定と重なるなら別の候補を先に試す
        # (呼び出し側が reserved 無しで再試行すれば、優先度勝負で取れる)
        if not blocking and reserved:
            for (s, e) in reserved.get(t, []):
                if start < e + gap and s < end + gap:
                    blocking.append((s, e, "(専用局の予定)", None))
                    break
        if not blocking:
            return t, []
        # このチューナーを塞いでいる番組(代表1件)
        b = blocking[0]
        title = b[2] if len(b) > 2 and b[2] else "(不明な番組)"
        conflicts.append((t, title))
    return None, conflicts


def sanitize(name):
    name = re.sub(r'[/\\:*?"<>|$`!;&()\[\]{}\s\']+', "_", name)
    return name.strip("_")[:60] or "no_name"


def has_good_recording(out_dir, title, station_name, start, end,
                       threshold=0.9):
    """この番組の録音ファイル(FM/radikoライブ/TFいずれか)が既に存在し、
    長さが番組尺の threshold(既定9割)以上あれば True。
    --reset や停止→再実行の後、既に録れている放送済み番組を
    タイムフリーで重複取得しないための判定。
    ffprobe が使えない場合など判定不能時は False(=取得する)を返す。"""
    try:
        out = Path(out_dir)
        if not out.is_dir():
            return False
        # ファイル名: <番組名>_<局名>_<方式>_<YYYY年MM月DD日(曜)HH時MM分>...
        # 曜日の有無・言語やFM/radikoの方式部分はワイルドカードで吸収
        pat = (f"{sanitize(title)}_{sanitize(station_name)}_"
               f"*{start.strftime('%Y年%m月%d日')}*"
               f"{start.strftime('%H時%M分')}*.m4a")
        need = (end - start).total_seconds() * threshold
        for f in out.glob(pat):
            if ".recording." in f.name or ".tmp." in f.name:
                continue
            try:
                r = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries",
                     "format=duration", "-of",
                     "default=noprint_wrappers=1:nokey=1", str(f)],
                    capture_output=True, timeout=30)
                dur = float(r.stdout.decode().strip() or 0)
            except Exception:
                continue
            if dur >= need:
                return True
        return False
    except Exception:
        return False


def shell_quote_env(value, limit=1500):
    """環境変数の値としてシングルクォートで安全に囲む(改行除去・長さ制限)"""
    clean = re.sub(r"\s+", " ", str(value)).strip()[:limit]
    return "'" + clean.replace("'", "'\\''") + "'"


def submit_at(at_args, cmd, desc="", img="", env=None):
    """atにジョブを投入し、ジョブ番号(str)を返す。失敗時None。
    desc(番組説明)があれば FMREC_DESC、img(番組画像URL)があれば FMREC_IMG を
    環境変数として録音コマンドに渡す(録音側でカバーアートとして埋め込む)。
    env は追加の環境変数(dict)。番組名・出演者・局名の元表記
    (FMREC_TITLE/FMREC_ARTIST/FMREC_STATION)を渡し、録音側は m4a の
    メタデータにこちらを使う(コマンド引数はファイル名用にサニタイズ済み)。
    descはHTMLタグや改行を除去し、長すぎる場合は切り詰める。"""
    for k, v in (env or {}).items():
        if v:
            cmd = f"{k}={shell_quote_env(v)} {cmd}"
    if img and re.match(r"^https?://[\w\-./%?=&+#~:]+$", img):
        cmd = f"FMREC_IMG='{img}' {cmd}"
    if desc:
        clean = re.sub(r"<[^>]+>", " ", desc)      # HTMLタグ除去
        # 連結番組は各パートの説明を並べるため長くなる。atのコマンドライン
        # 長は十分あるので、2000文字まで許容する(パート間の改行は残す)
        clean = re.sub(r"[ \t]+", " ", clean)
        clean = re.sub(r"\n{2,}", "\n", clean).strip()[:2000]
        # シングルクォートを安全化して環境変数を前置
        safe = clean.replace("'", "'\\''")
        cmd = f"FMREC_DESC='{safe}' {cmd}"
    proc = subprocess.run(at_args, input=cmd.encode(),
                          capture_output=True)
    if proc.returncode != 0:
        print(f"at登録失敗: {proc.stderr.decode(errors='replace')}",
              file=sys.stderr)
        return None
    jm = re.search(r"job\s+(\d+)", proc.stderr.decode(errors="replace"))
    return jm.group(1) if jm else None


def do_reset(force=False):
    """--reset: 台帳に載っている全atジョブを取り消し、台帳を削除する。
    録音中(開始≤現在<終了)のエントリがある場合は、誤って二重録音を
    起こさないよう中断する(--force で強行)。台帳外のatジョブ(ユーザーが
    別用途で入れたもの)には触らない。"""
    now = datetime.now()
    entries = load_schedule()
    if not entries:
        print("台帳が空です。リセットの必要はありません。")
        return
    active = [e for e in entries
              if e["start"] <= now < e["end"]
              and str(e["tuner"]) != "RT"]
    if active and not force:
        # 台帳上は録音時間帯でも、実際の録音プロセスが無ければ(受信機の
        # 占有などで失敗済み)、中断せず続行する。プロセスがあれば従来通り中断
        def _any_recording():
            for pat, exact in (("arecord", True), ("airspy-fmradion", True),
                               ("radiko_live.sh", False)):
                args = ["pgrep", "-x", pat] if exact else ["pgrep", "-f", pat]
                try:
                    if subprocess.run(args, capture_output=True).returncode == 0:
                        return True
                except FileNotFoundError:
                    return True   # 判定不能なら安全側(中断)
            return False
        if _any_recording():
            print("録音中(または録音時間帯)のエントリがあります:")
            for e in active:
                print(f"  {e['start']} {e.get('title', e['prog_id'])}")
            print("録音が終わってから実行するか、--reset --force で強行して"
                  "ください。\n(強行した場合、進行中の録音プロセスは止まり"
                  "ませんが、再予約で途中からの二重録音が始まることが"
                  "あります。不要なら stop_recording.sh で止めてください)")
            sys.exit(1)
        print("録音時間帯のエントリがありますが、録音プロセスは見つからない"
              "(失敗済み)ため続行します:")
        for e in active:
            print(f"  {e['start']} {e.get('title', e['prog_id'])}")
    removed = 0
    for e in entries:
        job = e.get("at_job") or ""
        if job.isdigit():
            subprocess.run(["atrm", job], capture_output=True)
            removed += 1
    SCHEDULE_LOG.unlink(missing_ok=True)
    print(f"リセット完了: atジョブ {removed}件を取り消し、台帳を削除"
          f"しました。続けて予約を作り直します。\n")


def do_retry(pattern):
    """--retry <文字列>: 台帳からタイトルに文字列を含むエントリを消し、
    続く通常処理で再予約(放送済みならタイムフリー取得)させる。
    録音中(開始≤現在<終了)のエントリは対象外(停止は stop_recording.sh で)。
    未起動のatジョブがあれば取り消す。"""
    now = datetime.now()
    entries = load_schedule()
    hit = [e for e in entries if pattern in (e.get("title") or "")]
    if not hit:
        print(f"--retry: 台帳に「{pattern}」を含む番組はありません")
        return
    keep, removed, skipped = [], [], []
    for e in entries:
        if e in hit:
            if e["start"] <= now < e["end"] and str(e["tuner"]) != "RT":
                skipped.append(e)
                keep.append(e)
                continue
            removed.append(e)
        else:
            keep.append(e)
    for e in removed:
        job = e.get("at_job") or ""
        if job.isdigit():
            try:
                subprocess.run(["atrm", job], capture_output=True)
            except FileNotFoundError:
                pass
    lines = ["\t".join([e["prog_id"], str(e["tuner"]),
                        e["start"].strftime(TIME_FMT),
                        e["end"].strftime(TIME_FMT),
                        e.get("at_job") or "", e.get("title") or ""])
             for e in keep]
    SCHEDULE_LOG.write_text("\n".join(lines) + ("\n" if lines else ""),
                            encoding="utf-8")
    for e in removed:
        print(f"--retry: 台帳から削除 [{e['tuner']}] {e['start']} {e['title']}")
    for e in skipped:
        print(f"--retry: 録音中のため対象外(stop_recording.sh で停止を): "
              f"{e['title']}")
    print(f"--retry: {len(removed)}件削除。続けて再予約します "
          f"(放送済みならタイムフリー取得、既存録音があればスキップ)\n")


def main():
    # --retry <文字列>: 該当番組を台帳から消して録り直す
    if "--retry" in sys.argv:
        i = sys.argv.index("--retry")
        if i + 1 >= len(sys.argv):
            die("--retry には番組名(の一部)を指定してください")
        do_retry(sys.argv[i + 1])
    # --reset [--force]: 全予約を消してから通常の予約処理を行う
    if "--reset" in sys.argv:
        do_reset(force="--force" in sys.argv)
    conf = load_conf(CONF_PATH)
    stations = load_stations(STATIONS_PATH)

    area = conf.get("AREA", "")
    if area.lower() in ("", "auto"):
        area = detect_area()
        print(f"エリア自動判定: {area}")
    if not re.fullmatch(r"JP([1-9]|[1-3][0-9]|4[0-7])", area):
        die(f"AREA の指定が不正です: {area!r}\n"
            f"fmrec.conf に AREA=\"JP13\" のように都道府県コード"
            f"(JP1〜JP47)を設定してください")

    record_script = conf.get("RECORD_SCRIPT", "")
    lead_sec = int(conf.get("LEAD_SECONDS", "60"))
    extra_min = int(conf.get("EXTRA_MINUTES", "1"))
    n_tuners = len(conf.get("RADIO_DEVS", "/dev/radio0").split())
    # チューナー種別。省略時は全チューナーがshark(radioSHARK)。
    # 例: TUNER_TYPES="shark shark airspy" → tuner2はSDR(sdr_radio.shで録音)
    tuner_types = conf.get("TUNER_TYPES", "").split()
    if not tuner_types:
        tuner_types = ["shark"] * n_tuners
    n_tuners = max(n_tuners, len(tuner_types))
    while len(tuner_types) < n_tuners:
        tuner_types.append("shark")
    sdr_record_script = conf.get("SDR_RECORD_SCRIPT", "")
    # stations.conf の tuner=N で専用指定されたチューナーは、
    # 指定なし局の自動割当から除外する(専用機として温存)
    pinned_tuners = {st[3] for st in stations.values()
                     if st[3] is not None}
    # 指定なし局の自動割当で試すチューナーの順序。
    # 例: TUNER_PRIORITY="2 0 1" → Airspy(2)を優先、埋まっていれば shark。
    # 未記載のチューナーは末尾に番号順で続く。省略時は番号順
    tuner_order = []
    # TUNER_PRIORITY が設定されていれば、tuner=N(専用)による温存は行わず、
    # 指定なし局は優先順に従って全チューナーを候補にする。
    # 未設定なら従来通り、専用指定されたチューナーは指定なし局から除外する
    priority_set = bool(conf.get("TUNER_PRIORITY", "").strip())
    for tok in conf.get("TUNER_PRIORITY", "").split():
        try:
            t = int(tok)
            if 0 <= t < n_tuners and t not in tuner_order:
                tuner_order.append(t)
        except ValueError:
            print(f"警告: TUNER_PRIORITY の値が不正のため無視: {tok}",
                  file=sys.stderr)
    tuner_order += [t for t in range(n_tuners) if t not in tuner_order]
    record_onair = conf.get("RECORD_ONAIR", "yes").lower() != "no"
    # 録音ファイルの保存先(タイムフリー取得スキップ判定で既存録音を探す)
    output_dir = conf.get("OUTPUT_DIR", "")
    # 番組表を何日分先まで取得するか(radikoは1週間先まで公開)
    try:
        guide_days = int(conf.get("GUIDE_DAYS", "7"))
    except ValueError:
        guide_days = 7
    guide_days = max(1, min(guide_days, 7))
    kw_path = Path(conf["KEYWORD_FILE"]) if conf.get("KEYWORD_FILE") \
        else BASE_DIR / "keywords.txt"

    # radiko録音の有効判定
    # タイムフリー: ラッパー(radiko_timefree.sh)と rec_radiko_ts の両方が必要
    rk_script = conf.get("RADIKO_TIMEFREE_SCRIPT",
                         conf.get("RADIKO_RECORD_SCRIPT", ""))
    tf_enabled = bool(
        rk_script and Path(rk_script).exists()
        and conf.get("RADIKO_TF_SCRIPT", "")
    )
    # ライブ: 同梱の radiko_live.sh(自己完結)
    live_script = conf.get("RADIKO_LIVE_SCRIPT", "")
    live_enabled = bool(live_script and Path(live_script).exists())
    # 方式: live(既定) = 放送をリアルタイム録音、タイムフリーは放送済み
    #       番組の取得と、ライブ不可時のフォールバックに使う
    #       timefree = 常に放送終了後のタイムフリー取得
    radiko_method = conf.get("RADIKO_METHOD", "live").lower()
    radiko_enabled = tf_enabled or live_enabled

    # radiko設定が中途半端な場合は原因を具体的に示す
    tf_path = conf.get("RADIKO_TF_SCRIPT", "")
    if tf_path and not tf_enabled:
        if not rk_script:
            print("注意: RADIKO_TF_SCRIPT は設定されていますが "
                  "RADIKO_TIMEFREE_SCRIPT が未設定のため、タイムフリー"
                  "取得は無効です (fmrec.conf に radiko_timefree.sh の"
                  "パスを設定してください)", file=sys.stderr)
        elif not Path(rk_script).exists():
            print(f"注意: RADIKO_TIMEFREE_SCRIPT のファイルがありません: "
                  f"{rk_script} (タイムフリー取得は無効です)",
                  file=sys.stderr)
    if tf_path and not Path(tf_path).exists():
        print(f"注意: RADIKO_TF_SCRIPT のファイルがありません: "
              f"{tf_path}", file=sys.stderr)
    if live_script and not live_enabled:
        print(f"注意: RADIKO_LIVE_SCRIPT のファイルがありません: "
              f"{live_script} (ライブ録音は無効です)", file=sys.stderr)

    # RECORD_SCRIPT(radioSHARK用)は shark のチューナーがある場合のみ必須。
    # SDR単体構成(TUNER_TYPES="airspy")では設定しなくてよい
    if "shark" in tuner_types:
        if not record_script or not Path(record_script).exists():
            die(f"RECORD_SCRIPT が見つかりません: {record_script!r}")
    elif not sdr_record_script or not Path(sdr_record_script).exists():
        die(f"SDR_RECORD_SCRIPT が見つかりません: {sdr_record_script!r}\n"
            f"(TUNER_TYPES に shark が無いため SDR 用の録音スクリプトが必要です)")

    keywords, global_nots = load_keywords(kw_path)
    entries = load_schedule()
    now = datetime.now()
    gap = timedelta(seconds=60)

    # --- 番組表の取得とマッチ収集(今日+明日) ---
    matches = []
    fetched_dates = set()
    registered_ids = {e["prog_id"] for e in entries}
    # 深夜(0〜5時)に実行した場合、いま放送中の番組は radiko の番組表では
    # 「前日」に属する(番組表の1日は5:00〜翌5:00)。前日分も取得して
    # 取りこぼさないようにする
    offsets = list(range(guide_days))
    if now.hour < 5:
        offsets = [-1] + offsets
    for offset in offsets:
        date_str = (now + timedelta(days=offset)).strftime("%Y%m%d")
        try:
            root = fetch_guide(area, date_str)
        except Exception as e:
            print(f"番組表取得失敗 ({date_str}): {e}")
            continue
        fetched_dates.add(date_str)
        matches.extend(collect_matches(root, stations, keywords,
                                       global_nots, now=now,
                                       registered=registered_ids,
                                       output_dir=output_dir))
    # 番組IDで重複排除(同一番組を二重に処理しない)
    uniq = {}
    for m in matches:
        uniq.setdefault(m["prog_id"], m)
    matches = list(uniq.values())
    matched_ids = set(uniq.keys())

    # --- 同期(1): 古い記録を台帳から削除 ---
    # 番組表の取得窓(今日以降)にある番組の記録は、放送・取得済みでも残す。
    # 消してしまうと同じ番組のradikoタイムフリー取得が毎回再予約されるため。
    window_min = min(fetched_dates) if fetched_dates else "00000000"
    entries = [
        e for e in entries
        if e["end"] > now
        or e["prog_id"].split("#")[0].split("_")[-1][:8] >= window_min
    ]

    # --- 同期(2): もうマッチしない未来の予約をキャンセル ---
    kept = []
    for e in entries:
        base_id = e["prog_id"].split("#")[0]
        e_date = base_id.split("_")[-1][:8]
        cancellable = (
            e["at_job"]
            and e["start"] > now
            and e_date in fetched_dates
            and base_id not in matched_ids
        )
        if cancellable:
            subprocess.run(["atrm", e["at_job"]],
                           stderr=subprocess.DEVNULL)
            kind = "radiko" if e["tuner"] == "R" else f"tuner{e['tuner']}"
            print(f"取消: {e['start']} [{kind}] {e['title']}")
        else:
            kept.append(e)
    entries = kept

    # --- 同期(3): 新しくマッチした番組を予約 ---
    registered = {e["prog_id"] for e in entries}
    busy = {}
    for e in entries:
        if str(e["tuner"]).isdigit():
            e_sid = e["prog_id"].split("_")[0]
            busy.setdefault(int(e["tuner"]), []).append(
                (e["start"], e["end"], e.get("title", ""), e_sid)
            )

    warned_no_radiko = [False]

    def add_entry(prog_id, tuner, job_start, job_end, at_job, title):
        entries.append({
            "prog_id": prog_id, "tuner": tuner,
            "start": job_start, "end": job_end,
            "at_job": at_job, "title": title,
        })
        registered.add(prog_id)

    # radikoは番組表どおり分割のまま、FMは連続番組を1本にまとめる
    fm_matches = merge_consecutive(matches)

    def _schedule_radiko_fallback(part, station_name, reason):
        """FMチューナー満杯時に、指定番組をradikoで代替録音する。
        part: matchesの1要素(分割単位)。ライブ優先、不可ならタイムフリー。"""
        rk_id = part["prog_id"] + "#R"
        if rk_id in registered:
            return
        artist = part["pfm"] if part["pfm"] else station_name
        # ライブのタイミング計算
        timing = None
        if part["end"] > now:
            js = part["start"] - timedelta(seconds=lead_sec)
            if js > now + timedelta(minutes=1):
                mins = int((part["end"] - part["start"]).total_seconds()
                           // 60) + extra_min
                timing = (js, mins,
                          ["at", "-t", js.strftime("%Y%m%d%H%M")], "")
            elif (record_onair
                  and part["start"] <= now + timedelta(minutes=2)
                  and part["end"] - now >= timedelta(minutes=3)):
                remain = (part["end"] - now).total_seconds()
                mins = -(-int(remain) // 60) + extra_min
                timing = (now, mins, ["at", "now"], "・放送中・途中から")
        live_ok = live_enabled and timing is not None
        if radiko_method == "live" and live_ok:
            js, mins, at_args, onair = timing
            cmd = (f'{live_script} {part["sid"]} {mins} '
                   f'"{sanitize(part["title"])}" "{sanitize(artist)}" '
                   f'"{sanitize(station_name)}" '
                   f'{part["start"].strftime(TIME_FMT)}')
            aj = submit_at(at_args, cmd, part["desc"], part.get("img", ""),
                           env={"FMREC_TITLE": part["title"], "FMREC_ARTIST": artist, "FMREC_STATION": station_name})
            if aj is None:
                return
            add_entry(rk_id, "R", js, js + timedelta(minutes=mins + 2),
                      aj, part["title"])
            print(f"予約: {part['start']} [radikoライブ{reason}{onair}] "
                  f"[{station_name}] {part['title']} / {part['pfm']}")
        elif tf_enabled:
            # タイムフリー配信対象外の番組は取得できない(対象外の区間は
            # 無音や告知に差し替えられる)ため予約しない
            if part.get("ts_ng"):
                print(f"スキップ: タイムフリー対象外(radikoで配信されません) "
                      f"[{station_name}] {part['title']}")
                add_entry(rk_id, "RT", now,
                          now + timedelta(minutes=1), "", part["title"])
                return
            # 既に十分な長さの録音ファイルがあれば重複取得しない
            # (--reset や停止→再実行の後の放送済み番組など)
            if has_good_recording(output_dir, part["title"],
                                  station_name, part["start"],
                                  part["end"]):
                print(f"スキップ: 既存録音あり(タイムフリー取得不要) "
                      f"[{station_name}] {part['title']}")
                add_entry(rk_id, "RT", now,
                          now + timedelta(minutes=1), "",
                          part["title"])
                return
            fetch_dt = max(now + timedelta(minutes=1),
                           part["end"] + timedelta(minutes=5))
            cmd = (f'{rk_script} {part["sid"]} '
                   f'{part["start"].strftime(TIME_FMT)} '
                   f'{part["end"].strftime(TIME_FMT)} '
                   f'"{sanitize(part["title"])}" "{sanitize(artist)}" '
                   f'"{sanitize(station_name)}"')
            aj = submit_at(["at", "-t", fetch_dt.strftime("%Y%m%d%H%M")],
                           cmd, part["desc"], part.get("img", ""),
                           env={"FMREC_TITLE": part["title"], "FMREC_ARTIST": artist, "FMREC_STATION": station_name})
            if aj is None:
                return
            add_entry(rk_id, "RT", fetch_dt,
                      fetch_dt + timedelta(minutes=30), aj, part["title"])
            print(f"予約: {part['end']}の放送終了後に取得 "
                  f"[radikoタイムフリー{reason}] "
                  f"[{station_name}] {part['title']} / {part['pfm']}")
        else:
            print(f"警告: radiko代替できず: {part['title']}")

    for m in sorted(matches, key=lambda x: x["start"]):
        freq, station_name, mode, _pin, _sdr, _pref = stations[m["sid"]]
        artist = m["pfm"] if m["pfm"] else station_name
        base_id = m["prog_id"]
        rk_id = base_id + "#R"

        def schedule_radiko_tf(reason=""):
            # タイムフリー配信対象外の番組は予約しない
            if m.get("ts_ng"):
                print(f"スキップ: タイムフリー対象外(radikoで配信されません) "
                      f"[{station_name}] {m['title']}")
                add_entry(rk_id, "RT", now,
                          now + timedelta(minutes=1), "", m["title"])
                return
            # 既に十分な長さの録音ファイルがあれば重複取得しない
            # (--reset や停止→再実行の後の放送済み番組など)
            if has_good_recording(output_dir, m["title"], station_name,
                                  m["start"], m["end"]):
                print(f"スキップ: 既存録音あり(タイムフリー取得不要) "
                      f"[{station_name}] {m['title']}")
                add_entry(rk_id, "RT", now,
                          now + timedelta(minutes=1), "", m["title"])
                return
            # 放送終了5分後にタイムフリーから完全版を取得する
            # (放送済み番組は今すぐ取得。タイムフリーの反映待ちで5分空ける)
            fetch_dt = max(now + timedelta(minutes=1),
                           m["end"] + timedelta(minutes=5))
            cmd = (
                f'{rk_script} {m["sid"]} '
                f'{m["start"].strftime(TIME_FMT)} '
                f'{m["end"].strftime(TIME_FMT)} '
                f'"{sanitize(m["title"])}" "{sanitize(artist)}" '
                f'"{sanitize(station_name)}"'
            )
            at_job = submit_at(
                ["at", "-t", fetch_dt.strftime("%Y%m%d%H%M")], cmd,
                m["desc"], m.get("img", ""),
                env={"FMREC_TITLE": m["title"], "FMREC_ARTIST": artist, "FMREC_STATION": station_name}
            )
            if at_job is None:
                return
            add_entry(rk_id, "RT", fetch_dt,
                      fetch_dt + timedelta(minutes=30), at_job, m["title"])
            print(f"予約: {m['end']}の放送終了後に取得 "
                  f"[radikoタイムフリー{reason}] "
                  f"[{station_name}] {m['title']} / {m['pfm']}")

        def schedule_radiko_live(reason=""):
            job_start, minutes, at_args, onair = timing
            cmd = (
                f'{live_script} {m["sid"]} {minutes} '
                f'"{sanitize(m["title"])}" "{sanitize(artist)}" '
                f'"{sanitize(station_name)}" '
                f'{m["start"].strftime(TIME_FMT)}'
            )
            at_job = submit_at(at_args, cmd, m["desc"], m.get("img", ""),
                               env={"FMREC_TITLE": m["title"], "FMREC_ARTIST": artist, "FMREC_STATION": station_name})
            if at_job is None:
                return
            add_entry(rk_id, "R", job_start,
                      job_start + timedelta(minutes=minutes + 2),
                      at_job, m["title"])
            print(f"予約: {m['start']} [radikoライブ{reason}{onair}] "
                  f"[{station_name}] {m['title']} / {m['pfm']}")

        def schedule_radiko(reason=""):
            # 方式選択: ライブ優先(未放送分のみ)、放送済み・ライブ不可は
            # タイムフリーへフォールバック
            live_ok = live_enabled and timing is not None
            if radiko_method == "live" and live_ok:
                schedule_radiko_live(reason)
            elif tf_enabled:
                schedule_radiko_tf(reason)
            elif live_ok:
                schedule_radiko_live(reason)
            else:
                print(f"警告: radiko録音できず(放送済みでタイムフリー"
                      f"未設定): {m['title']}")

        # ライブ録音のタイミング計算(放送済み等は None)
        timing = None
        if m["end"] > now:
            job_start = m["start"] - timedelta(seconds=lead_sec)
            if job_start > now + timedelta(minutes=1):
                minutes = int(
                    (m["end"] - m["start"]).total_seconds() // 60
                ) + extra_min
                timing = (job_start, minutes,
                          ["at", "-t", job_start.strftime("%Y%m%d%H%M")],
                          "")
            elif (record_onair
                  and m["start"] <= now + timedelta(minutes=2)
                  and m["end"] - now >= timedelta(minutes=3)):
                remain_sec = (m["end"] - now).total_seconds()
                minutes = -(-int(remain_sec) // 60) + extra_min
                timing = (now, minutes, ["at", "now"],
                          "・放送中・途中から")

        # --- radiko録音(both / radiko モード。番組表どおり分割で処理) ---
        # ライブ方式は放送前〜放送中のみ、タイムフリーは放送済みも可
        if mode in ("both", "radiko") and rk_id not in registered:
            if radiko_enabled:
                schedule_radiko()
            elif not warned_no_radiko[0]:
                warned_no_radiko[0] = True
                print("警告: radiko録音が未設定のため、both/radiko指定局の"
                      "radiko録音はスキップします (fmrec.conf の "
                      "RADIKO_LIVE_SCRIPT または RADIKO_TF_SCRIPT を設定)")

    # --- FM電波録音(連続番組は1本にまとめて処理) ---
    # 専用局(tuner=N)の番組の時間帯を控えておく。指定なし局がそのチューナーを
    # 取るときは、まずこの時間帯を避けて(他の候補を試して)から、それでも
    # 空きが無ければ優先度勝負で取る。TUNER_PRIORITY で専用機を優先しつつ、
    # 専用局の番組と無駄に衝突しないようにするため
    reserved = {}
    for x in fm_matches:
        pin = stations[x["sid"]][3]
        if pin is not None:
            reserved.setdefault(pin, []).append(
                (x["start"] - timedelta(seconds=lead_sec),
                 x["end"] + timedelta(minutes=extra_min)))

    # 処理順 = チューナー確保の優先順。keywords.txt の上の行にマッチした番組ほど
    # 先にチューナーを取る。同じ優先度なら開始時刻順。
    # (衝突時に低優先度の番組がradiko振替/未予約になる)
    for m in sorted(fm_matches,
                    key=lambda x: (x.get("priority", 0), x["start"])):
        (freq, station_name, mode, pin_tuner, sdr_conf,
         prefer_tuner) = stations[m["sid"]]
        artist = m["pfm"] if m["pfm"] else station_name
        base_id = m["prog_id"]
        rk_id = base_id + "#R"

        if mode not in ("fm", "both") or freq is None:
            continue
        if base_id in registered:
            continue

        # 録音タイミング(連続マージ後の開始〜終了で計算)
        if m["end"] <= now:
            continue  # 放送済みは電波録音不可
        # 同一局で、この番組の直後に始まる別のマッチ番組があるか。
        # ある場合は録り足し(EXTRA_MINUTES)をカットして定刻で録音を終え、
        # 同じチューナーを次番組へすぐ引き渡す(その時間帯は次番組の
        # ファイルの頭に入るので取りこぼしはない)
        # この局が使えるチューナー全体(引き継ぎ要否の判定に使う)
        if pin_tuner is not None:
            candidates_all = [pin_tuner] if pin_tuner < n_tuners else []
        elif priority_set:
            candidates_all = list(tuner_order)
        else:
            candidates_all = [t for t in tuner_order
                              if t not in pinned_tuners]

        # 直後に始まる別のマッチ番組(同一局・別局どちらも)。別局の場合は
        # 周波数を切り替えて続けて録るので、やはり引き継ぎが必要
        adj_next = [x for x in fm_matches
                    if x is not m
                    and abs((x["start"] - m["end"]).total_seconds()) <= 120]
        # ただし、次番組が別のチューナーで録れるなら引き継ぎは不要
        # (欠けゼロ優先)。次番組の時間帯に空いているチューナーが2台以上
        # あれば、1台はこの番組が使っても、もう1台を次番組が使える
        def _free_count(x):
            js = x["start"] - timedelta(seconds=lead_sec)
            je = (x["end"] + timedelta(minutes=extra_min)
                  + timedelta(seconds=lead_sec))
            n = 0
            for t in candidates_all:
                if all(not (js < e + gap and s < je + gap)
                       for (s, e, *_r) in busy.get(t, [])):
                    n += 1
            return n
        # 明示指定(tuner=/prefer=)の局は分散させないので、隣接があれば
        # 必ず同じチューナーでの引き継ぎになる
        want_fixed = (pin_tuner if pin_tuner is not None else prefer_tuner)
        has_adjacent_next = bool(adj_next) and (
            want_fixed is not None or all(_free_count(x) < 2 for x in adj_next))
        eff_extra = 0 if has_adjacent_next else extra_min

        job_start = m["start"] - timedelta(seconds=lead_sec)
        if job_start > now + timedelta(minutes=1):
            minutes = int(
                (m["end"] - m["start"]).total_seconds() // 60) + eff_extra
            at_args = ["at", "-t", job_start.strftime("%Y%m%d%H%M")]
            onair = ""
        elif (record_onair
              and m["start"] <= now + timedelta(minutes=2)
              and m["end"] - now >= timedelta(minutes=3)):
            job_start = now
            remain_sec = (m["end"] - now).total_seconds()
            if has_adjacent_next:
                # 同一局の次番組が控えている: 定刻を越えて録るとチューナーを
                # 離せず次番組が失敗するため、残り時間は切り捨て(最大59秒
                # 早く終わる。次番組の頭を優先)。録音スクリプトの HANDOFF
                # 早じまいと合わせ、定刻前に確実に手放す
                minutes = max(1, int(remain_sec) // 60)
            else:
                minutes = -(-int(remain_sec) // 60) + eff_extra
            at_args = ["at", "now"]
            onair = "・放送中・途中から"
        else:
            continue
        job_end = job_start + timedelta(seconds=lead_sec) \
            + timedelta(minutes=minutes)

        n_parts = len(m.get("parts", [m]))
        span = f"・{n_parts}番組連結" if n_parts > 1 else ""

        # 割当候補: tuner=N 指定局はそのチューナーのみ。
        # 指定なし局は、専用予約されていないチューナーから選ぶ。
        if pin_tuner is not None:
            if pin_tuner >= n_tuners:
                print(f"警告: {station_name} の tuner={pin_tuner} は"
                      f"存在しません(チューナー数{n_tuners})")
                continue
            candidates = [pin_tuner]
        else:
            if priority_set:
                candidates = list(tuner_order)
            else:
                candidates = [t for t in tuner_order
                              if t not in pinned_tuners]
            if prefer_tuner is not None and prefer_tuner < n_tuners:
                # 優先チューナーを先頭に(専用温存されていても自局は使える)
                candidates = ([prefer_tuner]
                              + [t for t in candidates if t != prefer_tuner])
        # チューナー選択は段階的に条件を緩める。
        #  (1) 同一局隣接の同居を許さず、専用局の予定も避けて探す
        #      → 完全に空いているチューナーが見つかれば欠けゼロで録れる
        #  (2) 専用局の予定を無視して探す(優先度勝負)
        #  (3) 同一局隣接の同居を許す(HANDOFFで前番組の尻が欠ける)
        # 複数チューナーがある場合、連続番組は別チューナーに振られるので
        # 引き継ぎによる欠けが発生しない
        def _pick(sid_for_adj, use_reserved):
            return pick_tuner(busy, candidates, job_start, job_end, gap,
                              sid=sid_for_adj,
                              reserved=reserved if use_reserved else None)

        want = pin_tuner if pin_tuner is not None else prefer_tuner
        if want is not None and want < n_tuners:
            # tuner=N / prefer=N を明示した局は「その機械で録りたい」という
            # 意思表示。空きチューナーへ分散させず、同一局隣接の同居(引き継ぎ)
            # も許して指定チューナーを最優先で使う
            tuner, conflicts = pick_tuner(busy, [want], job_start, job_end,
                                          gap, sid=m["sid"])
            if tuner is None:
                tuner, conflicts = pick_tuner(busy, [want], job_start,
                                              job_end, gap, sid=m["sid"],
                                              allow_other_sid=True)
            if tuner is None and pin_tuner is None:
                # prefer は埋まっていれば他へフォールバック
                tuner, conflicts = _pick(None, True)
                if tuner is None:
                    tuner, conflicts = _pick(None, False)
                if tuner is None:
                    tuner, conflicts = _pick(m["sid"], False)
        elif pin_tuner is None:
            tuner, conflicts = _pick(None, True)      # (1)
            if tuner is None:
                tuner, conflicts = _pick(None, False)  # (2)
            if tuner is None:
                tuner, conflicts = _pick(m["sid"], False)   # (3) 同一局の同居
            if tuner is None:
                # (4) 別局でも隣接なら同居(周波数を切り替えて続けて録る)
                tuner, conflicts = pick_tuner(
                    busy, candidates, job_start, job_end, gap,
                    sid=m["sid"], allow_other_sid=True)
        else:
            tuner, conflicts = _pick(None, False)
            if tuner is None:
                tuner, conflicts = _pick(m["sid"], False)
            if tuner is None:
                tuner, conflicts = pick_tuner(
                    busy, candidates, job_start, job_end, gap,
                    sid=m["sid"], allow_other_sid=True)
        if tuner is None:
            conflict_note = "、".join(
                f"tuner{t}={title}" for t, title in conflicts)
            # チューナー満杯: fmモードかつradiko可能なら振替(bothは既にradiko予約済)
            if mode == "fm" and radiko_enabled and rk_id not in registered:
                # 連結番組は各パートをradikoで個別に振り替える
                for part in m.get("parts", [m]):
                    _schedule_radiko_fallback(part, station_name,
                                              "代替・チューナー満杯")
                print(f"  (チューナー競合: {conflict_note})")
            else:
                print(f"警告: 全チューナー使用中のため未予約: "
                      f"{m['start']} {m['title']}")
                print(f"  (競合: {conflict_note})")
            continue
        # チューナー種別に応じた録音スクリプトを選択
        t_type = tuner_types[tuner] if tuner < len(tuner_types) else "shark"
        if t_type == "shark":
            rec = record_script
        else:
            rec = sdr_record_script
            if not rec or not Path(rec).exists():
                print(f"警告: tuner{tuner}({t_type})用の "
                      f"SDR_RECORD_SCRIPT が未設定/不在のため未予約: "
                      f"{m['title']}")
                continue
        cmd = (
            f'{rec} {tuner} {minutes} '
            f'"{sanitize(m["title"])}" "{sanitize(artist)}" '
            f'{freq} "{sanitize(station_name)}" '
            f'{m["start"].strftime(TIME_FMT)}'
        )
        # SDRで録る局に sdr= 指定があれば、局別のデバイス設定として渡す
        if t_type != "shark" and sdr_conf:
            cmd = f"FMREC_SDR_CONF='{sdr_conf}' {cmd}"
        if has_adjacent_next:
            # 次番組へチューナーを引き継ぐ: こちらは定刻より早く録音を
            # 切り上げ(尻が欠ける)、次番組は定刻より遅く始める。
            # 次が別局なら周波数の切り替えが挟まるので、録音側で
            # 別の猶予(CROSS_HANDOFF_*)を使えるよう区別して渡す
            same_sid = any(x["sid"] == m["sid"] for x in adj_next)
            cmd = (f"FMREC_HANDOFF={'1' if same_sid else 'cross'} {cmd}")
        # 同一局の前番組から引き継ぐ側: 割り当てたチューナーの busy に、
        # この番組の開始とほぼ同時刻に終わる同一局の予約があれば、
        # PRE_ROLL なしで定刻ちょうどに開始させる(前番組が定刻5秒前まで
        # デバイスを使っているため)
        # 同じチューナーで、この番組の開始とほぼ同時刻に終わる予約があれば
        # 引き継ぎを受ける側(局が同じでも違っても)
        handoff_prev = any(
            abs((ent[1] - m["start"]).total_seconds()) <= 180
            for ent in busy.get(tuner, []))
        if handoff_prev:
            # 前番組が同一局か別局かで、次番組側の開始猶予も変える
            prev_same = any(
                len(ent) > 3 and ent[3] == m["sid"]
                and abs((ent[1] - m["start"]).total_seconds()) <= 180
                for ent in busy.get(tuner, []))
            cmd = (f"FMREC_HANDOFF_NEXT={'1' if prev_same else 'cross'} {cmd}")
        at_job = submit_at(at_args, cmd, m["desc"], m.get("img", ""),
                               env={"FMREC_TITLE": m["title"], "FMREC_ARTIST": artist, "FMREC_STATION": station_name})
        if at_job is None:
            continue
        add_entry(base_id, str(tuner), job_start, job_end, at_job,
                  m["title"])
        busy.setdefault(tuner, []).append((job_start, job_end,
                                           m["title"], m["sid"]))
        print(f"予約: {m['start']} [tuner{tuner}{onair}{span}] "
              f"[{station_name} {freq}MHz] {m['title']} / {m['pfm']}")

    save_schedule(entries)


if __name__ == "__main__":
    main()
