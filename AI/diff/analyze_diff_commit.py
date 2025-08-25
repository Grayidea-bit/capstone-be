from fastapi import APIRouter, HTTPException, Query
from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    get_available_model,
    generate_gemini_content,
)
import google.generativeai as genai
import httpx
import logging
import os


diff_router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)


@diff_router.post("/repos/{owner}/{repo}/commits/{sha}")
async def analyze_commit_diff(
    owner: str, repo: str, sha: str, access_token: str = Query(None)
):
    if not access_token:
        raise HTTPException(status_code=401, detail="Access token is missing.")
    logger.info(
        f"收到 commit 分析請求: owner={owner}, repo={repo}, sha={sha}, token (前5碼)={access_token[:5]}..."
    )
    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="Invalid or expired GitHub token.")

    async with httpx.AsyncClient() as client:
        try:
            commit_map, commits_data = await get_commit_number_and_list(
                owner, repo, access_token
            )
            if not commits_data:
                raise HTTPException(
                    status_code=404, detail="倉庫中沒有 commits，無法進行分析。"
                )

            target_commit_obj = next((c for c in commits_data if c["sha"] == sha), None)
            if not target_commit_obj:
                logger.warning(
                    f"目標 commit SHA {sha} 未在快取的 commit 列表中找到。將嘗試直接從 GitHub API 獲取。"
                )
                try:
                    target_commit_res = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                    target_commit_res.raise_for_status()
                    logger.info(
                        f"目標 commit SHA {sha} 在 GitHub 上找到，但不在本地快取中。繼續分析，但可能缺少序號上下文。"
                    )
                except httpx.HTTPStatusError:
                    raise HTTPException(
                        status_code=404,
                        detail=f"目標 commit SHA {sha} 未在倉庫 {owner}/{repo} 中找到。",
                    )

            target_commit_number = commit_map.get(sha)
            if target_commit_number is None:
                logger.warning(f"無法為 SHA {sha} 計算序號 (analyze)，將不顯示序號。")

            current_diff_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github.v3.diff",
                },
            )
            current_diff_response.raise_for_status()
            current_diff_text = current_diff_response.text
            logger.info(
                f"已獲取目標 commit (序號: {target_commit_number or 'N/A'}, SHA: {sha}) 的 diff。長度: {len(current_diff_text)}"
            )

            previous_diff_text = None
            previous_commit_sha = None
            previous_commit_number = None

            if target_commit_obj:
                target_index = commits_data.index(target_commit_obj)
                if target_index + 1 < len(commits_data):
                    previous_commit_obj = commits_data[target_index + 1]
                    previous_commit_sha = previous_commit_obj["sha"]
                    previous_commit_number = commit_map.get(previous_commit_sha)
                    if previous_commit_sha:
                        prev_diff_response = await client.get(
                            f"https://api.github.com/repos/{owner}/{repo}/commits/{previous_commit_sha}",
                            headers={
                                "Authorization": f"Bearer {access_token}",
                                "Accept": "application/vnd.github.v3.diff",
                            },
                        )
                        if prev_diff_response.status_code == 200:
                            previous_diff_text = prev_diff_response.text
                            logger.info(
                                f"已獲取前一個 commit (序號: {previous_commit_number or 'N/A'}, SHA: {previous_commit_sha}) 的 diff。長度: {len(previous_diff_text)}"
                            )
                        else:
                            logger.warning(
                                f"獲取前一個 commit {previous_commit_sha} 的 diff 失敗: 狀態碼 {prev_diff_response.status_code}"
                            )
            else:
                logger.info(
                    f"目標 commit SHA {sha} 不在快取中，無法自動確定前一個 commit。"
                )

            current_diff_for_prompt = current_diff_text
            if len(current_diff_for_prompt) > 60000:
                logger.warning(
                    f"當前 commit diff ({len(current_diff_for_prompt)} 字元) 過長，截斷至 60000。"
                )
                current_diff_for_prompt = (
                    current_diff_for_prompt[:60000] + "\n... [diff 因過長已被截斷]"
                )

            previous_diff_for_prompt = previous_diff_text
            if previous_diff_for_prompt and len(previous_diff_for_prompt) > 15000:
                logger.warning(
                    f"前一個 commit diff ({len(previous_diff_for_prompt)} 字元) 過長，截斷至 15000。"
                )
                previous_diff_for_prompt = (
                    previous_diff_for_prompt[:15000]
                    + "\n... [前一個 diff 因過長已被截斷]"
                )

            combined_diff_for_gemini = ""
            if previous_diff_for_prompt and previous_commit_number is not None:
                combined_diff_for_gemini += f"**上下文：前一個 Commit (序號: {previous_commit_number}, SHA: {previous_commit_sha}) 的 Diff 摘要:**\n```diff\n{previous_diff_for_prompt}\n```\n\n"
            elif previous_diff_for_prompt:
                combined_diff_for_gemini += f"**上下文：前一個 Commit (SHA: {previous_commit_sha}) 的 Diff 摘要:**\n```diff\n{previous_diff_for_prompt}\n```\n\n"
            else:
                combined_diff_for_gemini += "沒有找到前一個 commit 的 diff，或者這是倉庫中的第一個有效 commit。\n\n"
            combined_diff_for_gemini += f"**主要分析目標：當前 Commit (序號: {target_commit_number or 'N/A'}, SHA: {sha}) 的 Diff:**\n```diff\n{current_diff_for_prompt}\n```"

            selected_model_name = get_available_model()
            logger.info(f"為 commit 分析選擇的模型: {selected_model_name}")
            model_instance = genai.GenerativeModel(selected_model_name)
            prompt = f"""
作為一位經驗豐富的程式碼審查專家，請分析以下 GitHub commit 變更。
主要分析目標是「當前 Commit (序號: {target_commit_number or 'N/A'}, SHA: {sha})」的變更。
如果提供了「前一個 Commit」的 diff，請將其作為比較的上下文，以理解變更的演進。

你的分析應包含：
1.  **變更摘要**: 簡要說明當前 commit 的主要目的是什麼。
2.  **詳細變更**: 描述當前 commit 中引入的關鍵程式碼更改。可以分點說明。
3.  **影響與改進**: 這些變更如何影響或改進了程式碼庫？它們解決了什麼問題（如果有的話）？
4.  **潛在問題或建議 (可選)**: 是否有任何潛在的風險、需要注意的地方或可以進一步改進的建議？
請使用清晰、專業的語言。

**Commit Diff 上下文**:
{combined_diff_for_gemini}

請提供你的分析報告:
"""
            log_prompt = prompt[:300] + "..." if len(prompt) > 300 else prompt
            logger.info(
                f"送往 Gemini 的分析提示詞 (模型: {selected_model_name}, 提示詞長度約: {len(prompt)} 字元): {log_prompt}"
            )
            analysis_text = await generate_gemini_content(model_instance, prompt)
            log_analysis = (
                analysis_text[:150] + "..."
                if len(analysis_text) > 150
                else analysis_text
            )
            logger.info(
                f"Gemini 分析結果 (模型: {selected_model_name}): '{log_analysis}'"
            )
            return {
                "sha": sha,
                "diff": current_diff_text,
                "previous_diff": previous_diff_text,
                "analysis": analysis_text,
                "commit_number": target_commit_number,
                "previous_commit_number": previous_commit_number,
            }
        except httpx.HTTPStatusError as e:
            logger.error(
                f"分析 commit diff 時發生 GitHub API 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}"
            )
            detail = f"因 GitHub API 錯誤，無法分析 commit diff: {e.response.status_code} - {e.response.text}"
            if e.response.status_code == 401:
                detail = "GitHub token 可能無效或已過期 (分析 commit diff 時)。"
            elif e.response.status_code == 404:
                detail = f"Commit SHA {sha} 或相關數據未在倉庫 {owner}/{repo} 中找到以進行分析。"
            elif e.response.status_code == 422:
                detail = f"無法處理 commit SHA {sha} 的 diff (可能是無效的 SHA 或 diff 過大): {e.response.text}"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e:
            logger.error(f"分析 commit diff 時發生 HTTPException: {e.detail}")
            raise e
        except Exception as e:
            logger.error(f"分析 commit diff 時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"分析 commit diff 時發生意外錯誤: {str(e)}"
            )
