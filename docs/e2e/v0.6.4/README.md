# v0.6.4 Phase 2：実接続の事前準備

確認日：2026-10-15。公開仕様の閲覧とローカル成果物作成のみ。
認証付きOpenAI通信・リソース作成・有料通信・AWS操作は未実施。
既存アプリコード、依存関係、環境変数、配備設定は変更していない。

## 公式仕様とコードの比較

| 項目 | 公開仕様で確認した事実 | 現コードとの比較 |
|---|---|---|
| gpt-5.6-luna | 正式ID、ResponsesとFile Search対応 | 指定可能。アカウント実利用権限・残高は未確認 |
| Storeファイル登録 | POST /v1/vector_stores/{id}/files、file_idとattributes | personal_llm_document_id=file_idを登録、仕様に沿う |
| 属性フィルター | file_search.filtersにtype=in、key、値の配列 | 選択document_idsをinに渡す、仕様に沿う |
| 出典 | outputのmessage/content/output_text/annotations内のfile_citation、file_id/filename/index | file_id/filenameを抽出。indexを省くのはアプリの仕様 |
| 関連付け解除 | DELETE /v1/vector_stores/{vs}/files/{file} | 実装済み、元Filesは残る |
| 元削除 | DELETE /v1/files/{file}、すべてのStoreからも除去 | 実装済み、同じ中継プロセスでアップロードしたものに限定 |
| Store削除 | DELETE /v1/vector_stores/{vs} | アプリには未実装、検証用の別途承認済み操作で実施 |

