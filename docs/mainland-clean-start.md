# Windows A 股研究：从干净检出开始

## 个人研究与本机验收

在 PowerShell 7 中打开项目根目录。记下 `git rev-parse HEAD`，在自己的 Python 3.13 虚拟环境安装本项目的公开依赖及 `pit`、`dev` extras。个人研究使用交易所日历、本股票的已收盘行情和用户自己的数据源、模型账户。个人入口和本机验收均无需项目签名或维护者批准。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[pit,dev]"
.\start_personal_research.ps1 -Ticker 600895.SH -CheckOnly
.\start_personal_research.ps1 -Ticker 000001.SZ -CheckOnly
.\.venv\Scripts\python.exe -m pytest -q tests/test_personal_full.py
```

预检不调用模型或供应商。它先按北京时间查最近已收盘的交易日，再核验该股票本日的收盘和缓存新鲜度。不同股票不能互用收盘记录。空缓存会返回 `market_cache_missing`。本版支持已有可验证本地资料的机器；刚安装依赖并填写密钥的空白机器，还缺少首次自动建立证券身份、交易所日历、日线和三表覆盖的流程。合成测试只在临时目录产生测试报告。

温启动使用项目现有的 CURRENT 个人研究配置、可核验的身份与日历、Tushare 市场来源、账户限速及三个 Tushare 财报工具的有效缓存。在个人配置中将 `market_data_vendors.cn_a.core_stock_apis` 和 `tool_vendors` 里的 `get_balance_sheet`、`get_income_statement`、`get_cashflow` 设为 `tushare`。财报 HTTPS 地址与账户作用域必须和本机缓存的来源一致。将自己的 `TUSHARE_TOKEN` 和 `DEEPSEEK_API_KEY` 一起配在进程环境或项目根目录的私有 `.env`。通过项目支持的环境变量 `TRADINGAGENTS_LLM_PROVIDER`、`TRADINGAGENTS_QUICK_THINK_LLM`、`TRADINGAGENTS_DEEP_THINK_LLM` 明确选择 DeepSeek 和账户可用的模型。预检通过后，确认愿意付费时运行：

```powershell
.\start_personal_research.ps1 -Ticker 600895.SH -Run
```

该命令使用四个分析师、多空辩论、交易员与风险团队，沿用普通图的证据准入和决策规则。已配置的市场适配器可按现有协调策略补行情缺口。财报只读缓存，此路径的财报请求上限为零。公开网页抓取关闭，新闻或情绪缺证据时如实降级。模型通过固定 DeepSeek HTTPS 入口，禁用 SDK 重试。总尝试上限 60，分析预算最多 20 分钟，个人报告位于 `reports/personal/<run_id>/` 下回执给出的相对路径。证据不足可产生无评级结果。

本机真实验收需在同一提交上完成一次本人明确启动的研究，核对交易日、实际终态、四类分析、辩论与风险章节、中文报告和用量。预检或合成测试成功只能证明其各自覆盖的步骤。对外分享提交号、终态、用量和缺项即可，报告正文、原始行情、路径及密钥留在本机。

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
