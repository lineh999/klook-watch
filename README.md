# Klook 優惠提醒

讀 Klook 台灣活動頁事先排好的搶購時間，台灣的五折（含）以下、買一送一，發現就推到手機，開搶前 2 小時與 10 分鐘再提醒。

- 在 GitHub Actions 每 5 分鐘跑一次（`.github/workflows/klook.yml`），進度存在 `data/`。
- 推送管道的設定放在倉庫的加密設定（Secrets）：`KLOOK_NTFY_TOPIC`、`LINE_CHANNEL_ID`、`LINE_CHANNEL_SECRET`。
- 本機也能跑：`python watch.py scan｜list｜test｜tick`（設定放 `config.json`，不進版控）。
