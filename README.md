# 🎨 AstrBot Plugin ComfyUI Pro

## 需要特别提到的事：
本插件并没有提供ui来直接配置工作流的图片尺寸，步数，lora等的选项，这并不是为了增加上手难度，相反，这是为了提供最大的灵活性和兼容性。这个调整可以让插件适配所有（大概）工作流，只要你有，经过简单的几个步骤，你就可以通过本插件调用。
同时，为了提高对不同来源 AI 的兼容，我使用了正则来提取 LLM 回复中的绘图标签，而不是依靠 AI 本身的函数调用功能。因此如果出现问题，可以尝试关闭 AI 函数调用权限，并检查回复中是否正确包含 `<pic prompt="...">`。

## 介绍
一个功能强大的 AstrBot 插件，旨在将你本地的 **ComfyUI** 无缝集成到聊天机器人中。

它不仅支持基础的文生图，更创新的引入了 LLM（大语言模型）作为“提示词工程师”，能够将用户的日常对话自动转化为 ComfyUI 可识别的高质量英文 Prompt。同时，它对 ComfyUI 用户极其友好，支持便捷地导入和切换你自己的工作流，并提供了完善的权限控制和敏感词过滤系统。

导入工作流请务必参考教程。

## 🚀 核心优势：轻松使用你自己的工作流

本插件最大的特点就是让你几乎无缝地使用你在 ComfyUI 中已经搭建好的工作流。只需简单几步，就能将你的创意带入 AstrBot。

### 一、导出你的工作流
 在你的工作流界面，点击菜单的 **`Save (API Format)`** 按钮，将工作流导出为 `.json` 文件。

### 二、找到关键节点 ID
记下你的工作流中 **输入** 和 **输出** 节点的 ID。开启开发者模式后，ID 会显示在每个节点的标题上方。
*   **输入节点 (Input ID)**: 通常是接收提示词的 `CLIP Text Encode` 节点。**（必需）**
*   **输出节点 (Output ID)**: 最终生成图像的 `Save Image` 或 `Preview Image` 节点。**（必选）**

### 三、放置并配置
1.  将导出的 `.json` 文件放入插件的 `workflow` 目录中。
    *   路径为: `data/plugins/astrbot_plugin_comfyui_pro/workflow/`
2.  **重载插件**，然后刷新网页，再次**重载插件**，这样才可以看到你刚才放进的workflow。
3.  进入插件设置，在“工作流设置”中：
    *   选择你刚刚放入的 `.json` 文件。
    *   填入你记下的 **节点ID**。
4.  **完成！** 现在你的机器人就可以使用这个专属工作流进行绘画了。

---

## ✨ 主要功能

### 🔌 ComfyUI 深度集成
*   **便捷工作流导入**: 完美支持 ComfyUI 的 API 格式工作流，让你专注于创意。
*   **多工作流热切换**: 在 AstrBot 后台或通过管理员指令，随时切换不同的模型和风格（如 SDXL、二次元、写实、特定 LoRA 流等）。
*   **智能参数注入**: 自动将提示词注入到你指定的输入节点，并智能寻找种子节点以实现随机化，避免生成重复图片。

### 🤖 智能 LLM 绘图
*   **自然语言生图**: 用户只需说“帮我画一个...”，LLM 即自动分析、优化并生成高质量英文提示词，触发绘图，真正实现“开箱即用”。
*   **指令生图**: 为高级用户保留了传统的 `/画图` 指令，可直接输入英文 Tag 进行精准控制。
*   **高度可定制**: 你可以随时在后台修改 System Prompt，定制你的“AI 绘画助手”人设和回复风格。
*   **当前触发格式**: 插件会从 LLM 回复中提取 `<pic prompt="...">` 标签作为绘图提示词，并会自动忽略 `<think> ... </think>` 内容。
*   **注意事项**：如果你自定义了 System Prompt，请确保需要出图时最终回复里包含合法的 `<pic prompt="英文 tags">`；如果没有这个标签，插件就不会触发绘图。
  
