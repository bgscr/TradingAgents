# 显式更新并采用银行身份快照

这项功能依赖固定项目 `.env` 候选及银行身份补丁安装完成。安装代码不代表已采集身份资料；未获得新接口、凭据发送及写入批准时，不执行下面的更新命令。

元数据更新与分析分开。它只调用 Tushare `stock_basic`，不调用模型，不在选股时联网补身份，不改变原财报和行情缓存。普通分析仍为每次财报最多 3、分析数据与模型最多 60，high / 65536、SDK 重试 0、共享 1200 秒、不续跑。

## 获准后由用户手动更新

在普通非管理员 PowerShell 中，从项目使用固定解释器：

```powershell
& './.venv/Scripts/python.exe' -I -B './scripts/update_bank_identity.py' --refresh --use-project-env
```

`--use-project-env` 只采用项目 `.env` 的 `TUSHARE_TOKEN`；不采用 DeepSeek 密钥，不向上搜索或保存新密钥。省略它则使用隐藏输入。六分区更新必须输入 `UPDATE_BANK_IDENTITIES`；单分区诊断使用下文所列的独立确认。既无 `--refresh` 又无 `--diagnose-schema` 时只检查发布源码，不创建采集目录。

唯一网络目标为 `POST https://api.waditu.com/dataapi/stock_basic`。六个固定请求是 SSE/L、SZSE/L、SSE/D、SZSE/D、SSE/P、SZSE/P。body 的 api_name=stock_basic，fields 为 ts_code,symbol,name,industry,market,exchange,curr_type,list_status,list_date,delist_date；params 仅 exchange/list_status 与现有公开路由提示 ts_type_name=https://api.waditu.com/dataapi。没有重定向、分页、重试、备用来源或后台更新。

最多六次物理发送，失败也计数；记录请求前计数与 HTTP 响应数，二者不能等同成功送达。每次 I/O 最多 30 秒，整轮 240 秒协作期限，不是强杀保证。响应每份最多 4 MiB；达到 6000 行边界、未知分页元数据或截断均停止。既有 Tushare 协调库身份、40/min 或更严格间隔、冷却沿用，不迁移、不重配、不重置。429 保存受限 Retry-After 的冷却；缺失/无效时按现有保守规则 300 秒停止，不自动重试。

只能在主项目固定目录 `data/instrument_identity/stock_basic/<32位随机ID>/` 新建资料及脱敏回执。保存六分区白名单公共列至 snapshot.json，完整校验回执至 result.json；所有写入、关闭和到期检查成功后，Windows 原子重命名发布 READY.json。READY.pending.json 不能被消费。READY 是提交点；随后终端显示失败不能据此重复采集，应先只读核实该目录。旧快照和失败产物不覆盖、不删除。

成功输出 `snapshot_ref`，格式为 `UUID:SHA256`。不要编辑快照、READY 或摘要来满足校验。它只证明本次六分区返回满足约束，不能证明供应商从未遗漏证券。

## 获准后在新分析中显式选择快照

```powershell
pwsh -NoProfile -File './scripts/Start-MainlandAnalysis.ps1' -UseProjectEnv -BankSnapshot '成功更新实际输出的UUID:SHA256'
```

上面是用法模板，不是已激活会话；必须替换为已批准并完成的真实快照引用。菜单选择股票和日期，每次仍需 `START`，只运行一只股票，不自动批量。不得复用旧 consumed 入口。

优先解析原发布版本的基础登记；原有股票保持原身份、摘要和缓存 revision。只有基础登记 NOT_FOUND 且显式提供快照时，才进行银行补充。基础登记损坏、冲突或其他失败不会回退。裸代码先检查所有身份命中唯一，再验证 industry 精确为“银行”、curr_type=CNY、exchange 为 SSE/SZSE 且后缀一致；.SH/.SS 只映射沪市，.SZ 不能借用沪市身份。与原别名冲突、未知行业、缺日期或非银行一律拒绝。

新会话根据快照确定性生成自己的 bank-identity-registry.json；绑定快照摘要、原登记摘要、选股、截止日、会话及有效期。财报 live/reuse 都重新验证绑定，不接受任意登记路径或仅父进程自报的摘要。原财报 FinancialNetworkGuard 仍只允许三张财报，不因此获得 stock_basic 许可。

有效期为最早分区 checked_at 起 24 小时，不允许未来时间、延长或拼接批次。到期会缩短本次 epoch 与 monotonic 两条期限；不会重置 1200 秒或自动更新。分析截止日须不早于 list_date，已退市证券须早于 delist_date。P 状态不证明可交易，仍须通过原行情、日历、停复牌及准入检查。

## 能证明与不能证明

### 单分区结构诊断（需要另行批准真实请求）

遇到未保存响应结构的失败时，不重跑旧更新，也不使用旧剩余额度续跑。
已批准新的单次诊断后，可使用同一入口的独立模式：

```powershell
& './.venv/Scripts/python.exe' -I -B './scripts/update_bank_identity.py' --diagnose-schema --use-project-env
```

