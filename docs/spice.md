# SPICE 実行・RAW 解析ツール

Python は外部シミュレーターを起動し、保存された波形を集計します。数値計算のソルバーは **LTspice または ngspice** です。このリポジトリにはメーカー製ソルバー、部品モデル、実案件の回路・波形・試験結果を含めません。

## 実行環境

- Python 3.11 以上。SPICE/RAW 部分は Python 標準ライブラリだけで動作します。
- LTspice または ngspice を別途インストールしてください。
- LTspice は `executable` に実行ファイルまたは macOS の `LTspice.app` を指定します。省略時は `LTSPICE_EXECUTABLE`、標準の macOS アプリ配置、PATH を順に探します。
- ngspice は `executable`、`NGSPICE_EXECUTABLE`、PATH の順に探します。KiCad 内蔵の共有ライブラリを ctypes で読み込む方式は、このツールでは使用しません。**ngspice CLI が必要です。**

macOS LTspice 26 の公式アプリに含まれる Wine ランチャーと、従来のアプリ内バイナリを区別します。Wine を別途インストールする必要はありません。任意の Wine 構成やドライブマッピングを自動設定する機能はありません。

## Python API

```python
from kicad_mcp.spice import run_spice
from kicad_mcp.raw import read_raw, summarize_raw, write_csv

result = run_spice(
    "/path/to/trusted/rc.cir",
    engine="ltspice",
    executable="/Applications/LTspice.app",
    timeout=60,
    output_dir="/path/to/results",
)
print(result["completed"], result["numerical_complete"], result["timed_out"])

if result["raw_valid"]:
    stats = summarize_raw(
        result["raw_path"],
        signals=["V(out)"],
        start=0.004,
        end=0.005,
        binary_format=result["engine"],
    )
    print(stats)
    write_csv(result["raw_path"], "/path/to/new-waveform.csv",
              binary_format=result["engine"])
```

`netlist` は文字列形式の回路本文ではなく、既存ファイルのパスです。対応拡張子は `.cir`、`.sp`、`.spice`、`.net`、`.ckt` です。`.asc` や `.kicad_sch` から SPICE ネットリストを自動生成しません。

実行ごとに `output_dir/spice-run-<一意な値>/` を新規作成します。`output_dir` 省略時は OS の一時ディレクトリに作成します。元の回路ファイルや既存波形を上書き・削除せず、実行後もログと波形を残します。呼び出し側で不要になった実行ディレクトリを片付けてください。

`run_spice()` の主な返り値:

| キー | 意味 |
|---|---|
| `command` | 実際に使用した引数配列。シェル文字列として再解釈しません |
| `source_sha256` / `staged_netlist_sha256` | 入力と実行用コピーの SHA-256。モデルファイル全体のハッシュではありません |
| `run_dir` / `log_path` / `launcher_log_path` / `raw_path` | 保存した証跡へのパス |
| `returncode` / `timed_out` | 外部プロセスの終了コード、タイムアウトの有無 |
| `fatal` / `fatal_lines` | ログで検出した致命的診断。全バージョンの全診断を網羅するものではありません |
| `process_success` | 終了コード 0、タイムアウトなし、検出した致命的診断なし |
| `raw_valid` / `raw_error` | 対応する RAW として構文・データ長を読めたか。回路の妥当性ではありません |
| `native_completion_marker` | ソルバー固有の終了を示すログ文字列を検出したか |
| `requested_stop_s` | 単一の数値リテラル `.tran` から読み取れた終了時刻 |
| `numerical_complete` | 波形が有限値で、時刻が単調増加し、要求終了時刻まで保存されたか。自動判定対象外は `null` |
| `completed` | `process_success`・`raw_valid`・`numerical_complete == true` をすべて満たしたか |
| `verification_scope` | 自動完了判定の範囲、または判定できない理由 |

**`completed=true` は、回路が仕様を満たすという合格判定ではありません。** 実機の症状解消、モデルの忠実度、電池の特性、部品定格、熱設計、基板全体の動作を保証しません。メーカー提供モデルと近似モデルの区別、試験条件、合否基準は利用者が別途管理します。

`.param` 等の式で終了時刻を指定した解析、`.step`、複数解析、`.control`、AC/DC/OP 等は、起動できても自動の数値完了判定を行いません。`process_success=true` でも `numerical_complete=null`、`completed=false` になります。AC などの複素数 RAW は未対応です。

タイムアウト時は、この呼び出しが起動したプロセスグループだけを停止します。共有 Wine サーバーや名前が一致する別プロセスを一括終了しません。ランチャーから分離された子プロセスは残る可能性があり、Windows では直接の子プロセスのみを停止します。終了処理の猶予と RAW 読込時間は `timeout` に含まれません。

## include と実行上の注意

実行用コピーでは、入力と同じディレクトリから解決できる `.include` / `.inc` / `.lib` のファイル参照を絶対パスに変更し、`include_rewrites` に記録します。元のモデルをコピー・配布しません。解決できない名前はソルバーのライブラリ検索に委ねます。モデル内部の入れ子の参照や独自の検索パスは再帰的に修正しないため、必要なら元のネットリスト・モデル側でパスを整えてください。LTspice 26 macOS では公式バンドルの Y:（ホーム）/Z:（ルート）の対応を使用します。

