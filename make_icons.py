#!/usr/bin/env python3
"""
make_icons.py : Podcast用のカバーアート(ラジオアイコン)を生成する。
FM用・radiko用・統合用の3種類を、オリジナルのSVGから1400x1400のPNGで出力。
著作権フリー(このスクリプトが生成するオリジナル図形)。

使い方: ./make_icons.py [出力先ディレクトリ]
  省略時はカレントディレクトリに podcast_fm.png / podcast_radiko.png /
  podcast.png を生成する。
"""

import sys
from pathlib import Path

try:
    import cairosvg
except ImportError:
    sys.exit("cairosvg が必要です: pip install cairosvg --break-system-packages")


def radio_svg(accent, accent2, label, bg1, bg2, y_offset=0):
    """ラジオ受信機のSVGを返す。accent系は差し色、labelは下部の文字。
    y_offset: ラジオ本体の縦方向移動量(負で上へ)。局ロゴ合成用ベースは
    下部にロゴパネルが入るため、上へ寄せてバランスを取る。"""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="1400" viewBox="0 0 1400 1400">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{bg1}"/>
      <stop offset="1" stop-color="{bg2}"/>
    </linearGradient>
    <linearGradient id="body" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#f5f0e6"/>
      <stop offset="1" stop-color="#e2d8c3"/>
    </linearGradient>
    <radialGradient id="spk" cx="0.5" cy="0.45" r="0.6">
      <stop offset="0" stop-color="#4a4038"/>
      <stop offset="1" stop-color="#2a2420"/>
    </radialGradient>
  </defs>

  <!-- 背景 -->
  <rect width="1400" height="1400" rx="0" fill="url(#bg)"/>

  <g transform="translate(0,{y_offset})">
  <!-- 電波(アンテナから出る波) -->
  <g stroke="{accent}" stroke-width="26" fill="none" stroke-linecap="round" opacity="0.9">
    <path d="M 995 360 q 150 150 0 300"/>
    <path d="M 1075 300 q 230 210 0 420"/>
  </g>

  <!-- アンテナ -->
  <line x1="880" y1="470" x2="1010" y2="300" stroke="#3a332c" stroke-width="20" stroke-linecap="round"/>
  <circle cx="1010" cy="300" r="26" fill="{accent}"/>

  <!-- ラジオ本体 -->
  <rect x="300" y="470" width="800" height="560" rx="60" fill="url(#body)" stroke="#3a332c" stroke-width="14"/>

  <!-- スピーカー(左) -->
  <circle cx="520" cy="750" r="170" fill="url(#spk)" stroke="#3a332c" stroke-width="12"/>
  <g fill="#5c5147">
    <circle cx="520" cy="750" r="130" fill="none" stroke="#5c5147" stroke-width="8"/>
    <circle cx="520" cy="750" r="90" fill="none" stroke="#5c5147" stroke-width="8"/>
    <circle cx="520" cy="750" r="50" fill="none" stroke="#5c5147" stroke-width="8"/>
    <circle cx="520" cy="750" r="16"/>
  </g>

  <!-- チューニング表示窓(右) -->
  <rect x="740" y="560" width="300" height="150" rx="18" fill="#1c2a24" stroke="#3a332c" stroke-width="10"/>
  <g stroke="{accent2}" stroke-width="6">
    <line x1="770" y1="600" x2="770" y2="670"/>
    <line x1="810" y1="600" x2="810" y2="670"/>
    <line x1="850" y1="600" x2="850" y2="670"/>
    <line x1="890" y1="600" x2="890" y2="670"/>
    <line x1="930" y1="600" x2="930" y2="670"/>
    <line x1="970" y1="600" x2="970" y2="670"/>
    <line x1="1010" y1="600" x2="1010" y2="670"/>
  </g>
  <!-- 選局針 -->
  <line x1="890" y1="590" x2="890" y2="680" stroke="{accent}" stroke-width="10" stroke-linecap="round"/>

  <!-- ツマミ2つ(右下) -->
  <circle cx="820" cy="880" r="60" fill="#3a332c"/>
  <circle cx="820" cy="880" r="40" fill="{accent}"/>
  <circle cx="980" cy="880" r="60" fill="#3a332c"/>
  <circle cx="980" cy="880" r="40" fill="#c9c0b0"/>

  <!-- ラベル -->
  <rect x="740" y="960" width="300" height="12" rx="6" fill="#3a332c" opacity="0.3"/>
  {f'<text x="700" y="1210" font-family="Arial, sans-serif" font-size="180" font-weight="bold" fill="#ffffff" text-anchor="middle" letter-spacing="4">{label}</text>' if label else ''}
  </g>
</svg>'''


ICONS = {
    # ファイル名: (accent, accent2, label, bg1, bg2)
    "podcast_fm.png": ("#e8622a", "#f4a259", "FM",
                       "#2b6cb0", "#1a4971"),      # 青背景・橙差し色
    "podcast_radiko.png": ("#00a4b4", "#5ad1de", "radiko",
                           "#0d7d5a", "#075039"),  # 緑背景・シアン差し色
    "podcast.png": ("#e8622a", "#5ad1de", "RADIO",
                    "#6b46c1", "#432874"),         # 紫背景(統合用)
    # ラベル無しベース(局別フィード用。fetch_logos.py が下部に局ロゴを合成)
    "podcast_base.png": ("#e8622a", "#5ad1de", "",
                         "#6b46c1", "#432874"),
}


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, (accent, accent2, label, bg1, bg2) in ICONS.items():
        # 局ロゴ合成用ベースは下部パネルが入るため、ラジオを上へ寄せる
        y_off = -100 if fname == "podcast_base.png" else 0
        svg = radio_svg(accent, accent2, label, bg1, bg2, y_offset=y_off)
        png_path = out_dir / fname
        cairosvg.svg2png(bytestring=svg.encode("utf-8"),
                         write_to=str(png_path),
                         output_width=1400, output_height=1400)
        print(f"生成: {png_path}")


if __name__ == "__main__":
    main()
