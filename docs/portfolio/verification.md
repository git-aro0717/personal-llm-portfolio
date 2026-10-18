> 公開用匿名化コピー。下記のGit ID・タグ・検証結果は元の開発環境の記録です。公開Gitの履歴・現時点の外部サービス動作を示すものではありません。

# 検証結果と証拠の範囲

確認日：2026-10-18 JST。対象HEAD：`aaca8366e28733963f2e23e1243735abaaf8ff3d`。
今回は資料とignore設定のみを整理し、AWS・OpenAIへ接続していない。

## 今回実行したテスト

| コマンド | 結果 |
|---|---|
| `.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider` | 271 passed / 1 warning / 9.07秒 |
| `node --test tests/ui_history.cjs tests/ui_documents.cjs` | 10 passed / 0 failed / 0 skipped |

Pythonの警告はMangumの`DeprecationWarning: There is no current event loop`。
テスト失敗ではないが依存更新・イベントループ互換性の課題として残す。
外部通信はMock・差し替えクライアントで検証し、稼働SQLiteではなく一時DBを使用する。
テストコードを実行した結果であり、全ネットワークのパケット監視を実施したものではない。

| 分野 | テスト対象 |
|---|---|
| Backend・履歴 | 空白・型・文字数・role・履歴件数・本文容量、従来message互換 |
| 推論アダプター | Mock / Bedrock / OpenAI、認証・クォータ・タイムアウト・異常応答 |
| Lambda | Mangumイベント、Secret取得、明示文書ID・元ファイル確認 |
| 文書管理 | 登録・一覧・索引状態、許可Store、サイズ・形式、削除・再開 |
| SQLite | 先行保存失敗、再起動、チャットと削除の競合、DB障害時の安全停止 |
| UI | CSRF付き送信、履歴・上限、安全な出典表示、アップロード・削除、CSV |
| CSV | BOM・日本語・引用符・改行、ヘッダー・行列・サイズ、TXT化・削除制御 |

UIテストはNode.js上のDOM/fetch代替を使うテストであり、今回のブラウザ実操作E2Eではない。
カバレッジ率、負荷性能、脆弱性・依存パッケージ監査、全ブラウザ互換性は未測定。

## 保存済みの結果（今回は再実行なし）

- v1.0タグ相当：238 Python / 8 UI。2026-10-17の[検証記録](../releases/release-validation.md)。
- SAM lint・ARM64ビルド：既存の配備資料に成功記録あり。今回はビルド・lint未実施。
- AWS実E2E：通常チャット、履歴、架空CSV由来TXTの検索・出典、detachと元Files保持。
- ローカル再起動E2E：同じDB保存先、complete / detached=true、文書GETと対象ID付きチャットPOSTの404。

実E2Eの累計Responsesは4回。請求額・実使用トークン・内部File Search回数は未取得。
CSV原本の具体値と出典ID・表示名は一致したが、生検索結果の直接観測とは区別する。
元TXTは削除していない。最後の再起動確認は外部通信なしの検証だった。

[最終E2E記録](../deploy/v1.0-e2e-completion.md)と[CSV検索記録](../e2e/v1.1/file-search-resume-20261017-result.md)が根拠。
v1.0とCSV拡張を含むローカル実行環境の違いは[リリース構成](../releases/release-organization.md)を参照。

## 未検証

今回時点のAWS稼働状態、現在のキー・モデル利用権限、Bedrock実モデル推論、
元Filesの完全削除、複数ユーザー・分散競合、継続運用コスト、SLAは未確認。
Mock成功をこれらの証明として扱わない。

## 公開コピーでの再確認（2026-10-18）

公開コピーでPython 271件・UI 10件成功。詳細と匿名化に伴う初回失敗・修正の記録は[公開コピー結果](public-export-report.md)を参照。
元開発リポジトリを変更した結果ではない。実API・AWSの再検証ではない。
