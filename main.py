import os
import uuid
import time
import re
import base64
import traceback
import urllib.request
import json
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import *
from .comfyui_api import ComfyUI
from astrbot.api import llm_tool, logger

# 获取当前文件的绝对路径
current_file_path = os.path.abspath(__file__)
# 获取当前文件所在目录的绝对路径
current_directory = os.path.dirname(current_file_path)
# 图片生成存放目录
img_output_dir = os.path.join(current_directory, 'output')
os.makedirs(img_output_dir, exist_ok=True)
@register(
    "astrbot_plugin_comfyui_pro",  
    "lumingya",                    
    "ComfyUI Pro 连接器",           
    "1.1.0",                      
    "https://github.com/lumingya/astrbot_plugin_comfyui_pro" 
)
class ComfyUIPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self._auto_update_schema()
        self.config = config  
        
        # 从配置里读全局参数
        self.cooldown_seconds = config["control"]["cooldown_seconds"]
        self.user_cooldowns = {}

        # 管理员 QQ
        self.admin_user_ids = set(map(str, config["control"]["admin_ids"]))
        # 全局锁定开关
        self.lockdown = bool(config["control"].get("lockdown", False))
        # 白名单群
        self.whitelist_group_ids = set(map(str, config["control"]["whitelist_group_ids"]))

        # 默认敏感词策略
        self.default_group_policy = str(config["control"]["default_group_policy"]).lower()
        self.default_private_policy = str(config["control"]["default_private_policy"]).lower()
        self.group_policies = {
            str(k): str(v).lower()
            for k, v in config["control"].get("group_policies", {}).items()
        }
        # 违禁词策略
        self.policies = {
            "none": set(),
            "lite": {"legacy_lite"},
            "full": {
                "legacy_lite",
                "minors",
                "sexual_violence",
                "bestiality_incest_necrophilia",
                "violence_gore",
                "scat_urine_vomit",
                "self_harm",
                "sexual",
                "nudity",
                "fetish",
            },
        }

        # 管理员绕过控制
        self.admin_bypass_whitelist = config["control"]["admin_bypass"]["whitelist"]
        self.admin_bypass_cooldown = config["control"]["admin_bypass"]["cooldown"]
        self.admin_bypass_sensitive = config["control"]["admin_bypass"]["sensitive_words"]

        logger.info(f"[ComfyUIPlugin] 载入配置的白名单群: {self.whitelist_group_ids}")
        logger.info(f"[ComfyUIPlugin] 管理员账号列表: {self.admin_user_ids}")

        # 违禁词分类词库 (这里帮你补全了空字典，避免语法报错)
        self.lexicon = {
            "legacy_lite": [
                # scat/urine
                "shit", "poop", "feces", "urine", "piss", "scat", "pee", "peeing", "pissing",
                "defecate", "defecation", "excrement", "bowel", "toilet", "potty",
        
                # 血腥/暴力（英文）
                "blood", "gore", "bloody", "wound", "injury", "decapitation", "guro", "torture",
                "behead", "severed", "bleeding", "hemorrhage", "bruise", "bruised", "cut", "cuts",
                "stab", "stabbing", "slash", "slashing", "violence", "violent", "massacre",
                "butcher", "mutilate", "dismember", "amputate", "laceration", "gash",
        
                # 体型/身体（英文）
                "obese", "fat", "chubby", "plump", "overweight", "fatty", "fatso", "lard",
                "blob", "thick", "chunky", "hefty", "pudgy", "rotund", "tubby", "porky",
        
                # 畸形/残疾/多肢等（英文）
                "deformed", "mutilated", "amputee", "missing limbs", "extra limbs", "malformed", 
                "mutation", "deformity", "disfigured", "disfigure", "cripple", "crippled",
                "handicap", "handicapped", "disabled", "disability", "prosthetic", "stump",
                "birth defect", "abnormal", "freak", "grotesque", "monstrous",
        
                # 兽交等（英文）
                "zoophilia", "bestiality", "zoo", "animal sex", "beast", "bestial",
                "furry sex", "anthro sex", "knot", "knotting", "mating",
        
                # 扶她等（英文）
                "futanari", "futa", "dickgirl", "shemale", "newhalf", "hermaphrodite",
                "trans", "transgender", "ladyboy", "femboy", "trap", "otokonoko",
        
                # 同人向（英文）
                "yaoi", "bara", "bl", "boys love", "gay", "male on male", "homo",
                "homosexual", "queer", "mlm", "shounen ai", "june", "tanbi",
                "seme", "uke", "fujoshi", "fudanshi",

                # ===== 拼音（扩展） =====
                # 排泄类
                "da bian", "dabian", "niao", "xiao bian", "xiaobian", "bian bian", "bianbian",
                "la shi", "lashi", "ce suo", "cesuo", "mao keng", "maokeng",
        
                # 血腥/暴力
                "xie xing", "xiexing", "duan tou", "duantou", "shang kou", "shangkou",
                "ku xing", "kuxing", "lie qi", "lieqi", "sha ren", "sharen", "can sha", "cansha",
                "xue", "liu xue", "liuxue", "bao li", "baoli", "nue sha", "nuesha",
        
                # 体型/身体
                "fei pang", "feipang", "chao zhong", "chaozhong", "si fei zhu", "sifeizhu",
                "pang zi", "pangzi", "fei zhu", "feizhu", "da pang zi", "dapangzi",
        
                # 畸形/残疾/多肢
                "ji xing", "jixing", "jie zhi", "jiezhi", "can ji", "canji",
                "tu bian", "tubian", "duo zhi", "duozhi", "que xian", "quexian",
                "guai wu", "guaiwu", "guai tai", "guaitai",
        
                # 兽交等
                "shou jiao", "shoujiao", "ren shou", "renshou", "dong wu", "dongwu",
                "ye shou", "yeshou", "qin shou", "qinshou",
        
                # 同人向
                "dan mei", "danmei", "nan tong", "nantong", "nan nan", "nannan",
                "gei", "shou", "gong", "tong xing lian", "tongxinglian", "ji you", "jiyou",
        
                # 扶她
                "fu ta", "futa", "bian xing", "bianxing", "liang xing", "liangxing",
                "yin yang ren", "yinyangren", "shuang xing", "shuangxing",
            ],

            # 以下为 full 模式扩展
            "minors": [
                "loli", "lolicon", "shota", "shotacon", "lolita", "shouta",
                "child porn", "cp", "underage", "minor", "kid", "kiddie",
                "jk", "js", "jc", "elementary", "middle school", "kindergarten",
                "toddler", "infant", "baby", "preteen", "prepubescent",
                "pedo", "pedophile", "pedophilia", "hebephile", "hebephilia",
            ],
    
            "sexual_violence": [
                "rape", "rapist", "raping", "sexual assault", "molest", "molestation",
                "forced sex", "coerce", "noncon", "non-consensual", "dubcon", "dub-con",
                "date rape", "drugged", "rohypnol", "roofies", "assault", "violate",
                "gang rape", "gangrape", "abuse", "abused", "force", "forced",
                "blackmail", "hypnosis", "mind break", "mindbreak", "slave", "slavery",
            ],
    
            "bestiality_incest_necrophilia": [
                "bestiality", "zoophilia", "zoo", "animal sex", "beast sex",
                "incest", "stepbro", "stepbrother", "stepsis", "stepsister", "stepmom",
                "stepdad", "stepfather", "stepmother", "daddy", "mommy", "sister", "brother",
                "father daughter", "mother son", "sibling", "family sex", "inbreeding",
                "necrophilia", "necrophile", "corpse", "dead body", "cadaver",
                "snuff", "death", "dying", "kill", "murder",
            ],
    
            "violence_gore": [
                "blood", "bloody", "gore", "gory", "guro", "bleeding", "hemorrhage",
                "severed", "dismember", "decapitate", "decapitation", "behead", "beheading",
                "amputation", "mutilate", "mutilation", "dissect", "dissection",
                "disembowel", "entrails", "intestines", "organs", "viscera",
                "slit throat", "stabbed", "execution", "impale", "impaled",
                "torture", "corpse", "rotting", "laceration", "eviscerate",
                "cannibalism", "cannibal", "flesh", "meat", "butcher",
            ],
    
            "scat_urine_vomit": [
                "shit", "poop", "feces", "urine", "pee", "piss", "scat", "scatology",
                "vomit", "puke", "throw up", "barf", "diarrhea", "enema", 
                "coprophagia", "coprophilia", "urophagia", "urophilia", "watersports",
                "golden shower", "brown shower", "toilet", "potty", "diaper",
                "da bian", "dabian", "niao", "la shi", "lashi",
            ],
    
            "self_harm": [
                "suicide", "self harm", "self-harm", "selfharm", "cut", "cutting",
                "kms", "kys", "kill myself", "kill yourself", "end my life",
                "slit wrists", "slit wrist", "overdose", "od", "hang myself",
                "hanging", "noose", "jump off", "pill", "pills", "bleach",
                "razor", "blade", "burn", "burning", "self mutilation",
            ],
    
            "sexual": [
                "sex", "porn", "pornography", "xxx", "adult", "explicit",
                "nipple", "nipples", "areola", "tit", "tits", "titty", "titties",
                "breasts", "breast", "boobs", "boob", "busty", "cleavage",
                "ass", "butt", "buttocks", "anus", "anal", "oral", "fellatio",
                "blowjob", "bj", "handjob", "hj", "footjob", "titjob", "paizuri",
                "cum", "cumming", "semen", "ejaculate", "ejaculation", "orgasm",
                "creampie", "bukkake", "facial", "deepthroat", "throat fuck",
                "vagina", "vulva", "labia", "clitoris", "clit", "g-spot",
                "penis", "dick", "cock", "phallus", "shaft", "glans", "balls",
                "pussy", "cunt", "cunnilingus", "fingering", "masturbate",
                "69", "threesome", "foursome", "gangbang", "orgy", "swinger",
                "milf", "dilf", "gilf", "mature", "cougar",
                "pegging", "rimming", "fisting", "anal beads", "dildo", "vibrator",
                "nsfw", "lewd", "erotic", "explicit", "r18", "r-18", "adult only",
                "hentai", "ecchi", "ahegao", "paipan", "oppai", "ero",
            ],
    
            "nudity": [
                "nude", "naked", "topless", "bottomless", "nip slip", "exposed",
                "undress", "undressed", "strip", "stripped", "bare", "unclothed",
                "birthday suit", "au naturel", "in the buff", "skinny dip",
                "wardrobe malfunction", "see through", "transparent", "revealing",
            ],
    
            "fetish": [
                "bdsm", "bondage", "dominatrix", "fetish", "kink", "kinky",
                "spanking", "spank", "whip", "whipping", "paddle", "cane",
                "submissive", "sub", "dom", "dominant", "master", "slave",
                "chastity", "chastity belt", "collar", "leash", "cage",
                "latex", "leather", "rubber", "pvc", "catsuit",
                "footjob", "foot fetish", "feet", "toes", "soles",
                "armpit", "smell", "sniff", "lick", "worship",
            ],
        }


        # 预编译不同策略对应的正则
        self._policy_patterns = {}
        self._build_policy_patterns()
        llm_settings = config.get("llm_settings", {})
        system_prompt = llm_settings.get("system_prompt", "")
        if system_prompt:
            # 修改类方法的文档
            self.comfyui_txt2img.__func__.__doc__ = system_prompt
            logger.info("[ComfyUIPlugin] 已从配置加载自定义 System Prompt")
        else:
            logger.warning("[ComfyUIPlugin] 未检测到自定义 Prompt，将使用代码内默认值")
        self.comfy_ui = None
        self.api = None
        try:
            self.api = ComfyUI(self.config) 
            self.comfy_ui = self.api
        except Exception as e:
            logger.error(f"【初始化 ComfyUI 客户端失败】: {e}")
                # ====== 初始化入口 ======
    async def initialize(self):
        # 这里只做初始化操作
        self.context.activate_llm_tool("comfyui_txt2img")
    def _auto_update_schema(self):
        """[调试版] 启动时扫描 workflow 目录，强制更新 UI"""
        try:
            # 1. 确定路径
            base_path = os.path.dirname(os.path.abspath(__file__))
            schema_path = os.path.join(base_path, '_conf_schema.json')
            workflow_dir = os.path.join(base_path, 'workflow')
            
            logger.info(f"[ComfyUI] 正在检查工作流目录: {workflow_dir}")

            # 2. 扫描文件
            if not os.path.exists(workflow_dir):
                logger.error(f"[ComfyUI] 目录不存在: {workflow_dir}")
                return

            files = [f for f in os.listdir(workflow_dir) if f.endswith('.json')]
            logger.info(f"[ComfyUI] 扫描到的文件: {files}")

            if not files:
                files = ["workflow_api.json"] # 兜底

            # 3. 读取并修改 JSON
            with open(schema_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 直接定位，不再用 get，强制修改
            # 路径: workflow_settings -> items -> json_file
            target = data['workflow_settings']['items']['json_file']
            
            # 强制覆盖旧配置
            target['options'] = sorted(files)
            target['enum'] = sorted(files) # 双重保险
            
            # 4. 写回文件
            with open(schema_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"[ComfyUI] 配置文件已更新! 写入列表: {files}")
            logger.info(f"[ComfyUI] 请【完全重启】AstrBot 以加载新选项")

        except Exception as e:
            # 把错误完整打印出来
            logger.error(f"[ComfyUI] 更新 UI 失败，报错信息如下:")
            logger.error(traceback.format_exc())
    # ====== 核心绘图逻辑 (从 initialize 里移出来的) ======
    async def _handle_paint_logic(self, event: AstrMessageEvent, direct_send: bool):
        """这是处理画图的核心逻辑"""
        if self._is_locked_for(event):
            yield event.plain_result("全局锁定。")
            return
        try:
            logger.info(f"进入核心绘图逻辑, direct_send={direct_send}, full_message='{event.message_str}'")
            
            full_message = event.message_str.strip()
            parts = full_message.split(' ', 1)
            prompt = parts[1].strip() if len(parts) > 1 else ""

            if not prompt:
                yield event.plain_result("请输入提示词。")
                return

            if prompt:
                user_id = str(event.get_sender_id())
                is_admin = user_id in self.admin_user_ids
                can_bypass_sensitive = is_admin and self.admin_bypass_sensitive
                sensitive = self._find_sensitive_words(prompt, event)
                if sensitive and not can_bypass_sensitive:
                    tip = "、".join(sensitive)
                    logger.warning(f"用户 {user_id} 违禁: {tip}")
                    yield event.plain_result(f"检测到敏感词：{tip}，无法生成图片。")
                    return
                elif sensitive and can_bypass_sensitive:
                    logger.info(f"管理员 {user_id} 使用敏感词 {sensitive}，已放行。")

            # 调用绘图工具
            async for result in self.comfyui_txt2img(event, prompt=prompt, direct_send=direct_send):
                yield result
        except Exception as e:
            logger.error(f"画图插件发生未知错误: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result("执行画图命令时出错，请查看后台日志。")
    # ====== 指令函数 (全部移到类的一级缩进下，并添加 self) ======

    @filter.command("comfy帮助")
    async def cmd_comfyui_help(self, event: AstrMessageEvent):
        if self._is_group_message(event) and not self._is_group_allowed(event):
            yield event.plain_result(f"禁止输入。")
            return
        gid = self._get_group_id(event)
        policy = self._get_policy_for_event(event)
        tips = [
            "🎨 ComfyUI 插件帮助",
            "━━━━━━━━━━━━━━",
            "【基础指令】",
            "  • /画图 <提示词>    → 生成图片（转发模式）",
            "  • /画图no <提示词> → 生成图片（直发模式）",
            "  • LLM 对话模式       → '帮我画一个...'",
            ""
        ]
        user_id = str(event.get_sender_id())
        is_admin = user_id in self.admin_user_ids
        # ==================
        # 只有管理员才显示高级指令
        if is_admin:
            tips.extend([
                "【工作流管理 (管理员)】",
                "  • /comfy_ls               → 列出所有工作流",
                "  • /comfy_use <文件名> [ID...] → 切换工作流",
                "  • /comfy_save <文件名> <JSON> → 导入新工作流",
                "",
                "【控制指令】",
                "  • /违禁级别 [none|lite|full] → 设置群敏感度",
            ])
            
        tips.append("━━━━━━━━━━━━━━")
        tips.append(f"📌 当前违禁级别：{policy}" + (f" (群 {gid})" if gid else " (私聊)"))
        yield event.plain_result("\n".join(tips))

    @filter.command("违禁级别", aliases={"banlevel", "敏感级别"})
    async def cmd_set_policy(self, event: AstrMessageEvent):
        if self._is_locked_for(event):
            yield event.plain_result("全局锁定。")
            return
        if not self._is_group_message(event):
            yield event.plain_result("该指令仅支持群聊使用。")
            return
        if not self._is_group_allowed(event):
            yield event.plain_result(f"禁止输入。")
            return

        full_msg = event.message_str.strip()
        parts = full_msg.split()
        gid = self._get_group_id(event) or "未知群"

        if len(parts) == 1:
            current = self.group_policies.get(gid, self.default_group_policy)
            yield event.plain_result(f"本群当前违禁级别：{current}（可选：none / lite / full）")
            return

        level = parts[1].lower()
        if level not in self.policies:
            yield event.plain_result("用法：/违禁级别 [none|lite|full]")
            return

        self.group_policies[gid] = level
        yield event.plain_result(f"已将本群违禁级别设置为：{level}")
    # ====== 新增：工作流管理指令 ======

    @filter.command("comfy_ls")
    async def cmd_comfy_list(self, event: AstrMessageEvent):
        """列出当前所有可用工作流"""
        # 权限校验
        if not self._check_permission(event): 
            yield event.plain_result("权限不足。")
            return

        workflow_dir = os.path.join(current_directory, 'workflow')
        if not os.path.exists(workflow_dir):
            yield event.plain_result("错误：workflow 目录不存在。")
            return

        files = [f for f in os.listdir(workflow_dir) if f.endswith('.json')]
        if not files:
            yield event.plain_result("目录中没有 .json 文件。")
            return

        current_file = self.api.wf_filename if self.api else "未知"
        
        msg = ["📂 可用工作流列表："]
        for f in files:
            mark = "✅ " if f == current_file else "   "
            msg.append(f"{mark}{f}")
        
        msg.append("")
        msg.append("切换指令：/comfy_use <文件名> [input_id] [seed_id]")
        yield event.plain_result("\n".join(msg))

    @filter.command("comfy_use")
    async def cmd_comfy_use(self, event: AstrMessageEvent):
        """切换工作流
        用法: /comfy_use file.json [input_id] [seed_id] [output_id]
        """
        if not self._check_permission(event):
            yield event.plain_result("权限不足。")
            return

        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("参数错误。\n用法: /comfy_use <文件名> [input_id] [seed_id] [output_id]")
            return

        filename = args[1]
        # 如果用户只输入了文件名，不带后缀，自动补全
        if not filename.endswith(".json"):
            filename += ".json"

        # 获取可选参数
        inp_id = args[2] if len(args) > 2 else None
        seed_id = args[3] if len(args) > 3 else None
        out_id = args[4] if len(args) > 4 else None

        if not self.api:
            yield event.plain_result("插件未初始化。")
            return

        # 调用 API 进行热切换
        exists, msg = self.api.reload_config(filename, inp_id, seed_id, out_id)
        yield event.plain_result(msg)

    @filter.command("comfy_save")
    async def cmd_comfy_save(self, event: AstrMessageEvent):
        """保存/导入工作流
        用法: /comfy_save <文件名> <JSON内容>
        """
        if not self._check_permission(event):
            yield event.plain_result("权限不足。")
            return

        # 1. 解析命令
        full_text = event.message_str
        # 去掉命令头 /comfy_save
        content = full_text.split(maxsplit=2)
        
        if len(content) < 3:
            yield event.plain_result("用法: /comfy_save <新文件名.json> <JSON代码>")
            return
        
        filename = content[1]
        json_str = content[2]

        if not filename.endswith(".json"):
            filename += ".json"

        # 2. 校验 JSON
        try:
            # 尝试清洗一下代码块标记 (```json ... ```)
            json_str = json_str.replace("```json", "").replace("```", "").strip()
            json_data = json.loads(json_str)
        except json.JSONDecodeError:
            yield event.plain_result("解析失败：这不是合法的 JSON 格式。")
            return

        # 3. 保存文件
        workflow_dir = os.path.join(current_directory, 'workflow')
        os.makedirs(workflow_dir, exist_ok=True)
        save_path = os.path.join(workflow_dir, filename)

        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            yield event.plain_result(f"✅ 保存成功！\n文件已存为: {filename}\n请使用 /comfy_use {filename} 切换。")
        except Exception as e:
            yield event.plain_result(f"保存文件失败: {e}")

    # 辅助：简易权限检查
    def _check_permission(self, event: AstrMessageEvent) -> bool:
        uid = str(event.get_sender_id())
        return uid in self.admin_user_ids
    @filter.command("画图", aliases=["绘画"])
    async def cmd_paint(self, event: AstrMessageEvent):
        if self._is_locked_for(event):
            yield event.plain_result("全局锁定。")
            return
        if self._is_group_message(event) and not self._is_group_allowed(event):
            yield event.plain_result(f"禁止输入。")
            return
        
        # 调用核心逻辑
        async for result in self._handle_paint_logic(event, direct_send=False):
            yield result

    @filter.command("画图no")
    async def cmd_paint_no(self, event: AstrMessageEvent):
        if self._is_locked_for(event):
            yield event.plain_result("全局锁定。")
            return
        if self._is_group_message(event) and not self._is_group_allowed(event):
            yield event.plain_result(f"禁止输入。")
            return

        # 调用核心逻辑
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

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        if not self._is_group_message(event):
            return True
        gid = self._get_group_id(event)
        if not gid:
            return False

        uid = str(event.get_sender_id())

        # 管理员逻辑
        if uid in self.admin_user_ids:
            if gid in self.whitelist_group_ids:
                return True
            else:
                if self.admin_bypass_whitelist:
                    return True
                else:
                    return False

        # 普通用户
        return gid in self.whitelist_group_ids

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

    def _is_locked_for(self, event: AstrMessageEvent) -> bool:
        if not self.lockdown:
            return False
        return str(event.get_sender_id()) not in self.admin_user_ids

    def _check_and_update_cooldown(self, user_id: str) -> (bool, int):
        if user_id in self.admin_user_ids:
            if self.admin_bypass_cooldown:
                return True, 0

        current_time = time.time()
        last_time = self.user_cooldowns.get(user_id, 0)
        elapsed = current_time - last_time

        if elapsed < self.cooldown_seconds:
            remain = int(self.cooldown_seconds - elapsed)
            return False, remain

        self.user_cooldowns[user_id] = current_time
        return True, 0

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

    @filter.on_decorating_result(priority=99)
    async def _auto_paint_from_llm(self, event: AstrMessageEvent):
        """
        当 LLM 普通文本里出现『提示词是 "xxx"』时，
        立即调用 comfyui_txt2img 生成并发送图片。
        """
        import re
        
        # 0) 检查是否已经生过图
        def _has_image(comp):
            from astrbot.core.message.components import Image, Node
            if isinstance(comp, Image):
                return True
            if isinstance(comp, Node):
                return any(_has_image(c) for c in comp.content)
            return False

        chain = event.get_result().chain
        if chain and any(_has_image(c) for c in chain):
            return

        # 1) 拿到本次回复的完整文本
        try:
            text_chunks = [c.text for c in chain if hasattr(c, "text")]
            full_text = "".join(text_chunks)
        except Exception:
            full_text = ""

        if not full_text:
            return

        # 2) 提取 prompt
        prompt = None
        m = re.search(r"提示词[是:：]\s*([^\n]+)", full_text)
        if m:
            prompt = (
                m.group(1)
                .strip()
                .lstrip('`"“‘')
                .rstrip('`"”’')
                .strip()
            )

        if not prompt:
            return

        # 3) 调用绘图工具
        extra_chain = []
        async for res in self.comfyui_txt2img(
            event,
            prompt=prompt,
            direct_send=True,
        ):
            if hasattr(res, "chain"):
                extra_chain.extend(res.chain)

        if extra_chain:
            event.get_result().chain.extend(extra_chain)

    @llm_tool(name="comfyui_txt2img")
    async def comfyui_txt2img(self, event: AstrMessageEvent, ctx: Context = None, prompt: str = None, text: str = None, img_width: int = None, img_height: int = None, direct_send: bool = False) -> MessageEventResult:
        """
        (此处的 Prompt 已被 _conf_schema.json 中的配置覆盖)
        """
        if self._is_locked_for(event):
            yield event.plain_result("全局锁定。")
            return

        # === 参数兼容处理 ===
        if not prompt and text:
            prompt = text

        # === 空参数强制报错，防止兜底中文 ===
        if not prompt:
            yield event.plain_result("LLM 没有提供英文 prompt，请重试。")
            return

        # === 中文检测，直接拒绝 ===
        import re
        if re.search(r'[\u4e00-\u9fff]', prompt):
            yield event.plain_result(f"检测到中文 prompt（{prompt}），已取消。请确保生成英文关键词。")
            return

        if self._is_group_message(event) and not self._is_group_allowed(event):
            yield event.plain_result(f"禁止输入。")
            return

        if not getattr(self, 'api', None) and not getattr(self, 'comfy_ui', None):
            yield event.plain_result("错误：ComfyUI 服务未连接。")
            return

        # ========= 新增：兜底 prompt 逻辑 =========
        if not isinstance(prompt, str) or not prompt:
            raw = getattr(event, "message_str", "") or ""
            prompt = re.sub(r'```math\s*At:\d+```\s*', '', raw).strip()
            if not prompt:
                yield event.plain_result("请输入提示词。")
                return
        
        try:
            if prompt:
                user_id = str(event.get_sender_id())
                is_admin = user_id in self.admin_user_ids
                can_bypass_sensitive = is_admin and self.admin_bypass_sensitive
                sensitive = self._find_sensitive_words(prompt, event)

                if sensitive and not can_bypass_sensitive:
                    tip = "、".join(sensitive)
                    logger.warning(f"用户 {user_id} 通过 LLM 尝试生成违禁内容，触发敏感词: {tip}")
                    yield event.plain_result(f"抱歉，检测到敏感词：{tip}。我无法为您绘制。")
                    return
                elif sensitive and can_bypass_sensitive:
                    logger.info(f"管理员 {user_id} 使用敏感词 {sensitive}，已放行。")

            # ====== 统一冷却逻辑 ======
            user_id = str(event.get_sender_id())
            ok, remain = self._check_and_update_cooldown(user_id)
            if not ok:
                yield event.plain_result(f"请求太频繁, 请在 {remain} 秒后重试。")
                return

            logger.info(f"prompt:'{prompt}' | mode=txt2img | direct_send={direct_send}")

            # === 调用 API ===
            api_instance = getattr(self, 'api', getattr(self, 'comfy_ui', None))
            img_data, error_msg = await api_instance.generate(prompt)

            if not img_data:
                logger.error(f"ComfyUI 生成失败: {error_msg}")
                yield event.plain_result(f"生成图片失败了: {error_msg}")
                return

            # 保存图片
            img_filename = f"{uuid.uuid4()}.png"
            img_path = os.path.join(img_output_dir, img_filename)
            with open(img_path, 'wb') as fp:
                fp.write(img_data)

            # 发送结果
            if direct_send:
                image_component = Image.fromFileSystem(img_path)
                yield event.chain_result([image_component])
            else:
                self_id = self._get_self_id(event) or "0"
                image_component = Image.fromFileSystem(img_path)
                forward_node = Node(
                    user_id=int(self_id),
                    nickname="小鹿",
                    content=[image_component]
                )
                yield event.chain_result([forward_node])

        except Exception as e:
            logger.error(f"画图插件执行异常: {e}")
            yield event.plain_result(f"内部错误: {str(e)}")