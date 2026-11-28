# Windows A 股研究：从干净检出开始

## 个人研究与本机验收

在 Windows 的 PowerShell 7 中进入项目根目录。个人研究使用上游多 Agent 图和自己的 Tushare、DeepSeek 账户。首次运行可以从空缓存开始，无需项目签名、维护者批准或其他机器的文件。

```powershell
git rev-parse HEAD
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[pit,dev]"
.\.venv\Scripts\python.exe -m pytest -q tests/test_personal_full.py tests/test_personal_bootstrap.py tests/test_personal_observations.py tests/test_personal_dc_ratio.py
.\start_personal_research.ps1 -Ticker 600895.SH -CheckOnly
```

将自己的 `TUSHARE_TOKEN` 和 `DEEPSEEK_API_KEY` 一起配置在项目根目录的私有 `.env`，或一起配置在当前进程环境中。使用有权访问当前上市证券资料、交易日历、行情与财务报表的 Tushare 账户。默认模型为 `deepseek-v4-flash` 和 `deepseek-v4-pro`。如果账户需要其他受支持的 DeepSeek 型号，可用 `TRADINGAGENTS_QUICK_THINK_LLM` 和 `TRADINGAGENTS_DEEP_THINK_LLM` 指定。

`-CheckOnly` 完全离线。首次出现 `market_cache_missing` 和退出码 2 表示尚无可核验的本地行情。确认愿意使用账户配额和模型费用后，选择自己的股票，显式运行一次。例如深市股票：

```powershell
.\start_personal_research.ps1 -Ticker 000001.SZ -Run
```

首次取数针对一个标的核验上市身份和交易所，按北京时间选择最近已收盘交易日，核对该股当日成交、收盘与复权数据，再准备资产负债表、利润表和现金流量表的公告日期区间。合格缓存优先复用。三表每个端点最多尝试一次，分析期间只读财报缓存；成功空表会保留为无可用行。交易所覆盖有缺口、个股无合格收盘、账户拒绝或返回不完整时会停止。财报同一端点若有多个被新鲜数据隔开的缺口，保留原数据并报告 `disjoint_statement_gaps`。

首次准备与后续分析共用总尝试和截止时间，总计最多 60 次、最长 20 分钟，并服从更小的项目预算。证券、行情、三张财报及融资使用固定 Tushare HTTPS 来源和本地账户限速。模型只使用 DeepSeek HTTPS。默认 Full 还允许在项目配置已允许 AKShare 时读取一个东方财富公开新闻搜索页，最多一次，只传股票代码，不带账户凭据，不打开文章链接，不重试。经来源、股票和时间核验的文章链接与发布时间保存在回执。面向正文的研究工具会收到 `personal_news_index_only` 缺项，标题、正文及其情绪均未验证。四个分析师之后还有多空辩论、交易员、风险团队与最终决策。证据不足时可以无评级。

`-SkipNews`、`-SkipMargin`、`-SkipRatio` 可逐项关闭。例：`.\start_personal_research.ps1 -Ticker 000001.SZ -SkipNews -Run` 只使用账户数据和已有合格占比缓存。`-Basic` 显式关闭全部三项。以上选项都保留完整研究图。Basic 和跳过融资的 Full 沿用最多三次财务尝试。`-CheckOnly` 仅列出选择与核心准备状态，无法离线确认真实账户对融资端点的权限。

个人数据按账户保存在 `reports/personal/.data/`，成功或已开始准备的失败运行在 `reports/personal/<run_id>/status.json` 留下结果，成功时用其中的 `report_relative_path` 找到中文报告。已明确配置原有个人 CURRENT 数据源、身份与限速库的用户可显式加 `-PreparedData` 使用已有资料，其财报仍然只读。受控历史回放继续使用原有入口。

### 首次取得东财占比

