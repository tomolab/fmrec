#!/bin/bash
# install.sh : fmrec の基本セットアップ
#
# やること:
#   1. 必要なパッケージの導入(apt / pip)
#   2. 設定ファイルの雛形作成(fmrec.conf / stations.conf / keywords.txt)
#   3. スクリプトの実行権限
#   4. (任意) systemd ユニットの生成と有効化
#
# 使い方:
#   ./install.sh                 # 対話的に進める
#   ./install.sh --systemd       # systemd の常駐化まで行う
#   ./install.sh --no-apt        # パッケージ導入を飛ばす(手動で入れた場合)
#   ./install.sh --yes           # 確認をすべて y にする
#
# Airspy(SDR)を使う場合は、このあと ./install_sdr.sh を実行してください。
#
# 対応環境: Ubuntu 20.04(動作確認済み)。22.04 / 24.04、Raspberry Pi OS(Debian系)も想定

set -eu

FMREC_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${FMREC_DIR}"

DO_SYSTEMD=no
DO_APT=yes
ASSUME_YES=no
for arg in "$@"; do
    case "${arg}" in
        --systemd) DO_SYSTEMD=yes ;;
        --no-apt)  DO_APT=no ;;
        --yes|-y)  ASSUME_YES=yes ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "不明なオプション: ${arg}" >&2; exit 1 ;;
    esac
done