ngspice の実行は `-n` を付けるため、ユーザーの `.spiceinit` を読みません。互換モードやモデルパスに依存する回路では、回路内の設定を明示してください。`-b -r` を使うため、ngspice の版・解析指定によって `.measure` の実行に制約があります。このツールは `.measure` の実行成功を保証せず、保存された RAW から統計を計算します。

SPICE デッキには制御コマンドや外部モデルの読込みを記述できます。`run_spice()` は OS のサンドボックスではありません。内容を確認した信頼できるデッキを明示して実行してください。

## RAW 対応範囲

| 形式 | 対応 |
|---|---|
| LTspice real binary、先頭変数 float64・他変数 float32 | 対応 |
| LTspice `double` フラグ、全変数 float64 | 対応 |
| ngspice real binary、全変数 float64、little endian | `binary_format="ngspice"` で対応 |
| real ASCII `Values:` | 対応 |
| UTF-8 / UTF-16 LE ヘッダー、BOM 付き UTF-16 BE | 対応 |
| CRLF / LF ヘッダー | 対応 |
| complex / FastAccess | 明示的に拒否 |
| 複数 plot の連結ファイル | 明示的に拒否 |
| 切り詰め・余分なバイナリ・変数数不一致 | 明示的に拒否 |

`binary_format="auto"` はヘッダーに ngspice の識別文字がある場合に全変数 float64 を選択し、それ以外は LTspice のフラグに従います。**ngspice の RAW には識別文字がない版もあるため、生成元が分かる場合は必ず `binary_format="ngspice"` を指定してください。** 自動でバイト数が合う方を試す推測は行いません。ngspice の native-endian binary を他のエンディアンの機械で生成したファイルは対象外です。

`read_raw()` は `RawData` を返します。`variables` は保存順の変数名、`vectors` は `dict[str, array('d')]`、`axis` は先頭変数の配列です。`header`、`flags`、`plot_name`、`storage`、`point_count` も取得できます。すべての波形値はメモリに読み込みます。非常に大きい RAW ではファイルサイズより多くのメモリを使用します。

`summarize_raw()` は指定区間の最小・最大・独立変数で重み付けした平均 `axis_weighted_mean` を返します。時間軸の場合は台形積分による時間平均です。区間端は線形補間し、適応刻みの単純な算術平均に置き換えません。保存されていないピークは検出できません。非有限値を含む統計は `null`、`all_finite=false` です。時間の巻戻り・重複を含むステップ解析は混ぜて集計せず、エラーにします。

`write_csv()` は元の保存点をそのまま書き出し、再サンプリングしません。出力ファイルが存在する場合は上書きせずエラーにします。

## テストと確認範囲

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_raw.py' -v
PYTHONPATH=src python -m unittest discover -s tests -p 'test_spice.py' -v
```

テスト中に一時ディレクトリへ合成 RAW と短いダミーソルバーを生成します。外部 SPICE を単体テストで自動起動しません。タイムアウト、終了コード 0 でも致命的ログがある場合、途中終了、RAW 欠損、古い結果の混入防止、不正なフォーマット、適応刻みの平均を確認します。

2026-10-05 に macOS 上で、別途生成した **1 kΩ / 1 µF の小さな RC 回路だけ**を実エンジンで確認しました。確認時の式は `V(out, t)=1-exp(-t/1 ms)`、終了時刻は 5 ms です。

| 実エンジン | バージョン表示 | RAW 保存点 | 5 ms の V(out) | 自動数値完了 |
|---|---|---:|---:|---|
| LTspice | 26.0.2 for MacOS（アプリバンドル 26.0.2.1） | 518 | 0.99326235 V | true |
| ngspice CLI | ngspice revision 26 | 516 | 0.99326233 V | true |

使用したコマンド形式は次のとおりです。`$APP` と各ファイルパスは検証環境の実パスに置き換えて実行しました。

```sh
"$APP/Contents/SharedSupport/ltspice/bin/wine" \
  --bottle ltspice --wait --workdir 'C:/Program Files/ADI/LTspice' \
  LTspice.exe -b -Run 'Z:\path\to\run\simulation.cir'

ngspice -b -n -r /path/to/run/simulation.raw \
  -o /path/to/run/simulation.log /path/to/run/simulation.cir
```

この確認は RC の実行・波形読込みを確認したもので、別の回路・モデルや全 OS の動作確認ではありません。実案件のデータやメーカーのモデルはコミットしていません。

参考: [Analog Devices の LTspice リファレンス](https://github.com/analogdevicesinc/ltspice-reference)、[ngspice 公式マニュアル](https://ngspice.sourceforge.io/docs.html)、[ngspice 公式 control-language チュートリアル](https://ngspice.sourceforge.io/ngspice-control-language-tutorial.html)。
