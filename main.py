import os
import uuid
import time
import re
import traceback
import json
import shutil
import asyncio
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import *
from astrbot.api import llm_tool, logger, AstrBotConfig
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
    def __init__(self, prompt: str, index: int):
        self.prompt = prompt
        self.index = index

@register(
    "astrbot_plugin_comfyui_pro",
    "lumingya",
    "ComfyUI Pro 连接器",
    "2.4.0",
    "https://github.com/lumingya/astrbot_plugin_comfyui_pro"
)
class ComfyUIPlugin(Star):
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
        self.persona_dir = self.data_dir / "persona"
        self.profiles_dir = self.data_dir / "profiles"
        self.sensitive_words_path = self.data_dir / "sensitive_words.json"
        
        # ====== 4. 更新 UI 配置 ======
        self._auto_update_schema()
        
        # Control 配置
        control_conf = config.get("control", {})
        self.cooldown_seconds = control_conf.get("cooldown_seconds", 60)
        self.user_cooldowns = {}
        self.lockdown = bool(control_conf.get("lockdown", False))
        self.lockdown_command_enabled = bool(control_conf.get("lockdown_command_enabled", True))
        self.whitelist_group_ids = set(map(str, control_conf.get("whitelist_group_ids", [])))
    
        llm_settings = config.get("llm_settings", {})
        self.multi_image_mode = llm_settings.get("multi_image_mode", False)
        logger.info(f"[ComfyUI] 🖼️ 多图模式: {'开启' if self.multi_image_mode else '关闭'}")
        
        self.discard_prompt_from_history = llm_settings.get("discard_prompt_from_history", False)
        if self.discard_prompt_from_history:
            logger.info("[ComfyUI] 🗑️ 绘图提示词历史丢弃: 开启")       
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
        group_count = len(self.whitelist_group_ids)
        logger.info(f"[ComfyUI] 👤 管理员: 使用 AstrBot 系统管理员 | 🏠 白名单群: {group_count} 个")
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

        # ====== 加载当前 persona 选择（持久化） ======
        self._persona_state_file = self.data_dir / ".current_persona.txt"
        self.current_persona = None
        if self._persona_state_file.exists():
            try:
                self.current_persona = self._persona_state_file.read_text(encoding="utf-8").strip()
                logger.info(f"[ComfyUI] 👤 当前 Persona (持久化): {self.current_persona}")
            except Exception:
                self.current_persona = None

        # 如果持久化记录不存在，回退到 WebUI 配置中的 persona_file
        if not self.current_persona:
            config_persona = config.get("llm_settings", {}).get("persona_file", "").strip()
            if config_persona:
                persona_path = self.persona_dir / config_persona
                if persona_path.exists():
                    self.current_persona = config_persona
                    logger.info(f"[ComfyUI] 👤 当前 Persona (配置): {self.current_persona}")
                else:
                    logger.warning(f"[ComfyUI] ⚠️ 配置的 Persona 文件不存在: {config_persona}")

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

        # ====== 加载当前 profile 选择（持久化） ======
        self._profile_state_file = self.data_dir / ".current_profile.txt"
        self.current_profile = None
        if self._profile_state_file.exists():
            try:
                self.current_profile = self._profile_state_file.read_text(encoding="utf-8").strip()
                logger.info(f"[ComfyUI] 📋 当前 Profile (持久化): {self.current_profile}")
                self._apply_current_profile()
            except Exception as e:
                logger.warning(f"[ComfyUI] 应用持久化 profile 失败: {e}")
                self.current_profile = None

        # 如果持久化记录不存在，回退到 WebUI 配置中的 profile_file
        if not self.current_profile:
            config_profile = config.get("profile_settings", {}).get("profile_file", "").strip()
            if config_profile:
                profile_path = self.profiles_dir / config_profile
                if profile_path.exists():
                    self.current_profile = config_profile
                    logger.info(f"[ComfyUI] 📋 当前 Profile (配置): {self.current_profile}")
                    self._apply_current_profile()
                else:
                    logger.warning(f"[ComfyUI] ⚠️ 配置的 Profile 文件不存在: {config_profile}")

        # ====== 检测 WebUI 保存请求 ======
        save_name = config.get("profile_settings", {}).get("save_profile_name", "").strip()
        if save_name:
            self._save_profile_from_config(save_name)

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

        # 复制默认 persona
        persona_dir = self.data_dir / "persona"
        persona_dir.mkdir(exist_ok=True)
        plugin_persona_dir = PLUGIN_DIR / "persona"
        persona_copied = 0
        if plugin_persona_dir.exists():
            for src_file in plugin_persona_dir.glob("*.txt"):
                dst_file = persona_dir / src_file.name
                if not dst_file.exists():
                    try:
                        shutil.copy2(src_file, dst_file)
                        persona_copied += 1
                    except Exception as e:
                        logger.error(f"[ComfyUI] 复制 persona 失败 {src_file.name}: {e}")
        if persona_copied > 0:
            logger.info(f"[ComfyUI] 📋 已复制 {persona_copied} 个默认 Persona")

        # 复制默认 profiles
        profiles_dir = self.data_dir / "profiles"
        profiles_dir.mkdir(exist_ok=True)
        plugin_profiles_dir = PLUGIN_DIR / "profiles"
        profiles_copied = 0
        if plugin_profiles_dir.exists():
            for src_file in plugin_profiles_dir.glob("*.json"):
                dst_file = profiles_dir / src_file.name
                if not dst_file.exists():
                    try:
                        shutil.copy2(src_file, dst_file)
                        profiles_copied += 1
                    except Exception as e:
                        logger.error(f"[ComfyUI] 复制 profile 失败 {src_file.name}: {e}")
        if profiles_copied > 0:
            logger.info(f"[ComfyUI] 📋 已复制 {profiles_copied} 个默认 Profile")

    # ====== 更新 Schema ======
    def _auto_update_schema(self):
        """扫描持久化目录的工作流和 persona，更新 UI 下拉列表"""
        try:
            schema_path = PLUGIN_DIR / '_conf_schema.json'
            workflow_dir = self.data_dir / 'workflow'

            with open(schema_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            modified = False

            # === 工作流列表 ===
            if workflow_dir.exists():
                files = sorted([
                    f.name for f in workflow_dir.glob("*.json")
                    if not f.name.endswith(".steps.json")
                ])
                if not files:
                    files = ["workflow_api.json"]

                target = data['workflow_settings']['items']['json_file']
                target['options'] = files
                target['enum'] = files
                modified = True
                logger.info(f"[ComfyUI] 🔄 工作流列表已更新: {len(files)} 个可用")

            # === Persona 列表 ===
            persona_dir = self.data_dir / 'persona'
            if persona_dir.exists():
                persona_files = sorted([f.name for f in persona_dir.glob("*.txt")])
                target = data['llm_settings']['items'].get('persona_file')
                if target:
                    target['options'] = persona_files
                    target['enum'] = persona_files
                    modified = True
                    logger.info(f"[ComfyUI] 🔄 Persona 列表已更新: {len(persona_files)} 个可用")

            # === Profile 列表 ===
            profiles_dir = self.data_dir / 'profiles'
            if profiles_dir.exists():
                profile_files = sorted([f.name for f in profiles_dir.glob("*.json")])
                target = data['profile_settings']['items'].get('profile_file')
                if target:
                    target['options'] = profile_files
                    target['enum'] = profile_files
                    modified = True
                    logger.info(f"[ComfyUI] 🔄 Profile 列表已更新: {len(profile_files)} 个可用")

            if modified:
                with open(schema_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[ComfyUI] 更新 UI 列表失败: {e}")

    # ====== 权限检查（返回原因）======
    def _check_access(self, event: AstrMessageEvent) -> tuple:
        """
        统一的权限检查，返回 (是否通过, 拒绝原因)
        """
        is_admin = self._is_admin(event)
        
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
        is_admin = self._is_admin(event)
        
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
        is_admin = self._is_admin(event)
        
        sensitive = self._find_sensitive_words(prompt, event)
        
        if not sensitive:
            return True, []
        
        # 管理员绕过
        if is_admin and self.admin_bypass_sensitive:
            logger.info(f"[ComfyUI] 👑 管理员 {user_id} 使用敏感词 {sensitive}，已放行")
            return True, []
        
        return False, sensitive

    # ====== 管理员判断（使用 AstrBot 系统管理员） ======
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查用户是否为 AstrBot 全局管理员，使用 AstrBot 内置管理员系统"""
        try:
            return event.is_admin()
        except AttributeError:
            return getattr(event, 'role', None) == 'admin'

    @filter.on_llm_request(priority=100)
    async def inject_system_prompt(self, event: AstrMessageEvent, req):
        """注入系统提示词 + 清理历史中的绘图提示词"""
        try:
            # 优先从当前 persona 文件读取
            my_prompt = self._get_current_persona_text()

            # 如果 persona 为空，回退到配置中的 system_prompt（兼容旧方式）
            if not my_prompt:
                llm_settings = self.config.get("llm_settings", {})
                my_prompt = llm_settings.get("system_prompt", "")

            if my_prompt:
                current_prompt = getattr(req, "system_prompt", "") or ""
                if my_prompt not in current_prompt:
                    if current_prompt:
                        req.system_prompt = f"{current_prompt}\n\n{my_prompt}".strip()
                    else:
                        req.system_prompt = my_prompt.strip()

        except Exception as e:
            logger.error(f"[ComfyUI] 注入提示词异常: {e}")

        # 清理历史中的绘图提示词
        if self.discard_prompt_from_history:
            try:
                self._clean_pic_tags_from_req(req)
            except Exception as e:
                logger.error(f"[ComfyUI] 清理提示词异常: {e}")

    def _clean_pic_tags_from_req(self, req):
        """从请求的 conversation.history 中清理 <pic> 标签"""
        pic_pattern = re.compile(r'<pic\s+prompt=".*?">', re.DOTALL)

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
            if isinstance(content, str) and pic_pattern.search(content):
                entry["content"] = pic_pattern.sub("", content).strip()
                cleaned += 1

        if cleaned:
            # 写回 conversation.history
            conversation.history = json.dumps(history, ensure_ascii=False)
            logger.info(f"[ComfyUI] 🗑️ 已从 conversation.history 中清理 {cleaned} 条消息的绘图提示词")

    def _get_current_persona_text(self) -> str:
        """读取当前 persona 文件的文本内容"""
        if self.current_persona:
            persona_path = self.persona_dir / self.current_persona
            if persona_path.exists():
                try:
                    return persona_path.read_text(encoding="utf-8").strip()
                except Exception as e:
                    logger.warning(f"[ComfyUI] 读取 persona 文件失败: {e}")
        return ""

    # ====== Profile 辅助方法 ======
    def _load_profile_data(self, profile_name: str) -> dict:
        """加载指定 profile 文件的配置数据"""
        profile_path = self.profiles_dir / profile_name
        if not profile_path.exists():
            return {}
        try:
            with open(profile_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[ComfyUI] 读取 Profile 失败 {profile_name}: {e}")
            return {}

    def _apply_current_profile(self) -> list:
        """将当前 profile 的配置应用到各组件，返回应用结果列表"""
        if not self.current_profile:
            return []
        data = self._load_profile_data(self.current_profile)
        if not data:
            return [f"⚠️ Profile 文件为空或读取失败: {self.current_profile}"]
        return self._do_apply_profile(data)

    def _do_apply_profile(self, data: dict) -> list:
        """执行 profile 配置的应用"""
        results = []

        # 1. 切换工作流 + 节点 ID
        wf = data.get("workflow", "")
        if wf and self.api:
            try:
                exists, msg = self.api.reload_config(
                    wf,
                    input_id=data.get("input_node_id"),
                    output_id=data.get("output_node_id"),
                    neg_node_id=data.get("neg_node_id"),
                )
                results.append(f"{'✅' if exists else '⚠️'} 工作流: {wf} ({msg.split(chr(10))[0]})")
            except Exception as e:
                results.append(f"❌ 工作流切换失败: {e}")
        elif wf:
            results.append(f"⚠️ API未初始化，跳过工作流: {wf}")

        # 2. 切换 Persona
        persona = data.get("persona", "")
        if persona:
            persona_path = self.persona_dir / persona
            if persona_path.exists():
                self.current_persona = persona
                try:
                    self._persona_state_file.write_text(persona, encoding="utf-8")
                except Exception:
                    pass
                results.append(f"✅ Persona: {persona}")
            else:
                results.append(f"⚠️ Persona 文件不存在: {persona}")

        return results

    def _save_profile_from_config(self, name: str):
        """从当前 WebUI 配置保存为 Profile 文件（由 save_profile_name 字段触发）"""
        if not name.endswith(".json"):
            name += ".json"

        wf_settings = self.config.get("workflow_settings", {})
        data = {
            "workflow": wf_settings.get("json_file", ""),
            "input_node_id": str(wf_settings.get("input_node_id", "")),
            "neg_node_id": str(wf_settings.get("neg_node_id", "")),
            "output_node_id": str(wf_settings.get("output_node_id", "")),
            "persona": self.config.get("llm_settings", {}).get("persona_file", ""),
        }

        save_path = self.profiles_dir / name
        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            self._auto_update_schema()
            logger.info(f"[ComfyUI] 📋 已从 WebUI 保存 Profile: {name}")
        except Exception as e:
            logger.error(f"[ComfyUI] ❌ 从 WebUI 保存 Profile 失败: {e}")
            return

        # 尝试清除 save_profile_name 字段
        try:
            if isinstance(self.config, AstrBotConfig):
                self.config["profile_settings"]["save_profile_name"] = ""
                self.config.save_config()
                logger.info("[ComfyUI] 📋 save_profile_name 已自动清空")
                return
        except Exception:
            pass

        # 备用方案：直接写配置 JSON 文件
        try:
            config_dir = Path.cwd() / "data" / "config"
            config_path = config_dir / "astrbot_plugin_comfyui_pro_config.json"
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                if "profile_settings" in cfg:
                    cfg["profile_settings"]["save_profile_name"] = ""
                    with open(config_path, 'w', encoding='utf-8') as f:
                        json.dump(cfg, f, indent=2, ensure_ascii=False)
                    logger.info("[ComfyUI] 📋 save_profile_name 已通过配置文件清除")
        except Exception as e:
            logger.debug(f"[ComfyUI] 自动清除 save_profile_name 失败（非致命）: {e}")

    async def initialize(self):
        self.context.activate_llm_tool("comfyui_txt2img")
        logger.info("[ComfyUI] 🎨 插件初始化完成，LLM 工具已激活")

    # ====== 核心绘图逻辑 ======
    async def _handle_paint_logic(self, event: AstrMessageEvent, direct_send: bool):
        """处理画图的核心逻辑"""
        # 权限检查
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        
        try:
            full_message = event.message_str.strip()
            parts = full_message.split(' ', 1)
            prompt = parts[1].strip() if len(parts) > 1 else ""

            if not prompt:
                yield event.plain_result("❌ 请输入提示词，例如：/画图 1girl, smile")
                return

            # 敏感词检查
            passed, sensitive = self._check_sensitive(prompt, event)
            if not passed:
                tip = "、".join(sensitive[:5])  # 最多显示5个
                extra = f"等 {len(sensitive)} 个" if len(sensitive) > 5 else ""
                yield event.plain_result(f"🚫 检测到敏感词：{tip}{extra}，无法生成图片")
                return

            async for result in self.comfyui_txt2img(event, prompt=prompt, direct_send=direct_send):
                yield result
                
        except Exception as e:
            logger.error(f"[ComfyUI] 绘图异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 执行出错：{str(e)[:50]}")
    # ===== 探针：测试 event.send() 是否触发 on_decorating_result =====
    @filter.command("comfy_probe_send")
    async def cmd_probe_send(self, event: AstrMessageEvent):
        """探测 event.send() 是否会触发 on_decorating_result"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(event):
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
        is_admin = self._is_admin(event)
        
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
                "  /comfy_prompt_ls       列出 Persona 提示词",
                "  /comfy_prompt_use <序号> 切换 Persona 提示词",
                "  /comfy_profile_ls      列出配置档案",
                "  /comfy_profile_use <序号> 一键应用配置档案",
                "  /comfy_profile_save <名> 保存当前配置为档案",
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
            tips.append(f"👤 当前 Persona：{self.current_persona or '无（使用配置默认值）'}")
            tips.append(f"📋 当前 Profile：{self.current_profile or '无'}")
        
        yield event.plain_result("\n".join(tips))
    @filter.command("comfy_test_send2")
    async def cmd_test_send2(self, event: AstrMessageEvent):
        """测试主动发送 - 第二轮"""
    
        user_id = str(event.get_sender_id())
        if not self._is_admin(event):
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
        if not self._is_admin(event):
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
        if not self._is_admin(event):
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
        if not self._is_admin(event):
            yield event.plain_result("🚫 权限不足，仅管理员可查看工作流列表")
            return

        if not self.workflow_dir.exists():
            yield event.plain_result("❌ 工作流目录不存在")
            return

        # 排除 .steps.json 文件
        files = sorted([
            f.name for f in self.workflow_dir.glob("*.json") 
            if not f.name.endswith(".steps.json")
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
        if not self._is_admin(event):
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
                if not f.name.endswith(".steps.json")
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

    # ====== Persona 管理 ======
    @filter.command("comfy_prompt_ls")
    async def cmd_comfy_prompt_list(self, event: AstrMessageEvent):
        """列出所有可用 Persona 提示词文件"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(event):
            yield event.plain_result("🚫 权限不足，仅管理员可查看 Persona 列表")
            return

        if not self.persona_dir.exists():
            yield event.plain_result("❌ Persona 目录不存在")
            return

        files = sorted([f.name for f in self.persona_dir.glob("*.txt")])

        if not files:
            yield event.plain_result("📂 目录中没有 Persona 文件\n请将 .txt 文件放入 persona 目录")
            return

        current = self.current_persona or "无（使用配置默认值）"

        msg = ["📂 Persona 提示词列表", "━━━━━━━━━━━━━━━━━━"]

        for i, f in enumerate(files, 1):
            try:
                content = (self.persona_dir / f).read_text(encoding="utf-8")
                first_line = content.split('\n')[0][:50] or "(空文件)"
            except Exception:
                first_line = "(读取失败)"
            marker = "✅" if f == self.current_persona else "  "
            msg.append(f"{marker} {i}. {f}  —— {first_line}")

        msg.append("")
        msg.append("━━━━━━━━━━━━━━━━━━")
        msg.append(f"📍 当前: {current}")
        msg.append("切换：/comfy_prompt_use <序号>")

        yield event.plain_result("\n".join(msg))

    @filter.command("comfy_prompt_use")
    async def cmd_comfy_prompt_use(self, event: AstrMessageEvent):
        """切换 Persona 提示词文件"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(event):
            yield event.plain_result("🚫 权限不足，仅管理员可切换 Persona")
            return

        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "❌ 参数不足\n用法：/comfy_prompt_use <序号>"
            )
            return

        files = sorted([f.name for f in self.persona_dir.glob("*.txt")])
        if not files:
            yield event.plain_result("📂 目录中没有 Persona 文件")
            return

        try:
            index = int(args[1])
            if not (1 <= index <= len(files)):
                yield event.plain_result(f"❌ 序号错误，请输入 1 到 {len(files)} 之间的数字")
                return
            filename = files[index - 1]
        except ValueError:
            yield event.plain_result("❌ 请输入有效的数字序号")
            return

        # 持久化当前选择
        try:
            self._persona_state_file.write_text(filename, encoding="utf-8")
        except Exception as e:
            yield event.plain_result(f"❌ 保存状态失败: {e}")
            return

        self.current_persona = filename
        logger.info(f"[ComfyUI] 管理员 {user_id} 切换 Persona: {filename}")
        yield event.plain_result(
            f"✅ 已切换 Persona 为：{filename}\n"
            f"下次 LLM 请求时生效"
        )

    # ====== Profile 管理 ======
    @filter.command("comfy_profile_ls")
    async def cmd_comfy_profile_list(self, event: AstrMessageEvent):
        """列出所有配置档案 (Profile)"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(event):
            yield event.plain_result("🚫 权限不足，仅管理员可查看 Profile 列表")
            return

        if not self.profiles_dir.exists():
            yield event.plain_result("❌ Profile 目录不存在")
            return

        files = sorted([f.name for f in self.profiles_dir.glob("*.json")])

        if not files:
            yield event.plain_result("📂 目录中没有 Profile 文件\n请将 .json 文件放入 profiles 目录，或使用 /comfy_profile_save 创建")
            return

        current = self.current_profile or "无"

        msg = ["📂 配置档案 (Profile) 列表", "━━━━━━━━━━━━━━━━━━"]

        for i, f in enumerate(files, 1):
            data = self._load_profile_data(f)
            wf = data.get("workflow", "?")
            persona = data.get("persona", "?")
            in_id = data.get("input_node_id", "?")
            marker = "✅" if f == self.current_profile else "  "
            msg.append(f"{marker} {i}. {f}")
            msg.append(f"     └ 工作流: {wf} | Pos:{in_id} | Neg:{data.get('neg_node_id','?')} | Out:{data.get('output_node_id','?') or '自动'} | Persona:{persona}")

        msg.append("")
        msg.append("━━━━━━━━━━━━━━━━━━")
        msg.append(f"📍 当前: {current}")
        msg.append("切换：/comfy_profile_use <序号>")
        msg.append("保存当前配置为 Profile：/comfy_profile_save <名称>")

        yield event.plain_result("\n".join(msg))

    @filter.command("comfy_profile_use")
    async def cmd_comfy_profile_use(self, event: AstrMessageEvent):
        """一键切换配置档案（工作流+节点ID+Persona）"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(event):
            yield event.plain_result("🚫 权限不足，仅管理员可切换 Profile")
            return

        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "❌ 参数不足\n用法：/comfy_profile_use <序号>"
            )
            return

        files = sorted([f.name for f in self.profiles_dir.glob("*.json")])
        if not files:
            yield event.plain_result("📂 目录中没有 Profile 文件")
            return

        try:
            index = int(args[1])
            if not (1 <= index <= len(files)):
                yield event.plain_result(f"❌ 序号错误，请输入 1 到 {len(files)} 之间的数字")
                return
            filename = files[index - 1]
        except ValueError:
            yield event.plain_result("❌ 请输入有效的数字序号")
            return

        # 加载并应用 profile
        data = self._load_profile_data(filename)
        if not data:
            yield event.plain_result(f"❌ 无法读取 Profile 文件: {filename}")
            return

        results = self._do_apply_profile(data)

        # 持久化
        try:
            self._profile_state_file.write_text(filename, encoding="utf-8")
        except Exception as e:
            yield event.plain_result(f"❌ 保存状态失败: {e}")
            return

        self.current_profile = filename
        logger.info(f"[ComfyUI] 📋 管理员 {user_id} 切换 Profile: {filename}")

        msg = ["✅ 已应用配置档案\n", f"📋 Profile: {filename}", "━━━━━━━━━━━━━━━━━━"]
        msg.extend(results)
        yield event.plain_result("\n".join(msg))

    @filter.command("comfy_profile_save")
    async def cmd_comfy_profile_save(self, event: AstrMessageEvent):
        """将当前配置（工作流+节点ID+Persona）保存为 Profile"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(event):
            yield event.plain_result("🚫 权限不足，仅管理员可保存 Profile")
            return

        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "❌ 参数不足\n用法：/comfy_profile_save <名称>\n示例：/comfy_profile_save 爱丽丝-3d"
            )
            return

        name = args[1]
        if not name.endswith(".json"):
            name += ".json"

        # 收集当前配置
        wf = self.api.wf_filename if self.api else ""
        data = {
            "workflow": wf,
            "input_node_id": self.api.input_id if self.api else "",
            "neg_node_id": self.api.neg_node_id if self.api else "",
            "output_node_id": self.api.output_id if self.api else "",
            "persona": self.current_persona or "",
        }

        save_path = self.profiles_dir / name
        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            self._auto_update_schema()
            logger.info(f"[ComfyUI] 管理员 {user_id} 保存 Profile: {name}")
            yield event.plain_result(
                f"✅ 已保存配置档案: {name}\n"
                f"工作流: {wf}\n"
                f"Persona: {self.current_persona or '无'}"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 保存失败: {e}")

    @filter.command("comfy_save")
    async def cmd_comfy_save(self, event: AstrMessageEvent):
        """保存/导入工作流"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(event):
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
        if not self._is_admin(event):
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
    
        # 提取所有 <pic prompt="...">
        prompts = re.findall(r'<pic\s+prompt="(.*?)">', full_text, flags=re.DOTALL)
    
        if not prompts:
            return

        # 清理文本供其他插件使用（移除 <pic>、<think>、<ctx> 标签）
        cleaned_text = re.sub(r'<pic\s+prompt=".*?">', '', full_text, flags=re.DOTALL)
        cleaned_text = re.sub(r'<think>.*?</think>', '', cleaned_text, flags=re.DOTALL)
        cleaned_text = re.sub(r'</?ctx>', '', cleaned_text)
        cleaned_text = cleaned_text.strip()
        event.set_extra("comfy_cleaned_text", cleaned_text)
    
        # 清理提示词内容
        cleaned_prompts = []
        
        placeholder_patterns = [
            r'^\.{2,}$',
            r'^…+$',
            r'^[.。]+$',
            r'^[xX]{2,}$',
            r'^[-_=]{2,}$',
            r'^\[.*?\]$',
            r'^\{.*?\}$',
        ]
        
        for p in prompts:
            p = re.sub(r'^提示词是\s*[:：]?\s*', '', p).strip()
            p = p.strip('`"\'""''').strip()
            if not p:
                continue
            if len(p) < 3:
                logger.debug(f"[ComfyUI] 跳过过短提示词: '{p}'")
                continue
            is_placeholder = any(re.match(pat, p) for pat in placeholder_patterns)
            if is_placeholder:
                logger.debug(f"[ComfyUI] 跳过占位符提示词: '{p}'")
                continue
            cleaned_prompts.append(p)

        if not cleaned_prompts:
            return
    
        # 单图模式
        if len(cleaned_prompts) == 1:
            event._comfy_extracted_prompt = cleaned_prompts[0]
            logger.info(f"[ComfyUI] 📝 检测到单图模式: {cleaned_prompts[0][:50]}...")
            # 丢弃绘图提示词，避免污染历史记录上下文
            if self.discard_prompt_from_history:
                resp.completion_text = cleaned_text
                resp.result_chain = MessageChain().message(cleaned_text)
                logger.info("[ComfyUI] 🗑️ 已从历史记录中移除绘图提示词")
            return
    
        # 多图模式
        if self.multi_image_mode:
            parts = re.split(r'<pic\s+prompt=".*?">', full_text, flags=re.DOTALL)
        
            # 检测原始文本中的 <render> 标签信息，用于补全被切割的段落
            render_match = re.search(r'<render\b[^>]*>', full_text)
            render_open_tag = render_match.group(0) if render_match else None
            render_close_tag = "</render>" if render_open_tag else None

            segments = []
            prompt_idx = 0
        
            for i, text in enumerate(parts):
                text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
                text = re.sub(r'</?ctx>', '', text)
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
                    segments.append({"type": "prompt", "content": cleaned_prompts[prompt_idx]})
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
        if segments and self.multi_image_mode:
            event._comfy_auto_painted = True

            # 权限检查
            allowed, reason = self._check_access(event)
            if not allowed:
                logger.warning(f"[ComfyUI] 多图请求被拒绝: {reason}")
                try:
                    await event.send(event.plain_result(reason))
                except Exception as e:
                    logger.error(f"[ComfyUI] 发送权限拒绝提示失败: {e}")
                return

            # 冷却检查
            ok, remain = self._check_cooldown(event)
            if not ok:
                logger.info(f"[ComfyUI] 用户 {event.get_sender_id()} 冷却中")
                try:
                    await event.send(event.plain_result(f"⏱️ 冷却中，请在 {remain} 秒后重试"))
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
                        try:
                            await event.send(event.plain_result(f"🚫 检测到敏感词：{tip}，无法生成图片"))
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
                    new_chain.append(_ComfyImageMarker(segment["content"], img_idx))

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
            try:
                await event.send(event.plain_result(f"🚫 检测到敏感词：{tip}，无法生成图片"))
            except Exception as e:
                logger.error(f"[ComfyUI] 发送敏感词提示失败: {e}")
            return

        # 冷却检查
        ok, remain = self._check_cooldown(event)
        if not ok:
            logger.info(f"[ComfyUI] 用户 {event.get_sender_id()} 冷却中，图片跳过")
            try:
                await event.send(event.plain_result(f"⏱️ 冷却中，请在 {remain} 秒后重试"))
            except Exception as e:
                logger.error(f"[ComfyUI] 发送冷却提示失败: {e}")
            return

        # 不修改 result.chain → 文字由框架/HtmlRender 正常发送
        # 图片异步生成后单独发送
        asyncio.create_task(self._send_image_async(event, prompt))
    
    async def _send_image_async(self, event: AstrMessageEvent, prompt: str):
        """异步生成并发送图片（不阻塞文字消息发送）"""
        try:
            if not getattr(self, 'api', None):
                logger.error("[ComfyUI] API 未初始化，无法生成图片")
                return

            logger.info(f"[ComfyUI] 🎨 异步生成开始 | Prompt: {prompt[:50]}...")
            img_data, error_msg = await self.api.generate(prompt)

            if not img_data:
                logger.error(f"[ComfyUI] 异步生成失败: {error_msg}")
                try:
                    await event.send(event.plain_result(f"❌ 图片生成失败：{error_msg}"))
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
            logger.info(f"[ComfyUI] 📤 异步图片已发送: {img_filename}")

        except Exception as e:
            logger.error(f"[ComfyUI] 异步绘图异常: {e}")
            logger.error(traceback.format_exc())
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
                cleaned = re.sub(r'<pic\s+prompt=".*?">', '', content, flags=re.DOTALL)
                if cleaned != content:
                    entry["content"] = cleaned.strip()
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
                    img_data, error_msg = await self.api.generate(marker.prompt)

                    if not img_data:
                        logger.error(f"[ComfyUI] 图片 {marker.index} 生成失败: {error_msg}")
                        try:
                            await event.send(event.plain_result(f"❌ [图片{marker.index}] 生成失败：{error_msg}"))
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

        # 清空 chain，防止框架重复发送
        result.chain.clear()
        logger.info(f"[ComfyUI] ✅ 多图模式发送完成")
    @llm_tool(name="comfyui_txt2img")
    async def comfyui_txt2img(self, event: AstrMessageEvent, ctx: Context = None, prompt: str = None, text: str = None, img_width: int = None, img_height: int = None, direct_send: bool = False) -> MessageEventResult:
        """ComfyUI 文生图工具"""
        
        # 权限检查
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        # 参数处理
        if not prompt and text:
            prompt = text

        if not prompt:
            yield event.plain_result("❌ 未提供 prompt，请重试")
            return

        if not isinstance(prompt, str) or not prompt.strip():
            raw = getattr(event, "message_str", "") or ""
            prompt = re.sub(r'```math\s*At:\d+```\s*', '', raw).strip()
            if not prompt:
                yield event.plain_result("❌ 请输入提示词")
                return

        # API 检查
        if not getattr(self, 'api', None):
            yield event.plain_result("❌ ComfyUI 服务未连接，请检查配置")
            return
        
        try:
            # 敏感词检查
            passed, sensitive = self._check_sensitive(prompt, event)
            if not passed:
                tip = "、".join(sensitive[:5])
                logger.warning(f"[ComfyUI] 用户 {event.get_sender_id()} 触发敏感词: {tip}")
                yield event.plain_result(f"🚫 检测到敏感词：{tip}，无法生成")
                return

            # 冷却检查
            ok, remain = self._check_cooldown(event)
            if not ok:
                yield event.plain_result(f"⏱️ 冷却中，请在 {remain} 秒后重试")
                return

            logger.info(f"[ComfyUI] 🎨 开始生成 | 用户: {event.get_sender_id()} | Prompt: {prompt[:50]}...")

            # 调用 API
            img_data, error_msg = await self.api.generate(prompt)

            if not img_data:
                logger.error(f"[ComfyUI] 生成失败: {error_msg}")
                yield event.plain_result(f"❌ 生成失败：{error_msg}")
                return

            # 保存图片
            img_filename = f"{uuid.uuid4()}.png"
            img_path = self.output_dir / img_filename
            with open(img_path, 'wb') as fp:
                fp.write(img_data)
            
            logger.info(f"[ComfyUI] ✅ 图片已保存: {img_filename}")

            # 发送结果
            if direct_send:
                image_component = Image.fromFileSystem(str(img_path))
                yield event.chain_result([image_component])
            else:
                self_id = self._get_self_id(event) or "0"
                image_component = Image.fromFileSystem(str(img_path))
                forward_node = Node(
                    user_id=int(self_id),
                    nickname="ComfyUI",
                    content=[image_component]
                )
                yield event.chain_result([forward_node])

        except Exception as e:
            logger.error(f"[ComfyUI] 执行异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 内部错误: {str(e)[:50]}")