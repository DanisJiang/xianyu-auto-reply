#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第三方打码平台客户端
支持 2captcha 和 capsolver 两个 provider，用于滑块验证码的 fallback 解题。
"""

import os
import time
import json
import base64
import httpx
from loguru import logger
from typing import Optional, Dict, Any


class CaptchaSolver:
    """通用打码平台客户端"""

    # 各 provider 的 API 地址
    PROVIDER_URLS = {
        "2captcha": {
            "create_task": "https://api.2captcha.com/createTask",
            "get_result": "https://api.2captcha.com/getTaskResult",
            "get_balance": "https://api.2captcha.com/getBalance",
        },
        "capsolver": {
            "create_task": "https://api.capsolver.com/createTask",
            "get_result": "https://api.capsolver.com/getTaskResult",
            "get_balance": "https://api.capsolver.com/getBalance",
        },
    }

    def __init__(self, provider: str = None, api_key: str = None, timeout: int = None):
        """
        Args:
            provider: "2captcha" | "capsolver"，默认从环境变量/配置读取
            api_key: API 密钥，默认从环境变量/配置读取
            timeout: 等待解题超时秒数，默认 120
        """
        # 从环境变量或配置文件读取
        self.provider = provider or self._get_config_value("provider", "CAPTCHA_PROVIDER", "")
        self.api_key = api_key or self._get_config_value("api_key", "CAPTCHA_API_KEY", "")
        self.timeout = timeout or int(self._get_config_value("timeout", "CAPTCHA_TIMEOUT", "120"))

        # 轮询间隔（秒）
        self._poll_interval = 5

    @staticmethod
    def _get_config_value(config_key: str, env_key: str, default: str) -> str:
        """优先读环境变量，其次读 global_config.yml 中的配置"""
        env_val = os.environ.get(env_key)
        if env_val:
            return env_val
        try:
            from config import SLIDER_VERIFICATION
            captcha_cfg = SLIDER_VERIFICATION.get("captcha_service", {})
            val = captcha_cfg.get(config_key, default)
            return str(val) if val else default
        except Exception:
            return default

    @property
    def is_configured(self) -> bool:
        """检查是否已正确配置打码服务"""
        return bool(self.provider and self.api_key and self.provider in self.PROVIDER_URLS)

    def get_balance(self) -> Optional[float]:
        """查询账户余额"""
        if not self.is_configured:
            return None
        urls = self.PROVIDER_URLS[self.provider]
        try:
            payload = {"clientKey": self.api_key}
            resp = httpx.post(urls["get_balance"], json=payload, timeout=15)
            data = resp.json()
            if data.get("errorId", 1) == 0:
                return data.get("balance", 0.0)
            logger.warning(f"查询余额失败: {data}")
            return None
        except Exception as e:
            logger.error(f"查询打码平台余额出错: {e}")
            return None

    def solve_slider(self, screenshot_b64: str, comment: str = None) -> Optional[Dict[str, Any]]:
        """
        发送截图到打码平台，获取滑块目标位置。

        Args:
            screenshot_b64: 验证码区域截图的 base64 字符串
            comment: 给人工标注的提示语

        Returns:
            {"x": pixel_offset, "y": pixel_y} 或 None
        """
        if not self.is_configured:
            logger.debug("打码服务未配置，跳过")
            return None

        logger.info(f"正在使用第三方打码服务 ({self.provider}) 解题...")

        task_id = self._create_task(screenshot_b64, comment)
        if not task_id:
            return None

        result = self._poll_result(task_id)
        return result

    def _create_task(self, screenshot_b64: str, comment: str = None) -> Optional[str]:
        """创建解题任务"""
        urls = self.PROVIDER_URLS[self.provider]

        if self.provider == "2captcha":
            payload = {
                "clientKey": self.api_key,
                "task": {
                    "type": "CoordinatesTask",
                    "body": screenshot_b64,
                    "comment": comment or "请点击滑块应该停留的位置 (Click where the slider should stop)",
                },
            }
        elif self.provider == "capsolver":
            payload = {
                "clientKey": self.api_key,
                "task": {
                    "type": "ImageToCoordinatesTask",
                    "body": screenshot_b64,
                    "comment": comment or "Click where the slider should stop",
                },
            }
        else:
            logger.error(f"不支持的打码平台: {self.provider}")
            return None

        try:
            resp = httpx.post(urls["create_task"], json=payload, timeout=30)
            data = resp.json()

            if data.get("errorId", 1) != 0:
                error_desc = data.get("errorDescription", data.get("errorCode", "未知错误"))
                logger.error(f"创建打码任务失败: {error_desc}")
                return None

            task_id = data.get("taskId")
            if task_id:
                logger.info(f"打码任务已创建: taskId={task_id}")
            return task_id

        except Exception as e:
            logger.error(f"创建打码任务出错: {e}")
            return None

    def _poll_result(self, task_id: str) -> Optional[Dict[str, Any]]:
        """轮询等待解题结果"""
        urls = self.PROVIDER_URLS[self.provider]
        payload = {
            "clientKey": self.api_key,
            "taskId": task_id,
        }

        start_time = time.time()
        while time.time() - start_time < self.timeout:
            try:
                time.sleep(self._poll_interval)
                resp = httpx.post(urls["get_result"], json=payload, timeout=15)
                data = resp.json()

                if data.get("errorId", 1) != 0:
                    error_desc = data.get("errorDescription", data.get("errorCode", "未知错误"))
                    logger.error(f"打码任务出错: {error_desc}")
                    return None

                status = data.get("status")
                if status == "ready":
                    solution = data.get("solution", {})
                    result = self._parse_solution(solution)
                    if result:
                        logger.info(f"打码平台返回结果: x={result.get('x')}")
                    return result
                elif status == "processing":
                    elapsed = int(time.time() - start_time)
                    logger.debug(f"打码任务处理中... ({elapsed}s/{self.timeout}s)")
                    continue
                else:
                    logger.warning(f"打码任务未知状态: {status}")

            except Exception as e:
                logger.error(f"轮询打码结果出错: {e}")
                return None

        logger.warning(f"打码任务超时 ({self.timeout}秒)")
        return None

    def _parse_solution(self, solution: dict) -> Optional[Dict[str, Any]]:
        """解析不同 provider 的返回结果为统一格式 {"x": ..., "y": ...}"""
        try:
            # 2captcha CoordinatesTask 返回 {"coordinates": [{"x": 123, "y": 45}]}
            coordinates = solution.get("coordinates")
            if coordinates and isinstance(coordinates, list) and len(coordinates) > 0:
                coord = coordinates[0]
                return {"x": coord.get("x", 0), "y": coord.get("y", 0)}

            # capsolver 可能直接返回 {"x": ..., "y": ...}
            if "x" in solution:
                return {"x": solution["x"], "y": solution.get("y", 0)}

            # 其他格式：尝试从 slideDistance / distance 等字段获取
            for key in ("slideDistance", "distance", "slide_distance"):
                if key in solution:
                    return {"x": solution[key], "y": 0}

            logger.warning(f"无法解析打码结果: {solution}")
            return None

        except Exception as e:
            logger.error(f"解析打码结果出错: {e}")
            return None
