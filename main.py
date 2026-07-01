import os
import uuid
import time
import re
import traceback
import json
import shutil
import asyncio
import copy
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import *
from astrbot.api import llm_tool, logger
from astrbot.api.provider import LLMResponse
from astrbot.core.message.message_event_result import MessageChain
# 尝试导入 StarTools（兼容不同版本）
try:
    from astrbot.api.star import StarTools
    HAS_STAR_TOOLS = True
except ImportError:
    HAS_STAR_TOOLS = False
    logger.warning("[ComfyUI] 无法导入 StarTools，将使用备用目录方案")

# 获取插件目录（用于读取默认文件）
PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
class _ComfyImageMarker:
    """多图模式的图片占位标记，存储 prompt 信息，在 chain 中占位"""
    def __init__(self, prompt: str, index: int, lora_selections=None):
        self.prompt = prompt
        self.index = index
        self.lora_selections = lora_selections or []

@register(
    "astrbot_plugin_comfyui_pro",  
    "lumingya",                    
    "ComfyUI Pro 连接器",           
    "1.2.0",
    "https://github.com/lumingya/astrbot_plugin_comfyui_pro" 
)
class ComfyUIPlugin(Star):
    _FORCE_DRAW_CONTEXT_LIMIT = 12
    _FORCE_DRAW_MIN_REPLY_LENGTH = 100
    _DRAW_FAILURE_CONTEXT_TTL_SECONDS = 600
    _DRAW_FAILURE_CONTEXT_LIMIT = 5
    _PIC_PROMPT_TAG_PATTERN = re.compile(
        r'<pic\b[^>]*\bprompt=(?P<quote>["\'])(?P<prompt>.*?)(?P=quote)[^>]*?/?>\s*(?:</pic>)?',
        flags=re.DOTALL | re.IGNORECASE,
    )
    _PIC_PROMPT_UNCLOSED_PATTERN = re.compile(
        r'<pic\b(?P<prefix>[^>]*\bprompt=)(?P<quote>["\'])(?P<prompt>.*?)</pic>',
        flags=re.DOTALL | re.IGNORECASE,
    )

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # ====== 1. 获取持久化数据目录 ======
        self.data_dir = self._get_persistent_dir()
        logger.info(f"[ComfyUI] 📂 数据目录: {self.data_dir}")
        
        # ====== 2. 初始化目录结构 ======
        self._init_data_directories()
        
        # ====== 3. 设置路径变量 ======
        self.workflow_dir = self.data_dir / "workflow"
        self.output_dir = self.data_dir / "output"
        self.sensitive_words_path = self.data_dir / "sensitive_words.json"
        
        # ====== 4. 更新 UI 配置 ======
        self._auto_update_schema()
        
        # Control 配置
        control_conf = config.get("control", {})
        self.cooldown_seconds = control_conf.get("cooldown_seconds", 60)
        self.user_cooldowns = {}
        self._recent_draw_failures = {}
        self.admin_user_ids = set(map(str, control_conf.get("admin_ids", [])))
        self.lockdown = bool(control_conf.get("lockdown", False))
        self.lockdown_command_enabled = bool(control_conf.get("lockdown_command_enabled", True))
        self.whitelist_group_ids = set(map(str, control_conf.get("whitelist_group_ids", [])))
    
        llm_settings = config.get("llm_settings", {})
        self.multi_image_mode = llm_settings.get("multi_image_mode", False)
        logger.info(f"[ComfyUI] 🖼️ 多图模式: {'开启' if self.multi_image_mode else '关闭'}")
        
        self.discard_prompt_from_history = llm_settings.get("discard_prompt_from_history", False)
        self.force_draw_when_no_prompt = bool(llm_settings.get("force_draw_when_no_prompt", False))
        self.target_image_count = self._coerce_target_image_count(
            llm_settings.get("target_image_count", 1),
        )
        self.lora_control_conf = llm_settings.get("lora_control", {}) or {}
        self.lora_control_enabled = bool(self.lora_control_conf.get("enabled", False))
        if self.discard_prompt_from_history:
            logger.info("[ComfyUI] 🗑️ 绘图提示词历史丢弃: 开启")
        if self.force_draw_when_no_prompt:
            logger.info(
                f"[ComfyUI] 🎯 强制画图兜底: 开启（回复正文不少于 {self._FORCE_DRAW_MIN_REPLY_LENGTH} 字时生效）",
            )
        if self.target_image_count > 1:
            logger.info(f"[ComfyUI] 🖼️ 目标出图数量: {self.target_image_count}")
        # 策略配置
        self.default_group_policy = str(control_conf.get("default_group_policy", "none")).lower()
        self.default_private_policy = str(control_conf.get("default_private_policy", "none")).lower()
        self.group_policies = {
            str(k): str(v).lower()
            for k, v in control_conf.get("group_policies", {}).items()
        }
        self.policies = {
            "none": set(),
            "lite": {"legacy_lite"},
            "full": {"legacy_lite", "minors", "sexual_violence", "bestiality_incest_necrophilia", "violence_gore", "scat_urine_vomit", "self_harm", "sexual", "nudity", "fetish"},
        }

        # 管理员绕过配置
        bypass = control_conf.get("admin_bypass", {})
        self.admin_bypass_whitelist = bypass.get("whitelist", True)
        self.admin_bypass_cooldown = bypass.get("cooldown", True)
        self.admin_bypass_sensitive = bypass.get("sensitive_words", True)

        # 日志：显示管理员和白名单配置
        admin_count = len(self.admin_user_ids)
        group_count = len(self.whitelist_group_ids)
        logger.info(f"[ComfyUI] 👤 超级管理员: {admin_count} 个 | 🏠 白名单群: {group_count} 个")
        if self.lockdown:
            logger.warning("[ComfyUI]⚠️ 绘图功能全局锁定已启用，仅超级管理员可用")
        logger.info(f"[ComfyUI] 🔐 锁定命令开关: {'开启' if self.lockdown_command_enabled else '关闭'}")

        # 加载敏感词
        self.lexicon = {}
        try:
            if self.sensitive_words_path.exists():
                with open(self.sensitive_words_path, "r", encoding="utf-8") as f:
                    self.lexicon = json.load(f)
                word_count = sum(len(v) for v in self.lexicon.values() if isinstance(v, list))
                logger.info(f"[ComfyUI] 🔒 敏感词库已加载: {word_count} 个词条")
            else:
                self.lexicon = {"legacy_lite": [], "full": []} 
        except Exception:
            self.lexicon = {"legacy_lite": [], "full": []}

        self._policy_patterns = {}
        self._build_policy_patterns()
        
        # 初始化 ComfyUI API
        self.comfy_ui = None
        self.api = None
        try:
            from .comfyui_api import ComfyUI
            self.api = ComfyUI(self.config, data_dir=self.data_dir)
            logger.info(f"[ComfyUI] ✅ ComfyUI API 初始化成功")
        except Exception as e:
            logger.error(f"[ComfyUI] ❌ ComfyUI API 初始化失败: {e}")
            logger.error(traceback.format_exc())

    # ====== 获取持久化目录 ======
    def _get_persistent_dir(self) -> Path:
        """获取插件的持久化数据目录"""
        data_path = None
        
        if HAS_STAR_TOOLS:
            try:
                data_path = StarTools.get_data_dir(self)
            except Exception:
                try:
                    data_path = StarTools.get_data_dir()
                except Exception:
                    try:
                        data_path = StarTools.get_data_dir(self.context)
                    except Exception:
                        pass
        
        if data_path is None:
            current = Path.cwd()
            data_path = current / "data" / "plugin_data" / "astrbot_plugin_comfyui_pro"
        
        if not isinstance(data_path, Path):
            data_path = Path(data_path)
        
        data_path.mkdir(parents=True, exist_ok=True)
        return data_path

    # ====== 初始化目录结构 ======
    def _init_data_directories(self):
        """初始化持久化目录，首次安装时复制默认文件"""
        workflow_dir = self.data_dir / "workflow"
        output_dir = self.data_dir / "output"
        
        workflow_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)
        
        # 复制默认工作流
        plugin_workflow_dir = PLUGIN_DIR / "workflow"
        copied_count = 0
        if plugin_workflow_dir.exists():
            for src_file in plugin_workflow_dir.glob("*.json"):
                dst_file = workflow_dir / src_file.name
                if not dst_file.exists():
                    try:
                        shutil.copy2(src_file, dst_file)
                        copied_count += 1
                    except Exception as e:
                        logger.error(f"[ComfyUI] 复制工作流失败 {src_file.name}: {e}")
        
        if copied_count > 0:
            logger.info(f"[ComfyUI] 📋 已复制 {copied_count} 个默认工作流")
        
        # 复制默认敏感词文件
        sensitive_dst = self.data_dir / "sensitive_words.json"
        sensitive_src = PLUGIN_DIR / "sensitive_words.json"
        if not sensitive_dst.exists() and sensitive_src.exists():
            try:
                shutil.copy2(sensitive_src, sensitive_dst)
                logger.info(f"[ComfyUI] 📋 已复制默认敏感词文件")
            except Exception as e:
                logger.error(f"[ComfyUI] 复制敏感词文件失败: {e}")

    # ====== 更新 Schema ======
    def _auto_update_schema(self):
        """扫描持久化目录的工作流，更新 UI 下拉列表"""
        try:
            schema_path = PLUGIN_DIR / '_conf_schema.json'
            workflow_dir = self.data_dir / 'workflow'

            if not workflow_dir.exists():
                return

            # 排除 .steps.json 文件
            files = sorted([
                f.name for f in workflow_dir.glob("*.json")
                if not self._is_workflow_aux_file(f.name)
            ])
        
            if not files:
                files = ["workflow_api.json"]

            with open(schema_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            target = data['workflow_settings']['items']['json_file']
            target['options'] = files
            target['enum'] = files
        
            with open(schema_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        
            logger.info(f"[ComfyUI] 🔄 工作流列表已更新: {len(files)} 个可用")

        except Exception as e:
            logger.error(f"[ComfyUI] 更新工作流列表失败: {e}")

    # ====== 权限检查（返回原因）======
    def _check_access(self, event: AstrMessageEvent) -> tuple:
        """
        统一的权限检查，返回 (是否通过, 拒绝原因)
        """
        user_id = str(event.get_sender_id())
        is_admin = user_id in self.admin_user_ids
        
        # 1. 全局锁定检查
        if self.lockdown and not is_admin:
            return False, "🔒 绘图失败:绘图功能锁定中，仅超级管理员可用"
        
        # 2. 群聊白名单检查
        if self._is_group_message(event):
            gid = self._get_group_id(event)
            if not gid:
                return False, "⚠️ 无法获取群号"
            
            # 检查白名单
            if gid not in self.whitelist_group_ids:
                # 管理员可以绕过
                if is_admin and self.admin_bypass_whitelist:
                    pass  # 放行
                else:
                    return False, f"🚫 本群({gid})不在白名单中"
        
        return True, ""

    def _check_cooldown(self, event: AstrMessageEvent) -> tuple:
        """
        冷却检查，返回 (是否通过, 剩余秒数或0)
        """
        user_id = str(event.get_sender_id())
        is_admin = user_id in self.admin_user_ids
        
        # 管理员绕过冷却
        if is_admin and self.admin_bypass_cooldown:
            return True, 0
        
        current_time = time.time()
        last_time = self.user_cooldowns.get(user_id, 0)
        elapsed = current_time - last_time

        if elapsed < self.cooldown_seconds:
            remain = int(self.cooldown_seconds - elapsed)
            return False, remain

        self.user_cooldowns[user_id] = current_time
        return True, 0

    def _check_sensitive(self, prompt: str, event: AstrMessageEvent) -> tuple:
        """
        敏感词检查，返回 (是否通过, 触发的敏感词列表)
        """
        user_id = str(event.get_sender_id())
        is_admin = user_id in self.admin_user_ids
        
        sensitive = self._find_sensitive_words(prompt, event)
        
        if not sensitive:
            return True, []
        
        # 管理员绕过
        if is_admin and self.admin_bypass_sensitive:
            logger.info(f"[ComfyUI] 👑 管理员 {user_id} 使用敏感词 {sensitive}，已放行")
            return True, []
        
        return False, sensitive

    def _compact_draw_prompt(self, prompt, limit: int = 240) -> str:
        if prompt is None:
            return ""
        if isinstance(prompt, (list, tuple)):
            prompt = " | ".join(str(item) for item in prompt if item is not None)
        text = re.sub(r"\s+", " ", str(prompt)).strip()
        if not text:
            return ""
        return text[:limit] + ("..." if len(text) > limit else "")

    def _extract_command_prompt(self, event: AstrMessageEvent) -> str:
        full_message = (getattr(event, "message_str", "") or "").strip()
        parts = full_message.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _remember_draw_failure(
        self,
        event: AstrMessageEvent,
        reason: str,
        *,
        prompt: str = "",
        source: str = "绘图",
        append: bool = False,
    ) -> None:
        origin = getattr(event, "unified_msg_origin", None)
        if not origin:
            return

        reason_text = re.sub(r"\s+", " ", str(reason or "")).strip()
        if not reason_text:
            return

        now = time.time()
        record = {
            "ts": now,
            "reason": reason_text,
            "prompt": self._compact_draw_prompt(prompt),
            "source": str(source or "绘图"),
            "user_id": str(event.get_sender_id()),
        }

        records = self._recent_draw_failures.get(origin, []) if append else []
        records = [
            item
            for item in records
            if now - float(item.get("ts", 0)) <= self._DRAW_FAILURE_CONTEXT_TTL_SECONDS
        ]
        records.append(record)
        self._recent_draw_failures[origin] = records[-self._DRAW_FAILURE_CONTEXT_LIMIT :]

        try:
            event.set_extra("comfy_last_draw_failure", record)
        except Exception:
            pass

    def _clear_draw_failures(self, event: AstrMessageEvent) -> None:
        origin = getattr(event, "unified_msg_origin", None)
        if origin:
            self._recent_draw_failures.pop(origin, None)

    def _build_draw_failure_context(self, event: AstrMessageEvent) -> str:
        origin = getattr(event, "unified_msg_origin", None)
        if not origin:
            return ""

        now = time.time()
        records = [
            item
            for item in self._recent_draw_failures.get(origin, [])
            if now - float(item.get("ts", 0)) <= self._DRAW_FAILURE_CONTEXT_TTL_SECONDS
        ]
        if not records:
            self._recent_draw_failures.pop(origin, None)
            return ""

        self._recent_draw_failures[origin] = records[-self._DRAW_FAILURE_CONTEXT_LIMIT :]
        lines = [
            "<comfyui_draw_status>",
            "这是 ComfyUI 插件注入的内部状态，不是用户发言。",
            "最近一次或几次绘图没有生成图片；如果用户追问刚才的画图结果，请明确说明失败，并引用下面的原因。",
        ]

        for idx, item in enumerate(records[-self._DRAW_FAILURE_CONTEXT_LIMIT :], 1):
            ts = time.strftime("%H:%M:%S", time.localtime(float(item.get("ts", now))))
            line = (
                f"{idx}. 时间: {ts}; 来源: {item.get('source', '绘图')}; "
                f"用户ID: {item.get('user_id', '')}; 失败原因: {item.get('reason', '')}"
            )
            if item.get("prompt"):
                line += f"; 提示词: {item.get('prompt')}"
            lines.append(line)

        lines.append("</comfyui_draw_status>")
        return "<!--EPHEMERAL-->\n" + "\n".join(lines) + "\n<!--/EPHEMERAL-->"

    def _inject_draw_failure_context(self, event: AstrMessageEvent, req) -> None:
        status_text = self._build_draw_failure_context(event)
        if not status_text:
            return

        if hasattr(req, "add_user_text"):
            try:
                req.add_user_text(
                    status_text,
                    slot="comfyui_draw_status",
                    after="system_reminder",
                )
            except Exception:
                req.add_user_text(status_text, slot="comfyui_draw_status")
        else:
            current_prompt = getattr(req, "system_prompt", "") or ""
            req.system_prompt = f"{current_prompt}\n\n{status_text}".strip()

        logger.info("[ComfyUI] 已向本轮 LLM 请求注入最近绘图失败状态")

    @filter.on_llm_request(priority=100)
    async def inject_system_prompt(self, event: AstrMessageEvent, req):
        """注入系统提示词 + 清理历史中的绘图提示词"""
        try:
            my_prompt = self._get_comfy_system_prompt()

            if my_prompt:
                current_prompt = getattr(req, "system_prompt", "") or ""
                if my_prompt not in current_prompt:
                    if current_prompt:
                        req.system_prompt = f"{current_prompt}\n\n{my_prompt}".strip()
                    else:
                        req.system_prompt = my_prompt

        except Exception as e:
            logger.error(f"[ComfyUI] 注入提示词异常: {e}")

        try:
            self._inject_draw_failure_context(event, req)
        except Exception as e:
            logger.error(f"[ComfyUI] 注入绘图失败状态异常: {e}")

        # 清理历史中的绘图提示词
        if self.discard_prompt_from_history:
            try:
                self._clean_pic_tags_from_req(req)
            except Exception as e:
                logger.error(f"[ComfyUI] 清理提示词异常: {e}")

    def _clean_pic_tags_from_req(self, req):
        """从请求的 conversation.history 中清理 <pic> 标签"""
        conversation = getattr(req, "conversation", None)
        if conversation is None:
            logger.warning("[ComfyUI] 🗑️ req.conversation 不存在，跳过清理")
            return

        history_raw = getattr(conversation, "history", None)
        if not history_raw:
            return

        try:
            history = json.loads(history_raw) if isinstance(history_raw, str) else history_raw
        except (json.JSONDecodeError, TypeError):
            return

        if not isinstance(history, list):
            return

        cleaned = 0
        for entry in history:
            if not isinstance(entry, dict):
                continue
            if entry.get("role") != "assistant":
                continue
            content = entry.get("content", "")
            if isinstance(content, str):
                stripped = self._strip_comfy_control_tags(content, remove_think=True)
                if self._find_pic_prompt_matches(content) or "<lora " in content.lower():
                    entry["content"] = stripped
                    cleaned += 1

        if cleaned:
            # 写回 conversation.history
            conversation.history = json.dumps(history, ensure_ascii=False)
            logger.info(f"[ComfyUI] 🗑️ 已从 conversation.history 中清理 {cleaned} 条消息的绘图提示词")

    @staticmethod
    def _is_workflow_aux_file(filename: str) -> bool:
        return filename.endswith(".steps.json") or filename.endswith(".lora.json")

    @staticmethod
    def _strip_comfy_control_tags(text: str, remove_think: bool = False) -> str:
        if not isinstance(text, str):
            return text

        cleaned = re.sub(r'<lora\s+picks=".*?"\s*/?>', "", text, flags=re.DOTALL | re.IGNORECASE)
        pic_matches = ComfyUIPlugin._find_pic_prompt_matches(cleaned)
        cleaned = ComfyUIPlugin._remove_text_spans(
            cleaned,
            [(item["start"], item["end"]) for item in pic_matches],
        )
        cleaned = re.sub(r"</pic>", "", cleaned, flags=re.IGNORECASE)
        if remove_think:
            cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
        cleaned = re.sub(r"</?ctx>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def _count_visible_text_length(text: str) -> int:
        if not isinstance(text, str):
            return 0
        return len(re.sub(r"\s+", "", text))

    @staticmethod
    def _coerce_target_image_count(value) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, count)

    @staticmethod
    def _normalize_prompt_signature(prompt: str) -> str:
        if not isinstance(prompt, str):
            return ""
        return re.sub(r"\s+", " ", prompt).strip().casefold()

    def _should_use_multi_image_mode(self, prompt_count: int) -> bool:
        return prompt_count > 1 and (
            self.multi_image_mode or self.target_image_count > 1
        )

    @staticmethod
    def _normalize_lora_key(value) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", "", str(value)).casefold()

    def _parse_lora_pick_string(self, raw_text: str) -> list:
        if not raw_text:
            return []

        selections = []
        clear_token = self._normalize_lora_key("!clear_defaults")
        for piece in re.split(r"[,;\n，；]+", raw_text):
            token = piece.strip().strip("`").strip()
            if not token:
                continue

            if self._normalize_lora_key(token) == clear_token:
                selections.append({"name": "!clear_defaults", "control": "clear_defaults"})
                continue

            trigger_indexes = []
            if "@" in token:
                token, trigger_part = token.rsplit("@", 1)
                for raw_index in re.split(r"[+|/、\s]+", trigger_part.strip()):
                    raw_index = raw_index.strip()
                    if not raw_index:
                        continue
                    try:
                        index = int(raw_index)
                    except (TypeError, ValueError):
                        continue
                    if index > 0:
                        trigger_indexes.append(index)

            parts = [part.strip() for part in token.rsplit(":", 2)]
            name = ""
            strength = None
            clip_strength = None

            if len(parts) == 1:
                name = parts[0]
            elif len(parts) == 2:
                name, strength = parts
            else:
                name, strength, clip_strength = parts

            if not name:
                continue

            try:
                strength = float(strength) if strength not in ("", None) else None
            except (TypeError, ValueError):
                strength = None

            try:
                clip_strength = float(clip_strength) if clip_strength not in ("", None) else None
            except (TypeError, ValueError):
                clip_strength = None

            selections.append(
                {
                    "name": name,
                    "strength": strength,
                    "clip_strength": clip_strength,
                    "trigger_indexes": trigger_indexes,
                }
            )

        deduped = []
        seen = set()
        for item in selections:
            key = self._normalize_lora_key(item.get("name") or item.get("control"))
            if not key or key in seen:
                continue
            deduped.append(item)
            seen.add(key)
        return deduped

    def _extract_lora_control_tags(self, text: str):
        if not isinstance(text, str) or not text:
            return text, []

        collected = []

        def repl(match):
            collected.extend(self._parse_lora_pick_string(match.group(1)))
            return ""

        cleaned = re.sub(
            r'<lora\s+picks="(.*?)"\s*/?>',
            repl,
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned, collected

    @classmethod
    def _find_pic_prompt_matches(cls, text: str) -> list[dict]:
        if not isinstance(text, str) or not text:
            return []

        matches = []
        occupied = []

        for match in cls._PIC_PROMPT_TAG_PATTERN.finditer(text):
            start, end = match.span()
            matches.append(
                {
                    "start": start,
                    "end": end,
                    "prompt": match.group("prompt") or "",
                }
            )
            occupied.append((start, end))

        for match in cls._PIC_PROMPT_UNCLOSED_PATTERN.finditer(text):
            start, end = match.span()
            if any(not (end <= s or start >= e) for s, e in occupied):
                continue
            matches.append(
                {
                    "start": start,
                    "end": end,
                    "prompt": match.group("prompt") or "",
                }
            )

        matches.sort(key=lambda item: item["start"])
        return matches

    @staticmethod
    def _remove_text_spans(text: str, spans: list[tuple[int, int]]) -> str:
        if not isinstance(text, str) or not spans:
            return text

        parts = []
        cursor = 0
        for start, end in spans:
            parts.append(text[cursor:start])
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts)

    def _extract_prompt_lora_pairs(self, full_text: str) -> list:
        token_pattern = re.compile(
            r'<lora\s+picks="(.*?)"\s*/?>',
            flags=re.DOTALL | re.IGNORECASE,
        )

        tokens = []
        for match in token_pattern.finditer(full_text or ""):
            tokens.append(
                {
                    "type": "lora",
                    "start": match.start(),
                    "lora_raw": match.group(1) or "",
                }
            )
        for match in self._find_pic_prompt_matches(full_text or ""):
            tokens.append(
                {
                    "type": "pic",
                    "start": match["start"],
                    "prompt_raw": match["prompt"],
                }
            )
        tokens.sort(key=lambda item: item["start"])

        pairs = []
        pending_loras = []
        for token in tokens:
            if token["type"] == "lora":
                pending_loras.extend(self._parse_lora_pick_string(token["lora_raw"]))
                continue

            pairs.append(
                {
                    "prompt": token["prompt_raw"] or "",
                    "lora_selections": pending_loras,
                }
            )
            pending_loras = []

        return pairs

    def _format_lora_trigger_preview(self, entry: dict, limit: int = 3, preview_length: int = 60) -> str:
        options = list(entry.get("trigger_options", []) or [])[:limit]
        if not options:
            if entry.get("is_style_lora"):
                return "无触发词（更偏全局风格）"
            return "无触发词"

        rendered = []
        for idx, option in enumerate(options, start=1):
            text = re.sub(r"\s+", " ", str(option or "").strip()).strip(",; ")
            if len(text) > preview_length:
                text = text[: max(0, preview_length - 1)].rstrip() + "…"
            rendered.append(f"[{idx}] {text}")
        return " | ".join(rendered)

    def _get_comfy_system_prompt(self) -> str:
        llm_settings = self.config.get("llm_settings", {}) or {}
        my_prompt = (llm_settings.get("system_prompt", "") or "").strip()
        if not my_prompt:
            return ""

        if self.lora_control_enabled and getattr(self, "api", None):
            try:
                lora_appendix = self.api.get_lora_prompt_appendix()
            except Exception as e:
                logger.warning(f"[ComfyUI] 获取 LoRA prompt 附录失败: {e}")
                lora_appendix = ""
            if lora_appendix:
                my_prompt = f"{my_prompt}\n\n{lora_appendix}".strip()
        return my_prompt

    @staticmethod
    def _coerce_conversation_history(raw_history) -> list:
        if raw_history is None:
            return []

        if isinstance(raw_history, str):
            try:
                raw_history = json.loads(raw_history)
            except (json.JSONDecodeError, TypeError):
                return []

        if not isinstance(raw_history, list):
            return []

        return [copy.deepcopy(item) for item in raw_history if isinstance(item, dict)]

    def _sanitize_context_content_for_prompt(self, content):
        if isinstance(content, str):
            return self._strip_comfy_control_tags(content, remove_think=True)

        if not isinstance(content, list):
            return content

        sanitized_parts = []
        for part in content:
            if isinstance(part, str):
                cleaned_part = self._strip_comfy_control_tags(part, remove_think=True)
                if cleaned_part:
                    sanitized_parts.append(cleaned_part)
                continue

            if not isinstance(part, dict):
                sanitized_parts.append(copy.deepcopy(part))
                continue

            item = copy.deepcopy(part)
            item_type = str(item.get("type", "") or "").strip().lower()
            if item_type == "think":
                continue

            if item_type == "text":
                if "text" in item:
                    item["text"] = self._strip_comfy_control_tags(
                        str(item.get("text", "") or ""),
                        remove_think=True,
                    )
                    if not item["text"]:
                        continue
                elif isinstance(item.get("data"), dict):
                    item["data"] = copy.deepcopy(item["data"])
                    item["data"]["text"] = self._strip_comfy_control_tags(
                        str(item["data"].get("text", "") or ""),
                        remove_think=True,
                    )
                    if not item["data"]["text"]:
                        continue

            sanitized_parts.append(item)

        return sanitized_parts

    def _build_force_draw_contexts(
        self,
        conversation,
        latest_reply_text: str,
    ) -> list[dict]:
        history = self._coerce_conversation_history(
            getattr(conversation, "history", None),
        )
        sanitized_contexts = []

        for entry in history[-self._FORCE_DRAW_CONTEXT_LIMIT :]:
            message = copy.deepcopy(entry)
            message["content"] = self._sanitize_context_content_for_prompt(
                message.get("content"),
            )
            content = message.get("content")
            if content in ("", None, []):
                if not message.get("tool_calls") and not message.get("tool_call_id"):
                    continue
            sanitized_contexts.append(message)

        latest_reply_text = self._strip_comfy_control_tags(
            latest_reply_text,
            remove_think=True,
        )
        if latest_reply_text:
            should_append = True
            if sanitized_contexts:
                last_message = sanitized_contexts[-1]
                if (
                    last_message.get("role") == "assistant"
                    and isinstance(last_message.get("content"), str)
                    and last_message.get("content", "").strip() == latest_reply_text
                ):
                    should_append = False
            if should_append:
                sanitized_contexts.append(
                    {"role": "assistant", "content": latest_reply_text},
                )

        return sanitized_contexts[-self._FORCE_DRAW_CONTEXT_LIMIT :]

    def _extract_usable_prompt_entries(self, full_text: str) -> list[dict]:
        prompt_entries = self._extract_prompt_lora_pairs(full_text)
        if not prompt_entries:
            return []

        placeholder_patterns = [
            r"^\.{2,}$",
            r"^…+$",
            r"^[.。]+$",
            r"^[xX]{2,}$",
            r"^[-_=]{2,}$",
            r"^\[.*?\]$",
            r"^\{.*?\}$",
        ]

        cleaned_entries = []
        for entry in prompt_entries:
            prompt_text = entry.get("prompt", "")
            prompt_text = re.sub(r"^提示词是\s*[:：]?\s*", "", prompt_text).strip()
            prompt_text = prompt_text.strip('`"\'""''').strip()
            prompt_text, inline_loras = self._extract_lora_control_tags(prompt_text)
            if not prompt_text:
                continue
            if len(prompt_text) < 3:
                logger.debug(f"[ComfyUI] 跳过过短提示词: '{prompt_text}'")
                continue
            if any(re.match(pattern, prompt_text) for pattern in placeholder_patterns):
                logger.debug(f"[ComfyUI] 跳过占位符提示词: '{prompt_text}'")
                continue

            lora_selections = []
            seen_loras = set()
            for item in (entry.get("lora_selections") or []) + inline_loras:
                key = self._normalize_lora_key(item.get("name"))
                if not key or key in seen_loras:
                    continue
                lora_selections.append(item)
                seen_loras.add(key)

            cleaned_entries.append(
                {
                    "prompt": prompt_text,
                    "lora_selections": lora_selections,
                }
            )

        return cleaned_entries

    async def _generate_force_draw_prompt_entries(
        self,
        event: AstrMessageEvent,
        latest_reply_text: str,
        current_entries: list[dict] | None = None,
    ) -> list[dict]:
        if not self.force_draw_when_no_prompt:
            return []

        current_entries = list(current_entries or [])
        current_count = len(current_entries)
        missing_count = max(0, self.target_image_count - current_count)
        needs_fill_when_no_prompt = current_count == 0 and missing_count > 0
        needs_fill_to_target_count = missing_count > 0 and self.target_image_count > 1
        if not needs_fill_when_no_prompt and not needs_fill_to_target_count:
            return []

        latest_reply_text = self._strip_comfy_control_tags(
            latest_reply_text,
            remove_think=True,
        )
        reply_length = self._count_visible_text_length(latest_reply_text)
        if reply_length < self._FORCE_DRAW_MIN_REPLY_LENGTH:
            logger.info(
                "[ComfyUI] 跳过强制画图兜底：当前回复长度为 %s，小于阈值 %s",
                reply_length,
                self._FORCE_DRAW_MIN_REPLY_LENGTH,
            )
            return []

        provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider is None:
            logger.warning("[ComfyUI] 强制画图兜底失败：当前没有可用的聊天模型")
            return []

        conversation = None
        try:
            conversation_id = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin,
            )
            if conversation_id:
                conversation = await self.context.conversation_manager.get_conversation(
                    event.unified_msg_origin,
                    conversation_id,
                )
        except Exception as e:
            logger.warning(f"[ComfyUI] 读取当前会话上下文失败，继续使用空上下文补提示词: {e}")

        contexts = self._build_force_draw_contexts(conversation, latest_reply_text)
        base_system_prompt = self._get_comfy_system_prompt()
        latest_reply_excerpt = re.sub(r"\s+", " ", latest_reply_text).strip()
        if len(latest_reply_excerpt) > 1200:
            latest_reply_excerpt = latest_reply_excerpt[:1199].rstrip() + "…"
        seen_prompts = {
            self._normalize_prompt_signature(item.get("prompt", ""))
            for item in current_entries
            if item.get("prompt")
        }
        supplemented_entries = []
        max_rounds = 2
        remaining_count = missing_count

        for round_idx in range(max_rounds):
            if remaining_count <= 0:
                break

            existing_entries = current_entries + supplemented_entries
            existing_prompt_summary = ""
            if existing_entries:
                rendered_existing = []
                for item in existing_entries[:8]:
                    prompt_text = re.sub(r"\s+", " ", str(item.get("prompt", "") or "")).strip()
                    if not prompt_text:
                        continue
                    if len(prompt_text) > 180:
                        prompt_text = prompt_text[:179].rstrip() + "…"
                    rendered_existing.append(f"- {prompt_text}")
                if rendered_existing:
                    existing_prompt_summary = (
                        "\n当前已经有以下图片提示词，补图时不要重复或近似重复这些画面：\n"
                        + "\n".join(rendered_existing)
                    )

            if current_count == 0 and len(supplemented_entries) == 0:
                shortage_text = (
                    "上一轮回复没有产出可用的 <pic prompt=\"...\">。"
                    if self.target_image_count == 1
                    else f"上一轮回复没有产出可用的 <pic prompt=\"...\">，目标出图数量为 {self.target_image_count}。"
                )
            else:
                shortage_text = (
                    f"当前已经提取到 {current_count + len(supplemented_entries)} 个可用的 <pic prompt=\"...\">，"
                    f"但目标出图数量是 {self.target_image_count}，还差 {remaining_count} 个。"
                )

            output_rule = (
                f"1. 只输出 {remaining_count} 个 <pic prompt=\"...\">，不要输出 <think>、解释、正文、代码块或 Markdown；"
            )
            count_rule = (
                f"3. 必须补足 {remaining_count} 张图，每个 <pic prompt=\"...\"> 对应一张不同的图，不要少于或多于该数量；"
            )
            fallback_prompt = (
                "下面是最近的一条回复内容：\n"
                f"{latest_reply_excerpt}\n\n"
                f"请只基于上面这条最近回复内容，补齐剩余 {remaining_count} 张图。"
                "只返回对应数量的 <pic prompt=\"...\"> 标签，可按换行分隔。如果有满足动作（如壁屄）或人物要求的lora，则直接使用lora，注意人物一致性"
            )

            force_draw_instruction = (
                "你现在处于补图提示词模式。"
                f"{shortage_text}"
                f"{existing_prompt_summary}\n"
                "硬性要求：\n"
                f"{output_rule}\n"
                "2. prompt 必须是适合 ComfyUI / Stable Diffusion / Danbooru 的英文 tags，半角逗号分隔；\n"
                f"{count_rule}\n"
                "4. 只能基于最近的一条回复内容来补图，不要参考更早的整体会话上下文；\n"
                "5. 如果信息不足，就提炼最近一条回复里最值得定格的不同瞬间；\n"
                "6. 不要输出占位符，不要留空，不要重复已有画面。"
            )
            system_prompt = (
                f"{base_system_prompt}\n\n{force_draw_instruction}".strip()
                if base_system_prompt
                else force_draw_instruction
            )

            try:
                response = await provider.text_chat(
                    prompt=fallback_prompt,
                    contexts=contexts,
                    system_prompt=system_prompt,
                )
            except Exception as e:
                logger.error(f"[ComfyUI] 强制画图补提示词请求失败: {e}")
                logger.error(traceback.format_exc())
                break

            fallback_text = str(getattr(response, "completion_text", "") or "").strip()
            if not fallback_text:
                logger.warning("[ComfyUI] 强制画图补提示词返回空内容")
                break

            round_entries = self._extract_usable_prompt_entries(fallback_text)
            if not round_entries:
                logger.warning(
                    "[ComfyUI] 强制画图补提示词后仍未提取到可用 prompt: %s",
                    fallback_text[:160],
                )
                break

            added_this_round = 0
            for entry in round_entries:
                prompt_signature = self._normalize_prompt_signature(entry.get("prompt", ""))
                if not prompt_signature or prompt_signature in seen_prompts:
                    continue
                supplemented_entries.append(entry)
                seen_prompts.add(prompt_signature)
                added_this_round += 1
                if len(supplemented_entries) >= missing_count:
                    break

            if added_this_round <= 0:
                logger.warning("[ComfyUI] 强制画图补提示词返回的内容与已有 prompt 重复，停止补图")
                break

            remaining_count = max(0, missing_count - len(supplemented_entries))

        if supplemented_entries:
            logger.info(
                "[ComfyUI] 🎯 强制画图补提示词成功，共补出 %s 张图",
                len(supplemented_entries),
            )
        return supplemented_entries

    async def initialize(self):
        self.context.activate_llm_tool("comfyui_txt2img")
        logger.info("[ComfyUI] 🎨 插件初始化完成，LLM 工具已激活")

    def _is_telegram_timeout_error(self, exc: Exception) -> bool:
        name = exc.__class__.__name__.lower()
        module = exc.__class__.__module__.lower()
        text = str(exc).lower()
        return "telegram" in module and ("timeout" in name or "timed out" in text)

    async def _send_telegram_image_file(self, event: AstrMessageEvent, img_path: Path) -> None:
        if not hasattr(event, "client"):
            await event.send(event.chain_result([Image.fromFileSystem(str(img_path))]))
            return

        chat_id = None
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            chat_id = getattr(msg_obj, "group_id", None)
        if not chat_id:
            chat_id = event.get_sender_id()

        message_thread_id = None
        if isinstance(chat_id, str) and "#" in chat_id:
            chat_id, message_thread_id = chat_id.split("#", 1)

        payload = {
            "chat_id": chat_id,
            "photo": str(img_path),
            "connect_timeout": 20,
            "read_timeout": 60,
            "write_timeout": 60,
            "pool_timeout": 20,
        }
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        await event.client.send_photo(**payload)

    # ====== 核心绘图逻辑 ======
    async def _handle_paint_logic(self, event: AstrMessageEvent, direct_send: bool):
        """处理画图的核心逻辑"""
        # 权限检查
        allowed, reason = self._check_access(event)
        if not allowed:
            self._remember_draw_failure(
                event,
                reason,
                prompt=self._extract_command_prompt(event),
                source="用户指令",
            )
            yield event.plain_result(reason)
            return
        
        try:
            full_message = event.message_str.strip()
            parts = full_message.split(' ', 1)
            prompt = parts[1].strip() if len(parts) > 1 else ""

            if not prompt:
                message = "❌ 请输入提示词，例如：/画图 1girl, smile"
                self._remember_draw_failure(event, message, source="用户指令")
                yield event.plain_result(message)
                return

            # 敏感词检查
            prompt_to_check, _ = self._extract_lora_control_tags(prompt)
            passed, sensitive = self._check_sensitive(prompt_to_check, event)
            if not passed:
                tip = "、".join(sensitive[:5])  # 最多显示5个
                extra = f"等 {len(sensitive)} 个" if len(sensitive) > 5 else ""
                message = f"🚫 检测到敏感词：{tip}{extra}，无法生成图片"
                self._remember_draw_failure(
                    event,
                    message,
                    prompt=prompt,
                    source="用户指令",
                )
                yield event.plain_result(message)
                return

            event.set_extra("comfy_draw_source", "用户指令")
            async for result in self.comfyui_txt2img(event, prompt=prompt, direct_send=direct_send):
                if isinstance(result, str):
                    yield event.plain_result(result)
                else:
                    yield result
                
        except Exception as e:
            logger.error(f"[ComfyUI] 绘图异常: {e}")
            logger.error(traceback.format_exc())
            message = f"❌ 执行出错：{str(e)[:50]}"
            self._remember_draw_failure(
                event,
                message,
                prompt=self._extract_command_prompt(event),
                source="用户指令",
            )
            yield event.plain_result(message)
    # ===== 探针：测试 event.send() 是否触发 on_decorating_result =====
    @filter.command("comfy_probe_send")
    async def cmd_probe_send(self, event: AstrMessageEvent):
        """探测 event.send() 是否会触发 on_decorating_result"""
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 仅管理员可用")
            return

        # 设置一个标记
        event.set_extra("_probe_send_test", True)
        logger.info("[探针] 已设置 _probe_send_test 标记")

        # 通过 event.send 发送一条消息
        try:
            await event.send(event.plain_result("探针消息：这是通过 event.send() 发出的"))
            logger.info("[探针] event.send() 调用完成")
        except Exception as e:
            logger.error(f"[探针] event.send() 失败: {e}")

        yield event.plain_result("探针完成，请检查日志中是否出现 '[探针] on_decorating_result 被 event.send 触发'")
    # ===== 探针结束 =====
    @filter.command("comfy帮助")
    async def cmd_comfyui_help(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        
        gid = self._get_group_id(event)
        policy = self._get_policy_for_event(event)
        user_id = str(event.get_sender_id())
        is_admin = user_id in self.admin_user_ids
        
        tips = [
            "🎨 ComfyUI Pro 插件帮助",
            "━━━━━━━━━━━━━━━━━━",
            "",
            "【基础指令】",
            "  /画图 <提示词>     生成图片（转发模式）",
            "  /画图no <提示词>   生成图片（直发模式）",
            "  /comfy帮助         显示此帮助",
            "",
            "【LLM 模式】",
            "  直接对话：'帮我画一个可爱的猫娘'",
            ""
        ]
        
        if is_admin:
            tips.extend([
                "【管理员指令】 👑",
                "  /comfy_ls              列出所有工作流",
                "  /comfy_use <序号>      切换工作流",
                "  /comfy_save            导入新工作流",
                "  /comfy_add             步数覆盖（按节点ID）",
                "  /comfy_lock on|off     切换全局锁定",
                "  /违禁级别              设置群敏感度",
                ""
            ])
        
        # 状态信息
        tips.append("━━━━━━━━━━━━━━━━━━")
        tips.append(f"📍 当前位置：{'群聊 ' + gid if gid else '私聊'}")
        tips.append(f"🔒 违禁级别：{policy}")
        tips.append(f"⏱️ 冷却时间：{self.cooldown_seconds} 秒")
        tips.append(f"🔐 全局锁定：{'开启' if self.lockdown else '关闭'}")
        if is_admin:
            tips.append(f"👑 身份：管理员")
            tips.append(f"📂 数据目录：{self.data_dir}")
        
        yield event.plain_result("\n".join(tips))
    @filter.command("comfy_test_send2")
    async def cmd_test_send2(self, event: AstrMessageEvent):
        """测试主动发送 - 第二轮"""
    
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 仅管理员可用")
            return
    
        from astrbot.api.message_components import Plain
    
        results = []
    
        # 测试 1: event.send 传入 MessageEventResult
        try:
            msg_result = event.plain_result("测试1: send + plain_result")
            await event.send(msg_result)
            results.append("✅ event.send(event.plain_result(...)) 可用")
        except Exception as e:
            results.append(f"❌ send+plain_result: {type(e).__name__}: {e}")
    
        # 测试 2: event.send 传入 chain_result
        try:
            msg_result = event.chain_result([Plain("测试2: send + chain_result")])
            await event.send(msg_result)
            results.append("✅ event.send(event.chain_result([...])) 可用")
        except Exception as e:
            results.append(f"❌ send+chain_result: {type(e).__name__}: {e}")
    
        # 测试 3: event.send_message 带 target
        try:
            await event.send_message(
                event.unified_msg_origin,
                event.chain_result([Plain("测试3: send_message 两参数")])
            )
            results.append("✅ event.send_message(origin, chain_result) 可用")
        except Exception as e:
            results.append(f"❌ send_message两参数: {type(e).__name__}: {e}")
    
        # 测试 4: context.send_message 用 chain_result
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                event.chain_result([Plain("测试4: context + chain_result")])
            )
            results.append("✅ context.send_message(origin, chain_result) 可用")
        except Exception as e:
            results.append(f"❌ context+chain_result: {type(e).__name__}: {e}")
    
        # 测试 5: 查看 MessageChain 是否存在
        try:
            from astrbot.api.message_components import MessageChain
            chain = MessageChain([Plain("测试5: MessageChain")])
            await event.send(chain)
            results.append("✅ event.send(MessageChain([...])) 可用")
        except ImportError:
            results.append("ℹ️ MessageChain 不可导入")
        except Exception as e:
            results.append(f"❌ MessageChain: {type(e).__name__}: {e}")
    
        # 测试 6: 直接查看 send 的签名
        try:
            import inspect
            sig = inspect.signature(event.send)
            results.append(f"ℹ️ event.send 签名: {sig}")
        except Exception as e:
            results.append(f"ℹ️ 无法获取签名: {e}")
    
        # 测试 7: 查看 send_message 签名
        try:
            import inspect
            sig = inspect.signature(event.send_message)
            results.append(f"ℹ️ event.send_message 签名: {sig}")
        except Exception as e:
            results.append(f"ℹ️ 无法获取签名: {e}")
    
        yield event.plain_result("\n".join(["📋 发送测试结果 v2：", ""] + results))
    @filter.command("api_test_all")
    async def cmd_api_test_all(self, event: AstrMessageEvent):
        """一次性测试所有API和命令相关功能"""
    
        import inspect
        results = []
    
        results.append("=" * 50)
        results.append("🔍 ASTRBOT API 完整探测报告")
        results.append("=" * 50)
    
        # ========== 1. filter 模块所有成员 ==========
        results.append("\n📦 【filter 模块成员】")
        results.append("-" * 40)
        try:
            for name in sorted(dir(filter)):
                if name.startswith('_'):
                    continue
                try:
                    obj = getattr(filter, name)
                    if callable(obj):
                        try:
                            sig = str(inspect.signature(obj))
                            results.append(f"  ✅ filter.{name}{sig}")
                        except:
                            results.append(f"  ✅ filter.{name}() [callable]")
                    else:
                        results.append(f"  📌 filter.{name} = {repr(obj)[:30]}")
                except Exception as e:
                    results.append(f"  ❌ filter.{name}: {e}")
        except Exception as e:
            results.append(f"  ❌ 探测失败: {e}")

        # ========== 2. event 对象成员 ==========
        results.append("\n📦 【event 常用成员】")
        results.append("-" * 40)
    
        event_attrs = [
            'message_str', 'get_sender_id', 'get_sender_name', 
            'unified_msg_origin', 'session_id', 'message_obj',
            'plain_result', 'chain_result', 'send', 'send_message',
            'get_messages', 'is_private', 'is_group'
        ]
        for attr in event_attrs:
            try:
                obj = getattr(event, attr, None)
                if obj is None:
                    results.append(f"  ❌ event.{attr} 不存在")
                elif callable(obj):
                    try:
                        sig = str(inspect.signature(obj))
                        results.append(f"  ✅ event.{attr}{sig}")
                    except:
                        results.append(f"  ✅ event.{attr}() [callable]")
                else:
                    val = repr(obj)[:30]
                    results.append(f"  📌 event.{attr} = {val}")
            except Exception as e:
                results.append(f"  ❌ event.{attr}: {e}")

        # ========== 3. context 对象成员 ==========
        results.append("\n📦 【context 常用成员】")
        results.append("-" * 40)
    
        context_attrs = [
            'send_message', 'get_config', 'register_command',
            'get_all_stars', 'get_platform', 'llm_request'
        ]
        for attr in context_attrs:
            try:
                obj = getattr(self.context, attr, None)
                if obj is None:
                    results.append(f"  ❌ context.{attr} 不存在")
                elif callable(obj):
                    try:
                        sig = str(inspect.signature(obj))
                        results.append(f"  ✅ context.{attr}{sig}")
                    except:
                        results.append(f"  ✅ context.{attr}() [callable]")
                else:
                    results.append(f"  📌 context.{attr} = {type(obj).__name__}")
            except Exception as e:
                results.append(f"  ❌ context.{attr}: {e}")

        # ========== 4. 探测所有 context 成员 ==========
        results.append("\n📦 【context 全部成员】")
        results.append("-" * 40)
        try:
            for name in sorted(dir(self.context)):
                if name.startswith('_'):
                    continue
                try:
                    obj = getattr(self.context, name)
                    obj_type = type(obj).__name__
                    results.append(f"  • {name} ({obj_type})")
                except:
                    results.append(f"  • {name}")
        except Exception as e:
            results.append(f"  ❌ 探测失败: {e}")

        # ========== 5. 可用的消息组件 ==========
        results.append("\n📦 【消息组件探测】")
        results.append("-" * 40)
    
        components = [
            'Plain', 'Image', 'At', 'AtAll', 'Reply', 
            'Face', 'Voice', 'Video', 'File', 'MessageChain'
        ]
        for comp in components:
            try:
                exec(f"from astrbot.api.message_components import {comp}")
                results.append(f"  ✅ {comp} 可导入")
            except ImportError:
                results.append(f"  ❌ {comp} 不可导入")
            except Exception as e:
                results.append(f"  ❌ {comp}: {e}")

        # ========== 6. 其他可用模块 ==========
        results.append("\n📦 【其他模块探测】")
        results.append("-" * 40)
    
        modules = [
            ('astrbot.api', 'logger'),
            ('astrbot.api.event', 'filter'),
            ('astrbot.api.event', 'AstrMessageEvent'),
            ('astrbot.api.star', 'Context'),
            ('astrbot.api.star', 'Star'),
            ('astrbot.api.star', 'register'),
        ]
        for module, name in modules:
            try:
                exec(f"from {module} import {name}")
                results.append(f"  ✅ from {module} import {name}")
            except Exception as e:
                results.append(f"  ❌ {module}.{name}: {e}")

        # ========== 7. 当前事件信息 ==========
        results.append("\n📦 【当前事件信息】")
        results.append("-" * 40)
    
        try:
            results.append(f"  • message_str: {event.message_str[:50]}")
        except:
            pass
        try:
            results.append(f"  • sender_id: {event.get_sender_id()}")
        except:
            pass
        try:
            results.append(f"  • unified_msg_origin: {event.unified_msg_origin}")
        except:
            pass
        try:
            results.append(f"  • session_id: {event.session_id}")
        except:
            pass

        results.append("\n" + "=" * 50)
        results.append("🔍 探测完成")
        results.append("=" * 50)

        # 输出结果
        full_result = "\n".join(results)
    
        # 如果太长，分段发送
        if len(full_result) > 2000:
            chunks = [results[i:i+30] for i in range(0, len(results), 30)]
            for i, chunk in enumerate(chunks):
                yield event.plain_result(f"📋 第{i+1}部分:\n" + "\n".join(chunk))
        else:
            yield event.plain_result(full_result)

    @filter.command("违禁级别", aliases={"banlevel", "敏感级别"})
    async def cmd_set_policy(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        
        if not self._is_group_message(event):
            yield event.plain_result("⚠️ 该指令仅支持在群聊中使用")
            return

        # 检查管理员权限
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可修改违禁级别")
            return

        full_msg = event.message_str.strip()
        parts = full_msg.split()
        gid = self._get_group_id(event) or "未知"

        if len(parts) == 1:
            current = self.group_policies.get(gid, self.default_group_policy)
            yield event.plain_result(
                f"📊 本群当前违禁级别：{current}\n"
                f"━━━━━━━━━━━━━━\n"
                f"可选级别：\n"
                f"  none - 不过滤\n"
                f"  lite - 轻度过滤\n"
                f"  full - 完全过滤\n"
                f"━━━━━━━━━━━━━━\n"
                f"用法：/违禁级别 <级别>"
            )
            return

        level = parts[1].lower()
        if level not in self.policies:
            yield event.plain_result("❌ 无效级别，可选：none / lite / full")
            return

        self.group_policies[gid] = level
        logger.info(f"[ComfyUI] 群 {gid} 违禁级别已设为 {level}（操作者：{user_id}）")
        yield event.plain_result(f"✅ 已将本群违禁级别设置为：{level}")

    @filter.command("comfy_lock", aliases=["全局锁定", "锁图", "绘图锁定"])
    async def cmd_comfy_lock(self, event: AstrMessageEvent):
        """管理员动态切换全局锁定状态"""
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可切换全局锁定")
            return

        if not self.lockdown_command_enabled:
            yield event.plain_result("⚠️ 锁定命令开关已关闭，请在插件配置中启用 control.lockdown_command_enabled")
            return

        args = event.message_str.split()
        action = args[1].lower() if len(args) > 1 else "status"

        if action in ("status", "状态", "查询"):
            yield event.plain_result(
                "🔐 全局锁定状态\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"当前: {'开启' if self.lockdown else '关闭'}\n"
                f"命令开关: {'开启' if self.lockdown_command_enabled else '关闭'}\n"
                "用法: /comfy_lock on|off|status"
            )
            return

        if action in ("on", "true", "1", "enable", "start", "开启"):
            self.lockdown = True
            logger.warning(f"[ComfyUI] 管理员 {user_id} 通过命令开启全局锁定")
            yield event.plain_result("🔒 已开启全局锁定：当前仅超级管理员可用绘图功能")
            return

        if action in ("off", "false", "0", "disable", "stop", "关闭"):
            self.lockdown = False
            logger.info(f"[ComfyUI] 管理员 {user_id} 通过命令关闭全局锁定")
            yield event.plain_result("🔓 已关闭全局锁定：恢复正常访问控制")
            return

        yield event.plain_result("❌ 参数无效，用法：/comfy_lock on|off|status")

    @filter.command("comfy_ls")
    async def cmd_comfy_list(self, event: AstrMessageEvent):
        """列出当前所有可用工作流"""
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可查看工作流列表")
            return

        if not self.workflow_dir.exists():
            yield event.plain_result("❌ 工作流目录不存在")
            return

        # 排除 .steps.json 文件
        files = sorted([
            f.name for f in self.workflow_dir.glob("*.json") 
            if not self._is_workflow_aux_file(f.name)
        ])
    
        if not files:
            yield event.plain_result("📂 目录中没有工作流文件")
            return

        current_file = self.api.wf_filename if self.api else "未知"
    
        msg = ["📂 可用工作流列表", "━━━━━━━━━━━━━━━━━━"]
    
        for i, f in enumerate(files, 1):
            stem = Path(f).stem
            sidecar = self.workflow_dir / f"{stem}.steps.json"
        
            # 检查是否有步数覆盖（新格式：按节点ID存储）
            steps_info = ""
            if sidecar.exists():
                try:
                    with open(sidecar, "r", encoding="utf-8") as sf:
                        data = json.load(sf)
                        if data and isinstance(data, dict):
                            count = len(data)
                            steps_info = f" [覆盖:{count}项]"
                except:
                    pass
        
            if f == current_file:
                msg.append(f"✅ {i}. {f}{steps_info} (当前)")
            else:
                msg.append(f"   {i}. {f}{steps_info}")
    
        msg.append("")
        msg.append("━━━━━━━━━━━━━━━━━━")
        msg.append("切换：/comfy_use <序号>")
        msg.append("覆盖：/comfy_add <节点ID> <步数>")
        msg.append("查看：/comfy_add list")
    
        yield event.plain_result("\n".join(msg))

    @filter.command("comfy_use")
    async def cmd_comfy_use(self, event: AstrMessageEvent):
        """切换工作流"""
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可切换工作流")
            return

        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "❌ 参数不足\n"
                "用法：/comfy_use <序号> [正面ID] [负面ID] [输出ID]\n"
                "示例：/comfy_use 1 6 7 9"
            )
            return

        try:
            # 排除 .steps.json 文件
            files = sorted([
                f.name for f in self.workflow_dir.glob("*.json")
                if not self._is_workflow_aux_file(f.name)
            ])
        
            index = int(args[1])
            if not (1 <= index <= len(files)):
                yield event.plain_result(f"❌ 序号错误，请输入 1 到 {len(files)} 之间的数字")
                return
            filename = files[index - 1]
        except ValueError:
            yield event.plain_result("❌ 请输入有效的数字序号")
            return
        except Exception as e:
            yield event.plain_result(f"❌ 查找工作流失败: {e}")
            return

        inp_id = args[2] if len(args) > 2 else None
        neg_id = args[3] if len(args) > 3 else None
        out_id = args[4] if len(args) > 4 else None

        if not self.api:
            yield event.plain_result("❌ ComfyUI API 未初始化")
            return

        exists, msg = self.api.reload_config(
            filename, 
            input_id=inp_id, 
            neg_node_id=neg_id,
            output_id=out_id
        )
        
        status = "✅" if exists else "⚠️"
        logger.info(f"[ComfyUI] 管理员 {user_id} 切换工作流: {filename}")
        yield event.plain_result(f"{status} {msg}")

    @filter.command("comfy_save")
    async def cmd_comfy_save(self, event: AstrMessageEvent):
        """保存/导入工作流"""
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可导入工作流")
            return

        full_text = event.message_str
        content = full_text.split(maxsplit=2)
        
        if len(content) < 3:
            yield event.plain_result(
                "❌ 参数不足\n"
                "用法：/comfy_save <文件名> <JSON内容>\n"
                "示例：/comfy_save my_workflow.json {\"1\":{...}}"
            )
            return
        
        filename = content[1]
        json_str = content[2]

        if not filename.endswith(".json"):
            filename += ".json"

        try:
            json_str = json_str.replace("```json", "").replace("```", "").strip()
            json_data = json.loads(json_str)
        except json.JSONDecodeError as e:
            yield event.plain_result(f"❌ JSON 解析失败：{str(e)[:50]}")
            return

        save_path = self.workflow_dir / filename

        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            
            self._auto_update_schema()
            
            logger.info(f"[ComfyUI] 管理员 {user_id} 导入工作流: {filename}")
            yield event.plain_result(
                f"✅ 保存成功！\n"
                f"文件：{filename}\n"
                f"使用 /comfy_ls 查看列表"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 保存失败: {e}")
    @filter.command("comfy_add")
    async def cmd_comfy_add(self, event: AstrMessageEvent):
        """给当前工作流的指定节点绑定步数覆盖"""
    
        # 权限检查
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可设置步数覆盖")
            return
    
        # 检查 API
        if not self.api:
            yield event.plain_result("❌ ComfyUI API 未初始化")
            return
    
        # 解析参数
        args = event.message_str.split()
    
        # 无参数：显示帮助
        if len(args) < 2:
            yield event.plain_result(
                "📝 步数覆盖设置（按节点ID）\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "用法：\n"
                "  /comfy_add <节点ID> <步数>      单个设置\n"
                "  /comfy_add <ID1> <步数1> <ID2> <步数2>  批量设置\n"
                "  /comfy_add <节点ID> off         取消单个\n"
                "  /comfy_add list                 查看当前覆盖\n"
                "  /comfy_add clear                清空所有覆盖\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "示例：\n"
                "  /comfy_add 3839 20              节点3839设为20步\n"
                "  /comfy_add 3839 20 4521 50      同时设置两个节点\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "💡 节点ID可在工作流JSON中查找 ParameterBreak 节点"
            )
            return
    
        sub_cmd = args[1].lower()
    
        # 子命令：list
        if sub_cmd == "list":
            async for result in self._comfy_add_list(event):
                yield result
            return
    
        # 子命令：clear
        if sub_cmd == "clear":
            async for result in self._comfy_add_clear(event):
                yield result
            return
    
        # 正常流程：解析 <节点ID> <步数> 对
        params = args[1:]
    
        if len(params) % 2 != 0:
            yield event.plain_result("❌ 参数格式错误，需要成对输入：<节点ID> <步数>")
            return
    
        # 获取当前工作流的 sidecar 路径
        current_file = self.api.wf_filename
        stem = Path(current_file).stem
        sidecar_path = self.workflow_dir / f"{stem}.steps.json"
    
        # 读取现有配置
        existing = {}
        if sidecar_path.exists():
            try:
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except:
                existing = {}
    
        # 解析并更新
        changes = []
        removes = []
    
        for i in range(0, len(params), 2):
            node_id = params[i]
            value = params[i + 1].lower()
        
            if value in ("off", "0", "del", "delete", "rm", "remove"):
                # 删除该节点的覆盖
                if node_id in existing:
                    del existing[node_id]
                    removes.append(node_id)
            else:
                # 设置步数
                try:
                    steps = int(value)
                    if not (1 <= steps <= 200):
                        yield event.plain_result(f"❌ 步数应在 1-200 之间，节点 {node_id} 的值 {value} 无效")
                        return
                    existing[node_id] = {"steps": steps}
                    changes.append(f"{node_id}:{steps}步")
                except ValueError:
                    yield event.plain_result(f"❌ 无效的步数值：{value}")
                    return
    
        # 保存
        try:
            if existing:
                with open(sidecar_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
            else:
                # 如果清空了，删除文件
                if sidecar_path.exists():
                    sidecar_path.unlink()
        
            # 构建反馈消息
            msg_parts = []
            if changes:
                msg_parts.append(f"✅ 已设置: {', '.join(changes)}")
            if removes:
                msg_parts.append(f"🗑️ 已移除: {', '.join(removes)}")
        
            msg_parts.append(f"📍 工作流: {current_file}")
        
            logger.info(f"[ComfyUI] 管理员 {user_id} 修改步数覆盖: {current_file} -> {existing}")
            yield event.plain_result("\n".join(msg_parts))
    
        except Exception as e:
            yield event.plain_result(f"❌ 保存失败: {e}")

    async def _comfy_add_list(self, event: AstrMessageEvent):
        """列出当前工作流的步数覆盖"""
    
        current_file = self.api.wf_filename
        stem = Path(current_file).stem
        sidecar_path = self.workflow_dir / f"{stem}.steps.json"
    
        lines = [
            f"📊 当前工作流步数覆盖",
            f"━━━━━━━━━━━━━━━━━━",
            f"📍 工作流: {current_file}",
            ""
        ]
    
        if not sidecar_path.exists():
            lines.append("ℹ️ 暂无步数覆盖配置")
        else:
            try:
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            
                if not data:
                    lines.append("ℹ️ 暂无步数覆盖配置")
                else:
                    lines.append("节点覆盖列表：")
                    for node_id, value in data.items():
                        if isinstance(value, dict):
                            steps = value.get("steps", "?")
                        else:
                            steps = value
                        lines.append(f"  • 节点 {node_id}: {steps} 步")
            except Exception as e:
                lines.append(f"❌ 读取配置失败: {e}")
    
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("设置：/comfy_add <节点ID> <步数>")
        lines.append("清空：/comfy_add clear")
    
        yield event.plain_result("\n".join(lines))
    async def _comfy_add_clear(self, event: AstrMessageEvent):
        """清空当前工作流的所有步数覆盖"""
    
        current_file = self.api.wf_filename
        stem = Path(current_file).stem
        sidecar_path = self.workflow_dir / f"{stem}.steps.json"
    
        if not sidecar_path.exists():
            yield event.plain_result(f"ℹ️ {current_file} 本来就没有步数覆盖")
            return
    
        try:
            sidecar_path.unlink()
            user_id = str(event.get_sender_id())
            logger.info(f"[ComfyUI] 管理员 {user_id} 清空步数覆盖: {current_file}")
            yield event.plain_result(f"✅ 已清空 {current_file} 的所有步数覆盖")
        except Exception as e:
            yield event.plain_result(f"❌ 清空失败: {e}")

    @filter.command("当前工作流", aliases=["comfy_current", "当前wf"])
    async def cmd_comfy_current(self, event: AstrMessageEvent):
        current_file = self.config.get("json_file") or self.config.get("workflow_json") or "未配置"
        input_id = self.config.get("input_node_id") or self.config.get("input_id") or "未配置"
        output_id = self.config.get("output_node_id") or self.config.get("output_id") or "未配置"
        lines = [
            "🧠 当前 ComfyUI 工作流",
            f"- 文件: {current_file}",
            f"- 输入节点: {input_id}",
            f"- 输出节点: {output_id}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("测试lora", aliases=["lora_test", "lora测试"])
    async def cmd_lora_test(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        if not getattr(self, "api", None):
            yield event.plain_result("❌ ComfyUI 服务未连接，请先检查插件配置")
            return

        try:
            context = self.api.get_lora_runtime_context()
        except Exception as e:
            yield event.plain_result(f"❌ 读取 LoRA 上下文失败: {e}")
            return

        if not context.get("supported"):
            yield event.plain_result("⚠️ 当前工作流未检测到 LoRA 堆节点（Lora Loader / LoraManager）")
            return

        full_msg = (event.message_str or "").strip()
        parts = full_msg.split(None, 1)
        arg_text = parts[1].strip() if len(parts) > 1 else ""

        catalog_entries = list(context.get("catalog", {}).values())
        default_entries = list(context.get("default_active_loras", []))
        workflow_name = getattr(self.api, "wf_filename", "未知")
        scan_roots = context.get("scan_roots", [])
        lora_enabled = "开启" if self.lora_control_enabled else "关闭"

        if not arg_text or arg_text.lower() in ("help", "status", "状态", "帮助"):
            default_names = ", ".join(item.get("display_name", "") for item in default_entries) or "无"
            lines = [
                "🧪 LoRA 测试命令",
                "━━━━━━━━━━━━━━━━━━",
                f"当前工作流: {workflow_name}",
                f"LoRA 控制功能: {lora_enabled}",
                f"已识别 LoRA 数量: {len(catalog_entries)}",
                f"默认启用 LoRA: {default_names}",
            ]
            if scan_roots:
                lines.append("扫描目录:")
                lines.extend(f"- {root}" for root in scan_roots)
            lines.extend(
                [
                    "━━━━━━━━━━━━━━━━━━",
                    "用法示例:",
                    "/测试lora 列表",
                    "/测试lora 列表 菲比",
                    "/测试lora QAQ1121-24:0.8",
                    "/测试lora !clear_defaults, 菲比 鸣潮:0.8@1",
                    '/测试lora <lora picks="!clear_defaults, QAQ1121-24:0.8@1+2">',
                    "说明:",
                    "- @1 / @1+2 表示选该 LoRA 的第 1 个或第 1+2 个触发词候选",
                    "- !clear_defaults 表示本次禁用工作流默认 LoRA",
                    "- 如果用了 @序号，系统会自动注入对应触发词，<pic prompt> 里不要再手抄同一串角色/外观 tags",
                    "- 这个命令只做解析预演，不会真的出图",
                ]
            )
            yield event.plain_result("\n".join(lines))
            return

        arg_lower = arg_text.lower()
        if arg_lower == "列表" or arg_lower.startswith("列表 ") or arg_lower == "list" or arg_lower.startswith("list "):
            keyword = ""
            tokens = arg_text.split(None, 1)
            if len(tokens) > 1:
                keyword = tokens[1].strip()

            if keyword:
                normalized_keyword = self._normalize_lora_key(keyword)
                matched = []
                for entry in catalog_entries:
                    search_fields = [
                        entry.get("display_name", ""),
                        entry.get("workflow_name", ""),
                        entry.get("loader_name", ""),
                        entry.get("model_name", ""),
                    ] + list(entry.get("aliases", []))
                    if any(normalized_keyword in self._normalize_lora_key(value) for value in search_fields if value):
                        matched.append(entry)
            else:
                matched = catalog_entries

            if not matched:
                yield event.plain_result(f"🔍 没找到匹配的 LoRA: {keyword}")
                return

            limit = 20
            lines = [
                "📚 LoRA 列表",
                "━━━━━━━━━━━━━━━━━━",
                f"工作流: {workflow_name}",
                f"匹配数量: {len(matched)}",
            ]
            if len(matched) > limit:
                lines.append(f"仅展示前 {limit} 个结果")
            for entry in matched[:limit]:
                strength_text = self.api._format_lora_strength(entry.get("default_strength", 1.0))
                clip_text = self.api._format_lora_strength(
                    entry.get("default_clip_strength", entry.get("default_strength", 1.0))
                )
                status_text = "默认启用" if entry.get("default_active") else "可选"
                lines.append(
                    f"- {entry.get('display_name')} | {status_text} | 默认强度 {strength_text}/{clip_text}"
                )
                lines.append(
                    f"  触发词: {self._format_lora_trigger_preview(entry, limit=getattr(self.api, 'max_trigger_options_per_lora', 3))}"
                )
            yield event.plain_result("\n".join(lines))
            return

        parse_text = arg_text
        if arg_lower.startswith("解析 "):
            parse_text = arg_text.split(None, 1)[1].strip()
        elif arg_lower.startswith("test "):
            parse_text = arg_text.split(None, 1)[1].strip()

        if "<lora" in parse_text.lower():
            _, parsed = self._extract_lora_control_tags(parse_text)
        else:
            parsed = self._parse_lora_pick_string(parse_text)

        if not parsed:
            yield event.plain_result(
                "❌ 没解析到任何 LoRA 选择\n"
                "示例: /测试lora QAQ1121-24:0.8\n"
                "示例: /测试lora !clear_defaults, 菲比 鸣潮:0.8@1"
            )
            return

        resolved = self.api.resolve_lora_selections(parsed)
        preview_prompt, injected_hints = self.api._inject_selected_lora_prompt_hints(
            "1girl, masterpiece", resolved
        )
        clear_defaults = any(item.get("control") == "clear_defaults" for item in parsed)

        parsed_names = [
            str(item.get("name", "")).strip()
            for item in parsed
            if item.get("control") != "clear_defaults" and str(item.get("name", "")).strip()
        ]
        resolved_names = {
            str(item.get("requested_name", "")).strip()
            for item in resolved
            if item.get("control") != "clear_defaults"
        }
        unresolved_names = [name for name in parsed_names if name not in resolved_names]

        lines = [
            "🧪 LoRA 解析预演",
            "━━━━━━━━━━━━━━━━━━",
            f"工作流: {workflow_name}",
            f"默认 LoRA 处理: {'本次清空' if clear_defaults else '保持默认行为'}",
            f"原始输入: {parse_text}",
            "",
            "解析结果:",
        ]

        for item in parsed:
            if item.get("control") == "clear_defaults":
                lines.append("- !clear_defaults")
                continue
            trigger_indexes = item.get("trigger_indexes", []) or []
            trigger_text = f" @ {'+'.join(map(str, trigger_indexes))}" if trigger_indexes else ""
            lines.append(
                f"- {item.get('name')} | 强度 {item.get('strength')} / {item.get('clip_strength')}{trigger_text}"
            )

        lines.append("")
        lines.append("命中结果:")

        for item in resolved:
            if item.get("control") == "clear_defaults":
                lines.append("- 本次会清空工作流默认 LoRA")
                continue

            strength_text = self.api._format_lora_strength(item.get("strength", 1.0))
            clip_text = self.api._format_lora_strength(item.get("clip_strength", item.get("strength", 1.0)))
            trigger_indexes = item.get("trigger_indexes", []) or []
            trigger_index_text = "+".join(map(str, trigger_indexes)) if trigger_indexes else "无"
            lines.append(
                f"- {item.get('display_name')} | 实际载入名 {item.get('name')} | 强度 {strength_text}/{clip_text} | 触发词编号 {trigger_index_text}"
            )
            selected_hints = item.get("selected_prompt_hints", []) or []
            if selected_hints:
                preview = " | ".join(
                    re.sub(r"\s+", " ", str(text).strip())[:80] + ("…" if len(re.sub(r'\s+', ' ', str(text).strip())) > 80 else "")
                    for text in selected_hints[:3]
                )
                lines.append(f"  注入提示: {preview}")

        if unresolved_names:
            lines.append("")
            lines.append("未命中:")
            lines.extend(f"- {name}" for name in unresolved_names)

        if injected_hints:
            lines.append("")
            lines.append("提示词注入预览:")
            lines.append(f"- {preview_prompt[:300]}{'…' if len(preview_prompt) > 300 else ''}")

        yield event.plain_result("\n".join(lines))

    @filter.command("重绘", aliases=["重抽", "reroll"])
    async def cmd_reroll(self, event: AstrMessageEvent):
        full_msg = (event.message_str or "").strip()
        full_msg = re.sub(r'\[At:\d+\]\s*', '', full_msg).strip()
        parts = full_msg.split(None, 1)
        prompt = parts[1].strip() if len(parts) > 1 else ""
        if not prompt:
            yield event.plain_result("📖 用法: /重绘 <提示词>\n示例: /重绘 1girl, silver hair, cinematic lighting")
            return
        async for result in self._handle_paint_logic(event, direct_send=True):
            yield result

    @filter.command("画图", aliases=["绘画"])
    async def cmd_paint(self, event: AstrMessageEvent):
        async for result in self._handle_paint_logic(event, direct_send=False):
            yield result

    @filter.command("画图no")
    async def cmd_paint_no(self, event: AstrMessageEvent):
        async for result in self._handle_paint_logic(event, direct_send=True):
            yield result

    # ====== 辅助方法 ======
    def _is_group_message(self, event: AstrMessageEvent) -> bool:
        mt = getattr(event, "message_type", None)
        if mt is not None:
            return mt == "group"
        try:
            if hasattr(event, "get_group_id"):
                gid = event.get_group_id()
                if gid:
                    return True
            gid_attr = getattr(event, "group_id", None)
            return gid_attr is not None
        except Exception:
            return False

    def _get_group_id(self, event: AstrMessageEvent):
        if not self._is_group_message(event):
            return None
        getters = [
            lambda e: e.get_group_id() if hasattr(e, "get_group_id") else None,
            lambda e: getattr(e, "group_id", None),
            lambda e: getattr(getattr(e, "scene", None), "group_id", None),
        ]
        for g in getters:
            try:
                gid = g(event)
                if gid:
                    return str(gid)
            except Exception:
                continue
        return None

    def _get_self_id(self, event: AstrMessageEvent):
        getters = [
            lambda e: e.get_self_id() if hasattr(e, "get_self_id") else None,
            lambda e: getattr(e, "self_id", None),
            lambda e: getattr(getattr(self.context, "bot", None), "self_id", None),
            lambda e: getattr(self.context, "self_id", None),
        ]
        for g in getters:
            try:
                sid = g(event)
                if sid:
                    return str(sid)
            except Exception:
                continue
        return None

    def _is_ascii_term(self, s: str) -> bool:
        return all(ord(ch) < 128 for ch in s)

    def _build_policy_patterns(self):
        for policy, cats in self.policies.items():
            word_terms = []
            phrase_terms = []
            for cat in cats:
                for t in self.lexicon.get(cat, []):
                    if not t:
                        continue
                    if self._is_ascii_term(t):
                        if " " in t: 
                            phrase_terms.append(re.escape(t))
                        else:         
                            word_terms.append(re.escape(t))
            word_terms = list(dict.fromkeys(word_terms))
            phrase_terms = list(dict.fromkeys(phrase_terms))

            parts = []
            if word_terms:
                parts.append(r'(?<![A-Za-z0-9_])(?:' + '|'.join(word_terms) + r')(?![A-Za-z0-9_])')
            if phrase_terms:
                parts.append('|'.join(phrase_terms))

            ascii_pat = re.compile('|'.join(parts), re.IGNORECASE) if parts else None
            self._policy_patterns[policy] = ascii_pat

    def _get_policy_for_event(self, event: AstrMessageEvent) -> str:
        if self._is_group_message(event):
            gid = self._get_group_id(event)
            if not gid:
                return self.default_group_policy
            return self.group_policies.get(gid, self.default_group_policy)
        return self.default_private_policy

    def _find_sensitive_words(self, text: str, event: AstrMessageEvent = None):
        if not text:
            return []
        policy = "full"
        if event is not None:
            policy = self._get_policy_for_event(event)

        if policy == "none":
            return []

        ascii_pat = self._policy_patterns.get(str(policy).lower())
        if not ascii_pat:
            return []

        seen = set()
        result = []
        for m in ascii_pat.finditer(text):
            w = m.group(0)
            key = w.lower()
            if key not in seen:
                seen.add(key)
                result.append(w)
        return result

    # ====== 修改提取逻辑 ======
    @filter.on_llm_response(priority=70)
    async def _extract_prompt_before_filter(self, event: AstrMessageEvent, resp: LLMResponse):
        """提取 LLM 回复中的提示词（使用 <pic prompt="..."> 格式）"""
        if not resp or not resp.completion_text:
            return
    
        full_text = resp.completion_text
        cleaned_text = self._strip_comfy_control_tags(full_text, remove_think=True)
        cleaned_entries = self._extract_usable_prompt_entries(full_text)
        supplemented_entries = await self._generate_force_draw_prompt_entries(
            event,
            cleaned_text,
            cleaned_entries,
        )
        if supplemented_entries:
            cleaned_entries = cleaned_entries + supplemented_entries
        if not cleaned_entries:
            return

        # 清理文本供其他插件使用（移除 <pic>、<think>、<ctx> 标签）
        event.set_extra("comfy_cleaned_text", cleaned_text)

        cleaned_prompts = [item["prompt"] for item in cleaned_entries]
        use_multi_image_mode = self._should_use_multi_image_mode(len(cleaned_entries))
    
        # 单图模式
        if not use_multi_image_mode:
            event._comfy_extracted_prompt = cleaned_entries[0]["prompt"]
            event._comfy_extracted_loras = cleaned_entries[0].get("lora_selections", [])
            logger.info(f"[ComfyUI] 📝 检测到单图模式: {cleaned_prompts[0][:50]}...")
            # 丢弃绘图提示词，避免污染历史记录上下文
            if self.discard_prompt_from_history:
                resp.completion_text = cleaned_text
                resp.result_chain = MessageChain().message(cleaned_text)
                logger.info("[ComfyUI] 🗑️ 已从历史记录中移除绘图提示词")
            return
    
        # 多图模式
        if use_multi_image_mode:
            pic_matches = self._find_pic_prompt_matches(full_text)
            parts = []
            cursor = 0
            for item in pic_matches:
                parts.append(full_text[cursor:item["start"]])
                cursor = item["end"]
            parts.append(full_text[cursor:])
        
            # 检测原始文本中的 <render> 标签信息，用于补全被切割的段落
            render_match = re.search(r'<render\b[^>]*>', full_text)
            render_open_tag = render_match.group(0) if render_match else None
            render_close_tag = "</render>" if render_open_tag else None

            segments = []
            prompt_idx = 0
        
            for i, text in enumerate(parts):
                text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
                text = re.sub(r'</?ctx>', '', text)
                text = re.sub(r'</pic>', '', text, flags=re.IGNORECASE)
                text = re.sub(r'<lora\s+picks=".*?"\s*/?>', '', text, flags=re.DOTALL | re.IGNORECASE)
                text = text.strip()
                if text:
                    # 如果原文使用了 <render> 标签，确保每个文本段都有完整的标签对
                    if render_open_tag:
                        has_open = bool(re.search(r'<render\b', text))
                        has_close = '</render>' in text
                        if has_open and not has_close:
                            text = text + render_close_tag
                        elif has_close and not has_open:
                            text = render_open_tag + text
                        elif not has_open and not has_close:
                            text = render_open_tag + text + render_close_tag
                    segments.append({"type": "text", "content": text})
                if prompt_idx < len(cleaned_prompts):
                    segments.append(
                        {
                            "type": "prompt",
                            "content": cleaned_prompts[prompt_idx],
                            "lora_selections": cleaned_entries[prompt_idx].get("lora_selections", []),
                        }
                    )
                    prompt_idx += 1

            while prompt_idx < len(cleaned_prompts):
                segments.append(
                    {
                        "type": "prompt",
                        "content": cleaned_prompts[prompt_idx],
                        "lora_selections": cleaned_entries[prompt_idx].get("lora_selections", []),
                    }
                )
                prompt_idx += 1
        
            if segments:
                event._comfy_segments = segments
                logger.info(f"[ComfyUI] 📝 检测到多图模式，共 {len(cleaned_prompts)} 张图片")
                # 丢弃绘图提示词，避免污染历史记录上下文
                if self.discard_prompt_from_history:
                    resp.completion_text = cleaned_text
                    resp.result_chain = MessageChain().message(cleaned_text)
                    logger.info("[ComfyUI] 🗑️ 已从历史记录中移除绘图提示词（多图模式）")            

    # ====== 自动绘图逻辑保持不变 ======
    @filter.on_decorating_result(priority=99)
    async def _auto_paint_from_llm(self, event: AstrMessageEvent):
        """自动绘图 - 阶段1：构建 chain（多图）或启动异步任务（单图）"""
        if getattr(event, "_comfy_auto_painted", False):
            return

        # 检查是否有多图段落
        segments = getattr(event, "_comfy_segments", None)

        # === 多图分段模式：构建带标记的 chain，交给 HtmlRender 渲染后由 priority=10 发送 ===
        if segments and self._should_use_multi_image_mode(
            sum(1 for item in segments if item.get("type") == "prompt"),
        ):
            event._comfy_auto_painted = True

            # 权限检查
            allowed, reason = self._check_access(event)
            if not allowed:
                logger.warning(f"[ComfyUI] 多图请求被拒绝: {reason}")
                self._remember_draw_failure(
                    event,
                    reason,
                    prompt=[
                        item.get("content", "")
                        for item in segments
                        if item.get("type") == "prompt"
                    ],
                    source="LLM 自动多图",
                )
                try:
                    await event.send(event.plain_result(reason))
                except Exception as e:
                    logger.error(f"[ComfyUI] 发送权限拒绝提示失败: {e}")
                return

            # 冷却检查
            ok, remain = self._check_cooldown(event)
            if not ok:
                logger.info(f"[ComfyUI] 用户 {event.get_sender_id()} 冷却中")
                message = f"⏱️ 冷却中，请在 {remain} 秒后重试"
                self._remember_draw_failure(
                    event,
                    message,
                    prompt=[
                        item.get("content", "")
                        for item in segments
                        if item.get("type") == "prompt"
                    ],
                    source="LLM 自动多图",
                )
                try:
                    await event.send(event.plain_result(message))
                except Exception as e:
                    logger.error(f"[ComfyUI] 发送冷却提示失败: {e}")
                return

            # 敏感词预检所有 prompt
            for s in segments:
                if s["type"] == "prompt":
                    passed, sensitive = self._check_sensitive(s["content"], event)
                    if not passed:
                        tip = "、".join(sensitive[:3])
                        logger.warning(f"[ComfyUI] 多图模式触发敏感词: {tip}")
                        message = f"🚫 检测到敏感词：{tip}，无法生成图片"
                        self._remember_draw_failure(
                            event,
                            message,
                            prompt=s["content"],
                            source="LLM 自动多图",
                        )
                        try:
                            await event.send(event.plain_result(message))
                        except Exception as e:
                            logger.error(f"[ComfyUI] 发送敏感词提示失败: {e}")
                        return

            # 构建新的 chain：文字段 + 图片标记交替
            result = event.get_result()
            if not result:
                return

            new_chain = []
            img_idx = 0
            for segment in segments:
                if segment["type"] == "text":
                    new_chain.append(Plain(segment["content"]))
                elif segment["type"] == "prompt":
                    img_idx += 1
                    new_chain.append(
                        _ComfyImageMarker(
                            segment["content"],
                            img_idx,
                            segment.get("lora_selections", []),
                        )
                    )

            result.chain = new_chain
            event.set_extra("comfy_multi_image_mode", True)
            event.set_extra("comfy_multi_prompt_count", img_idx)
            logger.info(f"[ComfyUI] 📝 多图 chain 已构建: {len(new_chain)} 个元素, {img_idx} 张图片待生成")
            # → HtmlRender(priority=40) 渲染文字 → _send_multi_image_results(priority=10) 分组发送
            return

        # === 单图模式：文字先发，图片异步后发 ===
        prompt = getattr(event, "_comfy_extracted_prompt", None)
        if not prompt:
            return

        event._comfy_auto_painted = True

        # 权限检查
        allowed, reason = self._check_access(event)
        if not allowed:
            logger.warning(f"[ComfyUI] 单图请求被拒绝: {reason}")
            self._remember_draw_failure(
                event,
                reason,
                prompt=prompt,
                source="LLM 自动绘图",
            )
            try:
                await event.send(event.plain_result(reason))
            except Exception as e:
                logger.error(f"[ComfyUI] 发送权限拒绝提示失败: {e}")
            return

        # 敏感词检查
        passed, sensitive = self._check_sensitive(prompt, event)
        if not passed:
            tip = "、".join(sensitive[:5])
            logger.warning(f"[ComfyUI] 用户 {event.get_sender_id()} 触发敏感词: {tip}")
            message = f"🚫 检测到敏感词：{tip}，无法生成图片"
            self._remember_draw_failure(
                event,
                message,
                prompt=prompt,
                source="LLM 自动绘图",
            )
            try:
                await event.send(event.plain_result(message))
            except Exception as e:
                logger.error(f"[ComfyUI] 发送敏感词提示失败: {e}")
            return

        # 冷却检查
        ok, remain = self._check_cooldown(event)
        if not ok:
            logger.info(f"[ComfyUI] 用户 {event.get_sender_id()} 冷却中，图片跳过")
            message = f"⏱️ 冷却中，请在 {remain} 秒后重试"
            self._remember_draw_failure(
                event,
                message,
                prompt=prompt,
                source="LLM 自动绘图",
            )
            try:
                await event.send(event.plain_result(message))
            except Exception as e:
                logger.error(f"[ComfyUI] 发送冷却提示失败: {e}")
            return

        # 不修改 result.chain → 文字由框架/HtmlRender 正常发送
        # 图片异步生成后单独发送
        asyncio.create_task(
            self._send_image_async(
                event,
                prompt,
                getattr(event, "_comfy_extracted_loras", []),
            )
        )
    
    async def _send_image_async(self, event: AstrMessageEvent, prompt: str, lora_selections=None):
        """异步生成并发送图片（不阻塞文字消息发送）"""
        try:
            if not getattr(self, 'api', None):
                message = "❌ ComfyUI API 未初始化，无法生成图片"
                logger.error(f"[ComfyUI] {message}")
                self._remember_draw_failure(
                    event,
                    message,
                    prompt=prompt,
                    source="LLM 自动绘图",
                )
                return

            logger.info(f"[ComfyUI] 🎨 异步生成开始 | Prompt: {prompt[:50]}...")
            img_data, error_msg = await self.api.generate(prompt, lora_selections=lora_selections)

            if not img_data:
                message = f"❌ 图片生成失败：{error_msg}"
                logger.error(f"[ComfyUI] 异步生成失败: {error_msg}")
                self._remember_draw_failure(
                    event,
                    message,
                    prompt=prompt,
                    source="LLM 自动绘图",
                )
                try:
                    await event.send(event.plain_result(message))
                except Exception as e:
                    logger.error(f"[ComfyUI] 发送失败消息异常: {e}")
                return

            img_filename = f"{uuid.uuid4()}.png"
            img_path = self.output_dir / img_filename
            with open(img_path, 'wb') as fp:
                fp.write(img_data)

            logger.info(f"[ComfyUI] ✅ 异步图片已保存: {img_filename}")

            image_component = Image.fromFileSystem(str(img_path))
            await event.send(event.chain_result([image_component]))
            self._clear_draw_failures(event)
            logger.info(f"[ComfyUI] 📤 异步图片已发送: {img_filename}")

        except Exception as e:
            logger.error(f"[ComfyUI] 异步绘图异常: {e}")
            logger.error(traceback.format_exc())
            self._remember_draw_failure(
                event,
                f"❌ 异步绘图异常：{str(e)[:50]}",
                prompt=prompt,
                source="LLM 自动绘图",
            )
    @filter.on_decorating_result(priority=5)
    async def _cleanup_history_prompts(self, event: AstrMessageEvent):
        """在所有处理完成后，直接从对话历史中移除绘图提示词"""
        if not self.discard_prompt_from_history:
            return

        # 只在有提取到提示词时才需要清理
        has_prompt = hasattr(event, '_comfy_extracted_prompt') or hasattr(event, '_comfy_segments')
        if not has_prompt:
            return

        try:
            conv_mgr = self.context.conversation_manager
            unified_msg_origin = event.unified_msg_origin
            conv_id = await conv_mgr.get_curr_conversation_id(unified_msg_origin)

            if not conv_id:
                return

            conversation = await conv_mgr.get_conversation(unified_msg_origin, conv_id)
            if not conversation:
                return

            try:
                history = json.loads(conversation.history) if conversation.history else []
            except json.JSONDecodeError:
                return

            modified = False
            for entry in history:
                if entry.get("role") != "assistant":
                    continue
                content = str(entry.get("content", ""))
                cleaned = self._strip_comfy_control_tags(content, remove_think=True)
                if cleaned != content.strip():
                    entry["content"] = cleaned
                    modified = True

            if modified:
                await conv_mgr.update_conversation(
                    unified_msg_origin=unified_msg_origin,
                    conversation_id=conv_id,
                    history=history,
                )
                logger.info("[ComfyUI] 🗑️ 已从对话历史中清理绘图提示词")

        except Exception as e:
            logger.error(f"[ComfyUI] 清理历史记录失败: {e}")            
    @filter.on_decorating_result(priority=10)
    async def _send_multi_image_results(self, event: AstrMessageEvent):
        """多图模式 - 阶段2：在 HtmlRender 渲染完成后，分组发送"""
        if not event.get_extra("comfy_multi_image_mode"):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        prompt_count = event.get_extra("comfy_multi_prompt_count") or 0
        logger.info(f"[ComfyUI] 📤 多图发送阶段开始，chain 共 {len(result.chain)} 个元素")

        # 按 _ComfyImageMarker 分组：每组 = [渲染后的元素...] + 一个标记
        groups = []
        current_group = []

        for item in result.chain:
            if isinstance(item, _ComfyImageMarker):
                groups.append({"items": current_group, "marker": item})
                current_group = []
            else:
                current_group.append(item)

        # 最后一组（标记之后可能还有文字）
        if current_group:
            groups.append({"items": current_group, "marker": None})

        self._clear_draw_failures(event)

        # 逐组发送
        for group in groups:
            items = group["items"]
            marker = group["marker"]

            # 发送本组的渲染内容（文字/图片）
            if items:
                # 过滤空 Plain
                filtered = [it for it in items if not (isinstance(it, Plain) and not it.text.strip())]
                if filtered:
                    try:
                        await event.send(event.chain_result(filtered))
                        logger.info(f"[ComfyUI] 📤 文字段已发送 ({len(filtered)} 个元素)")
                    except Exception as e:
                        logger.error(f"[ComfyUI] 发送文字段失败: {e}")

            # 生成并发送图片
            if marker:
                try:
                    logger.info(f"[ComfyUI] 🎨 [{marker.index}/{prompt_count}] 开始生成: {marker.prompt[:50]}...")
                    img_data, error_msg = await self.api.generate(
                        marker.prompt,
                        lora_selections=marker.lora_selections,
                    )

                    if not img_data:
                        message = f"❌ [图片{marker.index}] 生成失败：{error_msg}"
                        logger.error(f"[ComfyUI] 图片 {marker.index} 生成失败: {error_msg}")
                        self._remember_draw_failure(
                            event,
                            message,
                            prompt=marker.prompt,
                            source=f"LLM 多图 图片{marker.index}",
                            append=True,
                        )
                        try:
                            await event.send(event.plain_result(message))
                        except:
                            pass
                        continue

                    img_filename = f"{uuid.uuid4()}.png"
                    img_path = self.output_dir / img_filename
                    with open(img_path, 'wb') as fp:
                        fp.write(img_data)

                    await event.send(event.chain_result([Image.fromFileSystem(str(img_path))]))
                    logger.info(f"[ComfyUI] ✅ [{marker.index}/{prompt_count}] 图片已发送: {img_filename}")

                except Exception as e:
                    logger.error(f"[ComfyUI] 图片 {marker.index} 处理异常: {e}")
                    logger.error(traceback.format_exc())
                    self._remember_draw_failure(
                        event,
                        f"❌ [图片{marker.index}] 处理异常：{str(e)[:50]}",
                        prompt=marker.prompt,
                        source=f"LLM 多图 图片{marker.index}",
                        append=True,
                    )

        # 清空 chain，防止框架重复发送
        result.chain.clear()
        logger.info(f"[ComfyUI] ✅ 多图模式发送完成")
    @llm_tool(name="comfyui_txt2img")
    async def comfyui_txt2img(self, event: AstrMessageEvent, ctx: Context = None, prompt: str = None, text: str = None, img_width: int = None, img_height: int = None, direct_send: bool = False) -> MessageEventResult:
        """ComfyUI 文生图工具。

        Args:
            prompt(string): Stable Diffusion / Danbooru 风格英文绘图提示词。
            text(string): prompt 的备用字段，和 prompt 二选一即可。
            img_width(number): 可选图片宽度。
            img_height(number): 可选图片高度。
            direct_send(boolean): 是否直接发送图片。
        """
        if not hasattr(event, "get_extra") and hasattr(event, "context"):
            event = event.context.event

        draw_source = event.get_extra("comfy_draw_source") or "LLM 工具"
        
        # 权限检查
        allowed, reason = self._check_access(event)
        if not allowed:
            self._remember_draw_failure(
                event,
                reason,
                prompt=prompt or text or self._extract_command_prompt(event),
                source=draw_source,
            )
            yield reason  # 以字符串形式返回，让 AI 能在 tool 上下文中看到拒绝原因
            return

        # 参数处理
        if not prompt and text:
            prompt = text

        if not prompt:
            message = "❌ 未提供 prompt，请重试"
            self._remember_draw_failure(event, message, source=draw_source)
            yield message
            return

        if not isinstance(prompt, str) or not prompt.strip():
            raw = getattr(event, "message_str", "") or ""
            prompt = re.sub(r'```math\s*At:\d+```\s*', '', raw).strip()
            if not prompt:
                message = "❌ 请输入提示词"
                self._remember_draw_failure(event, message, source=draw_source)
                yield message
                return

        # API 检查
        if not getattr(self, 'api', None):
            message = "❌ ComfyUI 服务未连接，请检查配置"
            self._remember_draw_failure(
                event,
                message,
                prompt=prompt,
                source=draw_source,
            )
            yield message
            return
        
        try:
            # 敏感词检查
            prompt, lora_selections = self._extract_lora_control_tags(prompt)
            passed, sensitive = self._check_sensitive(prompt, event)
            if not passed:
                tip = "、".join(sensitive[:5])
                logger.warning(f"[ComfyUI] 用户 {event.get_sender_id()} 触发敏感词: {tip}")
                message = f"🚫 检测到敏感词：{tip}，无法生成"
                self._remember_draw_failure(
                    event,
                    message,
                    prompt=prompt,
                    source=draw_source,
                )
                yield message
                return

            # 冷却检查
            ok, remain = self._check_cooldown(event)
            if not ok:
                message = f"⏱️ 冷却中，请在 {remain} 秒后重试"
                self._remember_draw_failure(
                    event,
                    message,
                    prompt=prompt,
                    source=draw_source,
                )
                yield message
                return

            logger.info(f"[ComfyUI] 🎨 开始生成 | 用户: {event.get_sender_id()} | Prompt: {prompt[:50]}...")

            # 调用 API
            img_data, error_msg = await self.api.generate(prompt, lora_selections=lora_selections)

            if not img_data:
                logger.error(f"[ComfyUI] 生成失败: {error_msg}")
                message = f"❌ 生成失败：{error_msg}"
                self._remember_draw_failure(
                    event,
                    message,
                    prompt=prompt,
                    source=draw_source,
                )
                yield message
                return

            # 保存图片
            img_filename = f"{uuid.uuid4()}.png"
            img_path = self.output_dir / img_filename
            with open(img_path, 'wb') as fp:
                fp.write(img_data)
            
            logger.info(f"[ComfyUI] ✅ 图片已保存: {img_filename}")
            self._clear_draw_failures(event)

            platform_name = ""
            try:
                platform_name = (event.get_platform_name() or "").lower()
            except Exception:
                platform_name = ""
            if not direct_send and platform_name == "telegram":
                logger.info("[ComfyUI] Telegram does not support forward Node; using direct image send")
                direct_send = True

            # 发送结果
            if direct_send:
                if platform_name == "telegram":
                    try:
                        await self._send_telegram_image_file(event, img_path)
                    except Exception as send_error:
                        if self._is_telegram_timeout_error(send_error):
                            logger.warning(
                                f"[ComfyUI] Telegram image send timed out after upload; treating as non-retry success: {img_filename}"
                            )
                            yield (
                                "image generated; Telegram send_photo timed out while waiting for response. "
                                f"The image may already be delivered. Do not retry automatically: {img_filename}"
                            )
                            return
                        raise
                else:
                    image_component = Image.fromFileSystem(str(img_path))
                    await event.send(event.chain_result([image_component]))
            else:
                self_id = self._get_self_id(event) or "0"
                image_component = Image.fromFileSystem(str(img_path))
                # 部分平台 self_id 非纯数字（如 webchat 为 "webchat"），用 hash 兜底
                try:
                    numeric_uid = int(self_id)
                except ValueError:
                    numeric_uid = abs(hash(self_id)) % (10**9)
                forward_node = Node(
                    user_id=numeric_uid,
                    nickname="ComfyUI",
                    content=[image_component]
                )
                await event.send(event.chain_result([forward_node]))

            yield f"image generated and sent to user: {img_filename}"
            return

        except Exception as e:
            logger.error(f"[ComfyUI] 执行异常: {e}")
            logger.error(traceback.format_exc())
            message = f"❌ 内部错误: {str(e)[:50]}"
            self._remember_draw_failure(
                event,
                message,
                prompt=prompt,
                source=draw_source,
            )
            yield message
