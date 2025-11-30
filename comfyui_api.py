import json
import random
import os
import aiohttp
import asyncio
from astrbot.api import logger

class ComfyUI:
    def __init__(self, config: dict) -> None:
        # 读取基础配置
        self.server_address = config.get("server_address", "127.0.0.1:8188")
        self.url = f"http://{self.server_address}"
        
        # 读取绘图参数
        sub_conf = config.get("sub_config", {})
        self.steps = sub_conf.get("steps", 20)
        self.width = sub_conf.get("width", 768)
        self.height = sub_conf.get("height", 1024)
        self.neg_prompt = sub_conf.get("negative_prompt", "")

        # 读取工作流配置 (初始值)
        wf_conf = config.get("workflow_settings", {})
        self.wf_filename = wf_conf.get("json_file", "workflow_api.json")
        self.input_id = str(wf_conf.get("input_node_id", "6"))
        self.output_id = str(wf_conf.get("output_node_id", "9"))

        # 种子节点配置已废弃，不再使用
        self.seed_id = None

        # 路径处理
        self.current_dir = os.path.dirname(os.path.abspath(__file__)) # 保存这个路径方便后续拼接
        self.workflow_path = os.path.join(self.current_dir, 'workflow', self.wf_filename)
        
        logger.info(f"ComfyUI API 已加载 | 工作流: {self.wf_filename}")

    # ====== 新增：热重载配置的方法 ======
    def reload_config(self, filename: str, input_id: str = None, output_id: str = None):
        """动态切换工作流，无需重启"""
        self.wf_filename = filename
        self.workflow_path = os.path.join(self.current_dir, 'workflow', filename)

        if input_id:
            self.input_id = str(input_id)
        if output_id:
            self.output_id = str(output_id)

        exists = os.path.exists(self.workflow_path)
        status = "存在" if exists else "不存在(请检查文件名)"

        logger.info(
            f"[ComfyUI] 切换工作流 -> {filename} [{status}] | Input:{self.input_id} "
            f"Output:{self.output_id or '自动'}"
        )
        return exists, f"已切换至 {filename}，文件{status}。\n当前节点设置: Input={self.input_id}, Output={self.output_id or '自动'}"
    # =================================

    def _load_workflow(self):
        if not os.path.exists(self.workflow_path):
            raise FileNotFoundError(f"工作流文件不存在: {self.workflow_path}")
        with open(self.workflow_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _inject_params(self, workflow, prompt):
        """最简版参数注入：写提示词 + 强制改所有 seed/noise_seed"""
        # 1. 注入正向提示词
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

        # 2. 为本次请求生成一个“基础随机种子”
        base_seed = random.randint(1, 999999999999999)

        # 3. 遍历整个 workflow，把所有含有 seed / noise_seed 的输入都改掉
        ks_count = 0
        offset = 0

        for nid, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            n_inputs = node_data.get("inputs", {})
            if not isinstance(n_inputs, dict):
                continue

            changed = False

            # 如果有 seed 字段，就写一个基于 base_seed 的值
            if "seed" in n_inputs:
                n_inputs["seed"] = base_seed + offset
                offset += 1
                changed = True

            # 如果有 noise_seed 字段，同样处理
            if "noise_seed" in n_inputs:
                n_inputs["noise_seed"] = base_seed + offset
                offset += 1
                changed = True

            if changed:
                ks_count += 1

        logger.info(
            f"[ComfyUI] 本次基础随机种: {base_seed}，已写入 {ks_count} 个 seed/noise_seed 输入"
        )

    async def generate(self, prompt):
        """异步生成图片"""
        client_id = str(random.randint(100000, 999999))
        try:
            workflow = self._load_workflow()
        except Exception as e:
            return None, str(e)
        
        # 注入所有参数
        self._inject_params(workflow, prompt)

        async with aiohttp.ClientSession() as session:
            # 1. 发送请求
            payload = {"prompt": workflow, "client_id": client_id}
            try:
                async with session.post(f"{self.url}/prompt", json=payload) as resp:
                    if resp.status != 200:
                        return None, f"连接 ComfyUI 失败: {resp.status}"
                    res_json = await resp.json()
                    prompt_id = res_json.get("prompt_id")
            except Exception as e:
                return None, f"请求报错: {str(e)}"

            # 2. 轮询等待结果
            for _ in range(300): 
                await asyncio.sleep(1)
                try:
                    async with session.get(f"{self.url}/history/{prompt_id}") as h_resp:
                        if h_resp.status != 200: continue
                        history = await h_resp.json()
                except:
                    continue

                if prompt_id in history:
                    outputs = history[prompt_id].get("outputs", {})
                    img_info = None
                    
                    # 策略 A: 指定了输出 ID
                    if self.output_id and self.output_id in outputs:
                        imgs = outputs[self.output_id].get("images", [])
                        if imgs: img_info = imgs[0]
                    
                    # 策略 B: 自动寻找
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
