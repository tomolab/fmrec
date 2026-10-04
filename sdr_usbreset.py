#!/usr/bin/env python3
"""sdr_usbreset.py : Airspy を USB レベルでリセットする(抜き差し相当)

Airspy がハングして「デバイスは開けるがストリームが来ない」「録音が0秒で
終わる」状態になったとき、物理的な抜き差しで復旧することがある。
これをソフトウェアから行う(USBDEVFS_RESET ioctl)。

デバイスノード(/dev/bus/usb/BBB/DDD)に書き込み権限があれば root 不要
(udev ルールで plugdev に開放してあれば一般ユーザーで動く)。

使い方:
  ./sdr_usbreset.py            # Airspy(1d50:60a1)を探してリセット
  ./sdr_usbreset.py --check    # リセットせず、見つかるかだけ確認

終了コード: 0=成功 / 1=デバイスが見つからない / 2=リセット失敗(権限など)
"""
import fcntl
import os
import sys
import time
from pathlib import Path

VENDOR, PRODUCT = "1d50", "60a1"          # Airspy Mini / R2
USBDEVFS_RESET = 0x5514                   # _IO('U', 20)


def find_airspy():
    """sysfs から Airspy を探し、(busnum, devnum, sysfsパス) を返す"""
    for dev in Path("/sys/bus/usb/devices").glob("*"):
        try:
            vid = (dev / "idVendor").read_text().strip()
            pid = (dev / "idProduct").read_text().strip()
        except (OSError, FileNotFoundError):
            continue
        if vid == VENDOR and pid == PRODUCT:
            bus = int((dev / "busnum").read_text())
            num = int((dev / "devnum").read_text())
            return bus, num, dev
    return None


def main():
    check_only = "--check" in sys.argv
    found = find_airspy()
    if not found:
        print("Airspy が USB 上に見つかりません(接続・電源を確認)", file=sys.stderr)
        return 1
    bus, num, sysdev = found
    node = f"/dev/bus/usb/{bus:03d}/{num:03d}"
    print(f"Airspy: {node} ({sysdev.name})")
    if check_only:
        return 0
    try:
        fd = os.open(node, os.O_WRONLY)
    except PermissionError:
        print(f"{node} に書き込めません。udev ルール(plugdev)と所属グループを確認",
              file=sys.stderr)
        return 2
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    except OSError as e:
        print(f"リセット失敗: {e}", file=sys.stderr)
        return 2
    finally:
        os.close(fd)
    # 再列挙を待つ(デバイス番号が変わることがある)
    for _ in range(20):
        time.sleep(0.5)
        again = find_airspy()
        if again:
            b, n, _ = again
            print(f"リセット完了: /dev/bus/usb/{b:03d}/{n:03d}")
            return 0
    print("リセット後に再認識されません。物理的な抜き差しを試してください",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