**2000 分账户**可先运行下方预检。普通 Full 保留占比缓存读取；干净安装没有合格历史行时会报告缺项。三表与融资余额的公布门槛为 2000 分，个人 Full 为合格的融资余额查询单独预留一次；即使三表均需补查，融资仍可在总次数和时限内查询一次。同账户的新鲜合格缓存可减少所需请求。是否已有目标日数据、账户实际授权和额度，都不能由积分声明证明。[三表积分说明](https://tushare.pro/document/1?doc_id=108) · [融资余额的更新及权限](https://tushare.pro/document/2?doc_id=59)

```powershell
.\start_personal_research.ps1 -Ticker 601328.SH -CheckOnly -TusharePoints 2000
```

**具备东财接口权限的账户**可在原本计划的 Full 中显式选择补查。须如实声明自己的当前积分，满足 5000 分门槛、项目已有的东方财富来源许可（若配置了数据商白名单，须包含 `akshare`）、回溯窗口和共同预算。低于门槛或未声明时，显式补查会在读取凭据、补行情或启动模型之前以退出码 2 结束。预检只报告已知门槛，不向供应商探测权限。以下以恰好 5000 分为例。

```powershell
.\start_personal_research.ps1 -Ticker 601328.SH -CheckOnly -FetchEastmoneyRatio -TusharePoints 5000
.\start_personal_research.ps1 -Ticker 601328.SH -Run -FetchEastmoneyRatio -TusharePoints 5000
```

数据日期来自供应商行的交易日。新路径通过 Tushare 获取东方财富的盘后日数据，可回查已公布的会话。实时排行截面、请求发生日期或其他资金流指标均不用于代填。回执的 `source=eastmoney.via_tushare.moneyflow_dc` 标明这一来源。该接口的公开文档列明权限条件，实际可用性取决于本人账户。[指标、更新时点与权限说明](https://tushare.pro/document/2?doc_id=349)

东方财富网站提供带日期的个股历史资金流向表，可供人工核对。项目现有直连接口使用含金额和价格的宽字段，生产采集器已关闭；个人入口没有能独立认证手填数值、网页截图或 CSV 来源的导入器。2000 分可用的 Tushare `moneyflow` 是另一个指标。以上材料均不能给个人回执补出已验证的东财占比。[东方财富个股历史表（交通银行示例）](https://data.eastmoney.com/zjlx/601328.html)

新闻索引只支持链接和标注时间。若有合法取得的交易所或巨潮公司公告 PDF，可按现有独立入口核对代码、日期、页码和文本摘要，作为本地补充。来源 URL 属于用户声明，未通过在线认证；公告和网页正文均不进入普通新闻评级。[单份本地公司公告说明](local-company-announcement.md)

### 如何读个人研究回执

| 核对项 | 本入口能证明什么 |
| --- | --- |
| 图与报告 | `status=completed` 和 `report_written=true` 表示研究已结束且中文报告已写出。 |
| 评级 | `rating_status=validated_decision` 表示通过图的决策门；`no_rating` 表示研究结束、无可验证评级；失败时为 `unavailable`。预检不会产生评级。 |
| 市场与财报 | `verified_inputs` 列出身份、所选股票收盘、交易所日历和各报表的公告区间检查。成功空响应会明确标注，不能推断财务值为零。 |
| 新闻 | `source_evidence.news` 给出单页窗口、收到时间和匹配文章的来源链接、上海发布时间。`news_evidence_scope=publication_index_only` 与 `news_content_verified=false` 说明仅核实观察时该页列有匹配链接及所标时间。索引不能佐证报道主题、事件事实、情绪或投资方向。新闻分析师无法从这个索引取得正文，评级不引用索引作为内容证据。单页缺行无法证明市场没有相关新闻。 |
| 融资余额 | 报告封存后先读已验证协调缓存。个人入口最早在交易日次日北京时间 09:00 以本人账户查询同股同日的 `margin_detail.rzye`；深市周五的数据最早在下周一 09:00 查询。这是本地查询窗口，遇节假日或上游延迟，届时仍可能没有数据。窗口开放后发出的合格空响应可以复用，已有合格正值仍优先读取。同一查询已发送且结果未定时，回执标记 `prior_margin_attempt_unsettled` 并停止补查。个人 Full 选用融资时，回执列出 `financial_attempt_limit=4` 和 `financial_reserved_endpoint=margin_detail`。`financial_attempts` 是实际物理尝试数，合格缓存命中不消耗名额。列出交易日、观察时间及人民币元，成功空行不补零。 |
| 东财占比 | 默认只读合格的个人缓存。`-FetchEastmoneyRatio` 允许在报告封存后由本人账户使用前三次财务尝试中的剩余名额补查一次。固定请求 `moneyflow_dc` 的 `ts_code,trade_date,net_amount_rate`，只认同股、同日及主力净流入百分比，成功后读回物理账本验证。错日、返回额外列或权限不足均不给出占比。 |

`evidence_preset=full` 为默认个人模式。`enhancements` 分别记录 `not_selected`、`permission_not_configured` / `permission_denied`、`valid_empty`、`publication_pending`、`date_mismatch`、`verified` 等结果。`enhancement_coverage=all_three_verified` 只表示该股票及交易日的三项观察均有证据。`full_data_acceptance` 保守标为 `unverified`，单页新闻及事后观察均不能证明截至收盘的完整信息。融资和资金流仅作为本次回执中的事后补充，不改变已写入的评级。`-PreparedData` 沿用独立的旧个人数据配置，未建立个人账户隔离会话时不在那里补采。PowerShell 的运行监督器在达到截止时间时返回 124；此时按本地记录核对已发生的用量，避免重复启动。

本机真实验收以固定 Git 提交、本人显式启动的真实研究、实际终态、中文报告及用量为准。上述离线回归使用合成市场和模型响应。对外只分享提交号、脱敏状态、各类尝试次数和覆盖缺项；密钥、原始响应和报告正文留在本机。

## 开发与离线检查

从本 fork 的 `main` 检出源码，在项目根目录使用 PowerShell 7。以下示例使用 Windows Python 3.13，安装步骤需要已准备好的依赖包或可访问的包源。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q tests/test_evidence_acceptance_matrix.py tests/test_pytest_persistence_isolation.py -k "acceptance_matrix or historical_ticket07 or repository_authority_and_audit"
```

这组选定的回归使用公开验收契约、跟踪的测试材料和 pytest 临时目录，无需账户密钥、旧缓存或历史 `.scratch`。隔离测试会验证历史路径受到写保护，空目录也必须拦截写入。默认 pytest 会排除标记为 `installed_release` 的本机审计。完成受审安装后，显式运行 `python -m pytest -m installed_release tests/test_bank_identity_diagnostic_entry.py tests/test_bounded_mainland_entry.py` 检查真实绑定；缺少必需输入时审计会失败。

## 旧受保护的验收与历史回放

完成受审安装后，从 PowerShell 7 运行：

```powershell
.\start_tradingagents.ps1
```

输入带交易所后缀的六位股票代码，例如 `600919.SH` 或 `000001.SZ`。确认后，普通 Full 使用北京时间最新合格交易日。入口需要可验证的证券身份、本机项目配置的合法数据源与模型凭据，以及协调库中已登记的上游服务和经审定的账户限流策略。日历与日线由现有准入、缓存及预算约束；建一个空数据库不会授予访问资格。缓存和协调状态由本机运行环境管理，报告写入 `reports/bounded-runs`。本地公告文件只在显式选择公告输入时需要。

干净 Git 检出提供源码。正式入口还会核验项目内的 `.venv`、机器与安装目录绑定的 `config/bounded_mainland_release.json` 和受审运行时。现有打包器 `scripts/build_ordinary_full.py` 还要求发行配方指定的来源清单及三份本地日期证据，产物需经受控发布。当前仓库没有从一台空白机器签发首份可信来源清单的流程。新机要运行受保护的 Full，需由发行维护者提供适用的受审安装与本机策略。不要复制旧机器的绑定清单、数据库、报告或凭据。

指定单日验收时，可在普通 `-Full -Run` 命令上加 `-ExpectedTradingDate YYYY-MM-DD`。入口仍按北京时间和本次运行的正常证据选择最新合格日；若核验出的交易日不同，停止在分析和模型调用之前。选日准备可能已使用原会话预算。省略该选项保留原选日行为；自动选日的 `-Full -CheckOnly` 也可使用同一断言。

显式日期的 `-Full -CheckOnly` 在有合格日期证据的受审安装上检查绑定和选择；不会验证真实供应商可用性或产出正式研究报告。

## 首装前只读检查

用计划用于构建的 Windows Python 3.13 运行下面的命令。`--repo` 指向可信来源检出，`--target` 指向拟存放候选的独立目录。检查可以在目标尚不存在时运行。

```powershell
py -3.13 -I -S -B scripts/check_ordinary_full_inputs.py --repo . --target ..\TradingAgents-candidate --pwsh ([Environment]::ProcessPath)
```

输出分别列出来源、目标运行时和 PowerShell 宿主的状态。空白检出将列出缺失的来源清单、上交所 2026-11-20 的完成日回执和两份原始日历载荷。来源清单存在时，检查会调用构建器，在本地核对来源清单固定摘要、来源目录、证据绑定和当前 Git 字节。目标需只有已准备的 `.venv`，且通过 24 项解释器环境和 5 项标准库摘要核验。空 venv 缺少依赖时，`missing_runtime` 会列出配方内的相对路径，可据此准备对应的受审运行包。请在拟用于 Full 的 PowerShell 7 窗口执行。`--pwsh` 指定当前宿主；若进程已有 `TRADINGAGENTS_ORDINARY_FULL_PWSH`，预检按正式入口的优先级核对该显式定位。`powershell` 为 `verified` 表示宿主文件摘要与受审配方绑定的策略一致；`pwsh_digest_mismatch` 表示该二进制不匹配，即使版本号相同也不能通过。检查只读取文件。退出 0 表示三项均通过；签发安装清单和真实 Full 仍需后续步骤。

正式暂存使用 `scripts/build_ordinary_full.py --repo <来源检出> --target <准备好的独立目录>`，同样使用 `-I -S -B`，暂存后加 `--verify` 复核。它生成 `config/ordinary-full-package.json` 候选回执；公开主线没有生成首份可信来源清单或批准正式目标清单的命令。应从维护者的受审发行流程取得适用于目标的安装，再配置该机器的授权来源与账户策略。`scripts/build_windows_release.ps1` 生成另一种通用便携分发包，不能用于签发此 Full 安装。

如原安装已经更新，维护者可以对预检、暂存和 `--verify` 使用同一个 `--source-manifest-backup RELATIVE_PREIMAGE`。参数应指向来源检出内 `.scratch` 事务目录的编号前像（文件名为数字加 `.before`），目标目录必须独立。三个步骤始终使用同一受审配方；备份须逐字节匹配配方指定的来源清单，且绑定同一来源根。三份日期证据仍从原来源检出的固定位置验证。省略此参数会读取原有默认清单。该选项用于维护者暂存和复核，不发布目标清单，也不授予正式运行权限。

### 在另一台 Windows 上核对首装

维护者须为目标电脑审定运行时、部署根目录和正式安装清单。开发检出、候选包回执及另一台电脑的正式清单都不能签发这台电脑的安装。每台机器独立配置账户准入、限流和市场证据。请勿把来源电脑的凭据、缓存、协调库或私有回执随源码分发。

从目标目录用 PowerShell 7 执行 `.\start_tradingagents.ps1 -Full -Ticker 600895.SH -CheckOnly`。保留退出码和脱敏 JSON。缺少项目 Python 时先完成受审运行时准备；Python 入口已启动后，以下诊断定位下一步：

| `diagnostic` | 处理 |
| --- | --- |
| `reviewed_install_manifest_missing` | 缺正式安装清单，请维护者完成目标机器发行审核。 |
| `bound_runtime_mismatch` 或 `installation_identity_mismatch` | 核对受审解释器、标准库和实际安装根。不接受本机自行改写摘要。 |
| `bound_project_mismatch` | 核对或恢复该发行的项目文件。 |
| `local_market_evidence_missing` | 根据 `reason` 区分缺行情库、交易所日历或该股票收盘证据，使用本机现有受控初始化与采集流程。 |

未知响应、来源不符或所有权校验失败仍按原错误阻止。跨机验收须在独立 Windows 主机重复检查，记录实际发行版本、宿主、退出码和诊断；离线预检不证明真实数据与模型可用。
