# AstrBot Plugin ComfyUI Pro

## 需要特别提到的事：
本插件并没有提供ui来直接配置工作流的图片尺寸，步数，lora等的选项，这并不是为了增加上手难度，相反，这是为了提供最大的灵活性和兼容性。这个调整可以让插件适配所有（大概）工作流，只要你有，经过简单的几个步骤，你就可以通过本插件调用。
同时，为了提高对不同来源 AI 的兼容，我使用了正则来提取 LLM 回复中的绘图标签，而不是依靠 AI 本身的函数调用功能。因此如果出现问题，可以尝试关闭 AI 函数调用权限，并检查回复中是否正确包含 `<pic prompt="...">`。

## 介绍
一个功能强大的 AstrBot 插件，旨在将你本地的 **ComfyUI** 无缝集成到聊天机器人中。

它不仅支持基础的文生图，更创新的引入了 LLM（大语言模型）作为"提示词工程师"，能够将用户的日常对话自动转化为 ComfyUI 可识别的高质量英文 Prompt。同时，它对 ComfyUI 用户极其友好，支持便捷地导入和切换你自己的工作流，并提供了完善的权限控制、敏感词过滤、Persona 提示词管理和 Profile 一键配置系统。

导入工作流请务必参考教程。

## 核心优势：轻松使用你自己的工作流

本插件最大的特点就是让你几乎无缝地使用你在 ComfyUI 中已经搭建好的工作流。只需简单几步，就能将你的创意带入 AstrBot。

### 一、导出你的工作流
 在你的工作流界面，点击菜单的 **`Save (API Format)`** 按钮，将工作流导出为 `.json` 文件。

### 二、找到关键节点 ID
记下你的工作流中 **输入** 和 **输出** 节点的 ID。开启开发者模式后，ID 会显示在每个节点的标题上方。
*   **输入节点 (Input ID)**: 通常是接收提示词的 `CLIP Text Encode` 节点。**（必需）**
*   **负面提示词节点 (Neg Node ID)**: 接收负面提示词的节点。可选，如无则填写任意不存在的 ID。
*   **输出节点 (Output ID)**: 最终生成图像的 `Save Image` 或 `Preview Image` 节点。**（必选）**

### 三、放置并配置
1.  将导出的 `.json` 文件放入插件的 `workflow` 目录中。
    *   路径为: `data/plugin_data/astrbot_plugin_comfyui_pro/workflow/`
2.  **重载插件**，然后刷新网页，再次**重载插件**，这样才可以看到你刚才放进的workflow。
3.  进入插件设置，在"工作流设置"中：
    *   选择你刚刚放入的 `.json` 文件。
    *   填入你记下的 **节点ID**。
4.  **完成！** 现在你的机器人就可以使用这个专属工作流进行绘画了。

---

## 主要功能

### ComfyUI 深度集成
*   **便捷工作流导入**: 完美支持 ComfyUI 的 API 格式工作流，让你专注于创意。
*   **多工作流热切换**: 在 AstrBot 后台或通过管理员指令，随时切换不同的模型和风格（如 SDXL、二次元、写实、特定 LoRA 流等）。
*   **智能参数注入**: 自动将提示词注入到你指定的输入节点，并智能寻找种子节点以实现随机化，避免生成重复图片。
*   **步数覆盖**: 支持按节点 ID 精确控制步数，适配复杂多面板工作流。

### 智能 LLM 绘图
*   **自然语言生图**: 用户只需说"帮我画一个..."，LLM 即自动分析、优化并生成高质量英文提示词，触发绘图，真正实现"开箱即用"。
*   **指令生图**: 为高级用户保留了传统的 `/画图` 指令，可直接输入英文 Tag 进行精准控制。
*   **高度可定制**: 可通过 Persona 文件管理多套系统提示词，在聊天中动态切换，定制你的"AI 绘画助手"人设和回复风格。
*   **当前触发格式**: 插件会从 LLM 回复中提取 `<pic prompt="...">` 标签作为绘图提示词，并会自动忽略 `<think> ... </think>` 内容。
*   **注意事项**：如果你自定义了 System Prompt，请确保需要出图时最终回复里包含合法的 `<pic prompt="英文 tags">`；如果没有这个标签，插件就不会触发绘图。

### Persona 提示词管理 (v2.4)
*   **文件化管理**: 提示词以 `.txt` 纯文本文件存储在 `persona/` 目录中，便于编辑和版本管理。
*   **WebUI 切换**: 在插件配置页面的下拉菜单中直接选择 Persona 文件。
*   **终端切换**: 管理员通过 QQ/终端发送指令切换当前 Persona：
    *   `/comfy_prompt_ls` — 列出所有 Persona 文件及预览
    *   `/comfy_prompt_use <序号>` — 切换当前使用的 Persona
*   **优先级**: Persona 文件 > WebUI 文本框输入（回退项）

