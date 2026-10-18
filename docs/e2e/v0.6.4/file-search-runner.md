# File Search検証スクリプト（未実行）

`deploy/verify_file_search.py`は既存アプリを変更せず、ローカルFastAPI /chat → Harness →
Responsesを検証する。VS Codeでキーを設定した同じPowerShellから実行する。
現段階ではプレビューと偽APIのMockテストだけを実施した。

## コマンド

```powershell
# 通信しないプレビュー
.\.venv\Scripts\python.exe -B deploy/verify_file_search.py

# 下記は別途承認後だけ。通常2回の推論、リソース作成と削除が発生する。
.\.venv\Scripts\python.exe -B deploy/verify_file_search.py --execute --run-id file-search-1

# 中断・削除失敗後、同じ台帳の既知IDだけを後片付け。推論・新規作成なし。
.\.venv\Scripts\python.exe -B deploy/verify_file_search.py --cleanup --run-id file-search-1
```

キーを引数・ファイルに入れない。OPENAI_API_KEYは親PowerShellから継承し、値を表示しない。
実行時に元のrunディレクトリがあれば作成の再実行を拒否する。失敗後に新しいrun-idで
作成をやり直さない。キーがない/不正、Lambda環境と判断される場合も通信前に停止する。
AWS、IAM、Secrets Manager、Lambdaへ接続する機能はない。

## 操作と制限

接続先は固定 `https://api.openai.com/v1`。HTTP自動再試行・リダイレクト・環境プロキシなし。
Vector Store操作（推論前の所属確認GETを含む）はOpenAI-Beta: assistants=v2を付ける。
モデルはgpt-5.6-luna固定、max_output_tokens=512、Responsesはstore=false。

1. 架空fixture2件のハッシュを検証。改変されていれば通信前に停止。
2. GET /models/gpt-5.6-lunaでモデル取得可否を確認（推論権限までは証明しない）。
3. Storeを1個作り、返却IDを即座に台帳へ保存。
4. A/Bを各1回Filesへアップロードし、IDを保存。既存fixture内容だけを送り、ファイル名はrun識別子付き。
5. Storeへpersonal_llm_document_id属性で各1回関連付け。状態GETは最大12回/文書、10秒間隔。
   POST登録は再送せず、in_progressだけ待つ。failed/cancelled/時間超過で停止。
6. 検証プロセス内だけ一時的にBackend=openai、モデル・Allowlistを今回Storeに設定。
   既存設定はfinallyで復元し、親PowerShellやAWSは変更しない。
7. /chatにA/Bのdocument_idsと空historyで各1回送信。APIキーは通常のローカル環境経由。
   検証用HTTPトランスポートが同じResponsesリクエストにinclude=file_search_call.resultsを
   追加する。別の診断推論は発生しない。アプリファイル自体は変更しない。
8. 未加工の検索file_id・file_citationが選択集合内にあること、出典filenameがアップロード名に
   一致すること、検索完了、非空結果、回答の製品ID/仕様値/固有コードを確認。
   回答本文や検索結果本文は台帳/コンソールに保存せず、ID・判定・usageだけを記録。
   source表示で除外する前の生データから混入を検査する。
9. Aの関連付けだけDELETEし、HTTP 200かつdeleted=trueを要求受理として記録。
   所属GET 404で解除確認成功、元Files GET 200で元ファイル保持を検証。
   削除済みAをアダプターへ渡し、metadata確認で拒否されることを確認。
   この確認用トランスポートはGETだけを許可し、追加Responses POSTを拒否する。
10. Bの元ファイルDELETEと所属GET 404を確認。
11. finallyのcleanupでA元削除、A/B FilesのGET 404、Store DELETEとGET 404を確認。
    B削除済みなどの完了工程は繰り返さない。失敗時も今回の既知IDだけを対象にする。

索引が即完了する正常系は管理/確認APIと推論を合わせて最少23回程度。
索引待ちの状態GETは別途増える。推論は通常2回、スクリプト内の防御上限5回。
今回通常チャット・履歴が既に成功しているので、それらを再送しない。

## 台帳と中断

既存テンプレートを再利用し `.aws-sam/v064-e2e/<run-id>/resources.json`へ保存する。
各操作の送信前intentと結果、作成ID、fixtureハッシュ、usage、推論回数、削除/不在確認を
追記型の不変ledger-*.jsonスナップショットへ先に保存・flush/fsyncする。その後resources.jsonを
更新する。Windowsでresources.jsonの置換が拒否された場合はローカル置換だけを最大3回試し、
復旧スナップショットが保存できていれば処理を継続する。APIリクエストは再試行しない。
cleanupはresources.jsonと全スナップショットから最新revisionを読み、破損を検出したら停止する。
キー・ヘッダー・文書/回答本文は保存しない。スナップショットを勝手に削除/編集しない。

