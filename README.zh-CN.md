# tgdl 使用说明

tgdl 用你自己的 Telegram 账号（不是机器人）登录，把指定群组里的文件下载到本机。支持同时下载多个文件，中断之后可以接着下。

命令行的输出是英文，方便脚本处理。这份说明是给人看的。

## 你需要准备

- Python 3.11 或更高版本
- 一个能收验证码的 Telegram 账号
- 在 <https://my.telegram.org> 申请的 `api_id` 和 `api_hash`

### 获取 api_id 和 api_hash

1. 用浏览器打开 <https://my.telegram.org> ，用手机号登录。
2. 进入 **API development tools**。
3. 如果还没有应用，按页面要求填一个应用名称（例如 `tgdl`）并创建。
4. 记下 **App api_id**（一串数字）和 **App api_hash**（一串字母数字）。

这两个值等于账号的登录钥匙。不要发给别人，不要提交到 git，不要写进截图。

## 安装

在项目目录里：

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

## 配置

两种方式都可以，同时存在时，**命令行参数优先于环境变量，环境变量优先于配置文件**。

### 方式 A：`.env`

```bash
cp .env.example .env
```

编辑 `.env`：

```
TGDL_API_ID=你的数字id
TGDL_API_HASH=你的api_hash
TGDL_PHONE=+8613800138000
```

`TGDL_PHONE` 可以留空，登录时再输入。

### 方式 B：`config.yaml`

```bash
cp config.example.yaml config.yaml
```

把 `api_id`、`api_hash` 改成你自己的。示例里的 `0` 和 `replace-me` 只是占位，程序会把它们当成「没填」。

不要把 `.env`、`config.yaml`、`*.session` 提交到仓库。

## 登录（只需一次）

```bash
tgdl login
```

按提示输入：

1. 手机号（国际格式，例如 `+8613800138000`）。如果 `.env` 里写了 `TGDL_PHONE`，会跳过这一步。
2. Telegram 发来的验证码。
3. 如果账号开了两步验证，再输入密码。

成功后会在 `.tgdl/tgdl.session` 生成会话文件。这就是登录状态，别人拿到这个文件就能操作你的账号。请保管好。

已经登录过时，再执行 `tgdl login` 会直接告诉你当前账号，不会重复要验证码。

## 查看可以下载的群

```bash
tgdl list-chats
tgdl list-chats --groups-only
tgdl list-chats --limit 50
```

输出四列：数字 id、类型、用户名、标题。后面的 `--chat` 可以填：

- `@群用户名`
- `https://t.me/群用户名`
- 邀请链接 `https://t.me/+……` 或 `https://t.me/joinchat/……`（程序会让你的账号加入这个群）
- 上面列出的数字 id，例如 `-1001234567890`

私密群如果只用数字 id，而这个 id 还没在会话里出现过，会失败。先跑一次 `list-chats`，或改用用户名 / 邀请链接。

## 先看看有哪些文件（不下载）

```bash
tgdl scan --chat @我的群 --ext pdf,zip
tgdl scan --chat @我的群 --min-size 1MB --since 2024-01-01 --until 2024-06-30
tgdl scan --chat @我的群 --name 报告 --sender ada
```

常用过滤：

| 参数 | 作用 |
|------|------|
| `--ext pdf,zip` | 只保留这些后缀，不区分大小写 |
| `--min-size` / `--max-size` | 文件大小，例如 `500KB`、`20MB`、`2GB` |
| `--since` / `--until` | 日期，UTC，含当天，格式 `2024-01-31` |
| `--sender` | 发送者的数字 id、`@用户名`，或显示名称里的一段文字 |
| `--name` | 文件名包含这段文字 |
| `--name-regex` | 文件名的正则表达式 |
| `--photos` | 连图片一起列出来。默认只处理文档（含视频、音频等以文件形式发送的内容） |
| `--limit 100` | 最多匹配多少个文件 |
| `--message-id 123` | 只看这一条消息，可以重复写多次 |

`scan` 不会写文件，也不会创建未完成的下载。

