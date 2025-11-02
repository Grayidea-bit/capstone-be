# capstone-be/AI/chat/chatting_repo.py
from fastapi import APIRouter, HTTPException, Query
import httpx
from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    generate_ai_content,
    parse_diff_for_previous_file_paths,
    MAX_FILES_FOR_PREVIOUS_CONTENT,
    MAX_TOTAL_CHARS_PREV_FILES,
    MAX_CHARS_CURRENT_DIFF,
    MAX_CHARS_PER_PREV_FILE,
    logger,
    redis_client,
    CACHE_TTL_SECONDS,  # 確保導入
)
from ..code_analyzer import CodeAnalyzer
import json
import hashlib  # 導入 hashlib

chat_router = APIRouter()


def get_conversation_history(history_key: str) -> list:
    if not redis_client:
        return []
    try:
        history_json = redis_client.get(history_key)
        return json.loads(history_json) if history_json else []
    except Exception as e:
        logger.error(f"讀取對話歷史快取失敗: {e}", extra={"history_key": history_key})
        return []


def set_conversation_history(history_key: str, history: list):
    if not redis_client:
        return
    try:
        redis_client.set(
            history_key, json.dumps(history[-10:]), ex=3600
        )  # 增加歷史紀錄到 10 則
    except Exception as e:
        logger.error(f"寫入對話歷史快取失敗: {e}", extra={"history_key": history_key})


@chat_router.post("/repos/{owner}/{repo}/{branch}")
async def chat_with_repo(
    owner: str,
    repo: str,
    branch: str,
    access_token: str = Query(None),
    question: str = Query(None),
    target_sha: str = Query(
        None, description="在 'commit' 模式下，指定上下文的 commit SHA"
    ),
    mode: str = Query(
        "commit", description="問答模式: 'commit', 'repository'"
    ),
):
    if not access_token or not question:
        missing = [
            p
            for p, v in [("access_token", access_token), ("question", question)]
            if not v
        ]
        raise HTTPException(
            status_code=400, detail=f"缺少必要的查詢參數: {', '.join(missing)}"
        )

    log_question = question[:50] + "..." if len(question) > 50 else question
    logger.info(
        f"收到對話請求: {owner}/{repo}",
        extra={"owner": owner, "repo": repo, "question": log_question, "mode": mode},
    )

    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="無效或過期的 GitHub token。")

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            commits_data = await get_commit_number_and_list(
                owner, repo,branch, access_token
            )
            if not commits_data:
                return {
                    "answer": "抱歉，這個倉庫目前沒有任何提交記錄，無法回答您的問題。",
                    "history": [],
                }

            analyzer = CodeAnalyzer(owner, repo,branch, access_token, client)

            # ***** 主要修改點：新增問答快取邏輯 *****
            cache_key = None
            question_hash = hashlib.md5(question.encode()).hexdigest()

            if mode == "repository":
                latest_commit_sha = commits_data[0]["sha"]
                cache_key = f"chat:repository:{owner}/{repo}/{branch}:{latest_commit_sha}:{question_hash}"
            elif mode == "commit":
                sha_to_use = target_sha or commits_data[0]["sha"]
                cache_key = f"chat:commit:{owner}/{repo}/{branch}:{sha_to_use}:{question_hash}"

            if cache_key and redis_client:
                try:
                    cached_result = redis_client.get(cache_key)
                    if cached_result:
                        logger.info(f"智能問答快取命中: {cache_key}")
                        answer_text = json.loads(cached_result)
                        # 即使快取命中，依然要更新對話歷史
                        history_key = f"chat_history:{owner}/{repo}/{access_token[:10]}"
                        conversation_history = get_conversation_history(history_key)
                        conversation_history.append(
                            {"question": question, "answer": answer_text}
                        )
                        set_conversation_history(history_key, conversation_history)
                        return {"answer": answer_text, "history": conversation_history}
                except Exception as e:
                    logger.error(
                        f"讀取智能問答快取失敗: {e}", extra={"cache_key": cache_key}
                    )
            # ***********************************

            if mode == "repository":
                answer_text = await handle_repository_qa(analyzer, question)
            else:  # mode == "commit"
                answer_text = await handle_commit_qa(
                    owner,
                    repo,
                    access_token,
                    question,
                    branch,
                    target_sha,
                    commits_data,
                    client,
                )

            # 將新結果存入快取
            if cache_key and redis_client:
                try:
                    redis_client.set(
                        cache_key, json.dumps(answer_text), ex=CACHE_TTL_SECONDS
                    )
                    logger.info(f"已快取智能問答結果: {cache_key}")
                except Exception as e:
                    logger.error(
                        f"寫入智能問答快取失敗: {e}", extra={"cache_key": cache_key}
                    )

            history_key = f"chat_history:{owner}/{repo}/{access_token[:10]}"
            conversation_history = get_conversation_history(history_key)
            conversation_history.append({"question": question, "answer": answer_text})
            set_conversation_history(history_key, conversation_history)

            return {"answer": answer_text, "history": conversation_history}

        except httpx.HTTPStatusError as e:
            detail = f"因 GitHub API 錯誤，無法處理對話: {e.response.status_code} - {e.response.text}"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e:
            raise e
        except Exception as e:
            logger.error(f"處理對話時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"處理對話時發生意外錯誤: {str(e)}"
            )