通常例外・Ctrl+Cでもfinallyで既知IDを後片付けする。プロセス強制終了/電源断では
finallyは保証されないため、同じrun-idでcleanupを明示実行する。
作成POSTの応答不明、開始intentだけで中断された作成はunknown_created_resourcesとして扱い、
既知IDを全削除できてもcleanup.complete=falseのままにする。
その場合はrun名・アップロード名等を使って対象プロジェクトで別途照合する必要がある。
曖昧な名前一致だけで消さず、広範囲一覧・追加操作の権限/承認を確認する。
新規作成POST・削除失敗の自動再試行はしない。

cleanupは台帳IDを信用する管理操作なので、台帳を手で別リソースのIDへ書換えない。
キーの権限剥奪や通信障害で削除できなければ未解決IDを表示し、残存なしと報告しない。
後片付け成功の判定はAPI上の不在であり、内部バックアップを含む物理消去証明ではない。

### Windows/OneDriveの台帳置換拒否

file-search-1の実行でresources.tmp→resources.jsonの置換がWinError 5で失敗した。
保存済み台帳にはModels GET 200のみ、作成POSTなし、推論0回、作成IDなし。
コードのAPI送信前保存順序ともStore作成前の停止が整合する。独立したAPI残存照会は未実施。
OneDriveや他プロセスのファイルロックが候補だが、拒否元までは断定していない。
このrunの証跡を残し、修正版を使う次の実行ではOneDrive外の台帳ディレクトリを推奨する。

```powershell
# 今回の未作成判定を確認後の再実行例。ここでは実行していない。
.\.venv\Scripts\python.exe -B deploy/verify_file_search.py --execute --run-id file-search-2 --ledger-root "$env:TEMP\personal-llm-v064-e2e"

# 中断した場合も必ず同じledger-root/run-idを指定する。
.\.venv\Scripts\python.exe -B deploy/verify_file_search.py --cleanup --run-id file-search-2 --ledger-root "$env:TEMP\personal-llm-v064-e2e"
```

TEMP内の台帳は後片付けが終わるまで保全する。OSの一時ファイル削除に注意する。
同じrunを同時に実行しない。台帳全体への書込みやスナップショット作成も失敗する場合は
API操作前に停止する。権限回避のために管理者実行やACL変更を自動では行わない。

## 費用・停止条件

既に確認した公開単価：モデル入力$0.20/100万tokens、出力$1.20/100万tokens、
File Search $2.50/1000 calls。各入力3000・出力512、検索1回/推論と仮定して、
2回で約$0.00743＋保存費。保存は$0.10/GB/日（無料枠1 GB）だが、現在の無料枠消費・
索引後の課金サイズ・実請求は未確認。予算目標$0.10を保証しない。

usageと実検索回数からの推論費見積りが$0.05を超えたら追加推論を停止しcleanupする。
これは事後停止でありハードな請求上限ではない。無料枠/割引/税/既存請求を含む実請求は
取得しない（管理用請求権限の追加もしない）。保存リソースは即時cleanupを試みる。

認証/権限エラー、モデル相違、仕様不一致、未許可ID、検索/引用混入、出典/usage欠落、
索引失敗、課金異常、追跡不能で推論を停止する。応答が非決定的に文言を変えて仕様値の
検証に失敗しても無断で再質問しない。失敗後に台帳の安全な結果だけを報告する。

## 検証範囲

ここで確認するのはローカルBackend/アダプターと標準OpenAI APIの連携。
Web UIの実描画、AWS/SigV4/新Lambda配備を含むE2Eではない。
Files/Storeの権限は各実操作で判明し、Models GET成功だけで全権限が確認済みとはしない。
実行前にキーの所属プロジェクト・保存/削除権限・予算とcleanupの承認範囲を確認する。

## 関連付け解除の反映待ち（オフライン修正）

要求受理（delete_accepted）と解除確認（confirmed）を別々に記録する。
DELETE直後のGET 200は即失敗にしない。単体GET/診断用一覧ページを合計最大5回、
確認開始から10秒の期限で実行する。200が続いた場合は原則2秒間隔で待つ。
単体GET 404のみ解除確認成功。一覧の不在は診断情報にとどめ、単独で成功にしない。
一覧はhas_more/last_id/afterを処理し、GET回数・時間の残予算内でページを進める。
予算で全ページを確認できなくても不在を断定しない。カーソル循環や不正形式は停止する。

200時は安全なid/vector_store_id/status/created_at/usage_bytesだけを台帳に保存する。
一覧は件数・対象有無・has_more・カーソルのみ。文書内容、ファイル名、属性値、
last_error本文、認証ヘッダーは保存しない。上限到達はdetach_verification_timeoutで区別する。
DELETE・作成POST・推論を再送する機能ではなく、状態GETの限定的な確認である。

