# 公開ファイルと除外ファイル

元の公開候補は88ファイル。うち78をコピーし、公開コピーだけの新規資料2件を加えた。
既存の資料は公開用に一部再作成した。Git内部・元履歴・仮想環境・私的データは含まない。

## 最終公開対象（80ファイル）

```text
.dockerignore
.gitignore
Dockerfile.lambda
README.md
app/__init__.py
app/api/__init__.py
app/api/chat.py
app/config.py
app/conversation.py
app/file_search.py
app/harness/__init__.py
app/harness/bedrock.py
app/harness/core.py
app/harness/errors.py
app/harness/mock.py
app/harness/openai_api.py
app/lambda_handler.py
app/main.py
deploy/openai-lambda-preparation.md
deploy/verify_file_search.py
deploy/verify_openai_chat.py
deploy/verify_openai_history.py
docs/README.md
docs/architecture.md
docs/deploy/v1.0-e2e-completion.md
docs/deploy/v1.0-final-e2e-progress.md
docs/design-decisions.md
docs/development-history.md
docs/e2e/v0.6.4/README.md
docs/e2e/v0.6.4/alpha-731.txt
docs/e2e/v0.6.4/beta-842.txt
docs/e2e/v0.6.4/file-search-runner.md
docs/e2e/v0.6.4/resource-ledger.template.json
docs/e2e/v1.1/README.md
docs/e2e/v1.1/aws-e2e-20261017-result.md
docs/e2e/v1.1/deletion-control-plan.md
docs/e2e/v1.1/detach-e2e-20261017-result.md
docs/e2e/v1.1/file-search-resume-20261017-result.md
docs/e2e/v1.1/inventory-fictional.csv
docs/portfolio/contributions.md
docs/portfolio/development-report-v0.6.md
docs/portfolio/profile-summary.md
docs/portfolio/public-export-report.md
docs/portfolio/publication-file-list.md
docs/portfolio/publication-security.md
docs/portfolio/third-party-review.md
docs/portfolio/verification.md
docs/releases/release-organization.md
docs/releases/release-validation.md
docs/v0.5-design.md
events/chat.json
local_web/__init__.py
local_web/csv_documents.py
local_web/deletion_store.py
local_web/documents.py
local_web/server.py
local_web/static/documents.js
local_web/static/index.html
local_web/static/style.css
local_web/static/ui.js
requirements.txt
template.yaml
tests/conftest.py
tests/test_bedrock.py
tests/test_chat.py
tests/test_config.py
tests/test_conversation.py
tests/test_csv_documents.py
tests/test_document_persistence.py
tests/test_documents.py
tests/test_file_search.py
tests/test_file_search_probe.py
tests/test_history_probe.py
tests/test_inference_adapters.py
tests/test_lambda.py
tests/test_local_web.py
tests/test_openai_api.py
tests/test_openai_lambda.py
tests/ui_documents.cjs
tests/ui_history.cjs
```

## 元候補から除外した10ファイル

| ファイル | 理由 |
|---|---|
| `deploy/backup_mock_environment.py` | Environment-specific AWS backup utility; unnecessary for public offline demo |
| `docs/archive/README-development-notes.md` | Long historical operating instructions and environment metadata |
| `docs/deploy/v0.6.6-aws-readiness.md` | Private-environment deployment preparation |
| `docs/deploy/v1.0-auth-diagnostic-plan.md` | Private-environment authentication investigation |
| `docs/deploy/v1.0-changeset-review.md` | Environment-specific change-set record |
| `docs/deploy/v1.0-deployment-result.md` | Environment-specific deployment identifiers |
| `docs/deploy/v1.0-integration-plan.md` | Environment-specific deployment commands |
| `docs/releases/deployment-map.json` | Original private Git refs and deployed-image mapping do not describe this sanitized repository |
| `docs/releases/v1.0-runtime-manifest.json` | Original runtime hashes do not describe this independent public copy |
| `docs/releases/recovery-verification.md` | Private backup is not distributed and cannot restore this public copy |

## 候補外の非公開データ

.env、AWS資格情報、Secret値、秘密鍵、SQLite、個人文書、実行済みJSON台帳、画面画像、IAM実体、バックアップ、venv・キャッシュはコピーしていない。
公開前の再検査は[確認結果](publication-security.md)を参照。
