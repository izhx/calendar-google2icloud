# Google Calendar → iCloud Calendar

Google Calendar API 单向同步到一个专用 iCloud CalDAV 日历。一个 Docker 服务、JSON 持久化状态，不开放入站端口。Google 只读授权；iCloud 使用 Apple App 专用密码。ICS 仅作为内部写入格式。

## 更新规则与开销

| 任务 | 频率 | 写入 iCloud 的范围 |
|---|---|---|
| `fast` | 默认每 600 秒 | 当前时间往前 7 天，到未来所有事件 |
| `history` | 每个当地自然日成功处理一次 | 6 个日历月前的当天 00:00，到当前时间往前 7 天 |
| iCloud 核对 | 每个当地自然日一次 | 读取整个专用日历，修复上述范围内被手动修改或删除的同步副本 |

日期边界使用 `SYNC_TIMEZONE`，默认 `Asia/Shanghai`。例如当地 2026-10-18 20:00：高频范围从 2026-10-11 20:00 起，历史范围为 2026-04-18 00:00 至 2026-10-11 20:00（不含）。跨越边界的活动可能与两个窗口都有交集，但不会重复创建或写入相同内容。日历自身的时区用于解释全天活动。

**Google 如何发现变化：**首次完整分页读取日历，保存原始活动缓存和最后一页的 `nextSyncToken`；随后每 10 分钟用 `syncToken` 只取新增、修改和删除。在本地缓存上应用完整增量，再按上面的窗口决定何时写入 iCloud。Google 不允许增量令牌与 `timeMin/timeMax` 同用，因此首次读取包含全部历史记录，但半年以前的普通事件不会写入、更新或清理。增量读取也可能收到范围以外的变更，只更新本地缓存。API 不是推送，设备端刷新还会增加延迟，不保证实时或强一致。

普通无变化的一轮通常只有 **1 次 Google 事件列表请求、0 次 iCloud 请求**；Google 访问令牌约每小时刷新一次。Google 日历身份和时区在进程启动及缓存满 24 小时时重新确认。活动较多、变更多或响应分页时会增加请求。

**iCloud 如何省请求：**日常比较本地源摘要；仅对需要新增、修改或已满足删除确认条件的事件读取当前内容和 ETag，再条件写入。专用日历地址缓存到状态中，每日核对时才重新发现和确认名称。每天的一次完整读取同时供两个窗口使用。没有相关变更时不访问 iCloud。副本在 iCloud 被手动编辑或删除后，通常在下一次每日核对修复；若该事件的 Google 内容先发生变化，也会在处理它时恢复。

历史任务和每日核对启动时即执行，此后记录成功日期；重启不会重复当天已完成的核对。删除保护锁定仍算完成检查。网络、解析或写入失败时保留待处理工作；连续失败按约 20 分钟、40 分钟、80 分钟逐步退避，最多 6 小时，并加入少量随机延迟；一次成功后恢复常规频率。默认频率每天约 144 次常规 Google 轮询，另外还有分页、身份查询和授权刷新开销。

调度以启动时刻为基准，每 600 秒触发。所有任务串行执行；上一轮未结束时跳过期间的触发，不排队、不补跑。例如 10:00 开始，到 10:13 结束，跳过 10:10，下一轮 10:20 开始。失败退避结束后从新的触发时间继续。历史任务和每日核对期间也不会并发启动高频任务。

**半年以前已同步的副本保留，暂不更新或清理。** 改期到历史范围的事件等待每日任务；改期到半年以前的事件暂不处理。重复日程用 `singleEvents=false` 读取主事件和全部例外，不展开无限未来。完整例外保存在增量缓存中，无需每轮为每组重复日程额外查询；取消某次实例只更新对应例外。**重复系列作为整体保存，因此入选系列在半年以前的历史实例仍可能显示，整组修改也可能影响这些实例**；不截断 RRULE。

## Google 一次性授权

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 创建或选择项目，在「APIs & Services → Library」搜索并启用 **Google Calendar API**。
2. 打开 **Google Auth platform**，依次配置：
   - **Branding**：应用名称可填 `calendar-google2icloud`，支持邮箱、联系邮箱填自己的邮箱。
   - **Audience**：个人 Gmail 账号选择 **External**；使用 Testing 状态时，在 Test users 中加入自己的 Google 账号。
   - **Data Access**：添加只读范围 `https://www.googleapis.com/auth/calendar.readonly`。程序只申请该范围，用于活动与日历身份/时区读取。
