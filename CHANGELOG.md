# Changelog

本專案的所有顯著變更將記錄在此檔案中。
格式參考自 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)。

## [1.0.0] - 2026-04-27

### Added

- **初始版本發佈**：核心管理邏輯與 SQLite 資料庫整合。
- **自動處置功能**：實作階梯式違規處分（警告 > 10分禁言 > 永久封鎖）。
- **訊息過濾系統**：
    - 連結偵測與阻擋。
    - 違禁詞匹配系統。
    - 非白名單用戶標記限制。
    - 外部頻道轉發與回覆來源驗證。
- **管理指令集**：實作 `/addadmin`, `/addwl`, `/addcwl`, `/addword` 等及其對應的刪除指令。
- **智慧通知**：實作 180 秒延遲自動刪除機器人通知的功能。
- **資料庫架構**：建立 `admins`, `whitelist`, `channel_whitelist`, `banned_words`, `violations` 資料表。
