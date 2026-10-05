# KiCad の読み取り・検査・製造データ出力

このモジュールは、Python MCP サーバーから KiCad 本体を別プロセスで呼び出す小さなラッパーです。特定基板の部品番号、電源構成、寸法には依存しません。回路図や基板の自動修正、配線、ゾーン再充填は行いません。

## 必要なソフトウェア

- ホスト側 Python 3.11 以上。ラッパー自体の依存は標準ライブラリーのみ。
- KiCad と `kicad-cli`。実動作確認は KiCad **10.0.6 / macOS**。他の OS / KiCad バージョンでは利用する CLI オプションと `pcbnew` API の互換性を確認してください。
- `inspect_board` では `pcbnew` を import できる Python が別途必要です。通常の仮想環境に `pip install pcbnew` する構成を想定していません。KiCad 配布物または OS パッケージの Python バインディングを使用します。

探索順は以下です。明示的に指定した実行ファイルが使えないときは、別のバージョンへ黙って切り替えません。

| 用途 | 探索順 |
| --- | --- |
| CLI | 関数引数 `executable` → `KICAD_CLI` → PATH → macOS 標準配置 / 一般的な Linux 配置 / Windows Program Files |
| pcbnew Python | 関数引数 `python_executable` → `KICAD_PYTHON` → 現在の Python → macOS KiCad 同梱 Python → PATH の `python3` |

Linux でシステムの `pcbnew` が仮想環境から見えない場合は、`KICAD_PYTHON=/usr/bin/python3` のように指定してください。macOS では `wx.App` をネイティブ worker 内だけで作成します。Linux worker はディスプレイサーバーを要求する `wx.App` を作成しません。macOS 以外のネイティブ実行は本リポジトリーで実測していません。

## Python API

```python
from kicad_mcp.kicad import (
    inspect_board, run_drc, run_erc, export_netlist, export_gerbers,
)

board = "design/board.kicad_pcb"
schematic = "design/board.kicad_sch"

summary = inspect_board(board)
drc = run_drc(board, "build/drc")
erc = run_erc(schematic, "build/erc")
netlist = export_netlist(schematic, "build/netlist")
gerbers = export_gerbers(board, "build/gerbers")
```

出力先は、**新規ディレクトリーまたは既存の空ディレクトリー**を明示します。1 ファイルでも存在する出力先や、出力ディレクトリー自体がシンボリックリンクである場合は拒否します。同じ出力先を繰り返し利用するときは、前回結果を別途保管してから新しい空ディレクトリーを指定してください。ツールは既存データを削除しません。空ディレクトリーを確保するときは `.kicad-mcp-output.lock` を排他的に作成し、同じ空ディレクトリーへの並行実行でも一方だけが書き込めるようにします。この予約ファイルは成功・失敗にかかわらず残ります。

| 関数 | 内容 | タイムアウト初期値 |
| --- | --- | --- |
| `inspect_board(board_path, python_executable=None, timeout=60)` | 外形の閉鎖性・外形寸法・有効レイヤー・部品／パッド／ネット数・パッドのネット割当・重複リファレンス | 60 秒 |
| `run_drc(board_path, output_dir, executable=None, timeout=120)` | 全 severity、全トラックの DRC。対応する同名 `.kicad_sch` があるときだけ回路図との一致も確認 | 120 秒 |
| `run_erc(schematic_path, output_dir, executable=None, timeout=120)` | 全 severity の ERC と全シートの指摘件数集計 | 120 秒 |
| `export_netlist(schematic_path, output_dir, executable=None, timeout=120)` | KiCad XML ネットリスト | 120 秒 |
| `export_gerbers(board_path, output_dir, executable=None, timeout=120)` | 全銅箔層、表裏のマスク／シルク／ペースト、Edge.Cuts、Excellon ドリル | **各 CLI コマンド** 120 秒 |

すべてのタイムアウトは有限の正数に限定します（0、負数、NaN、Infinity を拒否）。成功時は JSON 化可能な `dict` を返します。入力ファイルや実行ファイルの不備、CLI の異常終了、タイムアウト、読み取り中の入力変更は例外になります。失敗途中にできた出力は診断のため残ります。そのディレクトリーへの再実行は拒否されます。

`inspect_board` 以外は `manifest.json` を出力し、実行コマンド、標準出力／標準エラー、入力 SHA-256、生成ファイルのサイズと SHA-256 を記録します。manifest 自身はハッシュ一覧に含みません。検査では同名プロジェクトのルール severity、除外、カスタムルールのハッシュも保存します。元の基板／回路図ファイルのハッシュを処理前後で照合します。**このチェックは引数で渡したファイルに対するもので、階層回路図全ファイルや外部ライブラリーの変更を監視する機能ではありません。**

## 結果の読み方

- `completed` は CLI が正常終了し、レポートを読めたことを表します。
- `passed` はレポート内の全指摘グループが空であることだけを表します。除外指摘が出力された場合も `passed` は false になります。
- `reported_counts` の `violations`、`unconnected_items`、`schematic_parity` は別々に確認できます。ERC は全シートの違反を集計します。
- `ignored_checks` は KiCad が出力した無効化済みルールをそのまま返します。プロジェクト内の無効化や除外も `rules` に残します。`--severity-all` を付けても、無効化された検査が実行される保証はありません。
- DRC の `schematic_parity_requested=false` は、回路図との一致検査を行っていないことを表します。
- `inspect_board` のパッドのネット名が一致しても、銅箔がつながっていることの証明にはなりません。Airwire は DRC、保存済み銅箔の GND 接続グループは別の GND 監査機能で確認してください。
- 検査件数ゼロは電気性能、実装可能性、熱、部品耐圧、電流容量、ケースとの干渉、実機での安定動作を保証しません。

## Gerber 出力の範囲

全銅箔層は `*.Cu` で指定し、4 層以上の基板で内層を落とさないようにしています。Gerber X2 とネット属性は有効、座標は絶対原点です。ドリルも同じ絶対原点と mm 単位で、PTH / NPTH を分けます。基板の保存済みプロット設定による原点や層選択には切り替えません。

出力前に DRC を自動実行することや、ゾーンを再充填することはありません。**必要なゾーン充填を KiCad で保存してから検査・出力してください。** 製造業者固有のビア充填／銅キャップ／スタックアップ／部品実装条件や発注書は生成しません。Gerber ビューアーでの目視と設計条件との照合は別作業です。

## テスト

```sh
python -m pytest -m "not integration" tests/test_kicad.py -v
python -m pytest -m integration tests/test_kicad.py -v
```

`dev` extra の pytest を利用します。ネイティブ KiCad が見つからない環境では integration 試験をスキップします。ネイティブ試験は一時ディレクトリーに **合成の 4 層基板**と空回路図を生成します。閉じた外形、2 個の未接続パッド、DRC の未接続報告、内層を含む 4 銅箔層の Gerber、ドリル、ERC、XML ネットリスト、元基板の SHA-256 不変を確認します。実製品の設計データはテストにもリポジトリーにも含めません。
