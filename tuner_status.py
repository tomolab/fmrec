#!/usr/bin/env python3
"""
tuner_status.py : チューナーの使用状況を1画面で表示する。

- 各チューナー(shark/airspy)の「いま」(録音中/待機中)と「これから」(予約)
- radiko録音(ライブ/タイムフリー)の状況
- 台帳(registered.log)・実プロセス・チューナー種別(TUNER_TYPES)を突き合わせる

使い方: ./tuner_status.py
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONF_PATH = Path(os.environ.get("FMREC_CONF", BASE_DIR / "fmrec.conf"))
SCHEDULE_LOG = Path(os.environ.get("FMREC_SCHEDULE_LOG",
                                   BASE_DIR / "registered.log"))
STATIONS_PATH = Path(os.environ.get("FMREC_STATIONS",
                                    BASE_DIR / "stations.conf"))
TIME_FMT = "%Y%m%d%H%M%S"


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


def load_entries():
    entries = []
    if not SCHEDULE_LOG.exists():
        return entries
    for line in SCHEDULE_LOG.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            start = datetime.strptime(parts[2], TIME_FMT)
            end = datetime.strptime(parts[3], TIME_FMT)
        except ValueError:
            continue
        entries.append({
            "prog_id": parts[0],
            "tuner": parts[1],
            "start": start,
            "end": end,
            "at_job": parts[4] if len(parts) > 4 else "",
            "title": parts[5] if len(parts) > 5 else "",
        })
    return entries


def load_pinned(path, priority_set=False):
    """stations.conf から tuner=N / prefer=N を読み
    {tuner番号: [表示ラベル,...]} を返す。
    priority_set(TUNER_PRIORITY あり)のときは tuner=N でも他局が空き時間帯に
    使えるため「専用」ではなく「優先」と表示する(温存なしのため)。"""
    pinned = {}
    if not path.exists():
        return pinned
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[2]
        for opt in parts[3:]:
            o = opt.lower()
            # tuner=N: この局はこのチューナーしか使わない。TUNER_PRIORITY が
            #   設定されていれば他局も空き時間帯に使えるので「確保」、
            #   未設定なら他局は一切使えないので「専用」
            # prefer=N: 空いていればここ、埋まっていれば他へ = 「優先」
            pin_label = "確保" if priority_set else "専用"
            for key, label in (("tuner=", pin_label), ("prefer=", "優先")):
                if o.startswith(key):
                    try:
                        t = int(o.split("=", 1)[1])
                        pinned.setdefault(t, []).append(f"{name}{label}")
                    except ValueError:
                        pass
    return pinned


def pgrep(pattern):
    """pgrep -af pattern の結果行(自分自身を除く)を返す"""
    try:
        out = subprocess.run(["pgrep", "-af", pattern],
                             capture_output=True, text=True).stdout
    except FileNotFoundError:
        return []
    lines = []
    for line in out.splitlines():
        if "tuner_status" in line or "pgrep" in line:
            continue
        lines.append(line)
    return lines


def fmt_dt(dt, now):
    """日時を見やすく(今日なら時刻のみ、別日なら月/日+時刻)"""
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m/%d %H:%M")


def recent_failures(out_dir, now, hours=24):
    """録音ログから直近 hours 時間の失敗を集計する。
    戻り値: [(時刻, 種別, 番組名), ...] 新しい順"""
    import re
    fails = []
    since = now - timedelta(hours=hours)
    for logname, kind in (("sdr_radio.log", "SDR"),
                          ("record_error.log", "FM"),
                          ("radiko_live.log", "radiko")):
        path = Path(out_dir) / logname
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines[-2000:]:      # 末尾だけ見れば十分
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) 録音失敗 .*?: (.*)$", line)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts >= since:
                fails.append((ts, kind, m.group(2).strip()))
    fails.sort(reverse=True)
    return fails


def main():
    conf = load_conf(CONF_PATH)
    now = datetime.now()

    n_tuners = len(conf.get("RADIO_DEVS", "/dev/radio0").split())
    tuner_types = conf.get("TUNER_TYPES", "").split()
    if not tuner_types:
        tuner_types = ["shark"] * n_tuners
    n_tuners = max(n_tuners, len(tuner_types))
    while len(tuner_types) < n_tuners:
        tuner_types.append("shark")
    alsa_devs = conf.get("ALSA_DEVS", "").split()
    pinned = load_pinned(STATIONS_PATH,
                         priority_set=bool(conf.get("TUNER_PRIORITY", "").strip()))

    entries = load_entries()

    # 実プロセスの確認
    arecord_procs = pgrep("^arecord ")
    fmradion_procs = pgrep("^airspy-fmradion")
    radiko_procs = pgrep("radiko_live.sh")

    def tuner_recording(t, t_type):
        """tuner t が実際に録音中か(プロセスベース)"""
        if t_type == "shark":
            if t < len(alsa_devs):
                dev = alsa_devs[t]
                return any(dev in p for p in arecord_procs)
            return False
        else:  # airspy等のSDR
            return bool(fmradion_procs)

    print(f"=== チューナー使用状況  {now.strftime('%m/%d %H:%M')} ===")
    prio = conf.get("TUNER_PRIORITY", "").split()
    if prio:
        print(f"(自動割当の優先順: tuner{' → tuner'.join(prio)})")

    # 直近24時間の録音失敗。受信機のハング等で失敗が続いていても、radiko側が
    # 動いていると Podcast には番組が並ぶため気づきにくい。ここで目立たせる
    fails = recent_failures(conf.get("OUTPUT_DIR", "."), now)
    if fails:
        print()
        print(f"!!! 直近24時間に録音失敗 {len(fails)}件 !!!")
        for ts, kind, title in fails[:5]:
            print(f"  {ts.strftime('%m/%d %H:%M')} [{kind}] {title[:40]}")
        if len(fails) > 5:
            print(f"  ... ほか{len(fails) - 5}件")
        if any(k == "SDR" for _, k, _ in fails):
            print("  SDRの失敗が続く場合: ./sdr_usbreset.py でUSBリセット、"
                  "駄目なら Airspy を抜き差し")
    for t in range(n_tuners):
        t_type = tuner_types[t]
        pin_note = ""
        if t in pinned:
            pin_note = f" ※{'/'.join(pinned[t])}"
        print(f"\n[tuner{t}] ({t_type}){pin_note}")

        # いま: 台帳の録音中エントリ + プロセス確認
        active = [e for e in entries
                  if e["tuner"] == str(t) and e["start"] <= now < e["end"]]
        proc_rec = tuner_recording(t, t_type)
        if active:
            for e in active:
                mark = "" if proc_rec else " (プロセス未確認)"
                print(f"  録音中: {e['title']} "
                      f"(〜{fmt_dt(e['end'], now)}){mark}")
        elif proc_rec:
            print("  録音中: (台帳外の録音プロセスあり — 手動録音?)")
        else:
            print("  待機中")

        # これから: 未来の予約(開始時刻順、直近5件)
        future = sorted([e for e in entries
                         if e["tuner"] == str(t) and e["start"] > now],
                        key=lambda e: e["start"])
        if future:
            for e in future:
                print(f"  予約  : {fmt_dt(e['start'], now)}"
                      f"-{fmt_dt(e['end'], now)} {e['title']}")
        else:
            print("  予約  : なし")

    # radiko
    print("\n[radiko]")
    # tuner "R"=ライブ録音, "RT"=タイムフリー取得(旧台帳の"R"は両方混在)
    active_live = [e for e in entries
                   if e["tuner"] == "R" and e["start"] <= now < e["end"]]
    # at_jobが空のRTは「既存録音ありでタイムフリー取得をスキップした」
    # 記録なので表示しない(実際の取得ジョブではない)
    active_tf = [e for e in entries
                 if e["tuner"] == "RT" and e["start"] <= now < e["end"]
                 and e.get("at_job")]
    tf_procs = pgrep("radiko_timefree.sh|rec_radiko_ts")
    if active_live:
        for e in active_live:
            print(f"  録音中: {e['title']} (〜{fmt_dt(e['end'], now)})")
        if len(active_live) != len(radiko_procs):
            print(f"  (ライブ実プロセス: {len(radiko_procs)}本。"
                  f"差分は旧形式のタイムフリー窓か終了済み)")
    if active_tf:
        for e in active_tf:
            print(f"  取得中: {e['title']} (〜{fmt_dt(e['end'], now)})")
    if not active_live and not active_tf:
        if radiko_procs or tf_procs:
            print("  録音中: (台帳外のradikoプロセスあり)")
        else:
            print("  待機中")
    future_r = sorted([e for e in entries
                       if e["tuner"] in ("R", "RT") and e["start"] > now
                       and (e["tuner"] == "R" or e.get("at_job"))],
                      key=lambda e: e["start"])
    if future_r:
        for e in future_r:
            print(f"  予約  : {fmt_dt(e['start'], now)}"
                  f"-{fmt_dt(e['end'], now)} {e['title']}")
    else:
        print("  予約  : なし")

    # atジョブ数との突き合わせ(参考)
    # 録音中・取得中のエントリはatジョブが起動済みで atq に無いのが正常。
    # 比較対象は「開始前の予約」のみ。
    try:
        atq = subprocess.run(["atq"], capture_output=True,
                             text=True).stdout.strip()
        n_at = len(atq.splitlines()) if atq else 0
        n_future = len([e for e in entries if e["start"] > now])
        print(f"\n(atジョブ: {n_at}件 / 開始前の予約: {n_future}件)")
        if n_at < n_future:
            print("  注意: atジョブが開始前の予約より少ないようです。"
                  "schedule_programs.py の再実行を検討してください")
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    main()
