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

显式日期的 `-Full -CheckOnly` 在有合格日期证据的受审安装上检查绑定和选择；不会验证真实供应商可用性或产出正式研究报告。
