#!/usr/bin/env python3
"""
schedule_lib.py : 予約台帳(registered.log)を操作する補助ツール。
stop_recording.sh から呼ばれ、手動停止した番組を台帳から削除して
次回の schedule_programs.py 実行で再録音できるようにする。

使い方:
  schedule_lib.py remove-active --tuner 0        # tuner0で録音中の番組を削除
  schedule_lib.py remove-active --radiko FMT     # radiko FMT で録音中の番組を削除
  schedule_lib.py remove-active --radiko ""      # radiko録音中の番組を全て削除
台帳から削除された番組は、放送時間内なら次回実行で途中から再録音される。
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCHEDULE_LOG = Path(
    os.environ.get("FMREC_SCHEDULE_LOG", BASE_DIR / "registered.log"))
TIME_FMT = "%Y%m%d%H%M%S"


def load_entries():
    entries = []
    if not SCHEDULE_LOG.exists():
        return entries
    for line in SCHEDULE_LOG.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        entries.append({
            "prog_id": parts[0],
            "tuner": parts[1],
            "start": parts[2],
            "end": parts[3],
            "at_job": parts[4] if len(parts) > 4 else "",
            "title": parts[5] if len(parts) > 5 else "",
            "_raw": line,
        })
    return entries


def save_entries(entries):
    lines = [e["_raw"] for e in entries]
    SCHEDULE_LOG.write_text("\n".join(lines) + ("\n" if lines else ""))


def is_active(e, now):
    """今まさに録音中のエントリか(開始済み かつ 未終了)。
    未来の予約(まだ始まっていない)は対象外。"""
    try:
        start = datetime.strptime(e["start"], TIME_FMT)
        end = datetime.strptime(e["end"], TIME_FMT)
    except ValueError:
        return False
    return start <= now < end


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    rm = sub.add_parser("remove-active")
    rm.add_argument("--tuner", default=None,
                    help="FMチューナー番号(0,1,...)")
    rm.add_argument("--radiko", default=None,
                    help="radiko局ID(空文字なら全radiko)")
    args = ap.parse_args()

    if args.cmd != "remove-active":
        ap.error("未知のコマンド")

    entries = load_entries()
    now = datetime.now()
    removed = []
    kept = []
    for e in entries:
        match = False
        if args.tuner is not None:
            # FM: tuner番号一致 かつ 録音中
            if e["tuner"] == args.tuner and is_active(e, now):
                match = True
        elif args.radiko is not None:
            # radiko: tuner=="R"(ライブ録音)のみ対象。
            # "RT"(タイムフリー取得窓)は含めない — stop_recordingは
            # 取得プロセスを止めないし、窓を台帳から消すと次回実行で
            # 再取得予約が作られて二重取得になるため。
            # 局ID指定があればprog_idで絞る
            if e["tuner"] == "R" and is_active(e, now):
                if args.radiko == "" or \
                        e["prog_id"].startswith(f"{args.radiko}_"):
                    match = True
        if match:
            removed.append(e)
        else:
            kept.append(e)

    if removed:
        save_entries(kept)
        import subprocess
        for e in removed:
            # 残っている at ジョブがあればキャンセル
            # (放送中に予約起動前のジョブが残っている場合など)
            if e["at_job"]:
                try:
                    subprocess.run(["atrm", e["at_job"]],
                                   stderr=subprocess.DEVNULL)
                except FileNotFoundError:
                    pass  # atrm が無い環境でも台帳操作は完遂する
            print(f"台帳から削除(再録音可能): {e['title']} "
                  f"[{e['prog_id']}]")
    else:
        print("削除対象の録音中エントリはありませんでした")


if __name__ == "__main__":
    main()
