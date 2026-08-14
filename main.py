"""
QQ 点赞工具插件 - 给指定QQ用户点赞
使用 Napcat 的 send_like API

Napcat API 文档: https://napcat.apifox.cn/226656717e0
- 端点: /send_like
- 参数: user_id (string), times (int)
- 注意: 间隔小于3秒会被吞赞
"""
import os
import json
import time
import asyncio
from datetime import date
from typing import Dict, Any
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
from astrbot.api.platform import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext


def _unwrap_onebot_response(resp: Any) -> Any:
    """兼容不同 OneBot 实现的返回格式"""
    if isinstance(resp, dict) and "data" in resp:
        if any(k in resp for k in ("retcode", "status", "msg", "wording")):
            return resp.get("data")
    return resp


async def call_onebot(client, action: str, **kwargs) -> Any:
    """OneBot API 兼容层调用函数"""
    # 优先尝试 client.call_action
    if hasattr(client, 'call_action') and callable(getattr(client, 'call_action', None)):
        try:
            resp = await client.call_action(action, **kwargs)
            return _unwrap_onebot_response(resp)
        except AttributeError:
            pass
        except Exception as e:
            if hasattr(client, 'api') and hasattr(client.api, 'call_action'):
                try:
                    resp = await client.api.call_action(action, **kwargs)
                    return _unwrap_onebot_response(resp)
                except Exception:
                    raise e
            raise

    # Fallback 到 client.api.call_action
    if hasattr(client, 'api') and hasattr(client.api, 'call_action'):
        resp = await client.api.call_action(action, **kwargs)
        return _unwrap_onebot_response(resp)

    raise AttributeError(f"OneBot client 不支持 call_action 方法。Client type: {type(client).__name__}")


