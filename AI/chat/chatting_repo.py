from fastapi import APIRouter, HTTPException, Query
import httpx
from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    generate_ai_content,
    parse_diff_for_previous_file_paths,
    MAX_FILES_FOR_PREVIOUS_CONTENT,
    MAX_CHARS_PER_PREV_FILE,
    MAX_TOTAL_CHARS_PREV_FILES,
    MAX_CHARS_CURRENT_DIFF,
    MAX_CHARS_README,
    logger,
    redis_client,
)
import json

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
        redis_client.set(history_key, json.dumps(history[-5:]), ex=3600)
    except Exception as e:
        logger.error(f"寫入對話歷史快取失敗: {e}", extra={"history_key": history_key})

@chat_router.post("/repos/{owner}/{repo}")
async def chat_with_repo(
    owner: str,
    repo: str,
    access_token: str = Query(None),
    question: str = Query(None),
    target_sha: str = Query(None),
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
        extra={"owner": owner, "repo": repo, "question": log_question, "target_sha": target_sha}
    )

    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="無效或過期的 GitHub token。")

    async with httpx.AsyncClient() as client:
        try:
            commit_map, commits_data = await get_commit_number_and_list(
                owner, repo, access_token
            )
            if not commits_data:
                logger.info(f"倉庫 {owner}/{repo} 無 commits，無法進行對話。")
                return {
                    "answer": "抱歉，這個倉庫目前沒有任何提交記錄，我無法根據程式碼內容回答您的問題。",
                    "history": [],
                }

            current_commit_sha_for_context = None
            current_commit_number_for_context = None
            current_commit_diff_text = ""
            previous_commit_sha_for_context = None
            previous_commit_number_for_context = None
            previous_commit_files_content_text = ""
            commit_context_description = ""

            if target_sha:
                logger.info(
                    f"對話將使用特定 commit SHA: {target_sha} 作為上下文。"
                )
                target_commit_obj = next(
                    (c for c in commits_data if c["sha"] == target_sha), None
                )

                current_commit_sha_for_context = target_sha
                current_commit_number_for_context = commit_map.get(target_sha)
                
                diff_response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits/{target_sha}",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github.v3.diff",
                    },
                )
                diff_response.raise_for_status()
                current_commit_diff_text = diff_response.text

                if target_commit_obj:
                    target_index = commits_data.index(target_commit_obj)
                    if target_index + 1 < len(commits_data):
                        prev_commit_obj = commits_data[target_index + 1]
                        previous_commit_sha_for_context = prev_commit_obj["sha"]
                        previous_commit_number_for_context = commit_map.get(
                            previous_commit_sha_for_context
                        )

                if previous_commit_sha_for_context:
                    affected_files = parse_diff_for_previous_file_paths(
                        current_commit_diff_text
                    )
                    
                    temp_files_content = []
                    fetched_files_count = 0
                    total_chars_fetched = 0

                    for file_path in affected_files:
                        if fetched_files_count >= MAX_FILES_FOR_PREVIOUS_CONTENT or total_chars_fetched >= MAX_TOTAL_CHARS_PREV_FILES:
                            break
                        try:
                            file_content_response = await client.get(
                                f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={previous_commit_sha_for_context}",
                                headers={
                                    "Authorization": f"Bearer {access_token}",
                                    "Accept": "application/vnd.github.raw",
                                },
                            )
                            if file_content_response.status_code == 200:
                                file_content = file_content_response.text
                                temp_files_content.append(f"--- 檔案 {file_path} (來自 Commit {previous_commit_sha_for_context[:7]}) 的內容 ---\n{file_content}\n--- 結束 {file_path} 的內容 ---")
                                total_chars_fetched += len(file_content)
                                fetched_files_count += 1
                        except Exception as e_file:
                            logger.error(f"獲取檔案 {file_path} 內容時發生異常: {str(e_file)}")
                    
                    previous_commit_files_content_text = "\n\n".join(temp_files_content)

                commit_context_description = f"當前 commit (序號: {current_commit_number_for_context or 'N/A'}, SHA: {current_commit_sha_for_context[:7]})"
            
            else:
                logger.info("對話將使用最新的 commit diff 作為上下文。")
                latest_commit_obj = commits_data[0]
                current_commit_sha_for_context = latest_commit_obj["sha"]
                current_commit_number_for_context = commit_map.get(current_commit_sha_for_context)
                
                diff_response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits/{current_commit_sha_for_context}",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.v3.diff"},
                )
                diff_response.raise_for_status()
                current_commit_diff_text = diff_response.text
                commit_context_description = f"最新 commit (序號: {current_commit_number_for_context or 'N/A'}, SHA: {current_commit_sha_for_context[:7]})"

            if len(current_commit_diff_text) > MAX_CHARS_CURRENT_DIFF:
                current_commit_diff_text = current_commit_diff_text[:MAX_CHARS_CURRENT_DIFF] + "\n... [diff 因過長已被截斷]"

            readme_content_for_prompt = ""
            try:
                readme_response = await client.get(f"https://api.github.com/repos/{owner}/{repo}/readme", headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.raw"})
                if readme_response.status_code == 200:
                    readme_content_for_prompt = readme_response.text
                    if len(readme_content_for_prompt) > MAX_CHARS_README:
                        readme_content_for_prompt = readme_content_for_prompt[:MAX_CHARS_README] + "\n... [README 因過長已被截斷]"
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    logger.warning(f"獲取 README 時發生 HTTP 錯誤: {str(e)}")

            history_key = f"chat_history:{owner}/{repo}/{access_token[:10]}"
            conversation_history = get_conversation_history(history_key)
            
            history_for_prompt_parts = []
            for item in conversation_history:
                history_for_prompt_parts.append(f"使用者先前問: {item['question']}")
                history_for_prompt_parts.append(f"你先前答: {item['answer']}")
            history_for_prompt = "\n".join(history_for_prompt_parts)
            
            prompt_context_parts = [f"以下是關於「{commit_context_description}」的程式碼變更摘要:\n"]
            if previous_commit_files_content_text:
                prompt_context_parts.append(f"**來自前一個 Commit (序號: {previous_commit_number_for_context or 'N/A'}) 的相關檔案內容:**\n```text\n{previous_commit_files_content_text}\n```\n")
            
            prompt_context_parts.append(f"**當前 Commit 的 Diff:**\n```diff\n{current_commit_diff_text}\n```")
            diff_data_for_prompt = "\n".join(prompt_context_parts)

            prompt = f"""
### **角色 (Role)**
你是一位 GitHub 倉庫的資深技術專家助手。你的核心任務是整合多種資訊來源，精準地回答使用者關於特定程式碼變更的問題。

### **資訊來源 (Information Sources)**
1.  **主要上下文 (Primary Context)**: 關於「{commit_context_description}」的程式碼。這包含了**當前 Commit 的 Diff** 和**前一個 Commit 的相關檔案內容**。這是最直接的證據。
2.  **專案概覽 (Project Overview)**: 倉庫的 README 文件，用於理解專案的宏觀目標。
3.  **對話記憶 (Conversation Memory)**: 我們之前的對話記錄，用於理解問題的連續性。

### **任務 (Task)**
根據使用者提出的「當前問題」，綜合上述所有「資訊來源」，生成一個清晰、準確的回答。

### **執行指令 (Execution Instructions)**
1.  **答案優先級**: 你的回答必須**優先基於**「主要上下文」中的程式碼。如果程式碼本身就能回答，就不要過度依賴 README 或猜測。
2.  **綜合分析**: 如果問題較為複雜，請嘗試**結合** Diff（變了什麼）、前序檔案內容（變更前的狀態）和 README（為什麼要這麼做）來給出一個完整的答案。
3.  **誠信原則**: 如果所有資訊來源都無法回答使用者的問題，請明確告知「根據我目前掌握的程式碼上下文，無法回答這個問題」，**絕對不要杜撰答案**。
4.  **引用與定位**: 如果可能，請簡要說明你的答案是基於哪一部分的程式碼變更。
5.  **簡潔性**: 保持回答的簡潔和直接，避免不必要的客套話。

---
**[資訊輸入區]**

**1. 主要上下文: {commit_context_description}**
{diff_data_for_prompt}

**2. 專案概覽: README**
```markdown
{readme_content_for_prompt if readme_content_for_prompt else "未提供 README。"}
```

**3. 對話記憶 (最近的在最後)**
{history_for_prompt if history_for_prompt else "這是我們的第一次對話。"}

**[使用者問題]**
{question}

**[你的回答]**
"""
            answer_text = await generate_ai_content(prompt)
            conversation_history.append({"question": question, "answer": answer_text})
            set_conversation_history(history_key, conversation_history)

            return {"answer": answer_text, "history": conversation_history}

        except httpx.HTTPStatusError as e:
            logger.error(
                f"處理對話時發生 GitHub API 錯誤: {str(e)}",
                extra={"url": str(e.request.url)},
            )
            detail = f"因 GitHub API 錯誤，無法處理對話: {e.response.status_code} - {e.response.text}"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e:
            logger.error(f"處理對話時發生 HTTPException: {e.detail}")
            raise e
        except Exception as e:
            logger.error(f"處理對話時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"處理對話時發生意外錯誤: {str(e)}"
            )