3. 在 **Clients → Create client**，类型选择 **Desktop app（桌面应用）**，名称可填 `calendar-google2icloud-local`。创建后把 **Client ID** 和 **Client secret** 填入本地配置。若下载了客户端 JSON，对应字段是 `installed.client_id` 和 `installed.client_secret`；程序不需要该 JSON 文件。
4. 在有系统浏览器的本地电脑准备 Python 3.12 环境。新安装可执行：

   ```sh
   cd ~/workspace/calendar-google2icloud
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env
   chmod 600 .env
   ```

   **已有 `.env` 时跳过复制，保留已有 iCloud 密码和其他设置，填写 Google 配置项：**

   ```dotenv
   GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=xxxx
   GOOGLE_REFRESH_TOKEN=
   GOOGLE_CALENDAR_ID=primary
   ```

   `GOOGLE_REFRESH_TOKEN` 暂时留空，由下一步自动写入。
5. 执行：

   ```sh
   .venv/bin/python authorize_google.py
   ```

   脚本使用系统浏览器、PKCE 和随机 state，在 `127.0.0.1` 的随机端口临时接收回调。成功后直接将 refresh token 写入本地 `.env` 并设为 0600，保留其他配置行；**不会在终端打印 token、授权码或密码**。授权等待最多 5 分钟。脚本只在本地授权期间监听，常驻服务不监听端口。不要在远程 Docker 容器中执行浏览器授权；本地完成后，用自己的安全传输方式把 `.env` 放到服务器项目目录。
6. `GOOGLE_CALENDAR_ID=primary` 读取授权账号的主日历；其他日历填写 Google Calendar 设置中的日历 ID。授权时选择需要同步的 Google 账号。服务会解析真实日历 ID 并绑定，后续换成其他账号或日历时停止，避免误同步。