确认文字为 `DIAGNOSE_BANK_SCHEMA`，与六分区更新的确认不同；两种操作参数不能并用。
此模式仅请求原顺序第一分区 SSE/L，最多 1 次 POST；端点、固定请求字段、凭据选择、
30 秒 I/O、240 秒整轮协作期限、既有协调库限制、零重试和禁止跳转规则均不变。
即使该分区通过原校验，也只返回 `diagnostic_complete`，不生成 snapshot.json、READY.json
或 snapshot_ref，不能用于选股、身份登记或分析。非零退出时应先看本次回执，不能自动再试。

普通更新和诊断都会先在本次独立目录写入 `schema_diagnostics.json`，再写结果回执。
结构摘要只包括固定阶段、分区、HTTP 状态、响应长度、行列数量/宽度、受限类型及
主异常与关闭异常的安全类别。诊断键名词表不等于准入白名单：未知键仍会触发原校验。
词表外键名及非预期字段名匿名化，不保存任意键名、表格值、原始响应、headers 或异常正文；
凭据回显检查先于结构摘要记录。摘要数量受限并显式标明省略项；摘要指纹只针对脱敏结构。
摘要写入失败会阻止快照发布，并保留固定错误类别；不会重试同一诊断写入。

单次诊断只能说明新响应的结构，不能恢复旧响应、证明其他五分区或供应商完整覆盖。
若未知键仍须匿名化，应保留未知状态，不能据此放宽字段、分页或身份检查。

快照提供 CURRENT 证券身份及供应商行业分类，不是历史 PIT、监管牌照证明、银行风险指标或财务事实。stock_basic 不提供所选历史日的行业/曾用名/代码迁移全史。原完整性、数值、字段/单位/期间校验和投资准入均保留。

新银行身份解析成功也不保证行情窗口、财报或日历已准备好。现有行情缓存缺失可按原分析预算取数，但日历不完整、关键事实缺失或真实账号无权限时仍停止；可选来源按原策略标为缺口。不得把身份补充当成完整分析通过。

新接口实际 HTTPS 可用性、账户权限和实际覆盖尚需授权后的真实验证。官方资料列 stock_basic 2000 积分及 6000 行限制；不承诺现有账号满足或服务免费： https://tushare.pro/document/2?doc_id=25 。HTTP body 协议参考： https://tushare.pro/document/1?doc_id=40 。本功能不自动购买或升级权限。

## 单证券正向身份（独立 v2）

新模式与旧六分区 v1、结构诊断互斥。需要单独确认一次查询；不会在菜单选股时自动刷新。
安装该版本后可显式执行：

    & './.venv/Scripts/python.exe' -I -B './scripts/update_bank_identity.py' --refresh-security 600919.SH --use-project-env

确认语绑定规范化代码：UPDATE_BANK_IDENTITY:600919.SH。此处600919是示例，不是硬编码许可名单。
首次支持显式SH/SZ，SS仅规范化为SH；裸码不能建立新身份，只能匹配已验证证据的唯一别名。
范围为沪深当前上市L、CNY、供应商行业严格为银行、market为主板/创业板/科创板；不支持CDR/BSE或未知分类。

请求仍为唯一 POST https://api.waditu.com/dataapi/stock_basic，params额外绑定精确ts_code，
exchange和list_status=L，保留原ts_type_name，fields保持原十个公共列。
最多1次物理尝试（发送前计数，送达另记）、30秒I/O、240秒协作期限、原40/min或更严协调限制。
无重试/跳转/分页/续跑，不调用模型。仅本机固定项目.env的TUSHARE_TOKEN用于该目标，不输出或另存。

严格业务码整数0、十个唯一字段、恰好一行、代码/交易所/状态/日期匹配。
零行是未验证而非不存在，多行（包括同值重复）、错证券和冲突均拒绝。
data.has_more必须是布尔false；缺失/true/错误类型或已知分页冲突停止。
这是本代理的保守局部门槛，不是全集完整证明。count没有已证实的总数语义，
不参与接受/拒绝、不要求等于1；未知扩展不进入身份事实，仅保存受限结构摘要。
凭据回显在任何字段或键中均先拒绝。响应原文和自由字符串元数据不留存。

新证据为bank-stock-basic-exact-positive-v2，READY状态exact_positive、实际1次请求，
不能伪造六次完成或给v1加partial例外。标注exact_current_security/query_response_only/
universe_coverage=partial/historical_pit=false。它只证明获准来源返回的当前单证券身份，
不证明全市场覆盖、历史PIT、监管资质、财报/新闻/风险指标或投资结论。

证据保存在原固定stock_basic/<新ID>/内，保留白名单公共行、query/row摘要、时间及脱敏回执，
24小时过期，不改旧文件。沿用UUID:SHA256引用以及Start-MainlandAnalysis.ps1的-BankSnapshot参数。
新会话及财报acquire/reuse都重建新closed binding，绑定查询、基础登记、行、会话、证券、
分析截止日和期限；不信任父进程自报字段。旧已登记证券不覆盖、不全局注册新身份。
独立查询预算与分析3+60分开；取得身份后仍需适用的真实分析授权。
