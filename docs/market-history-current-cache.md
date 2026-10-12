# 普通 A 股分析的本地行情缓存

`market_history_mode="current"` 显式启用普通 A 股分析的 local-first 行情读取。默认仍是 `shadow`，默认供应商顺序仍是 AKShare、BaoStock、Yahoo；本功能不会自动切换用户配置。

## 配置与读取顺序

日常使用可在项目根目录创建独立、无秘密的 `tradingagents.local.json`：

```json
{
  "market_history_mode": "current",
  "data_usage_mode": "personal_research",
  "mainland_capability_routing_mode": "legacy",
  "market_data_vendors": {
    "cn_a": {"core_stock_apis": "tushare"}
  }
}
```

启动脚本通过 `TRADINGAGENTS_PROJECT_ROOT` 指定该目录；直接运行 CLI 或 Python 包时，使用所加载源码的项目根目录，不搜索当前工作目录。此文件只允许上述四项普通研究配置，可仅提供其中部分项。其明确设置优先于环境变量和 dotenv，其他配置保持原值；文件不存在时原行为完全不变。重复键、未知字段和无效类型会使配置加载失败。不要在此文件放入密钥、数据库路径或资格设置，也不要把个人启用文件提交到版本库。

无需改动全局环境变量或 `.env`。如调用方主动使用进程环境配置，嵌套源选择还可通过 `TRADINGAGENTS_CN_A_CORE_STOCK_APIS` 指定；它只覆盖 `cn_a.core_stock_apis`。移除本地文件，或将其中 `market_history_mode` 改为 `shadow`，即可恢复相应行为。

- 设置 `market_history_mode="current"`，并使用 `data_usage_mode="personal_research"`。
- 普通入口和 picker 通过同一个客户端工厂使用固定的 `https://api.waditu.com/dataapi` 协议。应用只绑定每个 SDK 客户端的查询发送层，不修改已安装 SDK 或全局 Requests；证书及主机名验证始终开启，拒绝重定向、HTTP 降级和未知目标。没有自定义服务 URL 配置。发送层不自行重试，既有协调器、picker 显式重试策略及 40 次/分钟限制继续适用。资格路由的独立客户端及其资格绑定不在此功能范围内。
- 如需 Tushare 普通单源，显式设置 `market_data_vendors.cn_a.core_stock_apis="tushare"`，并设置 `mainland_capability_routing_mode="legacy"`；普通入口要求 legacy 路由才能选择 Tushare。这只选择当前分析供应商，不授予历史回放资格。凭据继续使用既有配置边界，不写入缓存配置或本文示例。
- `market_history_current_cache_max_age_seconds` 默认 `86400`，接受 `1..86400` 的整数秒；Tushare raw 历史超过该时长时只刷新请求窗最近 21 个交易日，其他 adjusted-frame 缓存超过有效期不能命中。
- Tushare 受项目每分钟最多 40 次调用的上限及既有协调器约束。2000 积分表示接口访问档位，不是每次请求扣减的可消耗余额，也不保证账户拥有每项接口权限。

CURRENT 核对证券身份、provider/dataset、复权方法与 normalizer 版本、证据完整性及 freshness。Tushare 在配置供应商链的相应位置读取持久 raw/status/factor 家族，允许平移、缩小、扩展窗口；其他供应商保留精确窗口 adjusted-frame 缓存。读取不会刷新原始观察时间。Tushare CURRENT 不以历史 acquisition outcomes 冒充本次网络调用，物理调用统计来自现有协调器的新增事件。

## 更新与日历

Tushare CURRENT 使用现有 MarketHistoryStore 的 observation/status/factor revision、bundle membership 和不可变 payload；不新增数据库，也不设置 200 日保留上限。后续仅补缺失的离散区间、新增交易日及到期的最近 21 个交易日；不把相隔很远的缺口压成一个大范围。每次需要 raw 更新时取得覆盖全部保留 raw 日期的同源因子集，从 raw 重建完整请求窗口，禁止拼接不同 qfq 基准的片段。缩小窗口不会删除旧行；全部候选验证成功后才一次发布，发布前核对前一版本，失败不推进覆盖。

调用方可在运行时配置中显式提供 `market_history_current_repair_ranges`（请求窗内 ISO 日期 `[start,end]` 列表）或 `market_history_current_force_refresh=true`（最近 21 个交易日）。它们不是 `tradingagents.local.json` 的新增允许字段，也不会自动读取环境变量。修复不会绕过精确身份、日历、因子和 freshness 校验。对早于保留尾部的窗口修复，因子请求必须覆盖保留尾部；外部执行授权若不允许该范围则应拒绝，不能截断因子或混用基准。

完整逻辑采集复用现有跨进程 coordinator workflow，当前每个 workflow 最多 24 个物理操作，provider 每页都计数；达到上限时失败并保留旧版本，不意味着只保留 24 页或 200 日。真正本地命中不取得物理请求许可。相同并发请求等待同一已提交结果；不同请求并发写入时，旧 head 的候选失败，不覆盖另一写入者。其他 adjusted-frame 供应商缺失或过期仍按整窗获取；BaoStock 的 qualified authoritative 路径独立存在。