### 🛡️ 完善的风控与权限
*   **分级违禁词过滤**: 内置 `Lite` 和 `Full` 两级敏感词库，支持中英文过滤，可为不同群组设置不同策略。
*   **白名单与全局锁定**: 可设置仅在白名单群组生效，或一键开启“全局锁定”，仅允许管理员使用。
*   **锁定命令开关**: 支持 `/comfy_lock on|off|status` 动态切换全局锁定，也可以在配置里关闭这个命令入口。
*   **管理员特权**: 管理员可配置“无视冷却”、“无视白名单”、“无视敏感词”等超级权限。

---

## ⚙️ 详细配置说明

在 AstrBot 仪表盘 -> 插件 -> `astrbot_plugin_comfyui_pro` 中点击设置：

### 1. ComfyUI 连接
*   `Server Address`: 你的 ComfyUI 运行地址，默认为 `127.0.0.1:8188`。

### 2. 工作流设置 (Workflow Settings)
*   `JSON File`: **(核心)** 选择一个你已放入 `workflow` 文件夹的工作流文件。
*   `Input Node ID`: **(核心)** 你的工作流中，接收正向提示词的节点 ID。
*   `Output Node ID`: **(核心)** 输出图片的节点 ID 。

### 3. LLM 设置 (LLM Settings)
*   `System Prompt`: 在这里编辑给 LLM 的系统提示词，定义它如何响应用户的画图请求。
    > ⚠️ **CRITICAL**: 插件当前通过正则表达式 `<pic\s+prompt="(.*?)">` 提取绘图提示词。无论你如何修改 System Prompt，**必须**确保最终 LLM 的回复在需要出图时包含合法的 `<pic prompt="...">` 标签，否则插件将无法触发绘图。
    >
    > 推荐同时保留 `<think> ... </think>` + `<pic prompt="...">` 的输出顺序，这与插件默认配置和多图分段逻辑保持一致。

### 4. 访问控制 (Control)
*   **管理员与白名单**: 设置管理员 QQ 号和允许使用插件的群号。
*   **冷却时间**: 防止用户刷屏。
*   **全局锁定**: `lockdown` 为静态总开关，开启后仅管理员可用。
*   **锁定命令开关**: `lockdown_command_enabled` 控制是否允许管理员使用 `/comfy_lock on|off|status` 动态切换锁定状态。
*   **违禁词策略**: 为私聊和群聊设置默认的敏感词拦截等级 (none/lite/full)。

---

## 📖 指令与用法

### 方式一：自然语言对话 (推荐)
直接与机器人对话，让它帮你画。
*   **你**: “帮我画一只猫，赛博朋克风格，在下雨的东京街头”
*   **机器人**: (分析需求 -> 生成 Prompt -> 调用 ComfyUI -> 发送图片)