### Profile 一键配置 (v2.4)
*   **一键绑定**: 将工作流文件 + 正/负面提示词节点 ID + 输出节点 ID + Persona 文件绑定为一个 Profile 配置文件。
*   **WebUI 管理**: 配置页面提供 Profile 文件下拉选择，以及现场保存功能。
*   **终端指令**:
    *   `/comfy_profile_ls` — 列出所有 Profile 绑定详情
    *   `/comfy_profile_use <序号>` — 一键应用 Profile（自动切换工作流+节点ID+Persona）
    *   `/comfy_profile_save <名称>` — 将当前配置保存为 Profile

### 风控与权限
*   **分级违禁词过滤**: 内置 `Lite` 和 `Full` 两级敏感词库，支持中英文过滤，可为不同群组设置不同策略。
*   **白名单与全局锁定**: 可设置仅在白名单群组生效，或一键开启"全局锁定"，仅允许管理员使用。
*   **锁定命令开关**: 支持 `/comfy_lock on|off|status` 动态切换全局锁定，也可以在配置里关闭这个命令入口。
*   **管理员特权**: 管理员可配置"无视冷却"、"无视白名单"、"无视敏感词"等超级权限。
*   **统一管理员**: 使用 AstrBot 系统管理员列表，无需在插件中重复配置管理员 ID。

---

## 详细配置说明

在 AstrBot 仪表盘 -> 插件 -> `astrbot_plugin_comfyui_pro` 中点击设置：

### 1. ComfyUI 连接
*   `Server Address`: 你的 ComfyUI 运行地址，默认为 `127.0.0.1:8188`。

### 2. 工作流设置 (Workflow Settings)
*   `JSON File`: **(核心)** 选择一个你已放入 `workflow` 文件夹的工作流文件。
*   `Input Node ID`: **(核心)** 你的工作流中，接收正向提示词的节点 ID。
*   `Neg Node ID`: 接收负面提示词的节点 ID（如无，可填任意不存在的 ID）。
*   `Output Node ID`: **(核心)** 输出图片的节点 ID。

### 3. 配置档案 (Profile Settings) — v2.4 新增
*   `Profile File`: 选择一个 Profile 配置文件，自动同步切换工作流、节点 ID 和 Persona。留空则使用各自的独立配置。
*   `Save Profile Name`: 调整好上方各设置后，在此输入档案名称并保存，当前配置将作为 Profile 持久化。

### 4. LLM 设置 (LLM Settings)
*   `Persona File`: 选择 Persona 提示词文件（`persona/` 目录下的 `.txt` 文件）。选择后优先使用文件内容。
*   `System Prompt`: 当上方 Persona 文件未选择时，使用此文本框内容作为回退提示词。
    > **CRITICAL**: 插件当前通过正则表达式 `<pic\s+prompt="(.*?)">` 提取绘图提示词。无论你如何修改 System Prompt，**必须**确保最终 LLM 的回复在需要出图时包含合法的 `<pic prompt="...">` 标签，否则插件将无法触发绘图。

### 5. 访问控制 (Control)
*   **管理员**: 插件使用 AstrBot 系统管理员列表（在 AstrBot WebUI 权限管理中设置），无需重复配置。
*   **白名单群**: 设置允许使用插件的群号列表。
*   **冷却时间**: 防止用户刷屏。
*   **全局锁定**: `lockdown` 为静态总开关，开启后仅管理员可用。
*   **锁定命令开关**: `lockdown_command_enabled` 控制是否允许管理员使用 `/comfy_lock on|off|status` 动态切换锁定状态。
*   **违禁词策略**: 为私聊和群聊设置默认的敏感词拦截等级 (none/lite/full)。

---

## 指令与用法

### 方式一：自然语言对话 (推荐)
直接与机器人对话，让它帮你画。
*   **你**: "帮我画一只猫，赛博朋克风格，在下雨的东京街头"
*   **机器人**: (分析需求 -> 生成 Prompt -> 调用 ComfyUI -> 发送图片)

