"""
QQ 点赞工具插件 - 给指定QQ用户点赞
通过 NapCat HTTP API 直接调用 send_like 接口

NapCat API 文档: https://napcat.apifox.cn/226656717e0
- 端点: POST /send_like
- 参数: user_id (string), times (number/string)
- 注意: 间隔小于3秒会被吞赞
"""
import os
import json
import time
import asyncio
from datetime import date
from typing import Dict, Any, Optional
from pathlib import Path

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
from astrbot.api.platform import MessageType
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext


class NapCatClient:
    """NapCat HTTP API 客户端"""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _build_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def call_action(self, action: str, **kwargs) -> Any:
        """调用 NapCat HTTP API"""
        url = f"{self.base_url}/{action.lstrip('/')}"
        payload = {k: v for k, v in kwargs.items() if v is not None}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=self._build_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                return data


class LikeRecordManager:
    """点赞记录管理器 - 用于记录今天已点赞的用户"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.record_file = os.path.join(data_dir, "like_records.json")
        self.records: Dict[str, Dict] = {}
        self._load_records()

    def _load_records(self):
        try:
            if os.path.exists(self.record_file):
                with open(self.record_file, "r", encoding="utf-8") as f:
                    self.records = json.load(f)
                self._cleanup_old_records()
        except Exception as e:
            logger.warning(f"[LikeTool] 加载点赞记录失败: {e}")
            self.records = {}

    def _save_records(self):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.record_file, "w", encoding="utf-8") as f:
                json.dump(self.records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[LikeTool] 保存点赞记录失败: {e}")

    def _cleanup_old_records(self):
        today = date.today().isoformat()
        old_keys = [k for k, v in self.records.items() if v.get("date") != today]
        for k in old_keys:
            del self.records[k]
        if old_keys:
            self._save_records()

    def has_liked_today(self, qq_id: str) -> bool:
        self._cleanup_old_records()
        record = self.records.get(str(qq_id))
        if record:
            return record.get("date") == date.today().isoformat()
        return False

    def record_like(self, qq_id: str, count: int = 10):
        today = date.today().isoformat()
        self.records[str(qq_id)] = {
            "date": today,
            "count": count,
            "timestamp": int(time.time()),
        }
        self._save_records()

    def get_today_liked_count(self) -> int:
        self._cleanup_old_records()
        return len(self.records)


class SendLikeTool(FunctionTool):
    """
    给指定QQ用户点赞工具

    通过 NapCat HTTP API 的 /send_like 接口实现点赞。
    - 每次点赞10个
    - 今天已经点赞过的用户不能再点赞
    - 注意: NapCat 限制间隔小于3秒会被吞赞
    """

    def __init__(self, record_manager: LikeRecordManager, napcat_client: NapCatClient):
        super().__init__(
            name="send_like",
            description="给指定QQ用户点赞。每次点赞10个，今天已经点赞过的用户不能再点赞。",
            parameters={
                "type": "object",
                "properties": {
                    "qq_id": {
                        "type": "string",
                        "description": "目标用户的QQ号",
                    }
                },
                "required": ["qq_id"],
            },
        )
        self.record_manager = record_manager
        self.napcat_client = napcat_client

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        qq_id = kwargs.get("qq_id", "").strip()

        if not qq_id:
            return "请提供目标用户的QQ号。"

        if not qq_id.isdigit():
            return f"QQ号格式错误: {qq_id}，请输入纯数字QQ号。"

        if self.record_manager and self.record_manager.has_liked_today(qq_id):
            return f"今天已经给 {qq_id} 点过赞了，明天再来吧~"

        try:
            like_times = 10

            logger.debug(
                f"[LikeTool] 调用 NapCat HTTP API: send_like user_id={qq_id} times={like_times}"
            )
            resp = await self.napcat_client.call_action(
                "send_like", user_id=qq_id, times=like_times
            )

            logger.debug(f"[LikeTool] NapCat 响应: {resp}")

            # 解析 NapCat 标准响应格式
            is_success = False
            last_error = None

            if isinstance(resp, dict):
                status = resp.get("status")
                retcode = resp.get("retcode")
                if status == "ok" or retcode == 0:
                    is_success = True
                else:
                    last_error = (
                        resp.get("message")
                        or resp.get("wording")
                        or resp.get("msg")
                        or f"retcode={retcode}"
                    )
                    logger.warning(f"[LikeTool] 点赞失败: {last_error}")
            else:
                # 非标准响应，尝试当作成功处理
                is_success = True

            total_likes = like_times if is_success else 0

            if self.record_manager and total_likes > 0:
                self.record_manager.record_like(qq_id, total_likes)

            today_count = 0
            if self.record_manager:
                today_count = self.record_manager.get_today_liked_count()

            if total_likes > 0:
                return (
                    f"已成功给 {qq_id} 点了 {total_likes} 个赞！"
                    f"今天已累计给 {today_count} 位用户点赞。"
                )
            else:
                error_detail = f" ({last_error})" if last_error else ""
                if "点赞数无效" in str(last_error):
                    return (
                        f"点赞失败: 无法给 {qq_id} 点赞。"
                        f"可能原因: 1.机器人从未见过该用户 2.该用户是陌生人 3.已达到今日点赞上限。"
                        f"建议: 先让该用户在群里发一条消息，然后再尝试点赞。"
                    )
                else:
                    return f"点赞失败{error_detail}: 无法给 {qq_id} 点赞，可能已达到今日点赞上限或对方不是好友。"

        except aiohttp.ClientError as e:
            logger.error(f"[LikeTool] 连接 NapCat 失败: {e}")
            return f"点赞失败: 无法连接到 NapCat HTTP 服务 ({self.napcat_client.base_url})，请检查配置是否正确以及 NapCat 是否在运行。"
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[LikeTool] 点赞失败: {error_msg}")

            if "not friend" in error_msg.lower() or "非好友" in error_msg:
                return f"点赞失败: {qq_id} 不是好友，只能给好友点赞。"
            elif "limit" in error_msg.lower() or "限制" in error_msg:
                return f"点赞失败: 已达到今日点赞上限，请明天再试。"
            elif "timeout" in error_msg.lower():
                return f"点赞失败: 请求超时，请稍后重试。"
            else:
                return f"点赞失败: {error_msg}"


class QQLikeToolsPlugin(Star):
    """QQ 点赞工具插件"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 读取配置
        napcat_url = self.config.get("napcat_url", "http://127.0.0.1:3000")
        napcat_token = self.config.get("napcat_token", "")

        logger.info(f"[QQLikeTools] NapCat HTTP API 地址: {napcat_url}")

        # 初始化 NapCat 客户端
        self.napcat_client = NapCatClient(napcat_url, napcat_token)

        # 初始化点赞记录管理器
        try:
            data_dir = str(StarTools.get_data_dir("astrbot_plugin_qq_like_tools"))
            self.record_manager = LikeRecordManager(data_dir)
            logger.info(f"[QQLikeTools] 数据目录: {data_dir}")
            logger.info(f"[QQLikeTools] 记录文件: {self.record_manager.record_file}")
        except Exception as e:
            logger.error(f"[QQLikeTools] 初始化记录管理器失败: {e}")
            self.record_manager = None

        # 注册 LLM 工具
        self.context.add_llm_tools(SendLikeTool(self.record_manager, self.napcat_client))
        logger.info("[QQLikeTools] 点赞工具已注册 (NapCat HTTP API 模式)")

    async def terminate(self):
        self.context.unregister_llm_tool("send_like")
        logger.info("[QQLikeTools] 点赞工具已卸载")
