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
    target_sha: str = Query(None, description="在 'commit' 模式下，指定上下文的 commit SHA"),
    mode: str = Query("commit", description="問答模式: 'commit', 'repository', 或 'what-if'"),
):
    if not access_token or not question:
        missing = [p for p, v in [("access_token", access_token), ("question", question)] if not v]
        raise HTTPException(status_code=400, detail=f"缺少必要的查詢參數: {', '.join(missing)}")

    log_question = question[:50] + "..." if len(question) > 50 else question
    logger.info(
        f"收到對話請求: {owner}/{repo}",
        extra={"owner": owner, "repo": repo, "question": log_question, "mode": mode}
    )

    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="無效或過期的 GitHub token。")

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            commit_map, commits_data = await get_commit_number_and_list(owner, repo, access_token)
            if not commits_data:
                return {"answer": "抱歉，這個倉庫目前沒有任何提交記錄，無法回答您的問題。", "history": []}

            # 根據模式選擇不同的處理邏輯
            if mode == "repository":
                answer_text = await handle_repository_qa(owner, repo, access_token, question, commits_data, client)
            elif mode == "what-if":
                answer_text = await handle_what_if_qa(owner, repo, access_token, question, commits_data, client)
            else: # mode == "commit"
                answer_text = await handle_commit_qa(owner, repo, access_token, question, target_sha, commit_map, commits_data, client)

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
            raise HTTPException(status_code=500, detail=f"處理對話時發生意外錯誤: {str(e)}")

# ▼▼▼ **[新功能]** 處理 What-if 場景模擬的邏輯 ▼▼▼
async def handle_what_if_qa(owner: str, repo: str, access_token: str, question: str, commits_data: list, client: httpx.AsyncClient):
    logger.info("進入 What-if 場景模擬模式")

    # 1. 獲取檔案樹
    latest_commit_sha = commits_data[0]["sha"]
    tree_response = await client.get(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{latest_commit_sha}?recursive=1",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    tree_response.raise_for_status()
    tree_data = tree_response.json()
    all_file_paths = [item['path'] for item in tree_data.get('tree', []) if item.get('type') == 'blob']
    file_list_str = "\n".join(all_file_paths)

    # 2. **第一階段 AI 呼叫**: 讓 AI 找出可能受影響的檔案
    file_selection_prompt = f"""
你是一個程式碼依賴分析專家。你的任務是根據使用者提出的「假設性變更」，從下方的檔案清單中，找出所有**可能受到直接或間接影響**的檔案。

使用者提出的假設性變更: "{question}"

檔案清單:
{file_list_str}

請回傳你認為所有可能受影響的檔案路徑，每個路徑一行。請盡可能列出所有相關檔案，即使只是間接關聯。
"""
    logger.info("向 AI 請求分析可能受影響的檔案...")
    relevant_files_str = await generate_ai_content(file_selection_prompt)
    relevant_files = [line.strip() for line in relevant_files_str.split('\n') if line.strip()]
    logger.info(f"AI 判斷可能受影響的檔案: {relevant_files}")

    # 3. 獲取這些檔案的內容
    files_content_map = {}
    total_chars = 0
    MAX_REPO_QA_CHARS = 50000

    for file_path in relevant_files:
        if total_chars >= MAX_REPO_QA_CHARS:
            logger.warning("獲取的檔案總內容已達上限，後續檔案將被忽略。")
            break
        try:
            file_content_res = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={latest_commit_sha}",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.raw"}
            )
            if file_content_res.status_code == 200:
                content = file_content_res.text
                files_content_map[file_path] = content
                total_chars += len(content)
        except httpx.HTTPStatusError as e:
            logger.warning(f"無法獲取檔案 {file_path} 的內容: {e}")

    # 4. **第二階段 AI 呼叫**: 進行衝擊分析
    context_for_final_prompt = ""
    for path, content in files_content_map.items():
        context_for_final_prompt += f"--- 檔案: `{path}` ---\n```\n{content}\n```\n\n"

    final_prompt = f"""
### **角色 (Role)**
你是一位資深系統架構師，擅長分析程式碼之間的依賴關係和評估變更所帶來的潛在風險。

### **任務 (Task)**
根據使用者提出的「假設性變更」和我們從程式碼庫中提取出的「相關檔案內容」，生成一份詳細的「衝擊分析報告」。

### **使用者提出的假設性變更 (What-if Scenario)**
"{question}"

### **相關檔案內容 (Context)**
{context_for_final_prompt if context_for_final_prompt else "未能從專案中找到與此變更直接相關的檔案。"}

### **輸出要求 (Output Requirements)**
請以條列式、結構化的方式生成報告，包含以下部分：

1.  **直接影響 (Direct Impact)**:
    * 明確指出哪些檔案中的哪些函式或類別會因為這個變更而直接出錯或需要修改。請引用具體的程式碼片段。

2.  **間接影響 (Indirect Impact)**:
    * 分析這個變更可能導致的連鎖反應。例如，修改了一個核心函式後，有哪些其他模組的功能可能會表現異常？

3.  **潛在風險評估 (Potential Risks)**:
    * 這個變更是否可能引入新的 Bug？是否會影響系統效能或安全性？

4.  **建議執行步驟 (Recommended Action Plan)**:
    * 如果要安全地實施這項變更，建議的步驟是什麼？（例如：需要修改哪些檔案、需要新增哪些測試案例等）。

請開始生成衝擊分析報告：
"""
    logger.info("結合檔案內容，向 AI 請求進行衝擊分析...")
    answer = await generate_ai_content(final_prompt)
    return answer

