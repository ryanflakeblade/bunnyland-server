# 野猪林：简体中文场景

这是根据《水浒传》野猪林情节手工编写的可分支改编，不是小说自动转换器。
三个地点为林间小路、古树空地、隐蔽树林。林冲、董超和薛霸从空地开始，
鲁智深在树林中跟随；开场时林冲尚未被捆绑，可以尝试脱身。

## 创建和继续世界

在项目目录的 PowerShell 中运行：

```powershell
uv run bunnyland serve --generator wild-boar-forest --seed 野猪林 --ticks 1 --tick-seconds 1 --time-scale 1 --save wild-boar-forest.json
```

第一轮只推进时钟并让控制器提出动作，动作下一轮才执行。因此这个存档保留开场状态。
使用不同文件名可以保留多份世界；重复使用同名文件会更新存档并轮转备份。

从存档继续五轮，并保存到另一文件：

```powershell
uv run bunnyland serve --load wild-boar-forest.json --ticks 5 --tick-seconds 1 --time-scale 1 --save wild-boar-forest-continued.json
```

默认四个角色使用确定性行为控制器，不需要大模型或 API 密钥。正常情况下，
董超准备动手，鲁智深现身并救下林冲。存档包含角色、关系、私有知识、观察记录、
场景阶段和游戏时间；未执行的命令不会保存，读档后控制器根据当前状态重新决定动作。

默认插件配置已经包含本场景所需的功能。如果自行使用 `--plugin` 指定插件列表，
须包含 `bunnyland.worldgen`、`bunnyland.core_verbs`、`bunnyland.persona` 和
`bunnyland.social`。CLI 的通用离线提示可能显示角色等待，但本场景的行为控制器仍会行动。

JSON 中中文可能写成 `\uXXXX` 转义，这是正常的 JSON 编码，读入后仍然是中文。
场景文本为简体中文；Bunnyland 原有界面、通用提示和错误信息仍可能使用英文。

## 接管角色与输入动作

### 四人使用 Qwen，真人按需接管

在已设置 `OPENROUTER_API_KEY` 和 `OPENROUTER_SERVER_URL` 的同一个 PowerShell
窗口运行（URL 指向阿里云，`openrouter` 是这里复用的兼容接口适配器）：

兼容端点缺少 `system_fingerprint` 时，客户端将其视为未知（`null`），
不影响角色动作解析；用量取自回复，不查询 OpenRouter 专用的账单接口。

```powershell
uv run --extra repl --extra server --extra llm bunnyland repl --generator wild-boar-forest --llm --chat-provider openrouter --chat-model qwen3.8-omni-flash --claim-fallback llm
```

这会新建世界，不会加载或改写已有的 `wild-boar-forest.json`。场景仍由手工定义，
四人的决策由 Qwen 负责，不再保证规则模式的救援结局。未启用 `--llm` 时仍是离线规则模式。

输入 `play 林冲` 接管林冲，其余三人继续由 Qwen 控制。输入 `play 鲁智深` 时，
先把林冲交回 Qwen，再接管鲁智深。输入 `release` 交还当前角色。
角色的知识、目标和关系保留；界面刷新保留控制权凭证，交还后沿用配置的模型和服务提供方。
可用 `say text=鲁智深，你怎么在这里？` 说话，其他角色按各自感知和目标决定是否回应。

自动运行期间，即使你没有输入，AI 角色仍可能调用模型并消耗 token；本命令没有费用上限。
退出本地程序会停止世界运行。这个本地 REPL 命令不自动保存世界。

`serve --ticks N` 是自动模拟，不会弹出聊天窗口。交互操作使用现有的
[REPL](clients/repl.md) 或 [TUI](clients/tui.md)，它们需要相应的可选客户端依赖。
例如在已安装 REPL 依赖的环境中，新建交互场景：

```powershell
uv run --extra repl bunnyland repl --generator wild-boar-forest --no-chat
```

进入后可使用 `who` 查看人物，再用 `play 林冲` 或 `play 鲁智深` 接管角色。
四个角色共用服务器的控制权、动作成本和验证规则，没有独立的“剧情管理员”玩家接口。
控制权交接会使旧控制器的命令失效，但不会清除目标、关系或记忆。
交还控制权的回退行为遵循客户端现有配置；管理员可将控制器重新指定为
`behavioral`，行为树名为 `wild-boar-forest`，恢复本场景的自动行为。

下列是 REPL 的命令格式；只能由对应角色在满足现场条件时执行：

| 角色 | 命令 | 条件和结果 |
| --- | --- | --- |
| 董超 | `forest-scene choice=准备` | 开场或差役迟疑时，形成威胁 |
| 林冲 | `forest-scene choice=求缓` | 已受威胁时，让差役暂时迟疑 |
| 林冲 | `move direction=出林` | 离开空地，进入脱险分支 |
| 鲁智深 | `move direction=现身` | 从隐蔽树林来到空地 |
| 鲁智深 | `forest-scene choice=救援` | 与林冲同处空地且存在威胁，救下林冲 |
| 薛霸 | `forest-scene choice=行凶` | 已形成威胁，林冲在场且鲁智深未在场保护时，林冲遇害 |
| 任意角色 | `say text=且慢动手！` | 正常说话，不会直接改写故事结局 |

林冲从空地经其他出口离开也算脱险。求缓只买到机会；董超可以再次准备。
救援、脱险、遇害三种结果互斥，结束后继续走动不会覆盖结果。
这不是完整的战斗或押解模拟，也不包含全书人物和后续章节。

## 信息边界

每人只知道自己的初始信息。两名差役知道加害计划；林冲只有戒备；鲁智深知道
自己在跟踪保护林冲。树林与空地隔开视线，远处人物不能直接读取现场阶段。
亲历的场景变化成为该角色的持久观察记录。已有的说话、私语和传闻机制可以传播信息，
但其他玩家查看角色时不能直接读取其私人目标或秘密。

世界存档是服务器的完整状态，包含全部秘密，不能把整个 JSON 当作玩家视图发送。
玩家和自动控制器应使用角色范围内的投影。