class LikeRecordManager:
    """点赞记录管理器 - 用于记录今天已点赞的用户"""
    
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.record_file = os.path.join(data_dir, "like_records.json")
        self.records: Dict[str, Dict] = {}  # {qq_id: {"date": "2026-08-11", "count": 50, "timestamp": 1234567890}}
        self._load_records()
    
    def _load_records(self):
        """从文件加载点赞记录"""
        try:
            if os.path.exists(self.record_file):
                with open(self.record_file, 'r', encoding='utf-8') as f:
                    self.records = json.load(f)
                # 清理过期记录
                self._cleanup_old_records()
        except Exception as e:
            logger.warning(f"[LikeTool] 加载点赞记录失败: {e}")
            self.records = {}
    
    def _save_records(self):
        """保存点赞记录到文件"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.record_file, 'w', encoding='utf-8') as f:
                json.dump(self.records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[LikeTool] 保存点赞记录失败: {e}")
    
    def _cleanup_old_records(self):
        """清理非今天的记录"""
        today = date.today().isoformat()
        old_keys = [k for k, v in self.records.items() if v.get("date") != today]
        for k in old_keys:
            del self.records[k]
        if old_keys:
            self._save_records()
    
    def has_liked_today(self, qq_id: str) -> bool:
        """检查今天是否已经点赞过该用户"""
        self._cleanup_old_records()
        record = self.records.get(str(qq_id))
        if record:
            return record.get("date") == date.today().isoformat()
        return False
    
    def record_like(self, qq_id: str, count: int = 50):
        """记录点赞"""
        today = date.today().isoformat()
        self.records[str(qq_id)] = {
            "date": today,
            "count": count,
            "timestamp": int(time.time())
        }
        self._save_records()
    
    def get_today_liked_count(self) -> int:
        """获取今天已点赞的用户数量"""
        self._cleanup_old_records()
        return len(self.records)


class SendLikeTool(FunctionTool):
    """
    给指定QQ用户点赞工具
    
    特性：
    - 每次点赞10个（调用1次API）
    - 今天已经点赞过的用户不能再点赞
    - 使用 Napcat 的 send_like API
    - 注意：Napcat限制间隔小于3秒会被吞赞
    """
    
    def __init__(self, record_manager: LikeRecordManager):
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
            }
        )
        self.record_manager = record_manager
    
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        qq_id = kwargs.get("qq_id", "").strip()
        
        if not qq_id:
            return "请提供目标用户的QQ号。"
        
        # 验证QQ号格式
        if not qq_id.isdigit():
            return f"QQ号格式错误: {qq_id}，请输入纯数字QQ号。"
        
        event = context.context.event
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return "当前平台不支持点赞功能 (仅支持 OneBot/Aiocqhttp)。"
        
        client = event.bot
        
        # 获取 self_id 用于反向 WS 模式下的路由
        routing_params = {}
        if hasattr(event, 'message_obj') and getattr(event.message_obj, 'self_id', None):
            routing_params['self_id'] = event.message_obj.self_id
        
        # 检查今天是否已经点赞过
        if self.record_manager and self.record_manager.has_liked_today(qq_id):
            return f"今天已经给 {qq_id} 点过赞了，明天再来吧~"
        
        try:
            # Napcat 的 send_like API
            # 根据 Napcat 文档，times=10 是合法的
            # Napcat 限制每天每个好友最多10次点赞
            # 注意：间隔小于3秒会被吞赞
            like_times = 10
            
            # 调用 send_like API，只点1次（10个赞）
            success_count = 0
            last_error = None
            for i in range(1):
                try:
                    logger.debug(f"[LikeTool] 第{i+1}次点赞调用: user_id={qq_id}, times={like_times}")
                    resp = await call_onebot(client, "send_like", user_id=int(qq_id), times=like_times, **routing_params)
                    
                    logger.debug(f"[LikeTool] 第{i+1}次点赞响应: {resp}")
                    
                    # 检查响应 - Napcat 返回格式处理
                    # 成功时可能返回: {"status": "ok"} 或空/None
                    # 失败时可能返回: {"status": "failed", "retcode": xxx, "message": "..."}
                    is_success = True
                    if isinstance(resp, dict):
                        status = resp.get("status")
                        retcode = resp.get("retcode")
                        # status 为 ok 或 retcode 为 0/None 表示成功
                        if status == "failed" or (retcode is not None and retcode != 0):
                            is_success = False
                            last_error = resp.get("message") or resp.get("wording") or resp.get("msg") or f"retcode={retcode}"
                            logger.warning(f"[LikeTool] 第{i+1}次点赞失败: {last_error}")
                            break
                    
                    if is_success:
                        success_count += 1
                    
                    # 每次点赞之间延迟3.5秒，避免被吞赞（Napcat文档说小于3秒会被吞赞）
                    if i < 4:  # 最后一次不需要延迟
                        await asyncio.sleep(3.5)
                        
                except Exception as e:
                    # 某次调用失败，可能是达到上限
                    last_error = str(e)
                    logger.warning(f"[LikeTool] 第{i+1}次点赞失败: {e}")
                    break
            
            # 计算总点赞数（每次10个赞，只点1次）
            total_likes = success_count * like_times
            
            # 记录点赞
            if self.record_manager and total_likes > 0:
                self.record_manager.record_like(qq_id, total_likes)
            
            # 获取今天已点赞数量
            today_count = 0
            if self.record_manager:
                today_count = self.record_manager.get_today_liked_count()
            
            if total_likes > 0:
                return f"已成功给 {qq_id} 点了 {total_likes} 个赞！今天已累计给 {today_count} 位用户点赞。"
            else:
                error_detail = f" ({last_error})" if last_error else ""
                if "点赞数无效" in str(last_error):
                    return f"点赞失败: 无法给 {qq_id} 点赞。可能原因: 1.机器人从未见过该用户（需要在同一群聊中互动过） 2.该用户是陌生人（从未有过任何交互） 3.已达到今日点赞上限。建议: 先让该用户在群里发一条消息，然后再尝试点赞。"
                else:
                    return f"点赞失败{error_detail}: 无法给 {qq_id} 点赞，可能已达到今日点赞上限或对方不是好友。"
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[LikeTool] 点赞失败: {error_msg}")
            
            # 常见错误处理
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
        
        # 初始化数据目录和记录管理器
        try:
            data_dir = str(StarTools.get_data_dir("astrbot_plugin_qq_like_tools"))
            self.record_manager = LikeRecordManager(data_dir)
            logger.info(f"[QQLikeTools] 数据目录: {data_dir}")
            logger.info(f"[QQLikeTools] 记录文件: {self.record_manager.record_file}")
            logger.info(f"[QQLikeTools] 当前记录: {self.record_manager.records}")
        except Exception as e:
            logger.error(f"[QQLikeTools] 初始化记录管理器失败: {e}")
            self.record_manager = None
        
        # 注册 LLM 工具
        self.context.add_llm_tools(SendLikeTool(self.record_manager))
        logger.info("[QQLikeTools] 点赞工具已注册")
    
    async def terminate(self):
        """插件卸载时清理"""
        # 卸载 LLM 工具
        self.context.unregister_llm_tool("send_like")
        logger.info("[QQLikeTools] 点赞工具已卸载")
