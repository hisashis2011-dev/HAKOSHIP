# 社内サーバー構築・運用手順(社内LANのみ)

## 0. 用意するもの
- 専用ミニPC(メモリ8GB・SSD256GB以上)、UPS、バックアップ先(NAS/外付けHDD)
- 社内LAN内で固定IP(ルーターのDHCP予約)。例 `192.168.1.50`
- **ルーターでポート8000をインターネットに公開しない**(ポート開放しない)

## 1. OS とDocker
1. Ubuntu Server LTS をインストール(SSHを有効、自動セキュリティ更新は既定で有効)
2. ```
   sudo apt update && sudo apt install -y docker.io docker-compose-v2 git ufw
   sudo ufw allow from 192.168.1.0/24 to any port 22,8000 proto tcp   # 社内LANのみ許可
   sudo ufw enable
   ```
   (`192.168.1.0/24` は自社LANのアドレス帯に置き換え)
3. BIOSで「電源復旧時に自動起動(AC Power Recovery: Power On)」を設定

## 2. アプリの配置と起動
```
git clone <GitHubのリポジトリURL> hakoship && cd hakoship
git checkout main                      # 本番は main(または運用ブランチ)
cp .env.example .env && nano .env      # SECRET_KEY を設定
docker compose up -d --build
docker compose run --rm app python -m app.seed --admin-email あなた@社内 --admin-password '10文字以上'
```
社内PC/スマホから `http://192.168.1.50:8000` を開く。管理画面で物件マスタとユーザー(CSV一括可)を登録。

## 3. バックアップ(必須)
```
sudo mkdir -p /mnt/backup && # NASなどを /mnt/backup にマウント(/etc/fstab)
crontab -e
0 2 * * * DATA_DIR=/home/あなた/hakoship/data BACKUP_DIR=/mnt/backup/hakoship KEEP_DAYS=2600 /home/あなた/hakoship/scripts/backup.sh >> /var/log/hakoship-backup.log 2>&1
```
- 領収書は原則7年保存のため `KEEP_DAYS=2600`(約7年)程度を推奨(容量と相談)
- **復元テストを最初に1回、以後年1回**: tar.gzを展開し `app.db` と `receipts/` を `data/` に戻して起動確認
- 月1回、バックアップが毎日できているか `ls /mnt/backup/hakoship` を確認

## 4. 更新(アプリを新しくするとき)
```
cd hakoship && ./scripts/backup.sh  # 念のため事前バックアップ(環境変数は上記同様)
git pull && docker compose up -d --build
```
DBの形式変更がある更新では、更新前に必ずバックアップを取ること。

## 5. 日常の確認
- 停止確認: `docker compose ps` / ログ: `docker compose logs --tail 100 app`
- ディスク残量: `df -h`(領収書画像で増えます。目安 1枚 約0.3〜1MB)
- OSの更新: 月1回 `sudo apt update && sudo apt upgrade -y` のあと再起動

## 6. HTTPS化(推奨・任意)
HTTPではパスワードがLAN内で平文になります。社内でも気になる場合は、Caddy 等で社内用証明書を使ってHTTPS化し、`.env` の `COOKIE_SECURE=1` にします(各端末に社内ルート証明書の導入が必要)。必要になったら手順を追加します。

## 7. 障害時
| 症状 | 対処 |
|---|---|
| 画面が開かない | `docker compose ps` → 停止なら `docker compose up -d`。サーバー電源/LANを確認 |
| 起動しない | `docker compose logs app` を確認。直前の更新を `git checkout <前の版>` で戻す |
| データ破損・機器故障 | 新しい機器で手順1〜2のあと、バックアップを `data/` に展開して起動 |
| AI読み取りだけ失敗 | 影響なし(手入力で申請可能)。APIキー・インターネット接続を確認 |
