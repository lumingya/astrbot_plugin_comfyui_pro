import json
import random
import os
import aiohttp
import asyncio
from pathlib import Path
from astrbot.api import logger


class ComfyUI:
    def __init__(self, config: dict, data_dir: Path = None) -> None:
        """
        初始化 ComfyUI API 客户端
        
        Args:
            config: 插件配置字典
            data_dir: 持久化数据目录（由 main.py 传入）
        """
        # 读取基础配置
        self.server_address = config.get("server_address", "127.0.0.1:8188")
        self.url = f"http://{self.server_address}"
        
        # 读取绘图参数
        sub_conf = config.get("sub_config", {})
        self.steps = sub_conf.get("steps", 20)
        self.width = sub_conf.get("width", 768)
        self.height = sub_conf.get("height", 1024)
        self.neg_prompt = sub_conf.get("negative_prompt", "")

        # 读取工作流配置
        wf_conf = config.get("workflow_settings", {})
        self.wf_filename = wf_conf.get("json_file", "workflow_api.json")
        self.input_id = str(wf_conf.get("input_node_id", "6"))
        self.neg_node_id = str(wf_conf.get("neg_node_id", "")) 
        self.output_id = str(wf_conf.get("output_node_id", "9"))

        self.seed_id = None

        # ====== 关键改动：使用持久化目录 ======
        if data_dir is not None:
            self.data_dir = Path(data_dir)
        else:
            # 备用方案：使用插件目录（不推荐）
            self.data_dir = Path(os.path.dirname(os.path.abspath(__file__)))
            logger.warning("[ComfyUI API] 未传入 data_dir，使用插件目录（更新后可能丢失数据）")
        
        self.workflow_dir = self.data_dir / "workflow"
        self.workflow_path = self.workflow_dir / self.wf_filename
        
        logger.info(f"[ComfyUI API] 已加载 | 工作流目录: {self.workflow_dir} | 当前工作流: {self.wf_filename}")

    def reload_config(self, filename: str, input_id: str = None, output_id: str = None, neg_node_id: str = None):
        """动态切换工作流，无需重启"""
        self.wf_filename = filename
        self.workflow_path = self.workflow_dir / filename

        if input_id:
            self.input_id = str(input_id)
        if output_id:
            self.output_id = str(output_id)
        if neg_node_id:
            self.neg_node_id = str(neg_node_id)
        
        exists = self.workflow_path.exists()
        status = "存在" if exists else "不存在(请检查文件名)"

        logger.info(
            f"[ComfyUI] 切换工作流 -> {filename} [{status}] | "
            f"Input:{self.input_id} | Neg:{self.neg_node_id} | Output:{self.output_id or '自动'}"
        )
        return exists, (f"已切换至 {filename}，文件{status}。\n"
                        f"当前节点设置: Positive={self.input_id}, Negative={self.neg_node_id}, Output={self.output_id or '自动'}")

    def _load_workflow(self):
        if not self.workflow_path.exists():
            raise FileNotFoundError(f"工作流文件不存在: {self.workflow_path}")
        with open(self.workflow_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _inject_params(self, workflow, prompt):
        """参数注入：写提示词 + 覆盖步数 + 强制改所有 seed/noise_seed"""
    
        # ========== 1. 注入正向提示词（原有代码）==========
        node = workflow.get(self.input_id)
        if not node:
            logger.error(f"严重错误: 找不到输入节点 ID {self.input_id}，请检查工作流或配置。")
            return

        inputs = node.get("inputs", {})
        target_keys = [
            "text", "opt_text", "string",
            "text_positive", "positive",
            "prompt", "wildcard_text",
        ]
        for key in target_keys:
            if key in inputs:
                inputs[key] = prompt
                break
    
        # 注入负面提示词（原有代码）
        if self.neg_node_id and self.neg_prompt:
            neg_node = workflow.get(self.neg_node_id)
            if neg_node:
                n_inputs = neg_node.get("inputs", {})
                n_keys = ["text", "string", "negative", "text_negative", "prompt"]
                for n_key in n_keys:
                    if n_key in n_inputs:
                        existing_neg = str(n_inputs.get(n_key, "")).strip()
                        config_neg = self.neg_prompt.strip()
                
                        if existing_neg and config_neg:
                            n_inputs[n_key] = f"{existing_neg}, {config_neg}"
                        elif config_neg:
                            n_inputs[n_key] = config_neg
                        break

        # ========== 2. 覆盖步数（按节点ID）==========
        overrides = self._load_steps_override()
        if overrides:
            count = self._apply_steps_override(workflow, overrides)
            if count > 0:
                override_info = ", ".join([f"{k}:{v}步" for k, v in overrides.items()])
                logger.info(f"[ComfyUI] ✓ 步数覆盖生效: {override_info} (修改 {count} 处)")
            else:
                logger.info(f"[ComfyUI] ⚠ 配置了步数覆盖但未找到匹配的引用")
    
        # ========== 3. 随机化种子（原有代码）==========
        base_seed = random.randint(1, 999999999999999)
        ks_count = 0
        offset = 0

        for nid, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            n_inputs = node_data.get("inputs", {})
            if not isinstance(n_inputs, dict):
                continue

            changed = False

            if "seed" in n_inputs:
                n_inputs["seed"] = base_seed + offset
                offset += 1
                changed = True

            if "noise_seed" in n_inputs:
                n_inputs["noise_seed"] = base_seed + offset
                offset += 1
                changed = True

            if changed:
                ks_count += 1

        logger.info(
            f"[ComfyUI] 本次基础随机种: {base_seed}，已写入 {ks_count} 个 seed/noise_seed 输入"
        )
    def _load_steps_override(self) -> dict:
        """
        读取当前工作流的 steps 覆盖配置
        返回格式：{"3839": 20, "4521": 50} 或 {}
        """
        try:
            stem = self.workflow_path.stem
            sidecar = self.workflow_path.parent / f"{stem}.steps.json"
        
            if not sidecar.exists():
                return {}
        
            with open(sidecar, "r", encoding="utf-8") as f:
                data = json.load(f)
        
            if not isinstance(data, dict):
                return {}
        
            # 转换格式：支持旧格式 {"steps": 20} 和新格式 {"3839": {"steps": 20}}
            result = {}
            for key, value in data.items():
                if isinstance(value, dict) and "steps" in value:
                    # 新格式：{"3839": {"steps": 20}}
                    steps = value.get("steps")
                    if isinstance(steps, (int, float)) and steps > 0:
                        result[str(key)] = int(steps)
                elif isinstance(value, (int, float)) and value > 0:
                    # 兼容简化格式：{"3839": 20}
                    result[str(key)] = int(value)
        
            return result
    
        except Exception as e:
            logger.warning(f"[ComfyUI] 读取 steps 覆盖文件失败: {e}")
            return {}
    def _apply_steps_override(self, workflow: dict, overrides: dict):
        """
        按节点ID覆盖步数
        overrides 格式：{"3839": 20, "4521": 50}
        只覆盖引用了指定 ParameterBreak 节点的 steps/steps_total
        """
        if not overrides:
            return 0
     
        # 第一步：找出所有 ParameterBreak 节点
        pb_nodes = {}
        for nid, node_data in workflow.items():
            if isinstance(node_data, dict):
                if node_data.get("class_type") == "ParameterBreak":
                    pb_nodes[str(nid)] = node_data
    
        if not pb_nodes:
            logger.debug("[ComfyUI] 未检测到 ParameterBreak 节点")
            return 0
    
        # 检查哪些覆盖配置的节点ID存在
        valid_overrides = {}
        for pb_id, steps in overrides.items():
            if pb_id in pb_nodes:
                valid_overrides[pb_id] = steps
            else:
                logger.warning(f"[ComfyUI] 覆盖配置中的节点 {pb_id} 不存在于当前工作流")
    
        if not valid_overrides:
            return 0
    
        # 第二步：扫描所有节点，覆盖引用了指定 ParameterBreak 的 steps
        override_count = 0
        steps_keys = ("steps", "steps_total")
    
        for nid, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
        
            n_inputs = node_data.get("inputs", {})
            if not isinstance(n_inputs, dict):
                continue
        
            for key in steps_keys:
                if key not in n_inputs:
                    continue
            
                value = n_inputs[key]
            
                # 检查是否是引用格式
                if isinstance(value, list) and len(value) == 2:
                    ref_node_id = str(value[0])
                
                    # 如果引用的 ParameterBreak 在覆盖列表中
                    if ref_node_id in valid_overrides:
                        new_steps = valid_overrides[ref_node_id]
                        n_inputs[key] = new_steps
                        override_count += 1
                        logger.debug(f"[ComfyUI] 节点 {nid}.{key}: [{ref_node_id}] -> {new_steps}")
    
        return override_count
    async def generate(self, prompt):
        """异步生成图片"""
        client_id = str(random.randint(100000, 999999))
        try:
            workflow = self._load_workflow()
        except Exception as e:
            return None, str(e)
        
        self._inject_params(workflow, prompt)

        async with aiohttp.ClientSession() as session:
            payload = {"prompt": workflow, "client_id": client_id}
            try:
                async with session.post(f"{self.url}/prompt", json=payload) as resp:
                    if resp.status != 200:
                        return None, f"连接 ComfyUI 失败: {resp.status}"
                    res_json = await resp.json()
                    prompt_id = res_json.get("prompt_id")
            except Exception as e:
                return None, f"请求报错: {str(e)}"

            for _ in range(300): 
                await asyncio.sleep(1)
                try:
                    async with session.get(f"{self.url}/history/{prompt_id}") as h_resp:
                        if h_resp.status != 200:
                            continue
                        history = await h_resp.json()
                except:
                    continue

                if prompt_id in history:
                    outputs = history[prompt_id].get("outputs", {})
                    img_info = None
                    
                    if self.output_id and self.output_id in outputs:
                        imgs = outputs[self.output_id].get("images", [])
                        if imgs:
                            img_info = imgs[0]
                    
                    if not img_info:
                        for node_out in outputs.values():
                            if "images" in node_out and node_out["images"]:
                                img_info = node_out["images"][0]
                                break
                    
                    if img_info:
                        fname = img_info['filename']
                        sfolder = img_info['subfolder']
                        itype = img_info['type']
                        img_url = f"{self.url}/view?filename={fname}&subfolder={sfolder}&type={itype}"
                        
                        async with session.get(img_url) as img_res:
                            if img_res.status == 200:
                                return await img_res.read(), None 
                            else:
                                return None, "下载图片失败"
                    else:
                        return None, "工作流执行完成，但未找到输出图片"
            
            return None, "生成超时"
