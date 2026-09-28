# astrbot_plugin_ebooks

在 QQ 等聊天平台里，用命令或直接对 AI 说话来搜索、下载电子书。支持的书源：

| 书源 | 需要准备 | 能做什么 | 说明 |
|---|---|---|---|
| **Calibre-Web** | 自建书库的地址 | 搜索、下载、随机推荐 | 你自己的书库，最稳定 |
| **Z-Library** | 账号 + 可用的镜像地址 | 搜索、下载 | 书最多；每天下载次数有限（通常约 10 次）；结果里显示版次 |
| **Anna's Archive** | 会员 secret key（可选）、playwright | 搜索、下载 | 能找到 Z-Library 已下架的书；每天约 25 次快速下载 |
| **archive.org** | 无 | 搜索、下载 | 只收 PDF/EPUB，只能在线借阅的书会被排除 |
| ~~Liber3~~ | — | — | 2026-09 起后端已无法访问，保持关闭 |

## 快速上手

1. 安装插件（需要 AstrBot ≥ 3.5.15；重排模型功能需要 AstrBot ≥ 4.0）。依赖见 `requirements.txt`，AstrBot 安装插件时会自动装好。
   要用 Anna's Archive 的话，还需要在 AstrBot 所在环境里装 playwright 和 chromium
   （`pip install playwright && playwright install chromium`），用来通过它的人机验证。
