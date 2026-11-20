# LTK Clip Timeline

個人用のTwitchクリップ収集・閲覧アプリです。League of Legendsのクリップだけを保存し、配信時刻・日付・配信者・チームで閲覧できます。

## 起動

1. Python 3.11以上を用意します。
2. プロジェクト直下に `.env` を作成し、Twitch Developer Consoleの認証情報を設定します。
   ```env
   TWITCH_CLIENT_ID=...
   TWITCH_CLIENT_SECRET=...
   ```
3. 仮想環境を作成し、依存ライブラリを入れます。
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
4. 起動します。
   ```powershell
   .\.venv\Scripts\python.exe app.py
   ```
5. `http://127.0.0.1:5000` を開きます。

## 主な機能

- Twitch配信者ごとにクリップを取得し、取得後に `game_id=21779` のLeague of Legendsクリップだけを採用
- SQLiteにクリップを保存。既存データベースには不足カラムを自動追加
- 初回はイベント期間（2026-10-15〜2026-11-23 JST）を取得し、2回目以降は前回同期位置の24時間前から差分取得
- VODの作成日時を `video_cache` に保存し、同じVOD情報の再取得を回避
- 配信者別の取得件数、見つからないログインID、エラーを同期結果に表示
- VOD時刻が分からないクリップに「推定」表示
- 100件ずつ表示するページングと「もっと見る」
- キーワード入力は300msのデバウンス
- Twitchクリップをページ内プレイヤーで再生（アクセス時のホスト名を `parent` に指定）
- 日付別カレンダー、配信者フィルター、チームフィルター、再生数順ソート
- 同一チャンネル内で時間が重なるクリップは再生数最大のものを表示。別チャンネルは別々に表示

## データと秘密情報

- `data/roster.json`: 配信者名簿
- `database/clips.db`: 取得したクリップ・VODキャッシュ・同期位置
- `.env`: Twitch認証情報。共有・公開しないでください
- `.venv`: ローカルのPython仮想環境。ZIPやGitには含めず、requirements.txtから再構築できます

同期はTwitch APIの制限に従って配信者ごと・最大7日単位で問い合わせます。差分同期は前回成功した同期位置から24時間重ねて取得し、重複IDは更新します。