# ---------- ユーティリティ ----------
step() { echo; echo "=== $* ==="; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*" >&2; }
ask() {
    # ask "質問" → y なら 0
    [ "${ASSUME_YES}" = "yes" ] && return 0
    local ans
    read -r -p "  $1 [y/N] " ans
    [ "${ans}" = "y" ] || [ "${ans}" = "Y" ]
}
has() { command -v "$1" > /dev/null 2>&1; }

echo "fmrec インストール"
echo "  ディレクトリ: ${FMREC_DIR}"
echo "  ユーザー    : $(id -un)"

# ---------- 1. パッケージ ----------
if [ "${DO_APT}" = "yes" ]; then
    step "1. パッケージの導入"
    if ! has apt-get; then
        warn "apt が無い環境です。以下を手動で導入してください:"
        warn "  at alsa-utils ffmpeg curl python3 python3-pip git mbuffer"
    else
        PKGS="at alsa-utils ffmpeg curl python3 python3-pip git mbuffer"
        missing=""
        for p in ${PKGS}; do
            dpkg -s "${p}" > /dev/null 2>&1 || missing="${missing} ${p}"
        done
        if [ -n "${missing}" ]; then
            echo "  導入するパッケージ:${missing}"
            if ask "apt で導入しますか?"; then
                sudo apt-get update
                # shellcheck disable=SC2086
                sudo apt-get install -y ${missing}
                ok "導入完了"
            else
                warn "飛ばしました。後で手動導入してください:${missing}"
            fi
        else
            ok "必要なパッケージは揃っています"
        fi
    fi

    # atd(予約ジョブの実行デーモン)。動いていないと録音されない
    if has systemctl; then
        if systemctl is-active --quiet atd 2>/dev/null; then
            ok "atd は動作中"
        else
            echo "  atd(予約ジョブ実行デーモン)が動いていません"
            if ask "atd を有効化して起動しますか?(必須)"; then
                sudo systemctl enable --now atd
                ok "atd 起動"
            else
                warn "atd が無いと録音予約が実行されません"
            fi
        fi
    fi

    # pip: Podcast アイコン生成・局ロゴ合成用
    step "1b. Python パッケージ(cairosvg / pillow)"
    if python3 -c "import cairosvg, PIL" 2>/dev/null; then
        ok "cairosvg / pillow は導入済み"
    else
        if ask "pip で cairosvg と pillow を導入しますか?(Podcast のアイコン用)"; then
            pip3 install --user cairosvg pillow 2>/dev/null \
            || pip3 install --user --break-system-packages cairosvg pillow
            ok "導入完了"
        else
            warn "make_icons.py / fetch_logos.py が使えません(録音・配信は可能)"
        fi
    fi
fi

# ---------- 2. 設定ファイルの雛形 ----------
step "2. 設定ファイル"
make_conf() {
    # make_conf <実ファイル> <雛形>
    if [ -e "$1" ]; then
        ok "$1 は既にあります(変更しません)"
    elif [ -e "$2" ]; then
        cp "$2" "$1"
        ok "$1 を $2 から作成しました"
    else
        warn "$2 が見つかりません"
    fi
}
make_conf fmrec.conf     fmrec.conf.example
make_conf stations.conf  stations.conf.example
make_conf keywords.txt   keywords.txt.example

# fmrec.conf のパスを、このディレクトリと現在のユーザーに合わせて書き換える
if grep -q '/home/USER' fmrec.conf 2>/dev/null; then
    sed -i "s|/home/USER/fmrec|${FMREC_DIR}|g; s|/home/USER|${HOME}|g" fmrec.conf
    ok "fmrec.conf 内のパスを ${FMREC_DIR} / ${HOME} に置換しました"
fi
if grep -q '^OUTPUT_DIR="/var/lib/fmrec' fmrec.conf 2>/dev/null; then
    echo "  OUTPUT_DIR(録音の保存先)は既定の /var/lib/fmrec/recordings です"
    echo "  大容量ディスクを使う場合は fmrec.conf で変更してください(1時間番組≒100〜200MB)"
fi

# ---------- 3. 実行権限 ----------
step "3. 実行権限"
chmod +x ./*.sh ./*.py 2>/dev/null || true
ok "スクリプトに実行権限を付けました"

# ---------- 4. radiko タイムフリー用ツール ----------
step "4. rec_radiko_ts(radiko タイムフリー取得用、任意)"
if [ -x "${HOME}/rec_radiko_ts/rec_radiko_ts.sh" ]; then
    ok "${HOME}/rec_radiko_ts は導入済み"
elif has git && ask "GitHub から rec_radiko_ts を取得しますか?(radiko の放送後取得に必要)"; then
    git clone https://github.com/uru2/rec_radiko_ts "${HOME}/rec_radiko_ts"
    ok "取得完了: ${HOME}/rec_radiko_ts/rec_radiko_ts.sh"
    if grep -q '^#*RADIKO_TF_SCRIPT=' fmrec.conf; then
        sed -i "s|^#*RADIKO_TF_SCRIPT=.*|RADIKO_TF_SCRIPT=\"${HOME}/rec_radiko_ts/rec_radiko_ts.sh\"|" fmrec.conf
        ok "fmrec.conf の RADIKO_TF_SCRIPT を設定しました"
    fi
else
    warn "飛ばしました。タイムフリーを使うなら後で導入し、fmrec.conf の RADIKO_TF_SCRIPT に設定"
fi

# ---------- 5. systemd ----------
step "5. systemd(常駐化)"
gen_units() {
    # テンプレートのユーザー名・パスを置換して /tmp に生成
    local out="/tmp/fmrec-systemd.$$"
    mkdir -p "${out}"
    for f in systemd/fmrec-*.service systemd/fmrec-*.timer; do
        sed "s|User=USER|User=$(id -un)|; s|/home/USER/fmrec|${FMREC_DIR}|g" \
            "${f}" > "${out}/$(basename "${f}")"
    done
    echo "${out}"
}
if [ "${DO_SYSTEMD}" = "yes" ] || ask "systemd で常駐化しますか?(毎日の予約・配信サーバー・フィード更新)"; then
    if ! has systemctl; then
        warn "systemd が無い環境です。systemd/INSTALL.md を参照して手動で設定してください"
    else
        units=$(gen_units)
        sudo cp "${units}"/fmrec-*.service "${units}"/fmrec-*.timer /etc/systemd/system/
        sudo systemctl daemon-reload
        sudo systemctl enable --now fmrec-schedule.timer
        sudo systemctl enable --now fmrec-podcast.service
        sudo systemctl enable --now fmrec-podcast-feed.timer
        rm -rf "${units}"
        ok "有効化: fmrec-schedule.timer / fmrec-podcast.service / fmrec-podcast-feed.timer"
        systemctl list-timers --no-pager 2>/dev/null | grep fmrec || true
    fi
else
    echo "  飛ばしました。後で ./install.sh --systemd で行えます"
fi

# ---------- 完了 ----------
step "完了"
cat << EOF
次にやること:

  1. 設定を編集する
       nano fmrec.conf        # OUTPUT_DIR(録音先)、チューナー、PODCAST_BASE_URL
       nano stations.conf     # 受信する局(自分のエリアの行の # を外す)
       nano keywords.txt      # 録りたい番組名

  2. 受信機の確認
       radioSHARK : ./check_tuners.sh
       Airspy     : ./install_sdr.sh   (SDR のツールチェーンを構築)

  3. 動作確認
       ./schedule_programs.py      # 予約が並べば OK
       ./tuner_status.py           # 状況表示

詳しい手順は SETUP.md、日々の使い方は USAGE.md を参照してください。
EOF