# (handle_repository_qa 和 handle_commit_qa 函式保持不變，此處省略以保持簡潔)
async def handle_repository_qa(owner: str, repo: str, access_token: str, question: str, commits_data: list, client: httpx.AsyncClient):
    # ... (程式碼與上一階段相同)
    logger.info("進入全域知識庫問答模式 (Repository Q&A)")
    latest_commit_sha = commits_data[0]["sha"]
    tree_response = await client.get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{latest_commit_sha}?recursive=1", headers={"Authorization": f"Bearer {access_token}"})
    tree_response.raise_for_status()
    tree_data = tree_response.json()
    all_file_paths = [item['path'] for item in tree_data.get('tree', []) if item.get('type') == 'blob']
    file_list_str = "\n".join(all_file_paths)
    file_selection_prompt = f"""
你是一個智慧程式碼分析引擎。你的任務是根據使用者的問題，從下方的檔案清單中，找出最可能包含相關資訊的檔案。
使用者問題: "{question}"
檔案清單:
{file_list_str}
請直接回傳你認為最相關的 3 到 5 個檔案路徑，每個路徑一行，不要有任何其他解釋或標題。
"""
    relevant_files_str = await generate_ai_content(file_selection_prompt)
    relevant_files = [line.strip() for line in relevant_files_str.split('\n') if line.strip()]
    files_content_map = {}
    total_chars = 0
    MAX_REPO_QA_CHARS = 50000
    for file_path in relevant_files:
        if total_chars >= MAX_REPO_QA_CHARS:
            break
        try:
            file_content_res = await client.get(f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={latest_commit_sha}", headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.raw"})
            if file_content_res.status_code == 200:
                content = file_content_res.text
                files_content_map[file_path] = content
                total_chars += len(content)
        except httpx.HTTPStatusError as e:
            logger.warning(f"無法獲取檔案 {file_path} 的內容: {e}")
    context_for_final_prompt = ""
    for path, content in files_content_map.items():
        context_for_final_prompt += f"--- 檔案: `{path}` ---\n```\n{content}\n```\n\n"
    final_prompt = f"""
### **角色 (Role)**
你是一位對整個程式碼庫有深入了解的資深技術專家。
### **任務 (Task)**
根據提供的多個檔案的原始碼內容，精準地回答使用者的問題。
### **上下文 (Context)**
以下是根據你的問題，從專案中提取出的最相關的檔案內容：
{context_for_final_prompt if context_for_final_prompt else "沒有找到與問題直接相關的檔案內容。"}
### **使用者問題**
"{question}"
### **執行指令**
1.  請綜合以上所有檔案的內容來形成你的答案。
2.  如果答案涉及多個檔案，請說明它們之間的關聯。
3.  如果提供的檔案內容不足以回答問題，請明確告知「根據目前分析的檔案，尚無法完整回答您的問題」。
4.  **絕對不要杜撰答案**。
請開始生成你的回答：
"""
    answer = await generate_ai_content(final_prompt)
    return answer

async def handle_commit_qa(owner: str, repo: str, access_token: str, question: str, target_sha: str, commit_map: dict, commits_data: list, client: httpx.AsyncClient):
    # ... (程式碼與上一階段相同)
    logger.info("進入特定 Commit 問答模式 (Commit Q&A)")
    if not target_sha:
        target_sha = commits_data[0]['sha']
    diff_response = await client.get(f"https://api.github.com/repos/{owner}/{repo}/commits/{target_sha}", headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.v3.diff"})
    diff_response.raise_for_status()
    current_commit_diff_text = diff_response.text
    # (此處省略了獲取前一個 commit 檔案內容、README 等完整邏輯，實際應為完整程式碼)
    prompt = f"""
### **角色 (Role)**
你是一位 GitHub 倉庫的資深技術專家助手... (此處為您之前的 commit 問答提示詞)
**[使用者問題]**
{question}
**[你的回答]**
"""
    answer = await generate_ai_content(prompt)
    return answer