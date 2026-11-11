# B 站直播间对话

<img src="logo.png" alt="B 站直播间对话：小电视、弹幕气泡与语音声波" width="160">

一个可独立安装的 AstrBot 插件。它同时读取多个直播间的新弹幕，用 AstrBot 的聊天模型生成短回复，再通过已登录的 B 站账号发回该直播间。

支持多房间同时监听、可自定义唤醒词、AstrBot 人格、独立扫码登录、可选 BiliBot 登录与记忆联动，以及每个房间单独配置的语音和画面识别。

## 安装和使用

在 AstrBot WebUI「插件管理」中选择通过仓库链接安装，输入：

```text
https://github.com/Eason4869/astrbot_plugin_bilibili_live_chat
```

已有旧版时，如果上传 ZIP 提示目录已存在，可用压缩包里的插件目录更新 AstrBot 的 `data/plugins/astrbot_plugin_bilibili_live_chat`，保留 `data/config/astrbot_plugin_bilibili_live_chat_config.json`，然后在 AstrBot 重载插件。

1. 在 AstrBot WebUI 的插件页上传本插件 ZIP，或将 `astrbot_plugin_bilibili_live_chat` 目录放进 `data/plugins/` 后加载。若依赖未自动安装，用 AstrBot 的 Python 环境安装本目录的 `requirements.txt`。
2. 在插件详情里打开「直播间对话」视图。新版面板也可从插件视图入口进入。页面需要支持插件 Pages 或 Views 的 AstrBot，建议使用 4.24.2 或更新版本。
3. 在「账号连接」生成二维码。用 B 站 App 扫码并在手机上确认，页面会自动更新账号状态。已在同一个 AstrBot 实例使用 BiliBot 时，也可以选择「复用 BiliBot」并使用该账号。
4. 在「对话设置」选择所有房间共用的聊天模型，并添加自定义唤醒词。可选用 AstrBot 已有的人格。默认唤醒词列表为空，支持忽略大小写与全半角差异的包含匹配。
5. 保存设置，在「直播间」输入数字房间号并点击「添加直播间」，最多可添加 20 个。点击各房间的「开始监听」或「停止监听」。每个房间的首批弹幕只用于建立监听起点，各自显示收发统计和接话窗口。

### 聊天命令

AstrBot 4.5.7 起可使用聊天命令。管理员在私聊发送 `/直播对话` 或 `/直播对话 状态` 可查看状态。发送 `/直播对话 登录` 获取二维码，手机确认后发送 `/直播对话 确认`。使用 `/直播对话 开始 <房间号>` 和 `/直播对话 停止 <房间号>` 管理单个房间；仅有一个房间时可省略房间号。群聊和非管理员会收到操作入口提示。

每个房间的启停选择会写入配置。修改共享设置、重载插件或重启 AstrBot 后，已开启的房间会自动恢复，手动停止的房间保持关闭。账号失效或配置未就绪时，房间会显示「已开启 · 待登录」或「已开启 · 待配置」。网络中断会重试。旧版房间号会自动导入，从未保存过启停选择的房间需开启一次。BiliBot 记忆开关可以在监听中切换。

## 对话设置

- 唤醒后默认有 90 秒的接话时间，窗口内其他观众也可以接话。成功发送正式回复续时 45 秒，同一轮最多 300 秒。再喊唤醒词不会重置这轮上限。初始窗口设为 0 时只回复带唤醒词的弹幕。
- 「窗口外低频附和」默认关闭。开启后默认每 180 秒最多尝试一次，候选采样概率为 50%。明显的提问会跳过，模型也可以选择安静。
- 同一账号下的所有直播间共用默认 12 秒发送冷却和每分钟 4 次发送尝试上限，正式回复与附和都计入。发送结果未知的请求不会重发。
- 正式回复和附和共用选定的 AstrBot 人格文本，可追加直播专用人格提示词，也可为两种回复分别填写附加提示词。人格内容在开始监听时读取。每个房间的观众上下文在内存中单独保留。可在 WebUI 填写忽略的 UID。

## 可选的 BiliBot 联动

可与 [BiliBot](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot) 2.0.3 同装。两项联动分别设置。

- **复用登录**。将「B 站登录来源」设为「复用 BiliBot 登录」。先在 BiliBot 中登录。插件从当前运行的 BiliBot 只读获取凭据，不复制到独立插件配置。凭据更换或刷新后，已开启的房间会用新凭据重连。BiliBot 停用时保留各房间的开启选择。若要切回自己的账号，选择「独立登录」。
- **直播记忆**。开启「联动 BiliBot 直播记忆」，即使用独立账号也可以联动。只召回能确认属于当前房间和当前 UID 的直播记忆。成功发送的正式回复会把当条弹幕和回复写到 BiliBot 的直播记忆。附和不读写记忆。BiliBot 未启动或接口不可用时，仍可使用房间的短期上下文对话。