参照：
- [モデル仕様](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [登録とattributes](https://developers.openai.com/api/reference/resources/vector_stores/subresources/files/methods/create)
- [File Searchとフィルター・出典](https://developers.openai.com/api/docs/guides/tools-file-search)
- [関連付け解除](https://developers.openai.com/api/reference/resources/vector_stores/subresources/files/methods/delete)
- [元ファイル削除](https://developers.openai.com/api/reference/resources/files/methods/delete)
- [Store削除](https://developers.openai.com/api/reference/resources/vector_stores/methods/delete)

### 残る差異と検証上の注意

1. Vector Store APIの公式curl例には `OpenAI-Beta: assistants=v2` があるが、
   現OpenAIFilesClientはAuthorizationのみを指定する。ヘッダーが現在も必須かは
   公開例だけで断定しない。SDKを使わないHTTPクライアントなので、実行前にこの差の
   扱いを決める。互換性のため追加する場合は別途コード変更・Mock回帰として承認を得る。
   接続失敗時に自動でヘッダー変更や再送はしない。
2. 現DocumentManagerは同じfile_idの削除mode変更を拒否する。
   解除したAの元削除はアプリ外の検証用管理操作で明示実行する。Bはアプリの
   delete_originalを検証する。解除後に同じAPIへ別modeを送る手順にはしない。
3. 未指定のtool_choiceはモデル判断に委ねられる。検索未実行・出典なしは成功扱いしない。
   自動で再質問やツール強制をしない。
4. アダプターは選択外の出典を返却前に除外する。従ってUI上の出典だけでは、
   検索結果への混入がなかったことを証明できない。検証専用のResponses呼出しで
   同じfiltersにinclude=["file_search_call.results"]を加え、未加工出典と検索結果の
   file_idも点検する。アプリ本体は変更しないが追加の有料呼出しとして予算に含める。
5. max_num_results=5は検索結果数であり、課金ツール呼出し回数の厳密な上限ではない。
   現アダプターに総費用のハード上限はなく、usageもHTTPレスポンスには返していない。

## キー権限と接続前の設定

[公式RBAC](https://developers.openai.com/api/docs/guides/rbac)に従い、プロジェクト内の
キー制限とユーザー/サービスアカウントの権限の両方を確認する。
検証に必要な操作は次。キーの既存権限・UIの実設定は未確認で、All権限を前提にしない。

| 対象 | 必要な操作 |
|---|---|
| Models | GET /v1/models/gpt-5.6-luna（一覧/取得権限がある場合だけ） |
| Files | POST /files、GET /files/{id}、DELETE /files/{id} |
| Vector Stores | POST /vector_stores、GET/DELETE /vector_stores/{id} |
| Store内Files | POST/GET /vector_stores/{vs}/files、GET/DELETE /vector_stores/{vs}/files/{id} |
| Responses | POST /responses、モデル利用とFile Searchの許可 |

Models取得成功だけで推論/ツール利用可まで確認できたとは扱わない。
権限不足を理由に勝手にキー権限を拡大しない。404は誤ID・プロジェクト違い等もあり得る。
モデル利用・残高・実料金・組織制限は認証なしの公開仕様では分からない。

承認後にPowerShellの非表示入力でOPENAI_API_KEYをプロセス環境に設定する。
値をコマンド文字列、ログ、.env、台帳に保存しない。setxを使わず、終了後に除去する。
ローカルBackend用：HARNESS_BACKEND=openai、OPENAI_MODEL_ID=gpt-5.6-luna。
OPENAI_ALLOWED_VECTOR_STORE_IDSにはこのrunで作成したStoreだけを含める。
文書中継のOPENAI_DOCUMENT_VECTOR_STORE_IDも同じStoreにする。
OPENAI_MAX_OUTPUT_TOKENSは初期512を維持。不足でincompleteになったら失敗として停止し、
無断で増量/再試行しない。Lambda用のSecretやAWS環境変数はPhase 2では触らない。

## 架空テストデータ

| 文書 | 識別子 | 周期 | 稼働 | 距離 | 固有校正コード |
|---|---|---|---|---|---|
| alpha-731.txt | ALPHA-731 | 37 ms | 19 h | 73 m | CITRINE-ALPHA-937 |
| beta-842.txt | BETA-842 | 82 ms | 46 h | 128 m | INDIGO-BETA-684 |

いずれもUTF-8、日本語、実在しない製品であり秘密情報はない。
本工程はTXTで登録・フィルターを検証する。PDFは未作成。PDF取込の合格には、後で
同内容の有効なPDFを作成し別の登録/検索検証が必要。TXT成功でPDF成功としない。

## リソース台帳と失敗時の原則

resource-ledger.template.jsonを、承認後の実行開始時に
`.aws-sam/v064-e2e/<一意run_id>/resources.json`へコピーする（Git除外領域）。
テンプレートには実ID・認証情報はない。実行前後の台帳を原子的に保存する。

- Store：id、nameのrun識別子、created_at、作成/削除/404確認状態。
- File：id、ローカルfixture名とSHA256、所属Store、索引状態、解除状態、元削除状態。
- 各operation：番号、method、検証対象ID、開始/完了/不明、HTTP状態、安全なエラー分類。
- Responses：ID、用途、usage、所要時間、観測したtool回数。認証ヘッダーやキーは保存しない。
- 送信前にintentを保存し、作成応答でIDを得たら次の操作より先に即時保存する。
- 通信タイムアウトは「未作成」ではなく結果不明。POSTは再送しない。
  Storeは一意のname/metadata、Fileはrun識別子付きファイル名で対象プロジェクトを
  確認する。必要な一覧照会は追加権限/回数を承認してから実施する。
  名前の一致だけで無関係なリソースを削除しない。曖昧なら未解決のまま報告する。
- 作成応答を失った未知IDや削除失敗が残る場合cleanup.completeをtrueにしない。
  メモリ内のアプリ台帳だけに依存せず、検証台帳から手動復旧可能にする。
- エラーで推論検証は停止。承認済みの当runの後片付けだけを継続する。
  削除失敗は自動再試行せずIDと状態を報告し、再実行を確認する。

## 承認後の実行順序

これは未実行の手順。現UIの推論は既存Lambdaに固定されているので、Phase 2は
文書管理ローカルAPI＋ローカルBackend /chatのAPI検証とする。
UI→新Lambdaまでの一連のE2EはPhase 3で行う。転送先を無断変更しない。

1. **前提確認**：キーのプロジェクト・必要権限、モデルID、予算、cleanup権限を確認。
   通常推論3回、アプリ検索2回、診断検索2回を最大計7回まで明示承認する。
   HTTP自動再試行とリダイレクトは無効。失敗した推論は再送しない。
2. **Store作成**：POST /vector_storesでnameにrun_id、metadataにもrun_idを付ける。
   対応仕様を確認したexpires_afterを補助的に設定する案だが、失効に後片付けを依存しない。
   1個だけ作り、IDを台帳へ即時記録してからローカルAllowlistを設定する。
3. **文書登録**：run識別子付きの名前でA/BをFilesへ各1回アップロード。
   各file_idを直ちに記録し、attributes.personal_llm_document_id=file_idで登録。
   中継のPOST /api/documentsを使う場合は/session取得とOrigin/Cookie/CSRFを維持する。
4. **索引確認**：GETで状態を確認。in_progressだけ待ち、completedまで両文書を選択不可。
   照会は各文書最大12回・10秒間隔、2分で打切り。failed/cancelledなら停止。
   これは状態照会であり、登録POSTの再試行ではない。
5. **通常チャット**：ローカル /chatに「日本語で短く挨拶してください。」を1回。
   history/vector_store_id/document_idsなし、HTTP 200・非空回答を確認。
6. **履歴**：1回目「この会話の合言葉はミント風船です。短く確認してください。」。
   そのuser/assistantをhistoryに渡し2回目「合言葉は何ですか？」。
   2回目に合言葉を重ねて送らず、回答にミント風船があることを確認（計2回）。
7. **A選択**：historyを空にし、vsとdocument_ids=[A]で「選択資料にある製品の識別子、
   測定周期、稼働時間、距離、校正コードを列挙。記載のない他製品の仕様は不明としてください。」。
   Aの値とA出典を確認。Bの固有値・校正コードやB出典があれば失敗。
8. **B選択**：historyを再び空にして[B]だけで同じ質問。
   Bの値・B出典のみを確認。前のA回答を履歴に含めない（検索範囲の独立性を検証）。
9. **診断検索**：アプリ送信と同じモデル/input/tools/filtersで、検証専用呼出しに
   include=["file_search_call.results"]だけを追加しA/B各1回。
   file_search_call実行、未加工annotations、検索結果file_idが選択集合の部分集合であることを
   確認。出典なし・検索なし・結果なしは範囲制限の合格にしない。
   生のヘッダー/キーは出さず、引用・結果は架空文書のIDと判定だけを記録する。
10. **出典表示**：sourcesのファイル名とfixtureの対応を照合する。
    UI表示ロジックはMockテスト済みだが、Phase 2のAPI結果だけでUI実E2E合格としない。
11. **A関連付け解除**：中継delete APIへmode=detachと正確な確認名。
    API GETでStore関連付けが404、元GET /files/{A}は200を確認。
    A選択の/chatがResponses POST前に拒否されることを確認。拒否チェックで推論を増やさない。
12. **元削除**：Aは台帳に基づく管理DELETE /files/{A}で削除（mode切替409を回避）。
    Bは中継mode=delete_originalで両方削除。両FilesのGET 404を確認する。
    正常終了応答だけで残存なしと判断せず、IDごとの不在を確認する。
13. **Store削除**：台帳に記録した当runのStoreだけDELETEし、GET 404を確認。
    作成した全File/StoreのIDを照合、unknown/unresolvedが空の場合だけcleanup.complete=true。
14. **終了**：ローカルサーバー停止、キーと一時Store設定の環境変数を親シェルから除去。
    stdoutを含む成果物に認証情報がないことを確認。AWSはそのままにする。

## 費用と実行可否

[公式料金](https://developers.openai.com/api/docs/pricing)ではFile Searchは$2.50/1000 tool calls、
保存$0.10/GB/日（無料枠1 GB）、モデルは100万tokensあたり入力$0.20・出力$1.20。
推論7回、各入力3000tokens・出力512tokens、検索toolが各検索で1回なら、
モデル約$0.0085008＋検索4回$0.010＝約$0.0185008。
これは条件付き概算で、請求上限ではない。検索内容・推論トークン・tool回数で変わる。
入力には検索で加わる文書内容も含める。保存課金量は解析/埋め込み後の量に依存し、
現在のアカウント無料枠消費も未確認。請求額を断定できない。
予算案は$0.10だが、ユーザーの予算承認と実使用量確認なしで実行しない。
既存アダプターの出力上限・usage未返却による計測制限も留意する。

**判定：公開仕様・データ・手順の準備は完了。実行は条件付き承認待ち。**
ベータヘッダー差の扱い、キー実権限/プロジェクト、予算、7回までの通信、作成/削除と
失敗時cleanupの範囲を確定するまで実APIには進まない。
Phase 2用の管理/診断ランナーは未実装。現UIから一括で完結するとは扱わず、
承認後に台帳保存を備えた検証手順を実行可能なコマンドへ落とし込む必要がある。
