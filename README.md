# KiCadMCP

KiCadの検査・製造データ出力と、SPICE実行・波形解析を行うローカルツール集です。Python CLIとMCP（stdio）の両方から呼び出せます。作業用スクリプトを整理し、基板名・部品番号・外形寸法・個人環境のパスに依存しない形にしています。

このリポジトリに含むのは**ツールのソース、テスト、使い方のドキュメント**です。実案件の回路図・PCB・ファームウェア・試験波形・測定結果・メーカー提供モデル・アプリ本体は含みません。既存のApache-2.0ライセンスを維持しています。

## 機能

| CLI | MCPツール | 内容 |
| --- | --- | --- |
| `inspect` | `inspect_board` | PCBの層・外形・部品・パッド・ネット等を読み取り |
| `drc` | `run_drc` | KiCadネイティブDRC、未配線、対応回路図との整合性を検査 |
| `erc` | `run_erc` | KiCadネイティブ回路図ERC |
| `netlist` | `export_netlist` | 回路図のXMLネットリストを出力 |
| `gerber` | `export_gerbers` | 保存済みPCBから全銅箔層のGerber・Excellonを出力 |
| `ground` | `audit_ground` | 保存済み2層銅箔のGND導通、穴・層間接続・孤立パッドを解析 |
| `spice-run` | `run_spice` | LTspice／ngspiceを時間制限付きで実行 |
| `raw-summary` | `raw_summary` | RAW波形の最小・最大・時間軸で重み付けした平均値 |
| `raw-csv` | `raw_csv` | RAWの保存点をCSVへ出力 |

KiCadのGUI操作や部品の自動配置・自動配線を行うサーバーではありません。既存の設計を検査し、生成物と検証の根拠を保存するためのツールです。

## 導入

Python 3.11以上を使います。KiCad、LTspice、ngspiceは必要なものを別途インストールしてください。

```bash
git clone https://github.com/FaBoAI/KiCadMCP.git
cd KiCadMCP
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[mcp,geometry]'
kicad-mcp --help
```

Windowsでは仮想環境の有効化に `.venv\Scripts\Activate.ps1` を使います。

- 基本CLI：`pip install -e .`（標準ライブラリのみ）
- MCP：`pip install -e '.[mcp]'`（MCP Python SDK 2.3系以上、3未満）
- GND解析：`pip install -e '.[geometry]'`（Shapely 2系）
- テスト・ビルド：`pip install -e '.[mcp,geometry,dev]'`

`pcbnew`は通常のPyPIパッケージとして導入せず、KiCadに対応するPythonを使用します。通常PythonとKiCad Pythonは別プロセスで動くため、同じ環境へインストールする必要はありません。

## 実行環境の指定

PATHや一般的なmacOSの配置から探索します。見つからない場合は環境変数を指定します。明示したパスが無効な場合、別のプログラムへ黙って切り替えません。

| 変数 | 指定するもの |
| --- | --- |
| `KICAD_CLI` | `kicad-cli`の実行ファイル |
| `KICAD_PYTHON` | `pcbnew`をimportできるPython実行ファイル |
| `LTSPICE_EXECUTABLE` | LTspice実行ファイル、またはmacOSの`.app` |
| `NGSPICE_EXECUTABLE` | ngspice実行ファイル |

macOSの例：

```bash
export KICAD_CLI="/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
export KICAD_PYTHON="/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
export LTSPICE_EXECUTABLE="/Applications/LTspice.app"
```

CLIでは `--executable` または `--python` でも指定できます。MCP側はサーバー起動環境の設定を使用します。

## CLIの例

以下の`design/`は利用者が用意する入力、`artifacts/`は出力先です。

```bash
kicad-mcp inspect design/board.kicad_pcb
kicad-mcp drc design/board.kicad_pcb --output artifacts/drc
kicad-mcp erc design/board.kicad_sch --output artifacts/erc
kicad-mcp netlist design/board.kicad_sch --output artifacts/netlist
kicad-mcp gerber design/board.kicad_pcb --output artifacts/gerber
kicad-mcp ground design/board.kicad_pcb --net GND --output artifacts/ground

kicad-mcp spice-run design/test.cir --engine ltspice --timeout 120 --output artifacts/spice
kicad-mcp spice-run design/test.cir --engine ngspice --timeout 60 --output artifacts/spice
kicad-mcp raw-summary artifacts/spice/spice-run-XXXX/simulation.raw \
  --signals 'V(out)' --start 0.001 --end 0.005
kicad-mcp raw-csv artifacts/spice/spice-run-XXXX/simulation.raw \
  --signals 'V(out)' --output artifacts/wave.csv
```

