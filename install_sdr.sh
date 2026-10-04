#!/bin/bash
# install_sdr.sh : Airspy(SDR)で FM を録音するためのツールチェーン構築
#
# やること(SETUP.md 4章の自動化):
#   1. カーネルの airspy ドライバを無効化(libairspy との二重管理を防ぐ)
#   2. libairspy を最新ソースからビルド(apt 版 1.0.9 は停止時にハングする)
#   3. libairspyhf を最新ソースからビルド(apt 版が古いと fmradion がビルドできない)
#   4. cmake 3.18 以上を用意(Ubuntu 20.04 は 3.16 なので pip で導入)
#   5. airspy-fmradion をビルド
#   6. 動作確認(認識・受信・停止テスト)
#
# 使い方:
#   ./install_sdr.sh            # 対話的に進める
#   ./install_sdr.sh --yes      # 確認をすべて y にする
#
# 実行後、fmrec.conf に以下を設定してください:
#   TUNER_TYPES="airspy"                   (Airspy のみ) または "shark airspy"
#   SDR_RECORD_SCRIPT="<fmrecのパス>/sdr_radio.sh"
#   SDR_SAMPLE_RATE="3000000"
#   SDR_DEVICE_CONF="lgain=5,mgain=3,vgain=5"   (環境に合わせて sdr_gaintest.sh で調整)

set -eu

FMREC_DIR="$(cd "$(dirname "$0")" && pwd)"
ASSUME_YES=no
for arg in "$@"; do
    case "${arg}" in
        --yes|-y) ASSUME_YES=yes ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "不明なオプション: ${arg}" >&2; exit 1 ;;
    esac
done

step() { echo; echo "=== $* ==="; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*" >&2; }
ask() {
    [ "${ASSUME_YES}" = "yes" ] && return 0
    local ans; read -r -p "  $1 [y/N] " ans
    [ "${ans}" = "y" ] || [ "${ans}" = "Y" ]
}
has() { command -v "$1" > /dev/null 2>&1; }
NPROC=$(nproc 2>/dev/null || echo 2)
SRC="${HOME}/src"
mkdir -p "${SRC}"

echo "fmrec SDR(Airspy)セットアップ"
echo "  ソースは ${SRC} 配下に取得します"

# ---------- 0. 依存パッケージ ----------
step "0. ビルド用パッケージ"
PKGS="build-essential cmake pkg-config git libusb-1.0-0-dev libasound2-dev \
      librtlsdr-dev libsndfile1-dev portaudio19-dev mbuffer"
# VOLK は環境でパッケージ名が違う
if apt-cache show libvolk2-dev > /dev/null 2>&1; then PKGS="${PKGS} libvolk2-dev"
elif apt-cache show libvolk-dev > /dev/null 2>&1; then PKGS="${PKGS} libvolk-dev"
else PKGS="${PKGS} libvolk1-dev"; fi
missing=""
for p in ${PKGS}; do dpkg -s "${p}" > /dev/null 2>&1 || missing="${missing} ${p}"; done
if [ -n "${missing}" ]; then
    echo "  導入:${missing}"
    ask "apt で導入しますか?" && { sudo apt-get update; sudo apt-get install -y ${missing}; }
else
    ok "揃っています"
fi

# ---------- 1. カーネルドライバの無効化 ----------
step "1. カーネルの airspy ドライバを無効化"
if [ -f /etc/modprobe.d/blacklist-airspy.conf ]; then
    ok "設定済み(/etc/modprobe.d/blacklist-airspy.conf)"
else
    echo "  Linux 標準の V4L2 airspy ドライバが先にデバイスを掴むと、libairspy と"
    echo "  二重管理になり「開けない」「不安定」の原因になります"
    if ask "無効化しますか?(必須)"; then
        echo "blacklist airspy" | sudo tee /etc/modprobe.d/blacklist-airspy.conf > /dev/null
        sudo modprobe -r airspy 2>/dev/null || warn "モジュールが使用中で外せません。後で再起動してください"
        ok "無効化しました"
    fi
fi

# ---------- 2. libairspy ----------
step "2. libairspy(最新ソースから)"
need_libairspy=yes
if has airspy_lib_version; then
    ver=$(airspy_lib_version 2>/dev/null | grep -o '[0-9.]*' | head -1 || true)
    case "${ver}" in
        1.0.1[0-9]*|1.[1-9]*) ok "libairspy ${ver}(十分新しい)"; need_libairspy=no ;;
        *) echo "  現在の libairspy: ${ver:-不明}(古い。1.0.9 は停止時にハングする不具合あり)" ;;
    esac
fi
if [ "${need_libairspy}" = "yes" ] && ask "libairspy を GitHub からビルドして置き換えますか?(推奨)"; then
    sudo apt-get remove -y libairspy-dev libairspy0 airspy 2>/dev/null || true
    if [ -d "${SRC}/airspyone_host" ]; then (cd "${SRC}/airspyone_host" && git pull -q)
    else git clone -q https://github.com/airspy/airspyone_host "${SRC}/airspyone_host"; fi
    (cd "${SRC}/airspyone_host" && rm -rf build && mkdir build && cd build \
     && /usr/bin/cmake .. -DINSTALL_UDEV_RULES=ON > /dev/null && make -j"${NPROC}" > /dev/null \
     && sudo make install > /dev/null && sudo ldconfig)
    hash -r
    ok "libairspy $(airspy_lib_version 2>/dev/null | grep -o '[0-9.]*' | head -1) を導入"
    ok "udev ルールも導入(/etc/udev/rules.d/52-airspy.rules)"
    sudo udevadm control --reload-rules && sudo udevadm trigger