各確認GETには残時間に応じた通信タイムアウトを設定し、待機側にも同じ期限を設ける。
期限切れの読み取りワーカーは台帳を変更しない。OS/DNSの中断不能な待機まで即時取消を
保証するものではない。台帳ディスク書込み・最初のDELETE・後片付け時間は別であり、
スクリプト全体が10秒以内に終わるという意味ではない。
file-search-2の元台帳・スナップショットは修正しない。再検証は別の承認済みrunで行う。

## 元ファイル削除の独立した状態確認

BETAの元ファイル削除では、最初に元Files GET、次にStore関連付けGETを行う。
未確認の対象を交互に確認し、404になった対象は以後スキップする。
GETは両対象を合わせて最大5回・確認開始から10秒以内。最初の2回には待機を挟まず、
両方の対象に確認枠を確保する。ただし最初の通信自体が期限を消費した場合は停止する。
この経路では診断用一覧を取得せず、限られた確認枠を単体GETに充てる。

台帳のoriginal_delete_verificationsにはDELETEのdelete_http_status/delete_acceptedと、
states.original / states.attachmentを個別に記録する。各状態はunconfirmed、observed_200、
absent、errorを区別し、get_count、last_http_status、elapsed_secondsを保持する。
original_confirmed_absentとattachment_confirmed_absentは、それぞれのGET 404だけで設定する。
両方404の場合のみconfirmed=true / verification_state=confirmed / result=original_deleted。

期限または回数上限ではverification_state=unconfirmed、
result=original_delete_verification_timeoutとし、削除要求の失敗とは断定しない。
片方だけ404でも検証全体は合格にしない。異常HTTP応答や不正メタデータはerrorとして停止する。
元ファイルのdeletedは削除要求受理、absent_confirmedはGET 404を意味する。
cleanupは従来どおり元FilesとStoreの不在を独立して確認するため、検証失敗でも
cleanup.complete=trueになり得る。cleanup成功だけでは関連付け確認の合格にならない。
元FilesやStoreのGET反映が遅れた場合、cleanupが未完了になる可能性は残る。
過去の台帳は更新せず、次回実API検証は別途承認を得た新規runで実施する。

## 削除専用モード（実行は別途承認後）

`--deletion-only`は既存ALPHA-731 TXTを1件と専用Storeを1個だけ使う。
Models API、Responses API、File Search推論を呼ばない。索引化は行う。
台帳にverification_mode=deletion_onlyを保存し、Responses呼び出しも実行時に拒否する。
登録後は上記の独立した削除状態確認を再利用する。

PowerShellで、既存のOPENAI_API_KEYを設定済みのプロセスから実行する。
次のプレビューは外部通信・台帳作成なし。

```powershell
.\.venv\Scripts\python.exe -B deploy/verify_file_search.py --deletion-only
```

実通信が別途承認された場合のみ（新規run IDを使用）：

```powershell
.\.venv\Scripts\python.exe -B deploy/verify_file_search.py --deletion-only --execute --run-id deletion-only-1 --ledger-root "$env:TEMP\personal-llm-v064-e2e"
```

通常はStore作成・Filesアップロード・Store登録を各1回、索引GETを1〜12回、
元Files DELETEを1回、削除確認GETを合計2〜5回、cleanupで元Files GET・Store DELETE・
Store GETを各1回実施する。正常応答の範囲で合計10〜24操作。
索引化待機は最大11回の10秒待機で、削除確認の10秒期限とは別。
推論課金は発生しないが、索引化・保存に関する費用までゼロと保証するものではない。

期限・回数上限では状態を未確認として保存し、検証全体は非合格のままcleanupする。
自動cleanupでは、この実行ですでに試行した元Files DELETEを再送しない。
GET 404を確認できないリソースはunresolvedに残し、cleanup.complete=falseとする。
認証・権限・通信異常、索引失敗、不正応答でも新規作成や推論を追加せずcleanupへ進む。
作成結果不明のIDは推測せずunknown_created_resourcesとして残す。

中断・cleanup未完了の場合は台帳と復旧スナップショットを保持する。
同じrun IDで--executeを再実行しない。追加削除の実施を確認してから、明示的に後片付けする：

```powershell
.\.venv\Scripts\python.exe -B deploy/verify_file_search.py --cleanup --run-id deletion-only-1 --ledger-root "$env:TEMP\personal-llm-v064-e2e"
```

このコマンドは記録済みIDだけを使い、作成・推論は行わない。
元FilesとStore双方のGET 404、cleanup.complete=true、unresolved=[]、
unknown_created_resources=falseを確認する。cleanup成功と削除検証合格は別の判定。
