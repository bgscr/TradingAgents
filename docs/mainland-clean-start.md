# Windows A 股研究：从干净检出开始

## 开发与离线检查

从本 fork 的 `main` 检出源码，在项目根目录使用 PowerShell 7。以下示例使用 Windows Python 3.13，安装步骤需要已准备好的依赖包或可访问的包源。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q tests/test_evidence_acceptance_matrix.py tests/test_pytest_persistence_isolation.py -k "acceptance_matrix or historical_ticket07 or repository_authority_and_audit"
```

这组选定的回归使用公开验收契约、跟踪的测试材料和 pytest 临时目录，无需账户密钥、旧缓存或历史 `.scratch`。隔离测试会验证历史路径受到写保护，空目录也必须拦截写入。默认 pytest 会排除标记为 `installed_release` 的本机审计。完成受审安装后，显式运行 `python -m pytest -m installed_release tests/test_bank_identity_diagnostic_entry.py tests/test_bounded_mainland_entry.py` 检查真实绑定；缺少必需输入时审计会失败。

## 正式 Full

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