## 下载

```bash
tgdl download --chat @我的群 --ext pdf --workers 4
tgdl download --chat @群甲 --chat @群乙 --output ./下载
tgdl download --chat -1001234567890 --min-size 10MB --timeout 2h
```

默认保存到：

```
downloads/群名称/2024-05/原始文件名.pdf
```

- `--flat`：不按群名和月份分子目录，全部放在输出目录里。
- `--workers 4`：同时下载 4 个文件。这是默认值，也是比较稳妥的值。
- 文件名冲突时会变成 `报告 (1).pdf`、`报告 (2).pdf`，不会覆盖另一个不同的文件。

想指定机器可读的结果：

```bash
tgdl download --chat @我的群 --json
```

这时标准输出只有一份 JSON；进度在标准错误里。

## 中断和续传

下载过程中按 Ctrl+C：当前这一小块会写完，进度记在旁边的两个文件里，然后程序退出。

```
报告.pdf.part          已经下到本地的内容
报告.pdf.tgdl.json     记录消息 id、文件大小、文件唯一 id
```

再次执行同一条 `tgdl download` 会从上次的位置继续，而不是从头开始。程序按 4KB 对齐，所以最多会重下不到 4KB。

已经下完并且大小一致的文件会跳过。

| 参数 | 作用 |
|------|------|
| 默认（可续传） | 接着 `.part` 往下写 |
| `--no-resume` | 匹配到的半成品从 0 字节重新下 |
| `--force` | 不把「以前已经下完的任务」当成完成；已完成的文件会另存一个 `名字 (1)`，半成品仍然续传 |

退出码 `4` 表示这次没下完，但进度已经保存，再跑一次即可。退出码 `0` 表示该下的都处理完了（包含本来就已经存在、被跳过的文件）。

## 并发说明

Telegram 的官方库 Telethon 是异步的。`--workers` 表示同时进行的下载个数，它们共用一个登录连接，不是开很多个系统线程。

默认 4 个。调到很大通常不会更快，反而容易触发 Telegram 的限流（FloodWait）。程序会按服务器要求等待，并在等待前把已下载的内容存盘。如果需要等待的时间超过 `--flood-cap`（默认 15 分钟），本次会停下来并退出码 `4`，稍后再执行同一条命令即可继续。

## 给脚本用的本地接口

不需要图形界面。需要给别的程序调用时：

```bash
tgdl serve
```

接口只监听本机 `127.0.0.1:18765`。令牌写在 `.tgdl/api-token`，不要外传。

```bash
curl -H "Authorization: Bearer $(cat .tgdl/api-token)" \
  http://127.0.0.1:18765/api/v1/ping
```

`serve` 开着的时候，再开的 `scan` / `download` / `list-chats` 会自动交给它，避免两个进程同时占用登录文件。登录前请先停掉 `serve`（在那个终端按 Ctrl+C）。

字段和退出码的完整说明见 `docs/AGENT_API.md`（英文）。

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | 下载或运行失败 |
| 2 | 参数不对 |
| 3 | 还没登录，或没填 api_id / api_hash |
| 4 | 中断或超时，可以续传 |
| 5 | 群进不去、邀请无效，或已经有一个下载在跑 |

## 常见问题

**提示 missing api_id / api_hash**  
`.env` 或 `config.yaml` 没填，或仍是示例里的 `0` / `replace-me`。

**提示 not logged in**  
先执行 `tgdl login`。

**数字 id 无法解析**  
先 `tgdl list-chats`，或改用 `@用户名` / 邀请链接。

**提示 stop tgdl serve before login**  
会话文件同一时间只能由一个进程使用。先在运行 `serve` 的终端按 Ctrl+C。

**下到一半断网**  
不用删文件。网络恢复后执行同一条 `download` 命令。

**想只下某几条消息**  
`tgdl download --chat @群 --message-id 100 --message-id 101`

**进度条乱码或刷很多行**  
接到管道或重定向时不会用彩色进度条，而会每隔一会儿打一行。不想看进度加 `--quiet`。
