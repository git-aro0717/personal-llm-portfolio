# v1.0 文書削除制御の最終E2E準備

2026-10-17。調査・計画のみ。実DELETE、関連付け解除、追加推論は行っていない。

## 対象と確認の限界

- Store: vs_public_example。
- File ID: file-public-example。
- 登録名: inventory-fictional.csv.txt。
- 台帳と登録時検査により、承認済みの架空CSVから生成した579バイトの専用テスト文書であることを確認。
- 他Store・外部処理からの参照が存在しないことは未証明。現在のプロジェクト全体の参照関係を網羅検査していない。
- 実クライアントのoriginal_usage_verifiedは既定でFalse。元ファイル削除はoriginal_usage_unverifiedで403にする。
- ownedはプロセス内メモリのみ。再起動後は所有記録も失われ、元ファイル削除にoriginal_delete_not_ownedが加わる場合がある。
- 今回は既存台帳・コード・公式公開仕様を確認した。稼働DBとOpenAIの現在状態は再取得していない。

## 推奨する承認範囲

対象のStore/Fileの組だけに、mode=detachの関連付け解除を1回行う。
既存のCSRF・Origin・Cookieを使い、次のローカルAPIへ送る計画。

POST /api/documents/file-public-example/delete

```json
{"mode":"detach","confirmation":"inventory-fictional.csv.txt"}
```

これは実行指示・承認ではなく、承認対象の明示。元FilesのDELETE、他Store操作、Store削除は含めない。
このStore経由で同じファイルを利用する他の呼び出しは検索できなくなるため、利用者側の確認が必要。
解除後も元TXTと手元のCSVは残る。元TXT削除はすべてのStoreへ影響するため別の承認対象。
公式仕様:
- https://developers.openai.com/api/reference/resources/vector_stores/subresources/files/methods/delete
- https://developers.openai.com/api/reference/resources/files/methods/delete

## 現行処理と安全性

1. 対象名・mode・Store許可を検証する。
2. SQLiteの(store_id,file_id)をキーにstate=deletingを保存し、外部DELETEより前にコミットする。
3. 記録があれば文書GET・選択・ensure_selected・チャットのselection_guardが404で拒否する。
4. 関連付けDELETEを送り、受理応答と404確認を分けて記録する。
5. 関連付けGETを最大5回・10秒で確認する。404ならdetached=true、detachの場合はstate=complete。
6. 200継続・期限超過・通信異常はunconfirmed。DELETE失敗や完全削除と同一扱いしない。

DBを使えなければ文書経路は503。削除意図の永続化失敗では外部DELETEへ進まない。
state=completeも削除記録として残し、再選択させない。一般チャットにはこのDB制御を適用しない。
selection_guardはロックとSQLiteのBEGIN IMMEDIATEを中継応答まで保持するため、
先行チャットが完了してから削除意図が確定する場合がある。既に動作中の推論を取り消す保証ではない。
削除状態確定後の同じDBを使うリクエストは拒否される。

## 承認後の検証順序と期待結果

| 段階 | 操作 | 期待結果・停止条件 |
|---|---|---|
| 事前 | 対象ID・Store・登録名・既存DBの位置と状態を確認 | 不一致や予期しない削除状態ならDELETEしない |
| 保全 | 必要なら既存DBの整合性ある読み取りバックアップと台帳をGit外へ保存 | 個別承認の範囲で実施。既存DBを上書きしない |
| 解除 | 上記ローカルAPIへmode=detachを1回 | 外部DELETEの200/deleted=true等の受理とSQLite状態を記録 |
| OpenAI確認 | 関連付けGETの結果を確認 | 404で解除確認。期限超過ならunconfirmedで停止、無制限ポーリングしない |
| 元ファイル保持 | 元FilesのメタデータGET | 200。本文取得APIは使わない |
| ローカル拒否 | 文書GETと同じdocument_ids付きチャットPOST | 404。Lambdaへ転送せず、モデル推論0回 |
| UI | 一覧更新、選択解除・削除状態表示を確認 | 既存回答からの流用を避け、古いタブの選択でもサーバーが拒否 |
| Lambda拒否 | 明示的に承認された場合だけSigV4でFunction URLへ対象IDを1回送信 | 関連付け不在を検知してHTTP 502、ログ分類document_unavailable。Responsesへ進まない想定 |

ローカル拒否とOpenAIの反映は独立。ローカル404だけではOpenAI側解除の証明にならない。
DELETE受理だけでも解除の反映確認にはならない。
一覧から不在でも単体関連付けGETが200なら未確認と扱い、確認済みとしない。
GET 404を確認できても、分散状態の観測間の変化を完全には排除できない。
LambdaからローカルDBは見えないため、解除反映前の直接呼び出しにローカルのdeleting拒否は適用されない。

## 残り1回の枠

現在は累計4/5回。ローカル404検証は推論を消費しない。
直接Lambdaの拒否検証は、外部GET確認後に1回だけ行う計画とし、保守的には残り1枠を予約する。
想定どおりならResponses APIの追加呼び出しは0回だが、予期せず200になれば推論1回を消費し、その時点で停止する。
5xxを見て再送しない。期待する502も単独では合格にせず、document_unavailable・実行ログと関連付け404を照合する。
現行コードはその分類を複数の文書確認失敗に使うため、502だけで未課金・未推論を完全証明できるわけではない。
削除前の追加検索や、拒否確認後の通常チャットをこの工程に加えない。

## 中断・復旧

- 再起動で削除記録は復元できるが、DELETEを自動再実行しない。
- UIの「削除状態を照合して再開」または同じmode/confirmationで、明示承認後に再開する。
- DELETE受理済みならGET確認を続ける。送信結果不明ならGETで照合し、存在する場合に再DELETEが必要になることがあるため、その再開操作も承認に含める。
- 完了済みの同じ削除要求は外部DELETEを繰り返さない。
- 現行UI/APIに関連付け解除の取消機能はない。OpenAI側だけ再関連付けしても、ローカル削除記録が残る限り検索は拒否される。
- SQLiteの記録を手動削除したり、DB全体を無断復元したりしない。復元は他の削除状態も巻き戻す危険がある。
- 検索機能を戻す必要がある場合は、原本CSVから新IDで再登録する方法等を別途設計・承認する。自動再登録はしない。
- 元TXT削除を後で行う場合は全参照への影響を確認し、再取得できない可能性を理解して別承認する。

## ローカル検証

既存の削除永続化・再起動・明示再開・競合・元ファイル制限に関するMockテストを選択実行し、8件成功、8件選択対象外。
テストは一時DBとFakeクライアントのみ使用。稼働DB・実API・AWS設定を変更していない。

## 判断

対象Storeの関連付け解除に影響を限定する計画なら、所有者がこのStore経由の他用途利用がないことを確認し、
対象ID・detach・SQLiteへの削除記録保存・確認GET・ローカル拒否検証を明示承認した後に実行可能。
Lambda拒否検証は残り1枠の使用を含め別途明示する。元TXT削除は現状では実行不可。
今回はどの削除操作も実行せず、承認待ちで停止する。
