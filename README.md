### 啟動相依服務 (Redis)

使用 Redis 進行 API 快取與對話紀錄儲存
建議先安裝 WSL (Windows Subsystem for Linux)，並在 WSL 的終端機 (例如 Ubuntu) 中執行後續所有指令，可以避免許多潛在的相容性問題

* **使用 Docker 啟動 (需先下載Docker)**：
    在終端機執行以下指令，即可在背景啟動一個 Redis 容器
    ```
    docker run -d --name my-redis -p 6379:6379 redis
    ```
* **檢查 Redis 狀態**：
    執行 `redis-cli ping`，若終端機回傳 `PONG`，則表示 Redis 已成功啟動

### 啟動前後端

1.  **啟動後端 FastAPI 服務**：
    * 終端機在專案根目錄 (`capstone-be/`) 下
    * 執行以下指令啟動後端伺服器：
        ```
        uvicorn main:app --reload
        ```
2.  **開啟前端介面**：
    ```
    python -m http.server 5175
    ```

## Ⅱ. 前端 API 端點列表

前端會呼叫的所有後端 API 節點

| **功能 (Feature)** | **HTTP 方法** | **路徑 (Path)** | **說明** |
| :--- | :--- | :--- | :--- |
| **GitHub 認證** | GET | `/login/` | 處理 GitHub OAuth 登入成功後的回調，用 `code` 換取 `access_token`|
| **使用者資訊** | GET | `/user_info/` | 使用 `access_token` 獲取已登入使用者的基本資訊 (名稱、頭像) |
| **倉庫列表** | GET | `/repo_list/` | 獲取使用者所有可存取的 GitHub 倉庫列表 |
| **Commit 列表** | GET | `/repo_commit/repos/{owner}/{repo}` | 獲取指定倉庫的最近 Commit 列表，用於 PRs/Commits 頁面顯示 |
| **PR 列表** | GET | `/pr/repos/{owner}/{repo}/pulls` | 獲取指定倉庫的 Pull Request 列表 |
| **AI - 專案概覽** | GET | `/overview/repos/{owner}/{repo}` | 產生 AI 專案摘要，並獲取專案的檔案結構樹 |
| **AI - 倉庫趨勢** | GET | `/trends/repos/{owner}/{repo}/trends` | AI 分析近期的 Commit，產生趨勢報告與分類統計圖表 |
| **AI - 分析 Commit** | POST | `/diff/repos/{owner}/{repo}/commits/{sha}` | AI 分析單一 Commit 的程式碼變更 (diff)，並生成審查報告 |
| **AI - 分析 PR** | GET | `/pr/repos/{owner}/{repo}/pulls/{pull_number}` | AI 分析單一 Pull Request 的程式碼變更，生成 Code Review 報告 |
| **AI - 智能問答** | POST | `/chat/repos/{owner}/{repo}` | 根據使用者問題及指定的 Commit 上下文，提供 AI 回答 |
