# v1.0 文書関連付け解除・検索拒否 E2E結果

実施日: 2026-10-17。ユーザーが承認したmode=detachだけを実行。

## 対象と範囲

- Store: vs_public_example。
- File ID: file-public-example。
- 登録名: inventory-fictional.csv.txt。
- 実行前、指定Store/Fileの関連付けGET 200・completed、元FilesメタデータGET 200・登録名一致。
- 既存ローカルCSRF付きAPIへdetachを1回送信。元Files DELETE・再試行・再登録は0回。
- 他文書・他Store・AWS/IAM/Secret/Lambda設定は変更していない。

## 状態とHTTP結果

| 確認 | 結果 |
|---|---|
| ローカル削除API | HTTP 200、attachment_delete_accepted=true |
| SQLite開始状態 | deletingを実DBの読み取り専用監視で観測 |
| deleting中の文書GET | HTTP 404 |
| 関連付け反映確認 | GET 200（約2.037秒）→404（約5.59秒） |
| 最終関連付けGET | 別途1回確認し404 |
| 元FilesメタデータGET | 200、元TXT名一致 |
| SQLite最終状態 | complete、detached=true、original_deleted=false |
| 解除後ローカル文書GET | 404 |
| 解除後ローカルチャットPOST | 404、Lambda転送前に拒否 |

SQLiteのdeleting観測はUTC 11:56:54.682、complete観測はUTC 11:57:02.050。
ローカルAPIの完了まで約7.471秒。外部DELETEの生HTTPステータスはアダプターが公開しないため、
ローカルHTTP 200とdeleted受理フラグ、関連付けGET 404を証拠として区別して記録した。
コードでは削除意図のコミットが外部DELETEに先行する。観測自体はDELETEの通信開始時刻まで捕捉していない。

## 永続性

対象DB: %LOCALAPPDATA%\PersonalLLM\documents.sqlite3。
後続の独立Pythonプロセスで同じDBをmode=ro・query_onlyで開き、completeと両フラグを再取得。
新しくロードしたアプリへ、そのDBを読み取り専用で使うDocumentManagerを組み込み、
ASGI TestClientで文書GET・チャットPOSTがともに404になることを確認。
プロバイダークライアントは呼ばれればエラーにするNoNetworkで置き換え、外部通信前の拒否を確認した。

これは実DBの別プロセス再読込とアプリ初期化後の拒否確認であり、稼働中継の実際の再起動ではない。
実行環境には稼働中継用のキー・起動環境を継承できないため、稼働プロセスは停止・再起動しなかった。
実中継再起動後の運用確認は未実施。元の設定済みPowerShellで再起動後、推論なしの文書GET 404等で確認可能。

## 費用・判定・残存リソース

- 今回の推論・File Search 0回、累計4/5回。Lambda直接呼び出しも0回。
- 追加推論・検索料金0。前段の参考見積もり累計約0.0084 USDは実使用量ではなく、保管・AWS費用は別。
- 元TXTはOpenAI Filesに残存。手元CSVとStore本体も保持。
- SQLiteには対象1件の削除状態を保存。全体の初期化・削除・復元は行っていない。
- 登録・索引化・検索・出典・関連付け解除・ローカル拒否・別プロセス再読込は合格。
- 厳密な稼働中継の再起動後確認は残るため、要求された全検証の完了判定は保留。
- 直接Lambdaの削除開始直後拒否はローカルDBを共有しないため保証対象外。今回の成功条件に含めていない。
- 過去の本文取得APIの400原因、生のFile Search内部回数・トークン使用量非公開は既知の残存事項。

復旧・再関連付け・元TXT削除はいずれも別途承認が必要。現行UI/APIに削除取消操作はない。
台帳: detach-e2e-20261017.json。今回はこの結果記録後に停止する。