# modify
async def handle_repository_qa(analyzer: CodeAnalyzer, question: str):
    logger.info("進入全域知識庫問答模式 (Repository Q&A)")

    context_for_final_prompt = await analyzer.file_embedding_similar(
        user_question=question
    )

    # 4. **第二階段 AI 呼叫**: 結合上下文回答問題
    final_prompt = f"""
### **角色 (Role)**
    你是一位對整個程式碼庫有深入了解的資深技術專家。
### **任務 (Task)**
    根據提供的輸入內容(Input)，精準地回答使用者的問題。
### **輸入內容 (Context)**
    以下是根據你的問題，從專案中提取出的最相關的檔案內容：
    {context_for_final_prompt if context_for_final_prompt else "沒有找到與問題直接相關的檔案內容。"}
    下方是使用者的問題：
    "{question}"
    
### **回答準則**
步驟 1 — 分析相關性
    閱讀擴充後的查詢與提供的程式碼。
    如果程式碼明顯與問題相關（例如相同函數、邏輯或主題），分析程式碼以產生有依據的回答。
    如果程式碼看起來不相關、不完整或無關，完全忽略程式碼，僅依靠一般程式知識回答。

步驟 2 — 若程式碼相關
    逐步說明程式碼如何對應問題。
    若問題是關於錯誤或行為，定位程式碼中負責的邏輯。
    參考程式碼中的具體函數名稱、變數或操作。

步驟 3 — 若程式碼不相關或不足
    直接使用自身技術知識回答。
    提供清晰、專業、準確的解釋，就像沒有程式碼可用一樣。

步驟 4 — 使用者不完整或無相關的問題
    當使用者問題資訊不足、非法問題、奇怪問題，可以回答"問題資訊不足"
    若只是問題不經準，根據標準軟體工程知識推斷最可能的意圖或缺失的細節。

步驟 5 — 語言一致性
    偵測使用者原始問題的語言。
    回答必須使用相同語言（例如使用者寫中文就用中文回答，寫英文就用英文回答）。
"""
    answer = await generate_ai_content(final_prompt)
    return answer