![llm演示](https://raw.githubusercontent.com/lumingya/astrbot_plugin_comfyui_pro/main/assets/llm.png)

### 方式二：直接指令
*   `/画图 <提示词>`: 以合并转发的方式发送图片。
*   
![指令演示](https://raw.githubusercontent.com/lumingya/astrbot_plugin_comfyui_pro/main/assets/draw.png)

*   `/画图no <提示词>`: 直接发送图片，更简洁。

![指令演示](https://raw.githubusercontent.com/lumingya/astrbot_plugin_comfyui_pro/main/assets/drawno.png)

### 方式三：管理指令 (仅管理员)
*   `/comfy_ls`: 列出所有可用的工作流，并显示序号。
*   `/comfy_use <序号> [input_id] [output_id]`: 通过序号快速切换工作流，该方法不需要重载插件。
*   `/comfy_lock on|off|status`: 动态查看或切换全局锁定状态。
*   `/违禁级别 <none/lite/full>`: 调整当前群的敏感词拦截等级。
*   `/comfy帮助`: 查看所有可用指令。

---

## ❓ 常见问题 (FAQ)

**Q: 为什么 LLM 回复了 `<pic prompt="...">`，但没有出图？**
A: 99% 的可能是配置问题。请检查：
    1.  你的 ComfyUI 服务是否已在本地成功启动？
    2.  插件设置中的 `Input Node ID` 是否填写正确？这是最常见的错误。
    3.  `system_prompt` 是否仍然要求在需要出图时输出合法的 `<pic prompt="英文 tags">` 标签？
    4.  后台日志中是否有报错信息？

**Q: 我新添加的 `.json` 文件在插件设置的下拉菜单里看不到？**
A: 这是 AstrBot 的缓存机制导致。请在后台 **“重载插件”**，然后 **“刷新你的浏览器网页 (F5)”**，然后再次**“重载插件”**，新的选项就会出现。

**Q: 生成的图片总是一样的？**
A: 插件会自动寻找并修改名为 `seed` 或 `noise_seed` 的参数。如果你的工作流使用了非常规的自定义种子节点，插件可能无法识别。请尝试在设置中手动指定 `Seed Node ID`。

# 📋 Version 2.0.0 更新日志
✨ 新增：步数覆盖功能（按节点ID精确控制）
针对复杂工作流（如包含多个 ParameterControlPanel 的场景），现在可以按节点ID单独设置步数，彻底解决了"ComfyUI 前端修改参数会影响插件生成"的问题。


新增指令：/comfy_add（该指令正常无需使用，目前仅针对 ParameterControl节点）

text

/comfy_add <节点ID> <步数>           单个设置
/comfy_add <ID1> <步数1> <ID2> <步数2>   批量设置
/comfy_add <节点ID> off              取消单个覆盖
/comfy_add list                      查看当前工作流的覆盖配置
/comfy_add clear                     清空当前工作流的所有覆盖
使用示例：

text

/comfy_add 3839 20              # 底图部分设为20步
/comfy_add 3839 20 4521 50      # 同时设置底图20步、放大50步
/comfy_add list                 # 查看当前配置
特性：

覆盖配置按工作流文件独立存储（工作流名.steps.json）
支持一个工作流配置多个不同节点的步数
切换工作流时自动加载对应的覆盖配置
完全脱离 ComfyUI 前端状态，插件生成参数独立可控
🔧 优化
📂 工作流管理优化
/comfy_ls 现在会显示每个工作流的覆盖配置数量
/comfy_use 切换工作流时自动排除配置文件（.steps.json）
工作流列表更新时自动过滤配置文件
🛡️ 稳定性提升
优化 ParameterBreak 节点检测逻辑，自动识别无需硬编码节点ID
增加覆盖生效日志，方便调试确认

从 v2.0 开始，插件采用 AstrBot 规范的数据持久化方案。你的工作流、生成的图片、自定义敏感词等数据将存储在独立的数据目录中，**更新插件不会覆盖这些文件**。

### 📂 新的目录结构

```
data/plugin_data/astrbot_plugin_comfyui_pro/   # ✅ 持久化目录（更新不丢失）
├── workflow/                                   # 你的工作流文件
│   ├── workflow_api.json                       # 首次安装时自动复制
│   └── my_custom_workflow.json                 # 你自己添加的
├── output/                                     # 生成的图片历史
│   └── *.png
└── sensitive_words.json                        # 敏感词配置（可自定义）

plugins/astrbot_plugin_comfyui_pro/            # ⚠️ 插件目录（更新会覆盖）
├── main.py
├── comfyui_api.py
└── ...
```

### 🔄 迁移指南（从 v1.x 升级）

如果你之前已经在使用本插件：

1. **备份你的工作流文件**
   - 复制 `plugins/astrbot_plugin_comfyui_pro/workflow/` 下的所有 `.json` 文件

2. **更新插件**

3. **恢复工作流**
   - 将备份的文件放入新目录：`data/plugin_data/astrbot_plugin_comfyui_pro/workflow/`

4. **重载插件**（两次重载 + 刷新浏览器，老规矩）

> 💡 **提示**：你在 AstrBot 后台填写的配置（管理员ID、白名单群等）由框架管理，**不会丢失**，无需额外操作。

---

## ✨ 新增功能

### 🚀 首次安装自动初始化
- 首次安装插件时，会自动将插件自带的默认工作流和敏感词文件复制到数据目录
- 无需手动配置即可开始使用

### 📝 全新的日志与提示系统
所有提示信息现在都会告诉你**具体原因**，不再是简单的"禁止输入"：

| 场景 | v1.x | v2.0 |
|------|------|------|
| 群不在白名单 | `禁止输入。` | `🚫 本群(123456)不在白名单中` |
| 全局锁定 | `全局锁定。` | `🔒 全局锁定中，仅管理员可用` |
| 冷却中 | `请求太频繁...` | `⏱️ 冷却中，请在 30 秒后重试` |
| 权限不足 | `权限不足。` | `🚫 权限不足，仅管理员可查看工作流列表` |
| 敏感词拦截 | `检测到敏感词：xxx` | `🚫 检测到敏感词：xxx、yyy，无法生成图片` |

### 📊 更清晰的启动日志
```
[ComfyUI] 📂 数据目录: .../plugin_data/astrbot_plugin_comfyui_pro
[ComfyUI] 📋 已复制 6 个默认工作流
[ComfyUI] 🔄 工作流列表已更新: 13 个可用
[ComfyUI] 👤 管理员: 2 个 | 🏠 白名单群: 3 个
[ComfyUI] 🔒 敏感词库已加载: 1234 个词条
[ComfyUI] ✅ ComfyUI API 初始化成功
[ComfyUI] 🎨 插件初始化完成，LLM 工具已激活
```

---

## 🔧 优化与修复

### 代码优化
- 统一使用 `pathlib.Path` 处理路径，提高跨平台兼容性
- 重构权限检查逻辑，代码更清晰，维护更方便
- 优化 ComfyUI API 初始化，支持传入数据目录参数

### 帮助命令优化
- `/comfy帮助` 现在会显示更多状态信息（当前位置、违禁级别、冷却时间）
- 管理员可以看到数据目录路径，方便管理
- `/comfy_ls` 现在会高亮显示当前正在使用的工作流

### 其他改进
- 敏感词提示最多显示 5 个，避免消息过长
- 生成图片时会记录日志，方便追踪问题
- 工作流切换时的提示更加详细

---

## ⚠️ 注意事项

### 关于工作流存放位置
**从 v2.0 开始，工作流文件应该放在：**
```
data\plugin_data\astrbot_plugin_comfyui_pro\workflow
```
而**不是**之前的：
```
data\plugins\astrbot_plugin_comfyui_pro/workflow/
```

插件目录下的 `workflow` 文件夹现在仅用于存放**默认模板**，供首次安装时复制使用。

敏感词配置和图片存储同样在：
```
data\plugin_data\astrbot_plugin_comfyui_pro
```
### 关于配置保留
以下配置由 AstrBot 框架管理，**更新插件不会丢失**：
- ✅ ComfyUI 服务地址
- ✅ 工作流节点 ID 配置
- ✅ 管理员 ID 列表
- ✅ 白名单群列表
- ✅ 冷却时间、违禁级别等所有设置

---

## Hardening Notes

This plugin now applies a few compatibility-preserving safety defaults:

- `server_address` values are normalized automatically:
  - surrounding whitespace is trimmed
  - missing scheme defaults to `http://`
  - trailing `/` is removed
- HTTP requests now use sane default timeouts instead of waiting forever
- transient API failures (for example `429`, `502`, `503`, `504`) are retried with backoff
- existing configs remain valid; if you already provide a full URL, behavior is unchanged

### Recommended configuration hygiene

- Prefer a direct ComfyUI URL such as `http://127.0.0.1:8188`
- Avoid trailing slashes in `server_address`
- If your ComfyUI runs behind a reverse proxy, make sure long-running requests are allowed enough upstream timeout budget
- Treat workflow execution as asynchronous and expect queue wait time under load