CURRENT 接纳行情需要请求范围内完整自然日期的 OPEN/CLOSED 日历，以验证交易日覆盖。可以使用已保存并通过完整性校验的 BaoStock 日历，或同次 Tushare 响应中的日历。缺少自然日期时，只对缺口进行 calendar discovery：原 BaoStock discovery 每块最多 732 天；Tushare 按实际请求自然日期范围和 5000 行/页分页，五年约 1827/1828 个自然日可在一页内返回。已有日历 revision 保留。日历获取不自行授权价格补抓，也不把本地组合视图变成官方原始 revision。

Tushare 独立日历扩展写入现有不可变 session-calendar 表；只有闭市日期增加时，不重取价格、不更新 raw 的观察时间，下一次同窗不会重复发现日历。超时、分页未完成、权限/限流错误、缺因子、缺日历和开市缺行均不能成为覆盖。`suspend_d` 的停牌标记没有官方 carried close，当前实现因此拒绝包含此类缺行的窗口；它不会制造停牌价格或声称完整停牌支持。这是实际五年窗口验收可能遇到的限制。

## 证据、资格与回滚

缓存保留原始来源 artifact、snapshot 身份、采集时间和来源观察时间，使 evidence 层能够重建同一 source facts 与 lineage；Tushare proof 保留原始响应及因子数据。恢复读取会重新校验帧与证据的绑定。关闭后重新打开数据库的离线复用验证，只能证明相应本地持久化与读取行为，不能代表真实 native/API 调用通过或供应商获得资格。

Tushare raw 重建明确使用 `tushare-current-view-v1`，绑定精确 bundle、源 payload digest、独立日历 revision 及请求窗口；它不标为原始 provider response。该 lineage 参与普通 snapshot 身份。新增记录均为 retrospective backfill，`strict_history_qualified=False`；qualified publication 和直接 strict reconstruction 都拒绝该数据家族。旧 revision、payload、snapshot 证据持续保留。

Current-cache compatibility manifest 仅约束当前缓存实现和对应源码版本的兼容性，不授予 PIT、Tushare strict history 或 B1 资格。缺少 manifest、源码变更导致不匹配或校验失败时，系统拒绝缓存命中；Tushare CURRENT raw 路径也拒绝读取，不能通过 fallback adapter 绕过 gate。此时 Tushare 可走独立完整 live 获取，逐页继续经过原有 typed-request 协调和请求预算，不读取 retained raw，也不宣称本地复用；其他供应商仍按原有独立 live 路径处理。切回 `shadow` 或 `disabled` 可停用 CURRENT 本地优先读取；`shadow` 仍遵守其既有旁路写入语义。

此配置指南不授予真实账户或生产数据库执行权限。启用后的真实账户权限与真实 API 行为仍需在相应授权范围内单独验证。

## 用户手动启动的双股票数据验收

`scripts/accept_tushare_current.py` 固定处理 `600895.SH`、`000001.SZ` 从 `E` 往前五年的完整窗口，不调用模型。用户启动后，脚本先安装 HTTPS 和共享预算闸，再通过既有跨进程 coordinator 查询 SSE、SZSE 在启动时上海日期之前的 31 个自然日。两个日历必须逐日完整、无重复且开闭市状态一致，才能自动选择最近共同开市日 E；空日历、覆盖不全、两所冲突或没有共同开市日均停止。日期窗口在启动时固定，跨午夜不扩展窗口、不重查。

`--end E` 可选；指定时 E 必须位于同一近期 31 日范围，并经两所日历确认开市。显式日期不会跳过日历查询。日历只证明交易安排，不能证明日线或因子已完整发布；后续普通行情入口仍校验完整五年覆盖，且每只返回的有效日期必须等于 E。本文不执行该脚本。

在明确授权的源码目录 `SOURCE` 中，用该项目的 Python 手动启动，例如（路径应替换为真实确认值，账本必须使用新文件名）：

```powershell
& 'SOURCE\.venv\Scripts\python.exe' -I -S -B 'SOURCE\scripts\accept_tushare_current.py' --database 'SOURCE\data\market_history\market_history.sqlite3' --ledger 'SOURCE\logs\tushare-current-acceptance-new.jsonl'
```

`--database` 必须精确指向本 checkout 既有的 `data/market_history/market_history.sqlite3`；该库及相邻 `.provider-requests.sqlite3` authority 库必须已具备当前 schema 9，authority 中还须存在主库对应的 legacy-state import marker。缺库、旧 schema、缺 marker 或 SDK source pin 不符均在应用导入/凭据加载前停止；脚本不会初始化、迁移或自动重试。`--ledger` 必须是 `SOURCE/logs` 直属、尚不存在的 `.jsonl` 文件，禁止复用旧账本。

日历发现、两个股票及每一分页/失败合计最多 24 次实际请求，使用同一账本和计数器，仍受 40 次/分钟限制。选定 E 时不重置预算或 pacing；没有额外日历预算。正常完成初次数据阶段后，关闭旧连接并使用新请求上下文重开，复用阶段必须新增 0 次发送，且价格 digest、snapshot 身份和原始检索时间一致。5000 行/页、每股五年 daily、完整自然日 calendar、空 suspend、6000 行全历史 factor 的合成场景为发现阶段 2 页，加每股 5 页，共 12 页；此计数不是对真实响应、权限或停牌窗口可成功的承诺。更多分页、错误或不完整数据仍服从合计 24 次上限，不能通过缩短五年范围或提高预算通过验收。账本保留两所 31 日开闭市安排、选定 E 和 `publication_checked=false`，随后单独记录实际行情验证结果。
