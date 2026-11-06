# 奶蛙阅读器 Interline

> 贴在别的窗口上的中文电子书阅读器，能压成一条、一键隐身，还有只奶蛙陪你念书。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)
![PyQt6](https://img.shields.io/badge/PyQt6-6.x-41cd52?style=flat-square&logo=qt&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078d4?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-8a8a8a?style=flat-square)

---

## 功能

### 浮在别的窗口上

- **无边框圆角面板**，拖顶栏移动，右下角拉大小
- **默认置顶**，可以关掉。整窗透明度 0.15–1.0；底板透明度是另外一项，只让背景变透，字始终是实的
- **四套皮肤**：墨蓝、纯黑、暖褐、纸白，`F10` 轮换
- 字号 10–40 px，滑条写多少屏幕上就是多少像素；行距、字体族也能调
- **一键隐身 `Ctrl+Alt+D`**：走 Win32 全局热键，焦点在别的程序里也能按，把阅读器和奶蛙一起藏掉
- 常驻托盘，点 × 只是收起来，退出在托盘菜单里。重复启动不会开第二个窗口

<p align="center">
  <img src="screenshots/skin-paper.png" width="320" alt="纸白皮肤">
  <img src="screenshots/one-line.png" width="320" alt="一行模式">
</p>

**一行模式（`F9`）**：整个窗口压成一条，只显示当前这句，宽度跟着字数走。顶部 8px 窄边用来拖位置，正文点一下翻下一句，方向键和滚轮也行。适合瞄两眼，不适合整章快读。

### 奶蛙陪读（`F8`）

这是这东西最不一样的地方。奶蛙是一只**独立悬浮窗**，规矩跟音乐软件的桌面歌词一样：主窗口照常看书，蛙浮在桌面上报当前这一句。

<p align="center">
  <img src="screenshots/pet-mode.png" width="540" alt="奶读模式：阅读器与奶蛙悬浮窗并存">
  <img src="screenshots/pet-thinking.png" width="280" alt="停下朗读后停在托腮的姿势">
</p>

**主窗和蛙各管各的。** 正文、目录、书库、设置、页码、朗读按钮全在主窗里，点「书库」就在主窗正常开书库，蛙一概不参与。拖蛙不会动主窗，主窗最小化、换主题、开书库，蛙只是继续念它的。为什么不做成"整窗变身"——变身期间正文/目录/书库全不可用，等于只能听，而且退出时还得记着还原窗口几何。拆开之后两边都是普通窗口，谁也不欠谁。

**十一种动作接的是真状态，不是装饰：**
- 朗读时**常驻「呼吸 + 口型」**——嘴跟着一句句开合，身子还是最安静的那套呼吸
- 隔几秒随机来个小动作：抬手放下、抬头看一眼、原地跑两步，播完自己回到念书
- **停下就抬手托腮停在那儿**，不循环
- 点一下蛙 = 摸它，捧腹大笑放出笑声；点气泡 = 开始/停止朗读
- 滚轮 / 方向键翻句，拖动挪位置；右键菜单能隐藏气泡、收起奶蛙

<p align="center">
  <img src="screenshots/pet-reading.png" width="760" alt="reading 动画的 24 帧口型循环">
</p>

**悬停工具条**：鼠标移到气泡上，外侧淡出一排五颗按钮——左三「上一句 / 播放切换 / 下一句」，右二「收起气泡 / 主面板切换」。播放键跟着朗读状态换图标。「收起气泡」只折掉气泡、蛙和朗读照旧；「主面板」是阅读器的显示/收起开关。悬停时工具带里还会跟着淡入一枚「第 3/12 章 · 42%」的进度标，带宽不够依次缩成「3/12 · 42%」「42%」。

<p align="center">
  <img src="screenshots/pet-tools.png" width="400" alt="站立形态气泡悬停时出现的五颗控制按钮">
  <img src="screenshots/pet-progress.png" width="400" alt="悬停气泡时工具带里出现的本章进度百分比">
</p>

**贴边形态**：拖到屏幕边缘附近松手，蛙就吸上去，改成"斜着露出半边身体"的姿态，八向都有。气泡跟着摆到屏幕内侧、尾巴转向蛙头；身体不动，只眨眼 + 压上来抬手；**朗读时嘴也跟着开合**。吸附判定按可见像素而不是窗口矩形（可见部分只占一两成），离屏幕边 10px 以内才吸，松手按住拖出来就变回站立。多屏时相邻那条边不吸，设置里可以整个关掉。

<p align="center">
  <img src="screenshots/pet-edge-reading.png" width="520" alt="八个贴边方向的闭口与张口对照">
  <img src="screenshots/pet-edge-tools.png" width="400" alt="顶部贴边形态气泡下方的五颗控制按钮">
</p>

- 主窗 × 只收阅读界面，**蛙留在桌面上接着念**——朗读本来就没停，把蛙一起藏掉等于声音还在人不见了。要连蛙一起消失用老板键
- 笑声、打招呼、自言自语可以单独关；出声时朗读音量自动压到 40%，不抢拍
- 蛙弹出不抢焦点，开着它照样能直接在主窗选中、搜索、滚正文
- 素材是低帧率关键帧，中间帧靠图生图补（不是求均值），所以动作不会双影；口型是构建期烘进精灵表、运行时换常驻状态，不新增定时器
- 底栏那颗蛙头图标从照片量出轮廓自绘，深浅主题直接换色，不额外存图片

### 听书

两套引擎，设置里挑音色：
- **系统语音**（Qt `winrt` 后端，离线可用，**默认**）
- **在线音色**（Edge 神经语音，更自然，需联网；连不上时自动回落系统语音，不改设置）

按句读、读到哪句高亮哪句；选中一段可从「这里」开始读。语速五档（0.8×–2.0×）。在线音色按句合成 MP3 并缓存，播当前句时后台预取下一句。在线接口非官方、无可用性保证，所以默认仍是离线系统语音。

<p align="center">
  <img src="screenshots/tts-over-code.png" width="420" alt="浮在代码上朗读，右键调语速和音色">
</p>

### 书库与标注

- 卡片 / 列表两种视图，显示全书进度（章 + 百分比）；文件不在了会标灰
- 目录、全书搜索，带命中数和上下跳转
- 全书书签，`Enter` 跳转、`Del` 删除；删书能撤销，书签一起回来

<p align="center">
  <img src="screenshots/library.png" width="420" alt="书库">
  <img src="screenshots/bookmarks.png" width="420" alt="书签">
</p>

### 格式

| 格式 | 说明 |
| --- | --- |
| `.txt` `.md` `.text` `.log` / 无扩展名 | 自动识别 UTF-8 / GB18030，按 `第X章` `Chapter N` `序章` `番外` 等切章；无标记按 2 万字分块 |
| `.epub` | zipfile + ElementTree 直接读 spine，不依赖第三方解析库 |
| `.html` `.htm` `.xhtml` | 去标签后按文本处理 |
| `.pdf` | pdfminer 按版面坐标取文本，用「首行缩进」断段而非行尾标点 |

单章超 6 万字会自动再切，免得单个富文本滚起来发涩。

## 快捷键

| 操作 | 默认键 | | 操作 | 默认键 |
| --- | --- | --- | --- | --- |
| 滚动一屏 | `Space` | | 添加书签 | `Ctrl+D` |
| 向上滚一屏 | `Shift+Space` | | 目录 | `Ctrl+T` |
| 朗读开关 | `F5` | | 搜索 | `Ctrl+F` |
| 一行模式 | `F9` | | 书库 | `Ctrl+L` |
| 奶读模式 | `F8` | | 老板键（全局） | `Ctrl+Alt+D` |
| 切换皮肤 | `F10` | | | |

头两项是正文区自带滚动、改不了。其余八项可在设置里改（右键 → 设置…），按下新组合即时生效，清空恢复默认。老板键被占用会自动换候选，设置页标出实际生效的那个。

<p align="center">
  <img src="screenshots/settings-keys.png" width="520" alt="快捷键设置">
  <img src="screenshots/settings-pet.png" width="360" alt="奶蛙设置">
</p>

## 下载即用

如果 [Releases](/releases) 有现成的 `Interline.exe`，下下来双击就能用；没有就按下面「打包」自己生成，跑起来一样。

单文件绿色版，不碰注册表和启动项，退出不留日志。数据在 `%LOCALAPPDATA%\Interline\`：

```
config.json    窗口位置、字体、皮肤、快捷键、朗读设置、奶蛙开关与位置
library.json   书库与阅读进度
marks.json     书签
tts-cache/     在线音色 MP3 缓存（按 文本+音色+语速 哈希）
```

想带着走就把 exe 放 U 盘，启动时加 `--portable`，数据改存 exe 同级的 `InterlineData\`。

正常情况下这里不会出现日志。只有某操作在 Qt 回调里抛了异常时会多一个 `error.log`——PyQt 默认遇到这种情况会直接 `abort()`（表现就是「点一下程序就没了」），所以程序接管了异常处理，把堆栈写进这个文件并继续跑。

## 从源码跑

```bash
pip install -r requirements.txt
python main.py              # 正常启动
python main.py --selftest   # 查 Qt / 解析层 / 奶蛙素材 / TTS 是否可用
python main.py --petcheck   # 奶蛙切帧与绘制冒烟，导出合成图
python main.py --edgecheck  # 贴边吸附：阈值用例 + 真窗口贴八向 + 口型落位
python main.py --toolcheck  # 工具条：八向落位/多档缩放不重叠/悬停链/3→2 行几何不变
python main.py --voicecheck # 在线音色链路：联网合成 → 解码 → 逐句播放
python main.py --pdfcheck a.pdf  # 只跑 PDF 解析，结果打到临时文件
```

调试时用 `set INTERLINE_DATA_DIR=D:\tmp\interline` 把数据目录指到别处，避免污染真实配置。

改奶蛙素材时 `make_pet_ai.py` 要额外装 `opencv-python` 和 `pillow`（仅构建期，不在 `requirements.txt`）。重生成中间帧会调几十次图像模型，成品帧放 `assets/naiwa/mids/` 随仓库走；`make_pet_sheet.py` 找不到成品帧会自动退回传统插值。贴边素材的规格与方向映射见 `assets/naiwa/edges/SPEC.md`。

## 打包

```bash
pip install pyinstaller
python -m PyInstaller Interline.spec --noconfirm
```

产物 `dist/Interline.exe`，约 65 MB。没开 UPX：单文件包本来就 deflate 过，UPX 只省一成左右，代价是每次启动多一层解压、还容易被杀软误报。spec 另剔掉 ffmpeg 解码后端和 `opengl32sw.dll`（合计 35 MB 上下），纯文字阅读用不到。

奶蛙的 `sheet.png`（2.9 MB）+ 5 个 mp3 由 spec 的 `datas` 逐条打进包；`datas` 不展开通配符，加素材要手动补一行，漏了不会崩、只会退化成空窗口，所以 `--selftest` 专门查素材。版本号只有一处：`main.py` 的 `BUILD_VERSION`。

## 已知限制

- **只有 Windows。** 老板键靠 Win32 `RegisterHotKey`，系统语音走 `winrt`；在线音色本身跨平台，但整个程序换平台仍跑不起来
- **在线音色依赖非官方接口**，可能限流或临时不可用；稳定离线使用就在设置里选系统音色
- **文件改名或移动，书签会失联**——书的 id 是路径 hash，换盘符当新书
- **PDF 是重排文本，非原版版式**；表格、多栏、图文混排效果差，扫描件读不出字
- **奶蛙只接了 9 组动作**，精灵图里另有 16 方向注视没接（属"眼睛跟鼠标转"路子，和现在状态机不是一套）
- **没有云同步、导出、加密。**

## 致谢

- 奶蛙精灵图：[timerring/codex-pet-naiwa](https://github.com/timerring/codex-pet-naiwa)（MIT）。中间帧由图像模型在该素材上生成，同样按 MIT 使用
- 奶蛙音效：同项目附带的语音包，仅供学习交流，**请勿商用**；发正式版请自行替换或去掉 `assets/naiwa/sfx/`
- 在线音色：[edge-tts](https://github.com/rany2/edge-tts)（LGPL-3.0）

## License

[MIT](LICENSE)
