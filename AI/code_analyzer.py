# AI/code_analyzer.py
import httpx
import json
from typing import List, Dict, Any

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
        # 快取鍵現在包含 commit SHA，以實現更細緻的快取
        self.latest_commit_sha = None

    async def _get_latest_commit_sha(self) -> str:
        """獲取最新的 commit SHA 並緩存結果"""
        if self.latest_commit_sha:
            return self.latest_commit_sha

        # 嘗試從 Redis 快取獲取最新的 commit SHA
        cache_key = f"latest_commit_sha:{self.owner}/{self.repo}"
        if redis_client:
            try:
                cached_sha = redis_client.get(cache_key)
                if cached_sha:
                    logger.info(f"從快取中獲取最新的 commit SHA: {cached_sha[:7]}")
                    self.latest_commit_sha = cached_sha
                    return cached_sha
            except Exception as e:
                logger.error(f"讀取最新 commit SHA 快取失敗: {e}")

        response = await self.client.get(
            f"https://api.github.com/repos/{self.owner}/{self.repo}/commits",
            headers={"Authorization": f"Bearer {self.access_token}"},
            params={"per_page": 1}
        )
        response.raise_for_status()
        latest_sha = response.json()[0]['sha']
        self.latest_commit_sha = latest_sha

        # 將最新的 commit SHA 存入 Redis 快取
        if redis_client:
            try:
                # 設定一個較短的過期時間，例如 5 分鐘，以確保能及時獲取到最新的 commit
                redis_client.set(cache_key, latest_sha, ex=300)
            except Exception as e:
                logger.error(f"寫入最新 commit SHA 快取失敗: {e}")

        return latest_sha

    async def get_all_py_files(self) -> List[str]:
        """
        獲取程式碼庫中所有的 .py 檔案路徑列表，並進行快取。
        快取現在與最新的 commit SHA 綁定。
        """
        latest_commit_sha = await self._get_latest_commit_sha()
        cache_key_file_list = f"code_analyzer:file_list:{self.owner}/{self.repo}:{latest_commit_sha}"

        if redis_client:
            try:
                cached_files = redis_client.get(cache_key_file_list)
                if cached_files:
                    logger.info(f"從快取中獲取檔案列表 (commit: {latest_commit_sha[:7]})")
                    return json.loads(cached_files)
            except Exception as e:
                logger.error(f"讀取檔案列表快取失敗: {e}")

        logger.info(f"正在為 {self.owner}/{self.repo} (commit: {latest_commit_sha[:7]}) 獲取檔案列表...")
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
                # 檔案列表與 commit SHA 綁定，可以設定較長的過期時間
                redis_client.set(cache_key_file_list, json.dumps(py_files), ex=CACHE_TTL_SECONDS)
                logger.info(f"已快取檔案列表 (commit: {latest_commit_sha[:7]})")
            except Exception as e:
                logger.error(f"寫入檔案列表快取失敗: {e}")

        return py_files

    async def get_files_content(self, file_paths: List[str], ref: str = None) -> Dict[str, str]:
        """
        獲取指定檔案路徑列表的內容，優先從快取讀取。
        可以指定 ref (commit SHA, branch, tag) 來獲取特定版本的檔案內容。
        """
        files_content_map = {}
        commit_sha_to_use = ref if ref else await self._get_latest_commit_sha()

        for file_path in file_paths:
            # 快取鍵包含 commit SHA，實現版本化快取
            content_cache_key = f"code_analyzer:file_content:{self.owner}/{self.repo}:{commit_sha_to_use}:{file_path}"
            
            if redis_client:
                try:
                    cached_content = redis_client.get(content_cache_key)
                    if cached_content:
                        logger.info(f"從快取獲取檔案內容: {file_path} @ {commit_sha_to_use[:7]}")
                        files_content_map[file_path] = cached_content
                        continue
                except Exception as e:
                    logger.error(f"讀取檔案內容快取失敗 for {file_path}: {e}")

            logger.info(f"正在從 API 獲取檔案內容: {file_path} @ {commit_sha_to_use[:7]}")
            try:
                file_content_res = await self.client.get(
                    f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{file_path}?ref={commit_sha_to_use}",
                    headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/vnd.github.raw"}
                )
                if file_content_res.status_code == 200:
                    content = file_content_res.text
                    files_content_map[file_path] = content
                    if redis_client:
                        try:
                            # 特定版本的檔案內容是永久不變的，可以設定較長的過期時間
                            redis_client.set(content_cache_key, content, ex=CACHE_TTL_SECONDS)
                        except Exception as e:
                            logger.error(f"寫入檔案內容快取失敗 for {file_path}: {e}")
            except httpx.HTTPStatusError as e:
                logger.warning(f"無法獲取檔案 {file_path} @ {commit_sha_to_use[:7]} 的內容: {e}")
        
        return files_content_map