async def handle_commit_qa(
    owner: str,
    repo: str,
    access_token: str,
    question: str,
    branch: str,
    target_sha: str,
    commits_data: list,
    client: httpx.AsyncClient,
):
    logger.info("進入特定 Commit 問答模式 (Commit Q&A)")

    if not target_sha:
        target_sha = commits_data[0]["sha"]
    
    # 獲取當前 commit 的 diff
    diff_response = await client.get(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{target_sha}",
        headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github.v3.diff",
        },
        params={"sha": branch}
    )
    diff_response.raise_for_status()
    current_commit_diff_text = diff_response.text

    # 獲取前一個 commit 的相關檔案內容
    previous_commit_files_content_text = "無法獲取前一個 commit 的檔案內容。"
    previous_commit_sha = None
    target_commit_obj = next((c for c in commits_data if c["sha"] == target_sha), None)

    if target_commit_obj:
        target_index = commits_data.index(target_commit_obj)
        if target_index + 1 < len(commits_data):
            previous_commit_obj = commits_data[target_index + 1]
            previous_commit_sha = previous_commit_obj["sha"]

            # 從 diff 中解析出被修改的檔案
            affected_files = parse_diff_for_previous_file_paths(
                current_commit_diff_text
            )

            temp_files_content = []
            total_chars = 0
            # 限制只抓取少量檔案，避免請求過多
            for file_path in affected_files[:MAX_FILES_FOR_PREVIOUS_CONTENT]:
                if total_chars >= MAX_TOTAL_CHARS_PREV_FILES:
                    break
                try:
                    file_content_res = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?sha={previous_commit_sha}",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Accept": "application/vnd.github.raw",
                        },
                    )
                    if file_content_res.status_code == 200:
                        content = file_content_res.text
                        content_truncated = content[:MAX_CHARS_PER_PREV_FILE]
                        temp_files_content.append(
                            f"--- 檔案: `{file_path}` ---\n```\n{content_truncated}\n```"
                        )
                        total_chars += len(content_truncated)
                except httpx.HTTPStatusError:
                    temp_files_content.append(f"--- 檔案: `{file_path}` (無法獲取) ---")

            if temp_files_content:
                previous_commit_files_content_text = "\n\n".join(temp_files_content)

    # 組合 Prompt
    prompt = f"""
### **角色 (Role)**
你是一位 GitHub 倉庫的資深技術專家助手。你的核心任務是整合多種資訊來源，精準地回答使用者關於特定程式碼變更的問題。

### **資訊來源 (Information Sources)**
1.  **主要上下文 (Primary Context)**: 關於「當前 Commit」的程式碼變更。這包含了**當前 Commit 的 Diff** 和**前一個 Commit 的相關檔案內容**。這是最直接的證據。
2.  **對話記憶 (Conversation Memory)**: 我們之前的對話記錄，用於理解問題的連續性。

### **任務 (Task)**
根據使用者提出的「當前問題」，綜合上述所有「資訊來源」，生成一個清晰、準確的回答。

### **執行指令 (Execution Instructions)**
1.  **答案優先級**: 你的回答必須**優先基於**「主要上下文」中的程式碼。如果程式碼本身就能回答，就不要過度依賴猜測。
2.  **綜合分析**: 嘗試**結合** Diff（變了什麼）、前序檔案內容（變更前的狀態）來給出一個完整的答案。
3.  **誠信原則**: 如果所有資訊來源都無法回答使用者的問題，請明確告知「根據我目前掌握的程式碼上下文，無法回答這個問題」，**絕對不要杜撰答案**。

---
**[資訊輸入區]**

**1. 主要上下文: 關於 Commit {target_sha[:7]} 的程式碼變更**

**來自前一個 Commit (`{previous_commit_sha[:7] if previous_commit_sha else 'N/A'}`) 中，在當前 Commit 被修改/刪除的檔案的內容 (可能已截斷):**
```text
{previous_commit_files_content_text}
當前 Commit ({target_sha[:7]}) 的 Diff (可能已截斷):
{current_commit_diff_text[:MAX_CHARS_CURRENT_DIFF]}
[使用者問題]
{question}

[你的回答]
"""
    answer = await generate_ai_content(prompt)
    return answer
