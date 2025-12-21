import os
import uuid
import time
import re
import traceback
import json
import shutil
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import *
from astrbot.api import llm_tool, logger
from astrbot.api.provider import LLMResponse

# 尝试导入 StarTools（兼容不同版本）
try:
    from astrbot.api.star import StarTools
    HAS_STAR_TOOLS = True
except ImportError:
    HAS_STAR_TOOLS = False
    logger.warning("[ComfyUI] 无法导入 StarTools，将使用备用目录方案")

# 获取插件目录（用于读取默认文件）
PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


@register(
    "astrbot_plugin_comfyui_pro",  
    "lumingya",                    
    "ComfyUI Pro 连接器",           
    "1.2.0",
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
        self.sensitive_words_path = self.data_dir / "sensitive_words.json"
        
        # ====== 4. 更新 UI 配置 ======
        self._auto_update_schema()
        
        # Control 配置
        control_conf = config.get("control", {})
        self.cooldown_seconds = control_conf.get("cooldown_seconds", 60)
        self.user_cooldowns = {}
        self.admin_user_ids = set(map(str, control_conf.get("admin_ids", [])))
        self.lockdown = bool(control_conf.get("lockdown", False))
        self.whitelist_group_ids = set(map(str, control_conf.get("whitelist_group_ids", [])))

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
        logger.info(f"[ComfyUI] 👤 管理员: {admin_count} 个 | 🏠 白名单群: {group_count} 个")
        if self.lockdown:
            logger.warning("[ComfyUI]⚠️ 全局锁定已启用，仅管理员可用")

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

            files = sorted([f.name for f in workflow_dir.glob("*.json")])
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
            return False, "🔒 全局锁定中，仅管理员可用"
        
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

    @filter.on_llm_request()
    async def inject_system_prompt(self, event: AstrMessageEvent, req):
        """注入系统提示词"""
        try:
            llm_settings = self.config.get("llm_settings", {}) 
            my_prompt = llm_settings.get("system_prompt", "")

            if not my_prompt:
                return

            current_prompt = getattr(req, "system_prompt", "") or ""

            if my_prompt in current_prompt:
                return

            if current_prompt:
                req.system_prompt = f"{current_prompt}\n\n{my_prompt}".strip()
            else:
                req.system_prompt = my_prompt.strip()

        except Exception as e:
            logger.error(f"[ComfyUI] 注入提示词异常: {e}")

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
                "  /comfy_ls          列出所有工作流",
                "  /comfy_use <序号>  切换工作流",
                "  /comfy_save        导入新工作流",
                "  /违禁级别          设置群敏感度",
                ""
            ])
        
        # 状态信息
        tips.append("━━━━━━━━━━━━━━━━━━")
        tips.append(f"📍 当前位置：{'群聊 ' + gid if gid else '私聊'}")
        tips.append(f"🔒 违禁级别：{policy}")
        tips.append(f"⏱️ 冷却时间：{self.cooldown_seconds} 秒")
        if is_admin:
            tips.append(f"👑 身份：管理员")
            tips.append(f"📂 数据目录：{self.data_dir}")
        
        yield event.plain_result("\n".join(tips))

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

        files = sorted([f.name for f in self.workflow_dir.glob("*.json")])
        if not files:
            yield event.plain_result("📂 目录中没有工作流文件")
            return

        current_file = self.api.wf_filename if self.api else "未知"
        
        msg = ["📂 可用工作流列表", "━━━━━━━━━━━━━━━━━━"]
        for i, f in enumerate(files, 1):
            if f == current_file:
                msg.append(f"✅ {i}. {f} (当前)")
            else:
                msg.append(f"   {i}. {f}")
        
        msg.append("")
        msg.append("━━━━━━━━━━━━━━━━━━")
        msg.append("切换：/comfy_use <序号> [正面ID] [负面ID] [输出ID]")
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
            files = sorted([f.name for f in self.workflow_dir.glob("*.json")])
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

    @filter.on_llm_response(priority=1)
    async def _extract_prompt_before_filter(self, event: AstrMessageEvent, resp: LLMResponse):
        """提取 LLM 回复中的提示词"""
        if not resp or not resp.completion_text:
            return
        
        full_text = resp.completion_text
        m = re.search(r"提示词是\s*[:：]?\s*(.+)", full_text)
        
        if not m:
            return
        
        prompt = m.group(1).strip()
        prompt = re.sub(r"</[^>]+>.*$", "", prompt, flags=re.DOTALL).strip()
        prompt = prompt.strip('`"\'""''').strip()
        
        if not prompt:
            return
        
        event._comfy_extracted_prompt = prompt

    @filter.on_decorating_result(priority=99)
    async def _auto_paint_from_llm(self, event: AstrMessageEvent):
        """自动绘图"""
        if getattr(event, "_comfy_auto_painted", False):
            return
        
        prompt = getattr(event, "_comfy_extracted_prompt", None)
        if not prompt:
            return
        
        event._comfy_auto_painted = True
        
        def _has_image(comp):
            if isinstance(comp, Image):
                return True
            if isinstance(comp, Node):
                return any(_has_image(c) for c in comp.content)
            return False
        
        result = event.get_result()
        if not result:
            return
            
        chain = result.chain
        if chain and any(_has_image(c) for c in chain):
            return
        
        extra_chain = []
        try:
            async for res in self.comfyui_txt2img(
                event,
                prompt=prompt,
                direct_send=True,
            ):
                if hasattr(res, "chain"):
                    extra_chain.extend(res.chain)
        except Exception as e:
            logger.error(f"[ComfyUI] 自动绘图异常: {e}")
            return
        
        if extra_chain and result:
            result.chain.extend(extra_chain)

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