`spice-run-XXXX`は実行ごとに発行されるディレクトリです。実際のパスはJSONの結果で確認してください。ngspiceで生成したバイナリRAWを直接読む場合は、`--binary-format ngspice`を指定します。

通常の結果は標準出力へJSONで返します。引数・実行環境の問題は標準エラーへJSONを返し終了コード2、検査不合格またはSPICEの数値完走未確認は終了コード1です。終了コード0も回路の実機性能を保証するものではありません。

## MCPの接続

サーバーはローカルのstdio方式です。作業対象ディレクトリを明示して起動します。

```bash
kicad-mcp serve --workspace /absolute/path/to/project
```

`mcpServers`形式に対応するMCPクライアント向け設定例です。実際の絶対パスへ置き換えてください。クライアントによって設定ファイルの形式は異なります。

```json
{
  "mcpServers": {
    "kicad": {
      "command": "/absolute/path/to/KiCadMCP/.venv/bin/kicad-mcp",
      "args": ["serve", "--workspace", "/absolute/path/to/project"],
      "env": {
        "KICAD_CLI": "/absolute/path/to/kicad-cli",
        "KICAD_PYTHON": "/absolute/path/to/python-with-pcbnew",
        "LTSPICE_EXECUTABLE": "/absolute/path/to/LTspice.app"
      }
    }
  }
}
```

MCPのパス引数はworkspaceからの相対パスでも指定できます。`..`やシンボリックリンクでworkspace外を指定する呼び出しは拒否します。実行ファイルの場所はサーバーの環境設定で管理します。

この制限は入口のパス確認です。KiCadの参照ライブラリ、SPICEの`.include`や`.control`まで隔離するOSサンドボックスではありません。自分で内容を確認したローカル設計・ネットリストを使用してください。

## 検証結果の読み方

- **DRC／ERC**：適用したKiCad規則での幾何・接続検査です。除外・無効化規則も結果に表示します。
- **GND解析**：保存済み銅箔の位相的導通を確認します。インピーダンス、EMI、熱性能の評価は含みません。
- **Gerber**：自動ゾーン再充填をせず、保存状態を出力します。必要な充填はKiCadで実施・保存してから実行してください。
- **SPICE**：実行終了、タイムアウト、ログ異常、RAWの形式、過渡解析の要求終了時刻への到達を別々に扱います。回路の仕様合格やモデルの妥当性を自動判定しません。
- **RAW解析**：実数の単一プロットが対象です。複素数、FastAccess、複数プロットなど未対応形式を推測して読みません。

[KiCadの詳細](docs/kicad.md) / [GND解析の詳細](docs/ground.md) / [SPICE・RAWの詳細](docs/spice.md)

## テスト

```bash
python -m pip install -e '.[mcp,geometry,dev]'
python -m pytest -m 'not integration'
python -m pytest -m integration
python -m build
```

テスト用の小さなPCB・回路図・RCネットリスト・RAWは一時ディレクトリ内で生成します。実案件の設計データは使いません。ネイティブアプリが必要な試験は、環境にない場合にskipされるため、実行結果のskip件数も確認してください。

2026-10-05のローカル確認では、Python 3.13・KiCad 10.0.6・LTspice 26.0.2（Mac版）・ngspice revision 26を使用し、74試験と54の追加条件が合格しました。Linux／Windows上のネイティブアプリ連携は未確認です。

MCPはツール一覧の取得だけでなく、実stdio接続から波形集計・パス境界の検査を行います。GitHub ActionsはPython 3.11／3.13の単体試験とパッケージビルドを実施し、KiCadやSPICE本体の試験はローカルで行います。

## 配布範囲と依存先

ソースコードはApache-2.0です（[LICENSE](LICENSE)）。外部アプリやモデルの利用条件は各提供元のものに従います。エンジン・メーカーSPICEモデル・KiCadライブラリを再配布する機能はありません。

- [KiCad CLI公式ドキュメント](https://docs.kicad.org/10.0/en/cli/cli.html)
- [Analog Devices LTspice](https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html)
- [ngspice](https://ngspice.sourceforge.io/)
- [公式MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