![llm演示](https://raw.githubusercontent.com/lumingya/astrbot_plugin_comfyui_pro/main/assets/llm.png)

### 方式二：直接指令
*   `/画图 <提示词>`: 以合并转发的方式发送图片。

![指令演示](https://raw.githubusercontent.com/lumingya/astrbot_plugin_comfyui_pro/main/assets/draw.png)

*   `/画图no <提示词>`: 直接发送图片，更简洁。

![指令演示](https://raw.githubusercontent.com/lumingya/astrbot_plugin_comfyui_pro/main/assets/drawno.png)

### 方式三：管理指令 (仅管理员)
**工作流管理：**
*   `/comfy_ls`: 列出所有可用的工作流，并显示序号。
*   `/comfy_use <序号> [input_id] [output_id]`: 通过序号快速切换工作流，该方法不需要重载插件。
*   `/comfy_save <文件名>`: 通过分享 JSON 文件导入新的工作流。

**Persona 管理 (v2.4)：**
*   `/comfy_prompt_ls`: 列出所有 Persona 文件及首行预览。
*   `/comfy_prompt_use <序号>`: 切换当前使用的 Persona 提示词。

**Profile 管理 (v2.4)：**
*   `/comfy_profile_ls`: 列出所有 Profile 配置及其绑定的工作流、节点 ID、Persona。
*   `/comfy_profile_use <序号>`: 一键应用 Profile（自动切换工作流 + 节点 ID + Persona）。
*   `/comfy_profile_save <名称>`: 将当前配置保存为 Profile 文件。

**步数覆盖：**
*   `/comfy_add <节点ID> <步数>`: 按节点 ID 设置步数覆盖。
*   `/comfy_add <节点ID> off`: 取消单个步数覆盖。
*   `/comfy_add list`: 查看当前工作流的覆盖配置。
*   `/comfy_add clear`: 清空当前工作流的所有步数覆盖。

**其他：**
*   `/comfy_lock on|off|status`: 动态查看或切换全局锁定状态。
*   `/违禁级别 <none/lite/full>`: 调整当前群的敏感词拦截等级。
*   `/comfy帮助`: 查看所有可用指令及当前状态。

---

## 常见问题 (FAQ)

**Q: 为什么 LLM 回复了 `<pic prompt="...">`，但没有出图？**
A: 99% 的可能是配置问题。请检查：
    1.  你的 ComfyUI 服务是否已在本地成功启动？
    2.  插件设置中的 `Input Node ID` 是否填写正确？这是最常见的错误。
    3.  `system_prompt` 是否仍然要求在需要出图时输出合法的 `<pic prompt="英文 tags">` 标签？
    4.  后台日志中是否有报错信息？

**Q: 我新添加的 `.json` 文件在插件设置的下拉菜单里看不到？**
A: 这是 AstrBot 的缓存机制导致。请在后台 **"重载插件"**，然后 **"刷新你的浏览器网页 (F5)"**，然后再次**"重载插件"**，新的选项就会出现。

**Q: 生成的图片总是一样的？**
A: 插件会自动寻找并修改名为 `seed` 或 `noise_seed` 的参数。如果你的工作流使用了非常规的自定义种子节点，插件可能无法识别。

---

## Version 2.4 更新日志

### 新增：Persona 提示词管理系统
*   提示词以 `.txt` 纯文本文件存储在 `persona/` 目录中
*   WebUI 配置页面提供 Persona 文件下拉选择
*   终端指令 `/comfy_prompt_ls` 和 `/comfy_prompt_use` 支持动态切换
*   优先级链：终端命令 > WebUI 下拉 > 文本框回退

### 新增：Profile 一键配置系统
*   将工作流 + 节点 ID + Persona 绑定为 Profile 配置文件
*   WebUI 配置页面提供 Profile 文件选择与现场保存
*   终端指令 `/comfy_profile_ls`、`/comfy_profile_use`、`/comfy_profile_save` 支持管理

### 修改：管理员系统统一
*   移除插件自定义管理员白名单 (`admin_ids`)
*   改用 AstrBot 框架内置管理系统 (`event.is_admin()`)
*   兼容新版 AstrBot 的多用户配置文件 (`abconf_*.json`)

---

## Version 2.0 更新日志

### 新增：步数覆盖功能（按节点ID精确控制）
针对复杂工作流（如包含多个 ParameterControlPanel 的场景），现在可以按节点ID单独设置步数，彻底解决了"ComfyUI 前端修改参数会影响插件生成"的问题。

新增指令：`/comfy_add`

```
/comfy_add <节点ID> <步数>           单个设置
/comfy_add <ID1> <步数1> <ID2> <步数2>   批量设置
/comfy_add <节点ID> off              取消单个覆盖
/comfy_add list                      查看当前工作流的覆盖配置
/comfy_add clear                     清空当前工作流的所有覆盖
```

特性：
*   覆盖配置按工作流文件独立存储（工作流名.steps.json）
*   支持一个工作流配置多个不同节点的步数
*   切换工作流时自动加载对应的覆盖配置
*   完全脱离 ComfyUI 前端状态，插件生成参数独立可控

### 优化
*   `/comfy_ls` 现在会显示每个工作流的覆盖配置数量
*   `/comfy_use` 切换工作流时自动排除配置文件（.steps.json）
*   优化 ParameterBreak 节点检测逻辑，自动识别无需硬编码节点ID

### 新的目录结构

```
data/plugin_data/astrbot_plugin_comfyui_pro/   # 持久化目录（更新不丢失）
├── workflow/                                   # 你的工作流文件
│   ├── workflow_api.json
│   └── my_custom_workflow.json
├── persona/                                    # Persona 提示词文件 (v2.4)
│   └── default.txt
├── profiles/                                   # Profile 配置文件 (v2.4)
│   └── default.json
├── output/                                     # 生成的图片
│   └── *.png
└── sensitive_words.json                        # 敏感词配置
```

---

## Hardening Notes

*   `server_address` 自动规范化（去空格、补协议、去末尾 `/`）
*   HTTP 请求有合理的超时设置和重试机制（429/502/503/504 自动退避重试）
*   插件更新不会覆盖 `data/plugin_data/` 下的持久化数据

### Recommended configuration hygiene

*   Prefer a direct ComfyUI URL such as `http://127.0.0.1:8188`
*   Avoid trailing slashes in `server_address`
*   If your ComfyUI runs behind a reverse proxy, make sure long-running requests are allowed enough upstream timeout budget
*   Treat workflow execution as asynchronous and expect queue wait time under load