2. 打开 AstrBot 管理面板 → 插件 → ebooks → 配置：至少开启一个「平台 · 启用 …」开关，
   并填好这个平台需要的地址或账号（见下文[配置项](#配置项)）。
3. 在聊天里发 `/ebooks search 生理学` 试一下。想下载哪本，就把结果里的「下载命令」原样发出去。

需要走代理时，给 AstrBot 进程设置 `https_proxy` / `http_proxy` / `all_proxy` 环境变量即可，插件会自动使用。

## 命令

### 常用：所有平台一起搜

| 命令 | 作用 |
|---|---|
| `/ebooks search 关键词 [数量]` | 在所有已启用的平台同时搜索。数量指**每个平台**取几条，默认是「搜索 · 默认返回条数」，最多 50 |
| `/ebooks download 参数` | 下载。参数就是搜索结果「下载命令」里的内容，插件会自动判断是哪个平台的书 |
| `/ebooks help` | 在聊天里显示命令说明 |

`/ebooks download` 能识别的参数：Calibre-Web 或 archive.org 的下载链接、
Anna's Archive 的 `A` + 32 位 ID、Z-Library 的「ID Hash」两段（只给 ID 会提示你补上 Hash）。

### 单独搜某个平台

| 命令 | 作用 |
|---|---|
| `/calibre search 关键词 [数量]` | 搜 Calibre-Web，数量 1–100 |
| `/calibre download 下载链接` | 下载 Calibre-Web 里的书 |
| `/calibre recommend 数量` | 从书库里随机推荐 1–50 本 |
| `/zlib search 关键词 [数量]` | 搜 Z-Library，数量最多 60 |
| `/zlib download ID Hash` | 下载 Z-Library 的书，例如 `/zlib download 11033158 e5897f` |
| `/annas search 关键词 [数量]` | 搜 Anna's Archive，数量最多 60 |
| `/annas download ID` | 下载 Anna's Archive 的书，ID 是 `A` + 32 位 md5 |
| `/archive search 关键词 [数量]` | 搜 archive.org，数量最多 60 |
| `/archive download 下载链接` | 下载 archive.org 的书 |

### 数量怎么写

- 数量写在关键词后面，用空格隔开：`/zlib search Python 10`。不写就用默认值。
- **末尾 1–3 位的数字都会被当成数量**。书名本身以数字结尾时（比如「Python 3」），
  在后面再补一个数量：`/ebooks search Python 3 10`。
- 4 位及以上的数字（年份、ISBN）会留在关键词里：`/ebooks search 生理学 2018` 搜的就是「生理学 2018」。
- 超过上限时：`/calibre` 会提示超出范围；其余命令按上限处理。

### 让 AI 帮你搜

插件给 AstrBot 的 AI 注册了两个函数工具，直接用自然语言说就行，比如
「帮我找一本 Fundamentals of Biostatistics 第 8 版」：

- `search_ebooks`：所有平台一起搜，效果等同 `/ebooks search`，每个平台取「默认返回条数」条
  （同样受「合并搜索每个平台最多取几条」限制，最多 50）。
- `download_ebook`：效果等同 `/ebooks download`。让 AI 下载时，把结果里的下载命令一起发给它最可靠；
  也可以直接发送下载命令。

随机推荐没有对应的函数工具，请用 `/calibre recommend`。

## 配置项

以下分组和管理面板里的顺序一致，面板里每项的名称都带着分组前缀（如「搜索 · 默认返回条数」）。

### 平台开关

| 面板名称 | 配置键 | 默认 | 说明 |
|---|---|---|---|
| 平台 · 启用 Calibre-Web | `enable_calibre` | 关 | 需要同时填「Calibre-Web · 地址」；地址留空时，插件启动会自动关掉这个开关 |
| 平台 · 启用 Z-Library | `enable_zlib` | 关 | 需要填 Z-Library 的站点地址和账号；账号留空时会自动关掉 |
| 平台 · 启用 Anna's Archive | `enable_annas` | 关 | 需要 playwright；建议同时启用 Z-Library（下载兜底用） |
| 平台 · 启用 archive.org | `enable_archive` | 开 | 不需要账号；服务器连不上 archive.org 时关掉 |
| 平台 · 启用 Liber3（已失效） | `enable_liber3` | 关 | 后端已无法访问，请保持关闭 |

### 各平台的地址和账号

| 面板名称 | 配置键 | 默认 | 说明 |
|---|---|---|---|
| Calibre-Web · 地址 | `calibre_web_url` | `http://127.0.0.1:8083` | 书库需要登录时写成 `http://用户名:密码@主机:端口`；账号密码不会出现在发给用户的下载命令里 |
| Z-Library · 站点地址 | `zlib_base_url` | `https://z-library.ec` | 必须是当前能用的镜像，镜像会不定期失效，见[常见问题](#z-library-登录失败返回了非-json-响应)|
| Z-Library · 登录邮箱 / 登录密码 | `zlib_email` / `zlib_password` | 空 | 登录会话过期时插件会自动重新登录 |
| Anna's Archive · 站点地址 | `annas_base_url` | `https://annas-archive.gl` | 主域名打不开时换镜像（`.li`、`.se`、`.org` 等） |
| Anna's Archive · 会员 secret key | `annas_secret_key` | 空 | 在网站 /account 页面查看。填了才能直接发文件，不填只返回下载链接 |
| Anna's Archive · 语言过滤 | `annas_language` | 空（不限） | 语言代码，如 `zh`、`en`、`ja`；推荐留空 |

### 搜索

| 面板名称 | 配置键 | 默认 | 说明 |
|---|---|---|---|
| 搜索 · 默认返回条数 | `max_results` | `20` | 命令里没写数量时用的值（1–100），AI 搜索也用它 |
| 搜索 · 最早出版年份 | `min_year` | `0`（不限） | 只要这一年及以后出版的书。只对 Z-Library 和 Anna's Archive 生效 |
| 搜索 · 合并搜索每个平台最多取几条 | `per_platform_results` | `20` | `/ebooks search` 每个平台取「命令里的数量」和这个值中较小的那个；`0` 表示不另外限制 |
| 搜索 · 合并搜索最多显示几条 | `merged_display_limit` | `30` | `/ebooks search` 排好序后只发前 N 本，`0` 表示全部发；被截掉时会提示还有几条没显示。不开重排时各平台轮流占名额，不会把排在后面的平台整个截掉 |

`/ebooks search` 的数量关系：每个平台取 `min(命令里的数量或默认条数, per_platform_results)` 条 →
汇总排序 → 发出前 `merged_display_limit` 条。
例：默认条数 10、每平台上限 60、最多显示 30，开两个平台 → 每个平台取 10 条，共 20 条参与排序，20 条全部显示。

### 排序（重排模型）

| 面板名称 | 配置键 | 默认 | 说明 |
|---|---|---|---|
| 排序 · 用重排模型排序结果 | `enable_rerank` | 关 | 用 AstrBot 里配置的重排序（Rerank）模型给结果排序，详见[搜索结果是怎么排的](#搜索结果是怎么排的) |
| 排序 · 指定重排模型 | `rerank_provider_id` | 空 | 留空用第一个已启用的重排序模型 |
| 排序 · 每次交给模型的候选条数 | `rerank_candidates` | `60` | 越多越可能把冷门的书排上来，但越慢 |
| 排序 · 每条候选交给模型的字数 | `rerank_doc_max_chars` | `300` | 每本书交给模型的文字长度上限，越长越慢 |
| 排序 · 重排模型指令 | `rerank_instruction` | 空 | 只在用 Qwen3-Reranker 时填写（推荐值见下），告诉模型怎样才算「找对了书」；留空按原样发送查询，适用于 bge 等其它重排模型 |
| 排序 · 各平台权重 | `platform_weight_calibre` / `_zlib` / `_annas` / `_archive` / `_liber3` | `1.0` | 只影响 `/ebooks search`：大于 1 往前排（1.5 轻微提升，10 基本置顶），小于 1 往后排，0 排到最后 |

重排模型要先在 AstrBot「服务提供商」→ 新增 →「重排序(Rerank)」里添加，支持 vllm、xinference、百炼、NVIDIA 等。
以 vllm 为例：`rerank_api_base` 填服务地址（会自动拼上 `/v1/rerank`），`rerank_model` 填模型名，超时建议 60 秒。

耗时参考（Qwen3-Reranker-4B）：80 条 × 300 字约 1.1 秒，80 条 × 1500 字约 3.8 秒。
`/ebooks search` 开启 Z-Library 时要调两轮模型（先在 Z-Library 内挑候选，再全局排序）。

**重排模型指令**默认留空。用 Qwen3-Reranker 时推荐填入：

> 给定用户的找书请求，判断文档描述的书是否正是用户要找的那本：书名要对得上；用户指定了作者、版次或年份时也必须符合，版次不符或看不出版次的书不算符合。

填写后插件会按 Qwen3-Reranker 的官方指令格式发送；对版次、作者这类要求，改这段话就能调整模型的判断，不需要改代码。
实测 Qwen3-Reranker-4B，查「Fundamentals of Biostatistics 8th edition」：不填时第 8 版 0.38、第 7 版 0.33；
填入推荐指令后第 8 版 0.54、第 7 版 0.31、看不出版次的书 0.36。其它重排模型不认这种格式，请保持留空。

注意平台权重是乘在得分上的：Calibre 权重 1.5 时，Calibre 里看不出版次的同名书（0.36 × 1.5 = 0.54）
仍可能和点名的版次打平甚至排到前面。

### 展示

| 面板名称 | 配置键 | 默认 | 说明 |
|---|---|---|---|
| 展示 · 合并转发 | `enable_merge_forward` | 开 | 结果打包成合并转发，每条最多 30 本，超过就分几条发。关闭后每本书单独一条消息，结果多时更不容易超时。随机推荐始终用合并转发 |
| 展示 · 显示封面 | `enable_cover_image` | 开 | 为每本书附上封面。会让搜索变慢、消息变大，QQ 合并转发超时（约 180 秒）时优先关掉它 |
| 展示 · 封面最大边长（像素） | `cover_max_size` | `400` | 封面缩小压缩后再发，每张约 10–20 KB；`0` 表示发原图 |

### 内容过滤

| 面板名称 | 配置键 | 默认 | 说明 |
|---|---|---|---|
| 过滤 · 启用内容过滤 | `enable_content_filter` | 开 | 用插件自带的敏感书目词表 + 补充关键词检查书名、作者、出版社和简介，命中就不显示这本书 |
| 过滤 · 启用内置禁书词表 | `enable_builtin_filter_keywords` | 开 | 插件自带的默认词表（`_filter_keywords.txt`，随仓库分发）。关掉就只用补充关键词 |
| 过滤 · 补充屏蔽关键词 | `extra_filter_keywords` | 空 | 在自带词表之外追加，简繁体都会匹配 |

补充关键词的匹配规则：

- 单个字：忽略。
- 两个字：前后都不是文字时才算命中，所以「出版」不会命中「人民出版社」。
- 纯英文或数字：按整个单词匹配、区分大小写，允许复数和时态词尾（`fuck` 能拦下 `fucking`，`anal` 不会误伤 `analysis`）。
- 其余三个字及以上：只要出现就算命中。

注意 `adult`、`amateur`、`morphine`、`narcotic` 这类词本身就是医学、护理书的常用词，加进来会误伤正常书籍。
被过滤掉多少条，日志里有「内容安全过滤丢弃 N 条」。插件不使用 AstrBot 全局的内容安全词表：
那套是给聊天内容准备的，单字、双字词会大面积误伤书名。

## 搜索结果是怎么排的

**Calibre-Web**：插件按书名匹配程度排序：书名完全一样 > 书名以关键词开头 > 书名包含关键词 >
多个关键词都在书名里 > 作者命中 > 部分命中 > 没命中。书名里的标点（《》、：、空格）不影响匹配。
整句搜不到时（比如「作者 - 年份 - 书名」这种混合写法），会自动拆成单个词分别搜再合并。

**Z-Library**：

- 不开重排：保持 Z-Library 网站自己的排序（同时参考它的「热门」和「最佳匹配」两种排序）。
  结果都不太对得上时，会自动多翻一页。
- 开启重排：模型从上百条候选里挑。候选名额有限，会优先放进：查询里点名的版次 → 书名等于关键词 →
  书名以关键词开头 → 书名包含关键词 → 关键词都出现在书名或简介里的书，剩下的名额按网站排序补满。
- **版次**：查询里写了版次（`8th edition`、`eighth edition`、`8e`、`第8版`、`第八版`，
  或关键词末尾单独的 `8th`、`eighth`）时，这一版会排在最前，结果里也会显示「版次: 第 N 版」。
  Z-Library 把英文单词 `edition` 当成书名里必须有的词，
  而版次其实存在单独的字段里，所以插件发给它的查询会去掉 `edition` 这个词，否则反而搜不到。
- **不按书名去重**：同名的第 7 版和第 8 版、不同年份的同名期刊都是不同的书。只有格式相同、ISBN 相同，
  并且文件大小或文件哈希也相同的记录才会合并，所以同一版书的不同上传版本可能会并列出现。

**archive.org**：先把整个查询当作书名里的完整短语搜。搜不到时（多了作者名、版次等词），改成「每个词都要出现在书名或作者里」，
仍搜不到就从末尾逐个去掉词再试（最多再试 3 次，至少保留 2 个词）。例：「fundamentals of biostatistics 8th edition」
去掉「8th edition」后能搜到 Rosner 的书，「rosner biostatistics」直接命中。结果按下载量排序。
只能在线借阅的书下载不了，会被排除；搜到的书全是借阅书时，提示「找到 N 本，但都只能在线借阅」，而不是「未找到」。

**`/ebooks search`（所有平台一起搜）**：

- 不开重排：按平台分段展示，平台先后由平台权重决定（大的在前）。超过「最多显示几条」时各平台轮流占名额。
- 开启重排：所有平台的书放在一起统一排序，得分乘以平台权重。各平台轮流占候选名额，保证每个平台都有书被模型看到。
- 每个平台最多等 90 秒，超时的平台会显示一条「搜索超时，本次已跳过该平台」，不会拖住其它平台的结果。
- 平台出故障（书库连不上、账号密码错误、接口报错）时会明确提示，不会显示成「未找到匹配的电子书」。

**简介摘录**：简介常有上千字，像「糖酵解」这种词多半在目录里、几百字之后。卡片上的简介（约 150 字）和交给模型的文字，
都会截取「开头一段 … 关键词出现的那一段 …」，一眼能看出这本书为什么被搜出来。没有命中、或者命中本来就在开头时，照旧截取开头。
合并搜索里交给模型的每条书目文字，在截断前还会丢掉下载命令和 MD5 行，并把语言/文件/ISBN 等元数据行挪到简介之后
——按字数截断时先丢的是元数据，而不是命中片段。

**简介里的 HTML**：Z-Library 一半以上的简介带 HTML 标签（archive.org 也有）。插件拿到结果时就转成纯文本：
`<br>`、`<p>` 等换行，目录会一行一条地显示；列表项前加「· 」；`<b>`、`<span>` 这类标签直接去掉；
`&nbsp;`、`&amp;` 等转义字符还原成正常字符。内容过滤、关键词匹配、简介摘录用的都是转换后的文字。

## 下载说明

- 下载的书以文件形式直接发到聊天里。临时文件放在 AstrBot 的 `data/temp/ebooks-*/` 下，发送后按文件大小延时自动删除
  （约 1 MB/秒估算，最少 5 秒、最多 30 分钟）。AstrBot 在这段时间里重启会来不及删，所以插件每次启动时会清掉
  1 小时以前的 `ebooks-*` 目录。
- Z-Library 同一时间只下载一本，后来的请求会排队并提示「已有下载任务进行中」。下载时边下边写入临时文件，
  大文件不会整本占用内存。
- Anna's Archive 下载优先级：会员直链 → 用 Z-Library 下载同一本书（按 md5 匹配）→ 返回下载链接让你自己下。
- 大文件很慢：下载服务器实测只有 60–190 KB/s，100 MB 的书要 20 分钟以上。中途断线会自动续传，
  日志里会反复出现「下载中断（…字节，第 N 次），续传重试」，只要字节数在增加就是正常的。
  插件在最后会核对文件大小，不会把只下了一半的文件发出去。
- **下载次数有限**：Anna's Archive 会员每天约 25 次快速下载（只有成功拿到下载地址才扣次数）；Z-Library 每天通常约 10 次。

## 常见问题

### Z-Library 登录失败：返回了非 JSON 响应

日志里是 `/eapi/user/login 返回了非 JSON 响应（HTTP 513, text/html）`：说明「Z-Library · 站点地址」填的镜像已经失效，
返回的是错误网页。镜像会不定期更换，换之前先在服务器上测一下，返回 `200` 才可用：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://z-library.ec/eapi/user/login -d 'email=你的邮箱&password=你的密码'
```

2026-09 实测：`z-library.ec` 可用；`z-lib.sk`、`z-library.sk`、`1lib.sk`、`z-lib.fm` 都返回 513。

### Z-Library 下载报 `Invalid credentials`

登录会话过期了。插件会自动重新登录再试一次；如果还是失败，提示里会写明重新登录失败的原因，
多半是镜像失效或账号密码被改过，按上一条的方法检查镜像。

### Anna's Archive 能搜到、标着来自 Z-Library，插件的 Z-Library 却搜不到

Anna's 结果里的「zlib」只是**来源标记**：意思是它以前从 Z-Library 备份过这个文件，不代表 Z-Library 现在还有。
教材类的书经常被 Z-Library 下架（例：Rosner《Fundamentals of Biostatistics》第 8 版）。这种书只能开启 Anna's Archive 来下载。

想确认的话：在 Anna's 这本书的详情页里找到 `zlib:数字` 这个编号，看它在 Z-Library 的搜索结果里还在不在。

### `[Anna's Archive] 无法获取直链：Invalid domain_index or path_index`

这本书不在 Anna's Archive 自己的下载服务器上。插件会自动改用 Z-Library 按同一 md5 下载，
所以启用 Anna's Archive 时建议同时配好 Z-Library。

### Anna's Archive 搜索报 403 或很慢

Anna's Archive 的搜索页有人机验证（DDoS-Guard），插件用无头浏览器自动通过，需要装好 playwright 和 chromium。
通过一次要 20–45 秒，之后 30 分钟内不用再等；所以重启后的第一次搜索会明显慢一些。

### 下载卡住或反复失败

看日志里 `下载中断（X/Y 字节，第 N 次）` 的 X：

- **X 在涨**：正常续传，耐心等。
- **X 长期不动**：下载地址过期了，重新发一次下载命令。
- **超过 45 分钟还没下完**：带宽不够，可以换 Anna's Archive 镜像，或改用 Z-Library 下载。

### 搜索结果不太相关，没有我要的那本

- 换更短、更准的关键词（书名的主干、作者名），或者换个平台搜。
- 开启重排模型，对「书名里有某个词 + 指定作者」这类查询效果很明显。
- Z-Library 对中文按字拆开匹配，搜「植物保护案例分析」会混进一堆只含「案例」「分析」的书。插件会自动多翻页，
  但书库里本来就没有的书也变不出来。

### 英文书莫名其妙被过滤掉

日志里「内容安全过滤丢弃 N 条」偏多时，检查「过滤 · 补充屏蔽关键词」里的英文词，
`adult`、`amateur`、`morphine`、`narcotic` 这类词会拦下正常的医学、护理书。规则见[内容过滤](#内容过滤)。

## 测试

- `tests/test_zlib_source.py`：不依赖 AstrBot，装好本插件的依赖后直接跑 `python -m unittest discover -s tests`。
- `tests/astrbot_env/`：完整的回归测试，插件只能在 AstrBot 里导入，所以在 AstrBot 的 Docker 容器里跑：
  `tests/astrbot_env/run.sh`（容器名不是 `astrbot` 时设 `ASTRBOT_CONTAINER`）。不联网，也不读取你的插件配置。

## 版本信息

- **插件名称**：ebooks
- **作者**：buding
- **版本**：2.0.1
- **源码**：[GitHub](https://github.com/zouyonghe/astrbot_plugin_ebooks)