## 音视频识别

在房间卡片展开「音视频识别」，分别开启语音或视频。两项默认关闭。插件自动查找运行环境中的 FFmpeg，未找到时使用 imageio-ffmpeg 依赖中携带的程序，通常无需修改系统 PATH。首次安装或升级时请等待 AstrBot 完成依赖安装。保存只会重新连接该房间，仍保留房间的启停选择。

共享「高级设置」中可以查看 AstrBot 实际使用的 FFmpeg 路径，也可手动指定程序文件或 bin 目录。远端或 Docker 部署时，这里填写远端机器或容器内的路径。少数没有兼容依赖包的系统仍需安装 FFmpeg 后指定路径。更新代码后如果 AstrBot 尚未安装新增依赖，可从插件管理执行依赖安装后重载。

- **语音**使用 AstrBot 默认语音识别，也可选择已配置的其他语音识别模型。语音片段上限默认 6 秒。转写命中同一组唤醒词时开启接话窗口，窗口内可以继续说。窗口外未命中的语音只可能低频附和。公开直播流里的语音可能包括主播、嘉宾和播放内容，转写不会把它们当成已确认的 B 站账号身份。
- **视频**默认每 5 秒抽一帧，使用当前对话模型看图，也可指定其他支持图片输入的模型。仅在正式回复涉及画面，或明显画面变化获选为低频附和时调用模型。只启用视频也能识别画面。模型看图失败时，房间面板会提示，普通文字对话仍可继续。
- **低频附和**的间隔和概率可按房间覆盖，留空继承共享设置。语音、画面和弹幕仍共用「窗口外低频附和」总开关；所有回复都遵守账号的发送冷却和每分钟上限。
- 转写使用短时 WAV，处理后清理，视频只保留近期帧。使用云端语音或视觉提供商时，相关片段或截图会交给所选提供商。断流时媒体通道单独重试，关闭房间时停止拉流。语音和视频同时开启会分别拉取音轨与画面。

## 环境自检

在控制台顶部点击「环境自检」，选择共享默认配置或某个房间，点击「开始自检」。使用房间时会采用该房间保存的语音和视觉模型覆盖项。检查过程中可以查看每一步的状态和耗时，单项失败后继续检查其余项。

- FFmpeg 会实际运行，读取内置 H.264/AAC 测试素材，验证音轨提取和 JPEG 抽帧。
- 语音模型会收到一段内置测试语音，并显示转写结果。
- 视觉模型会收到一张生成的数字图片，检查是否能正确读取图片内容。

模型检查会产生少量模型用量，失败原因会脱敏后显示在页面。自检在远端 AstrBot 进程中运行；重载或停用插件时会取消未完成的检查。

## 验证与来源

本插件使用网页弹幕快照轮询。高流量房间里，两次轮询之间滚出快照的弹幕可能收不到。真实可用性取决于 B 站接口和账号状态。可在授权的测试房间用自定义关键词验证监听、发回弹幕、续时和停止。

扫码流程参考 [BiliBot 2.0.3](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/blob/421a14219966403d3fbc59185802d9bf5e6a1af5/core/infra/bili/auth.py)，弹幕快照与发送参考 [BiliBot 1.7.3](https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot/blob/9f8f37bbc27b71d5f3cf0ea33edb61387da86028/core/live_danmaku.py)。关键词窗口和低频附和源自用户提供的补丁。插件采用 AGPL-3.0。

直播播放地址解析参考 [Bilibili Live Watcher](https://github.com/qingquanlingyu/astrbot_plugin_bilibililive_watcher/blob/master/bili_http.py)，转写与看图使用 [AstrBot 的模型接口](https://docs.astrbot.app/dev/star/guides/ai.html)。

语音自检素材来自 [AstrBot 的 STT 健康检查样本](https://github.com/AstrBotDevs/AstrBot/blob/v4.24.2/samples/stt_health_check.wav)。视频自检素材由本项目生成。

本项目为社区插件，与哔哩哔哩官方无隶属关系。Logo 使用小电视、弹幕气泡与声波表达直播对话功能。

## 开发验证

将仓库克隆为 `astrbot_plugin_bilibili_live_chat` 目录，并在其父目录运行：

```sh
python -m pip install -r astrbot_plugin_bilibili_live_chat/requirements.txt ruff
python -B -m unittest discover -s astrbot_plugin_bilibili_live_chat/tests
ruff check --no-cache astrbot_plugin_bilibili_live_chat
node --check astrbot_plugin_bilibili_live_chat/pages/live-chat/app.js
```

测试使用模拟的 AstrBot、模型和直播接口，不会发送真实弹幕。真实直播间端到端验证尚未进行。
