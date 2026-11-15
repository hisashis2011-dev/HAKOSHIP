# HAKOSHIP 経費精算システム

社員(約100名)が領収書をスマホカメラ/スキャナで取り込み、使用目的・物件コードを入力して申請し、承認フローを経て精算する Web アプリ。

## 機能
- **申請**: カメラ撮影 / 写真・PDF選択 → 利用日・金額・支払先・勘定科目・**物件コード**(マスタ検証・候補表示)・**使用目的**・インボイス番号(任意)。下書き保存可
- **承認フロー**: 申請 → 上長承認 → 経理承認 → 精算済。各段階で差戻し(理由必須)→ 修正して再申請。上長未設定の社員は経理へ直接。自己承認不可。管理者は上長不在時の代理承認可。全操作は履歴に記録
- **経理**: 月別・状況・物件・キーワード絞り込み、CSV出力(Excel対応UTF-8 BOM)、一括「精算済」
- **管理者**: ユーザー管理(個別/CSV一括登録、初期PW発行・再発行)、物件マスタ(個別/CSV一括)
- **不正・誤り対策**: 同一領収書(画像ハッシュ)の二重登録をブロック、同日・同額・同支払先は警告、提出後は編集不可、領収書は権限者のみ閲覧
- **セキュリティ**: bcrypt、CSRFトークン、ログイン試行制限、署名付きセッションCookie、CSVインジェクション対策、画像は再エンコード(EXIF位置情報除去)

## 起動
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python -m app.seed --admin-email you@example.com --admin-password '10文字以上のパスワード'
python -m app.seed --demo        # 開発用デモデータ(任意)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pytest                           # テスト
```
ユーザーは管理画面からCSV(`email,name,role,approver_email`)で一括登録できます。

## 設定(環境変数)
`DATA_DIR`(DB・領収書の保存先, 既定 `./data`), `DATABASE_URL`(PostgreSQL等に変更可), `SECRET_KEY`, `COOKIE_SECURE=1`(HTTPS時は必須), `MAX_UPLOAD_MB`, `MAX_AMOUNT`。勘定科目は `app/config.py` の `CATEGORIES`。

## 本番運用の注意
- 必ず **HTTPS**(リバースプロキシ等)で公開し `COOKIE_SECURE=1` を設定。カメラ起動もHTTPS必須です
- `DATA_DIR` を定期バックアップ(DBと領収書画像)
- 電子帳簿保存法のスキャナ保存要件(タイムスタンプ、訂正削除の履歴、検索要件等)を満たすかは税理士・経理と要確認。現状は登録日時・ハッシュ・承認履歴・日付/金額/取引先検索(経理画面)を保持
- ログイン試行制限はプロセス内メモリのため、複数プロセス運用時は共有ストアが必要
