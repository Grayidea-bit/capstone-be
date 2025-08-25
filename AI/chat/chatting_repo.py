from fastapi import APIRouter, HTTPException, Query
import httpx
import logging
import google.generativeai as genai
import os

from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    get_available_model,
    generate_gemini_content,
    parse_diff_for_previous_file_paths,
    conversation_history,
    MAX_FILES_FOR_PREVIOUS_CONTENT,
    MAX_CHARS_PER_PREV_FILE,
    MAX_TOTAL_CHARS_PREV_FILES,
    MAX_CHARS_CURRENT_DIFF,
    MAX_CHARS_README,
)

chat_router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)


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
        f"收到對話請求: {owner}/{repo}, token(前5):{access_token[:5]}..., q:'{log_question}', target_sha:{target_sha}"
    )

    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="Invalid or expired GitHub token.")

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
            previous_commit_files_content_text = ""  # 用於存儲 n-1 commit 的檔案內容

            commit_context_description = ""

            if target_sha:
                logger.info(
                    f"對話將使用特定 commit SHA: {target_sha} 及其前一個 commit 的相關檔案內容作為上下文。"
                )
                target_commit_obj = next(
                    (c for c in commits_data if c["sha"] == target_sha), None
                )

                current_commit_sha_for_context = target_sha
                current_commit_number_for_context = commit_map.get(target_sha)
                if current_commit_number_for_context is None:
                    logger.warning(
                        f"無法為目標 SHA {target_sha} 計算序號 (chat context)。"
                    )

                # 1. 獲取第 n 次 commit (target_sha) 的 diff
                diff_response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits/{target_sha}",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github.v3.diff",
                    },
                )
                diff_response.raise_for_status()
                current_commit_diff_text = diff_response.text
                logger.info(
                    f"已獲取目標 commit (序號: {current_commit_number_for_context}, SHA: {target_sha}) 的 diff。長度: {len(current_commit_diff_text)}"
                )

                # 2. 找到前一個 commit (n-1)
                if target_commit_obj:
                    target_index = commits_data.index(target_commit_obj)
                    if target_index + 1 < len(commits_data):
                        prev_commit_obj = commits_data[target_index + 1]
                        previous_commit_sha_for_context = prev_commit_obj["sha"]
                        previous_commit_number_for_context = commit_map.get(
                            previous_commit_sha_for_context
                        )
                        logger.info(
                            f"找到前一個 commit (序號: {previous_commit_number_for_context}, SHA: {previous_commit_sha_for_context})。"
                        )

                # 3. 如果找到了 n-1 commit，獲取其相關檔案內容
                if previous_commit_sha_for_context:
                    affected_files_in_n_minus_1 = parse_diff_for_previous_file_paths(
                        current_commit_diff_text
                    )
                    logger.info(
                        f"在 commit {target_sha} 中被修改/刪除的檔案 (來自 n-1 的路徑): {affected_files_in_n_minus_1[:MAX_FILES_FOR_PREVIOUS_CONTENT]}"
                    )

                    temp_files_content = []
                    fetched_files_count = 0
                    total_chars_fetched = 0

                    for file_path in affected_files_in_n_minus_1:
                        if fetched_files_count >= MAX_FILES_FOR_PREVIOUS_CONTENT:
                            logger.info(
                                f"已達到獲取前一個 commit 檔案內容的數量上限 ({MAX_FILES_FOR_PREVIOUS_CONTENT})。"
                            )
                            break
                        if total_chars_fetched >= MAX_TOTAL_CHARS_PREV_FILES:
                            logger.info(
                                f"已達到獲取前一個 commit 檔案內容的總字元數上限 ({MAX_TOTAL_CHARS_PREV_FILES})。"
                            )
                            break

                        try:
                            logger.debug(
                                f"正在獲取檔案 {file_path} 在 commit {previous_commit_sha_for_context} 的內容..."
                            )
                            file_content_response = await client.get(
                                f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={previous_commit_sha_for_context}",
                                headers={
                                    "Authorization": f"Bearer {access_token}",
                                    "Accept": "application/vnd.github.raw",
                                },
                            )
                            # 有些檔案可能因為權限或類型無法直接 raw 獲取，GitHub 會返回 JSON
                            if file_content_response.status_code == 200:
                                file_content = file_content_response.text
                                if len(file_content) > MAX_CHARS_PER_PREV_FILE:
                                    file_content = (
                                        file_content[:MAX_CHARS_PER_PREV_FILE]
                                        + f"\n... [檔案 {file_path} 內容因過長已被截斷]"
                                    )

                                if (
                                    total_chars_fetched + len(file_content)
                                    > MAX_TOTAL_CHARS_PREV_FILES
                                ):
                                    remaining_chars = (
                                        MAX_TOTAL_CHARS_PREV_FILES - total_chars_fetched
                                    )
                                    file_content = (
                                        file_content[:remaining_chars]
                                        + f"\n... [檔案 {file_path} 內容因總長度限制已被截斷]"
                                    )

                                temp_files_content.append(
                                    f"--- 檔案 {file_path} (來自 Commit {previous_commit_sha_for_context[:7]}) 的內容 ---\n{file_content}\n--- 結束 {file_path} 的內容 ---"
                                )
                                total_chars_fetched += len(file_content)
                                fetched_files_count += 1
                            elif file_content_response.status_code == 404:
                                logger.warning(
                                    f"檔案 {file_path} 在 commit {previous_commit_sha_for_context} 中未找到 (404)。"
                                )
                            else:
                                # 如果不是 200 或 404，記錄錯誤但繼續
                                logger.warning(
                                    f"獲取檔案 {file_path} (commit {previous_commit_sha_for_context}) 內容失敗: 狀態碼 {file_content_response.status_code}, {file_content_response.text[:100]}"
                                )
                        except Exception as e_file:
                            logger.error(
                                f"獲取檔案 {file_path} (commit {previous_commit_sha_for_context}) 內容時發生異常: {str(e_file)}"
                            )

                    previous_commit_files_content_text = "\n\n".join(temp_files_content)
                    if not previous_commit_files_content_text:
                        logger.info(
                            f"未能獲取到 commit {previous_commit_sha_for_context} 中的任何相關檔案內容。"
                        )
                    else:
                        logger.info(
                            f"成功獲取 {fetched_files_count} 個來自前一個 commit 的檔案內容，總長度約 {len(previous_commit_files_content_text)} 字元。"
                        )

                commit_context_description = f"當前 commit (序號: {current_commit_number_for_context}, SHA: {current_commit_sha_for_context})"
                if previous_commit_sha_for_context:
                    commit_context_description += f"，及其前一個 commit (序號: {previous_commit_number_for_context}, SHA: {previous_commit_sha_for_context}) 中相關檔案的內容"
                else:
                    commit_context_description += " (無前序 commit 資訊)"

            else:  # 未指定 target_sha，使用最新的 commit diff
                logger.info("對話將使用最新的 commit diff 作為上下文。")
                latest_commit_obj = commits_data[0]
                current_commit_sha_for_context = latest_commit_obj["sha"]
                current_commit_number_for_context = commit_map.get(
                    current_commit_sha_for_context
                )
                if current_commit_number_for_context is None:
                    logger.error(
                        f"無法為最新 commit SHA {current_commit_sha_for_context} 計算序號 (chat context)。"
                    )

                diff_response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits/{current_commit_sha_for_context}",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github.v3.diff",
                    },
                )
                diff_response.raise_for_status()
                current_commit_diff_text = diff_response.text
                logger.info(
                    f"已獲取最新 commit (序號: {current_commit_number_for_context}, SHA: {current_commit_sha_for_context}) 的 diff。"
                )
                commit_context_description = f"最新 commit (序號: {current_commit_number_for_context}, SHA: {current_commit_sha_for_context})"
                # previous_commit_files_content_text 保持為空

            # 截斷 diff 文本
            if len(current_commit_diff_text) > MAX_CHARS_CURRENT_DIFF:
                logger.warning(
                    f"當前 commit diff ({len(current_commit_diff_text)} 字元) 過長，截斷至 {MAX_CHARS_CURRENT_DIFF}。"
                )
                current_commit_diff_text = (
                    current_commit_diff_text[:MAX_CHARS_CURRENT_DIFF]
                    + "\n... [diff 因過長已被截斷]"
                )

            # 獲取 README
            readme_content_for_prompt = ""
            try:
                readme_response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/readme",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github.raw",
                    },
                )
                if readme_response.status_code == 200:
                    readme_content_for_prompt = readme_response.text
                    if len(readme_content_for_prompt) > MAX_CHARS_README:
                        readme_content_for_prompt = (
                            readme_content_for_prompt[:MAX_CHARS_README]
                            + "\n... [README 因過長已被截斷]"
                        )
                    logger.info(f"成功獲取 README 用於對話上下文。")
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    logger.warning(f"獲取 README 時發生 HTTP 錯誤 (非 404): {str(e)}")

            history_key = f"{owner}/{repo}/{access_token[:10]}"
            if history_key not in conversation_history:
                conversation_history[history_key] = []
            history_for_prompt_parts = []
            for item in conversation_history[history_key][-3:]:
                history_for_prompt_parts.append(f"使用者先前問: {item['question']}")
                history_for_prompt_parts.append(f"你先前答: {item['answer']}")
            history_for_prompt = "\n".join(history_for_prompt_parts)

            selected_model_name = get_available_model()
            logger.info(f"為對話選擇的模型: {selected_model_name}")
            model_instance = genai.GenerativeModel(selected_model_name)

            # 更新提示詞結構
            prompt_context_parts = [
                f"以下是關於「{commit_context_description}」的程式碼變更摘要:\n"
            ]

            if previous_commit_files_content_text:
                prompt_context_parts.append(
                    f"**來自前一個 Commit (序號: {previous_commit_number_for_context or 'N/A'}, SHA: {previous_commit_sha_for_context[:7] if previous_commit_sha_for_context else 'N/A'}) 中，在當前 Commit 被修改/刪除的檔案的內容 (可能已截斷):**\n```text\n{previous_commit_files_content_text}\n```\n"
                )
            else:
                if (
                    target_sha and previous_commit_sha_for_context
                ):  # 嘗試獲取但失敗或為空
                    prompt_context_parts.append(
                        "未能獲取到前一個 commit 的相關檔案內容，或這些檔案在前一個 commit 中不存在。\n"
                    )
                elif target_sha:  # 沒有前一個 commit (例如是第一個 commit)
                    prompt_context_parts.append(
                        "這是倉庫的第一個 commit，或無法確定前一個 commit。\n"
                    )

            prompt_context_parts.append(
                f"**當前 Commit (序號: {current_commit_number_for_context or 'N/A'}, SHA: {current_commit_sha_for_context[:7]}) 的 Diff (可能已截斷):**\n```diff\n{current_commit_diff_text}\n```"
            )

            diff_data_for_prompt = "\n".join(prompt_context_parts)

            prompt = f"""
作為一個專注於 GitHub 倉庫的 AI 助手，請根據以下提供的倉庫上下文（包括指定的 commit diff、相關的前一個 commit 中的檔案內容、以及 README）和先前的對話歷史來回答使用者的問題。
你的回答應該：
1. 簡潔、直接且與提供的上下文相關。
2. 如果問題與程式碼變更相關，請參考「{commit_context_description}」提供的程式碼和 diff。
3. 如果問題超出當前上下文，請誠實告知，避免編造。
4. 內容可能已被截斷以符合長度限制。

**倉庫程式碼上下文**:
{diff_data_for_prompt}

**倉庫 README (若可用, 可能已截斷)**:
```
{readme_content_for_prompt if readme_content_for_prompt else "未提供 README。"}
```

**先前對話歷史 (最近的在最後)**:
{history_for_prompt if history_for_prompt else "這是對話的開始。"}

**使用者當前問題**: {question}

請提供你的回答:
"""
            log_prompt = (
                prompt[:400] + "..." if len(prompt) > 400 else prompt
            )  # 增加日誌中 prompt 的長度
            logger.info(
                f"送往 Gemini 的對話提示詞 (模型: {selected_model_name}, 提示詞長度約: {len(prompt)} 字元): {log_prompt}"
            )

            answer_text = await generate_gemini_content(model_instance, prompt)
            log_answer = (
                answer_text[:100] + "..." if len(answer_text) > 100 else answer_text
            )
            logger.info(
                f"Gemini 對話回答 (模型: {selected_model_name}): '{log_answer}'"
            )

            conversation_history[history_key].append(
                {"question": question, "answer": answer_text}
            )
            if len(conversation_history[history_key]) > 5:
                conversation_history[history_key] = conversation_history[history_key][
                    -5:
                ]

            return {"answer": answer_text, "history": conversation_history[history_key]}

        except httpx.HTTPStatusError as e:
            logger.error(
                f"處理對話時發生 GitHub API 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}"
            )
            detail = f"因 GitHub API 錯誤，無法處理對話: {e.response.status_code} - {e.response.text}"
            if e.response.status_code == 401:
                detail = "GitHub token 可能無效或已過期 (處理對話時)。"
            elif e.response.status_code == 404 and target_sha:
                detail = f"指定的 commit SHA ({target_sha}) 或相關檔案未在倉庫 {owner}/{repo} 中找到。"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e:
            logger.error(f"處理對話時發生 HTTPException: {e.detail}")
            raise e
        except Exception as e:
            logger.error(f"處理對話時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"處理對話時發生意外錯誤: {str(e)}"
            )