fi
if ! id -nG | grep -qw plugdev; then
    echo "  一般ユーザーで Airspy を開くには plugdev グループが必要です"
    ask "$(id -un) を plugdev に追加しますか?" && { sudo usermod -aG plugdev "$(id -un)"; warn "再ログイン後に有効になります"; }
fi

# ---------- 3. libairspyhf ----------
step "3. libairspyhf(airspy-fmradion のビルドに必要)"
# apt 版が古いと airspyhf_is_low_if が無くてビルドに失敗する
if grep -q airspyhf_is_low_if /usr/include/libairspyhf/airspyhf.h /usr/local/include/libairspyhf/airspyhf.h 2>/dev/null; then
    ok "十分新しい libairspyhf があります"
elif ask "libairspyhf を GitHub からビルドしますか?(apt 版が古い環境で必要)"; then
    sudo apt-get remove -y libairspyhf-dev libairspyhf1 2>/dev/null || true
    if [ -d "${SRC}/airspyhf" ]; then (cd "${SRC}/airspyhf" && git pull -q)
    else git clone -q https://github.com/airspy/airspyhf "${SRC}/airspyhf"; fi
    (cd "${SRC}/airspyhf" && rm -rf build && mkdir build && cd build \
     && /usr/bin/cmake .. > /dev/null && make -j"${NPROC}" > /dev/null \
     && sudo make install > /dev/null && sudo ldconfig)
    ok "libairspyhf を導入"
fi

# ---------- 4. cmake ----------
step "4. cmake(3.18 以上)"
CMAKE=/usr/bin/cmake
cm_ver=$(/usr/bin/cmake --version 2>/dev/null | head -1 | grep -o '[0-9]*\.[0-9]*' | head -1 || echo 0)
if [ "$(printf '%s\n3.18\n' "${cm_ver}" | sort -V | head -1)" = "3.18" ]; then
    ok "cmake ${cm_ver}"
else
    echo "  システムの cmake は ${cm_ver}(airspy-fmradion は 3.18 以上が必要)"
    if [ -x "${HOME}/.local/bin/cmake" ]; then
        CMAKE="${HOME}/.local/bin/cmake"; ok "pip 版 cmake を使用: ${CMAKE}"
    elif ask "pip で新しい cmake を導入しますか?"; then
        pip3 install --user cmake 2>/dev/null || pip3 install --user --break-system-packages cmake
        CMAKE="${HOME}/.local/bin/cmake"; ok "導入: ${CMAKE}"
    fi
fi

# ---------- 5. airspy-fmradion ----------
step "5. airspy-fmradion"
if has airspy-fmradion && ! ask "airspy-fmradion は導入済みです。再ビルドしますか?"; then
    ok "既存の airspy-fmradion を使います"
else
    if [ -d "${SRC}/airspy-fmradion" ]; then (cd "${SRC}/airspy-fmradion" && git pull -q && git submodule update --init -q)
    else git clone -q --recursive https://github.com/jj1bdx/airspy-fmradion "${SRC}/airspy-fmradion"; fi
    echo "  ビルド中(数分かかります)..."
    (cd "${SRC}/airspy-fmradion" && rm -rf build && mkdir build && cd build \
     && "${CMAKE}" .. > /dev/null && make -j"${NPROC}" > /dev/null && sudo make install > /dev/null)
    hash -r
    ok "airspy-fmradion $(airspy-fmradion 2>&1 | grep -o 'Version [0-9-]*' | head -1)"
fi

# ---------- 6. 動作確認 ----------
step "6. 動作確認"
if ! lsusb 2>/dev/null | grep -q 1d50:60a1; then
    warn "Airspy が USB に見つかりません。接続してから以下を手動で確認してください:"
    warn "  airspy_info"
    warn "  timeout 10 airspy-fmradion -t airspy -q -c freq=80000000,srate=3000000 -W /tmp/t.wav"
else
    if airspy_info 2>&1 | grep -q "Found AirSpy"; then
        ok "認識: $(airspy_info 2>&1 | grep -o 'AirSpy MINI\|AirSpy R2\|Board ID[^,]*' | head -1)"
    else
        warn "airspy_info で開けません。plugdev への追加後の再ログイン、または Airspy の抜き差しを"
    fi
    echo "  停止テスト(10秒受信して正常に終了するか。ハングすれば libairspy が古い)"
    if timeout 15 airspy-fmradion -t airspy -q -c freq=80000000,srate=3000000 -W /tmp/fmrec_sdrtest.wav > /dev/null 2>&1 \
       || [ $? -eq 124 ]; then
        if [ -s /tmp/fmrec_sdrtest.wav ]; then
            ok "受信・停止とも正常(/tmp/fmrec_sdrtest.wav)"
        else
            warn "出力が空です。デバイスが開けていない可能性"
        fi
    else
        warn "停止テストに失敗しました(タイムアウト超過=ハング)。SDR.md の「停止がハングする場合」を参照"
    fi
    rm -f /tmp/fmrec_sdrtest.wav
fi

# ---------- 完了 ----------
step "完了"
cat << EOF
fmrec.conf に以下を設定してください:

  TUNER_TYPES="airspy"                       # Airspy のみ。radioSHARK と併用なら "shark airspy"
  SDR_RECORD_SCRIPT="${FMREC_DIR}/sdr_radio.sh"
  SDR_SAMPLE_RATE="3000000"
  SDR_DEVICE_CONF="lgain=5,mgain=3,vgain=5"   # 局ごとの最適値は ./sdr_gaintest.sh <周波数> で

弱い局・遠い局は stations.conf の局行に sdr= で個別設定できます:
  YFM  84.7  FMヨコハマ  both  tuner=0  sdr=lgain=12,mgain=8,vgain=12,multipath=288

詳細は SDR.md を参照してください。
EOF
