# AI/code_analyzer.py
import httpx
import json
from typing import List, Dict, Any

# 修正了相對導入的路徑，從 '..' 改為 '.'
from .setting import (
    logger,
    redis_client,
    CACHE_TTL_SECONDS
)

class CodeAnalyzer:
    """
    一個共用的程式碼分析器，負責建立和快取程式碼庫的知識庫。
    它會獲取所有 Python 檔案，並提供查詢檔案內容的功能。
    """
    def __init__(self, owner: str, repo: str, access_token: str, client: httpx.AsyncClient):
        self.owner = owner
        self.repo = repo
        self.access_token = access_token
        self.client = client
        self.cache_key_file_list = f"code_analyzer:file_list:{owner}/{repo}"
        self.cache_key_file_content_prefix = f"code_analyzer:file_content:{owner}/{repo}:"

    async def _get_latest_commit_sha(self) -> str:
        """獲取最新的 commit SHA"""
        response = await self.client.get(
            f"https://api.github.com/repos/{self.owner}/{self.repo}/commits",
            headers={"Authorization": f"Bearer {self.access_token}"},
            params={"per_page": 1}
        )
        response.raise_for_status()
        return response.json()[0]['sha']

    async def get_all_py_files(self) -> List[str]:
        """
        獲取程式碼庫中所有的 .py 檔案路徑列表，並進行快取。
        """
        if redis_client:
            try:
                cached_files = redis_client.get(self.cache_key_file_list)
                if cached_files:
                    logger.info(f"從快取中獲取檔案列表: {self.cache_key_file_list}")
                    return json.loads(cached_files)
            except Exception as e:
                logger.error(f"讀取檔案列表快取失敗: {e}")

        logger.info(f"正在為 {self.owner}/{self.repo} 獲取檔案列表...")
        latest_commit_sha = await self._get_latest_commit_sha()
        tree_response = await self.client.get(
            f"https://api.github.com/repos/{self.owner}/{self.repo}/git/trees/{latest_commit_sha}?recursive=1",
            headers={"Authorization": f"Bearer {self.access_token}"}
        )
        tree_response.raise_for_status()
        tree_data = tree_response.json()

        py_files = [
            item['path'] for item in tree_data.get('tree', [])
            if item.get('type') == 'blob' and item['path'].endswith('.py')
        ]

        if redis_client:
            try:
                redis_client.set(self.cache_key_file_list, json.dumps(py_files), ex=CACHE_TTL_SECONDS)
                logger.info(f"已快取檔案列表: {self.cache_key_file_list}")
            except Exception as e:
                logger.error(f"寫入檔案列表快取失敗: {e}")

        return py_files

    async def get_files_content(self, file_paths: List[str]) -> Dict[str, str]:
        """
        獲取指定檔案路徑列表的內容，優先從快取讀取。
        """
        files_content_map = {}
        latest_commit_sha = await self._get_latest_commit_sha()

        for file_path in file_paths:
            content_cache_key = f"{self.cache_key_file_content_prefix}{file_path}"
            
            if redis_client:
                try:
                    cached_content = redis_client.get(content_cache_key)
                    if cached_content:
                        logger.info(f"從快取獲取檔案內容: {file_path}")
                        files_content_map[file_path] = cached_content
                        continue
                except Exception as e:
                    logger.error(f"讀取檔案內容快取失敗 for {file_path}: {e}")

            logger.info(f"正在從 API 獲取檔案內容: {file_path}")
            try:
                file_content_res = await self.client.get(
                    f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{file_path}?ref={latest_commit_sha}",
                    headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/vnd.github.raw"}
                )
                if file_content_res.status_code == 200:
                    content = file_content_res.text
                    files_content_map[file_path] = content
                    if redis_client:
                        try:
                            redis_client.set(content_cache_key, content, ex=CACHE_TTL_SECONDS)
                        except Exception as e:
                            logger.error(f"寫入檔案內容快取失敗 for {file_path}: {e}")
            except httpx.HTTPStatusError as e:
                logger.warning(f"無法獲取檔案 {file_path} 的內容: {e}")
        
        return files_content_map