OAuth 应用处于 External + Testing 时，此类 refresh token 7 天过期。长期运行时，在 **Audience** 中把发布状态设为 **In production**，然后重新执行授权脚本取得新令牌。个人自用可按 [Google 的个人用途免验证规则](https://support.google.com/cloud/answer/13464323)处理；授权界面可能仍显示未验证提示，须核对确是自己创建的应用。令牌被撤销、过期或账号政策改变后，重新运行本地授权脚本即可。无需把任何秘密贴到聊天中。

## iCloud 与启动

在 [Apple 账户](https://account.apple.com/) 生成本服务专用的 App 密码，填入 `.env` 的 `ICLOUD_USERNAME`、`ICLOUD_APP_PASSWORD`。需要双重认证；不要使用账户主密码。在 iCloud 中选择或建立名称唯一的目标日历，例如 `Google`，对应 `ICLOUD_CALENDAR_NAME`；已有日历中的非本程序事件会保留。

Docker 与 **Docker Compose ≥ 2.30.0**；主机须能访问 Google API、Google OAuth 和 iCloud。`.env` 为 `KEY=value`，值不加外围引号、行尾注释；`$` 保留原样。Compose 使用 raw env_file；本地 dotenv 禁用插值。

新安装先构建、预览两个范围：

```sh
docker compose build
docker compose run --rm calendar-sync python sync.py --once --dry-run --window all
```

确认目标日历和计数后启动：

```sh
docker compose up -d
docker compose logs -f --tail=50
```

修改代码后用 `docker compose up -d --build`。修改 `.env` 后用 `docker compose up -d --force-recreate`；设置 `SYNC_INTERVAL=600` 采用推荐频率。停止用 `docker compose stop`。

## 固定部署：vps

本项目部署在 SSH 别名 `vps` 的 `~/calendar-google2icloud`。日历状态留在服务器的 Docker 命名卷中，日常更新只上传代码和所需配置，不上传本机 `data/`、`.git/`、`.venv/` 或客户端 JSON。`.env` 仅通过加密 SSH/SCP 传输；远端文件权限设为 0600，项目目录为 0700，不打印展开后的容器配置。

该服务器单核、约 545 MB 内存。Compose 默认限制服务 **160 MiB 内存、0.25 CPU、64 个进程**；容器设置 `mem_swappiness: 1`（降低交换倾向），临时目录最多 8 MiB，日志轮转为 1 MiB × 2。服务器内核不支持单独的 swap 配额，未修改宿主机内核或交换空间。没有额外数据库、Web 服务或开放端口。Google 缓存很大的日历可能触及内存上限，应先观察实际峰值，再按服务器余量调整。

部署工具为项目内 `.tools/docker-compose`（2.30.3，下载后校验官方 SHA-256），不安装或升级全局 Docker。镜像使用 Python 3.12 Alpine 3.22，仅安装预编译依赖，关闭安装进度线程，不在服务器上编译 Python 扩展。已实测该旧版 Docker 下 Debian 基础镜像无法创建线程，改用 musl 的 Alpine 基础镜像，保留默认容器安全策略，不放开 seccomp 或升级宿主机。小机器更新时采用受限的串行构建，然后启动已构建镜像：

```sh
ssh vps
cd ~/calendar-google2icloud
DOCKER_BUILDKIT=0 docker build --memory=160m \
  --cpu-period=100000 --cpu-quota=25000 --force-rm \
  -t calendar-google2icloud:local .
.tools/docker-compose up -d --no-build
.tools/docker-compose logs --tail=30 calendar-sync
docker stats --no-stream
```

今后仍部署到此目录。可从本机项目目录用 `scp` 上传明确的服务文件，并单独传 `.env`；不要把整个工作目录递归复制到服务器，以免携带秘密 JSON、本地状态或开发环境。保留远端 `.tools/` 与持久化卷。构建失败时先修复构建，不停止正在运行的旧服务；不执行全局镜像、容器或卷清理。

部署记录（2026-10-18）：已使用现有 iCloud「Google」日历启动常驻服务，Docker 随系统启动，容器自动重启策略为 `unless-stopped`。初次同步与随后从已保存游标恢复的增量检查均成功。初次观测空闲内存约 35 MiB、启动峰值约 67 MiB；宿主机仍有约 236 MiB 可用内存。`.env` 和状态文件权限均为 0600，未对外开放端口。原有数据库容器未改动。首次 Google 源缓存为 0 条，因此目前验证的是两端连接、空同步和状态恢复，后续真实事件将按 10 分钟规则同步。

## 删除、安全与状态

仅管理同时具有正确派生 UID、`X-GICS-OWNER`、`X-GICS-SOURCE-UID` 的副本。手工创建的其他事件不修改、不删除；同名日历多于一个、UID 冲突、归属标记不一致时停止。不创建、清空或删除整个 iCloud 日历。

每轮在完整读取 Google 所有增量分页、转换验证后才提交缓存和令牌；任一页出错、分页循环、超限、缺少最终令牌、日期或规则无效，都不会推进源缓存或访问 iCloud。所有新增和修改完成后才进入该窗口的删除阶段。

增量中的删除标记通过 Google event ID 更新本地完整缓存；iCalUID 负责关联目标副本。主事件被删除时移除整组缓存，某次实例取消则保留取消例外。源仍存在但改期到其他范围时保留副本，等待适当任务更新，不把移出窗口当成删除。

Google 返回 `410 Gone` 时，丢弃本轮增量并重新完整读取源，全部成功后才原子替换源缓存和令牌；不会清空 iCloud，也不会清除副本关联、缺失计数或删除锁。重建失败时保留原缓存和目标事件；重建成功后仍走正常的两次确认和批量删除保护。

默认保护：

- 同一事件须在**同一个任务的两次完整源检查中**均不存在于完整缓存，才可删除。下一次增量为空也能确认此前的删除；读取失败不会增加计数。两个窗口的计数独立，跨边界活动不会在同一轮凑成两次确认。历史删除通常需要两天的历史检查。
- 候选删除超过 20 条，或至少 5 条且超过本窗口已管理/待同步事件的 50%，锁定本任务全部删除。
- 本窗口原有副本但有效源结果突然为零，也锁定删除。锁定状态持久化，不会靠重启或候选数下降逐渐删空；所有候选恢复后可自动解除。另一个任务独立判断。
- 创建使用 `If-None-Match: *`，更新/删除使用强 ETag 的 `If-Match`；并发冲突时停止本轮。写入后回读，以适应服务器规范化。

核对确为合法批量删除后，停止常驻服务，单次预览和覆盖指定任务，例如高频任务：

```sh
docker compose stop
docker compose run --rm calendar-sync python sync.py --once --window fast --dry-run --allow-large-delete
docker compose run --rm calendar-sync python sync.py --once --window fast --allow-large-delete
docker compose up -d
```

历史任务改用 `--window history`。覆盖只作用于本次执行，仍需该任务至少两次缺失确认；下次重新检查阈值。`SYNC_DELETE=false` 可关闭删除。

持久化数据在命名卷 `calendar-data` 的 `/data/state.json`，包含 **Google 原始事件正文、同步游标 `syncToken`、iCloud 事件地址、上次同步的 ICS 内容**、日历绑定摘要、两组缺失次数及删除锁，以及历史任务和核对的成功日期。该文件含完整私密日历数据，备份也需保护；Google OAuth 凭据和 Apple 密码仍只来自 `.env`，不写入状态。访问令牌仅保留内存。状态使用原子替换、fsync、0600 权限和进程排他锁；损坏或格式错误时保留文件并停止，禁止自动重置。

Google 缓存与目标写入进度分别保存。即使 Google 增量已提交而 iCloud 随后失败，下次也会通过源摘要与目标进度的差异重试；不丢失变更。写入后连接中断时通过稳定 UID、缓存地址和归属标记恢复。

容器使用非 root 账号、只读根文件系统，不开放端口，限制多余权限。`.env`、本地数据目录与授权临时文件排除在 Git 和镜像构建上下文之外。普通 `docker compose down` 保留卷，**不要加 `-v`**。环境变量仍对宿主机管理员可见，不要公开展开的容器配置。日志仅含任务、计数和错误类别/HTTP 状态，不含密码、token、事件标题、UID 或异常正文。

剩余的主要开销是首次/令牌过期后的 Google 完整读取、每日 iCloud 完整核对，以及批量变更的逐条条件写入和状态落盘。Google 旧历史只缓存、不写入目标；数据量很大的日历仍会占用较多内存、磁盘和首次读取时间。分页达到保护上限时整轮失败，不会悄悄截断结果。

## 配置与手动执行

| 变量 | 默认值 / 用途 |
|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` | 必填，只放本地 `.env` |
| `GOOGLE_CALENDAR_ID` | `primary`，也可指定日历 ID |
| `ICLOUD_USERNAME` / `ICLOUD_APP_PASSWORD` | Apple 账户与 App 专用密码 |
| `ICLOUD_CALENDAR_NAME` / `SYNC_ID` | `Google` / `google-to-icloud`，首次同步后保持稳定 |
| `ICLOUD_CALDAV_URL` | `https://caldav.icloud.com/` |
| `SYNC_INTERVAL` / `SYNC_TIMEZONE` | `600` 秒 / `Asia/Shanghai` |
| `HTTP_TIMEOUT` | `30` 秒，每次连接/读取超时 |
| `SYNC_DELETE` / `DELETE_CONFIRMATIONS` | `true` / `2`，至少两次 |
| `DELETE_MAX_COUNT` | `20` |
| `DELETE_MAX_RATIO` / `DELETE_RATIO_MIN_COUNT` | `0.5` / `5` |
| `MAX_RESPONSE_BYTES` / `GOOGLE_MAX_PAGES` | 每 API 页 `20971520` 字节 / 每列表最多 `1000` 页；超限失败 |
| `STATE_FILE` | 本地 `data/state.json`；Compose 固定 `/data/state.json` |

`--once` 执行一次后退出；`--dry-run` 只读预览且不保存源缓存或令牌、不递增计数、不记录历史或核对成功日期。默认 `--window auto` 执行高频任务和当天尚未成功的历史任务；`fast`、`history`、`all` 用于单次强制运行，需要 `--once` 或 `--dry-run`。删除保护触发或任一任务失败时，单次执行退出码为 1。

保留标题、描述、地点、时间、全天日期、时区、重复规则与例外、popup 显示提醒及会议/活动 URL。Google 为唯一数据源；iCloud 副本的手动修改和删除在每日核对时按范围覆盖或重建；Google 对应事件发生变化时也会检查和修复。为避免发送邀请，不复制参会人、组织者和邮件提醒；Google 专有事件类型只映射基本日历内容。TZID 缺少源定义时补全 1900–2100 年 VTIMEZONE，远期 DST 和客户端显示仍需实际联调。

## 验证

```sh
.venv/bin/python -m unittest discover -s tests -v
# 镜像构建完成后也可：
docker compose run --rm calendar-sync python -m unittest discover -s tests -v
```

测试使用合成 Google JSON/ICS、模拟传输与临时状态，不使用真实凭据。覆盖增量分页与持久化、410 重建、缓存原子提交、无变化时零 iCloud 请求、单个变化定点读取、每日核对、默认提醒变化、跨窗口改期、完整重复例外、UID 与 ETag、删除保护、退避重试、超时跳过和 OAuth。

本地使用 Python 3.12，100 项测试全部通过。本机无 Docker；已在 VPS 的最终 Alpine 镜像内完成 100 项测试，并通过真实 Google/iCloud 连接、首次同步和持久化增量恢复检查。首次源缓存和同步范围均为 0 条，尚无实际事件可验证写入。

## 官方参考

- [Google：增量同步与令牌失效](https://developers.google.com/workspace/calendar/api/guides/sync)
- [Google Events.list：日期范围、分页及 syncToken 限制](https://developers.google.com/workspace/calendar/api/v3/reference/events/list)
- [Google：重复主事件与例外](https://developers.google.com/workspace/calendar/api/guides/recurringevents)
- [Google：Calendar API 只读授权](https://developers.google.com/workspace/calendar/api/auth)
- [Google：Desktop OAuth、PKCE、loopback 与 refresh token](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Google：refresh token 过期条件](https://developers.google.com/identity/protocols/oauth2#expiration)
- [Apple：App 专用密码](https://support.apple.com/en-us/102654)
- [Docker：raw env_file](https://docs.docker.com/reference/compose-file/services/#format)
