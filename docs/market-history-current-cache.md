# 普通 A 股分析的本地行情缓存

`market_history_mode="current"` 显式启用普通 A 股分析的 local-first 行情读取。默认仍是 `shadow`，默认供应商顺序仍是 AKShare、BaoStock、Yahoo；本功能不会自动切换用户配置。

## 配置与读取顺序

- 设置 `market_history_mode="current"`，并使用 `data_usage_mode="personal_research"`。
- 如需 Tushare 普通单源，显式设置 `market_data_vendors.cn_a.core_stock_apis="tushare"`，并设置 `mainland_capability_routing_mode="legacy"`；普通入口要求 legacy 路由才能选择 Tushare。这只选择当前分析供应商，不授予历史回放资格。凭据继续使用既有配置边界，不写入缓存配置或本文示例。
- `market_history_current_cache_max_age_seconds` 默认 `86400`，接受 `1..86400` 的整数秒；超过有效期的帧不能命中。
- Tushare 受项目每分钟最多 40 次调用的上限及既有协调器约束。2000 积分表示接口访问档位，不是每次请求扣减的可消耗余额，也不保证账户拥有每项接口权限。

CURRENT 按配置的供应商顺序查找本地完整帧，核对证券身份、精确起止窗口、provider/dataset、复权基准、方法与 normalizer 版本、证据完整性及 freshness。所有供应商的本地候选都未命中后，才进入原有 live provider chain。缓存中的历史 acquisition outcomes 会保留，它们描述原始采集过程，不能解读为本次主供应商请求失败；缓存命中也不会刷新原始观察时间。

## 更新与日历

缺失或过期的 adjusted-frame 窗口按整窗重新获取。不会直接拼接不同观察批次、供应商或因子基准的 qfq 片段。BaoStock authoritative 模式下经过资格约束的 raw + factor 增量路径仍独立存在；CURRENT 缓存不替代该路径。

CURRENT 接纳行情需要请求范围内完整自然日期的 OPEN/CLOSED 日历，以验证交易日覆盖。可以使用已保存并通过完整性校验的 BaoStock 日历，或同次 Tushare 响应中的日历。缺少自然日期时，只对缺口进行有界 calendar discovery，每块最多 732 天；长窗口可以分块，已有日历 revision 保留。日历获取不自行授权价格补抓，也不把本地组合视图变成官方原始 revision。

## 证据、资格与回滚

缓存保留原始来源 artifact、snapshot 身份、采集时间和来源观察时间，使 evidence 层能够重建同一 source facts 与 lineage；Tushare proof 保留原始响应及因子数据。恢复读取会重新校验帧与证据的绑定。关闭后重新打开数据库的离线复用验证，只能证明相应本地持久化与读取行为，不能代表真实 native/API 调用通过或供应商获得资格。

Current-cache compatibility manifest 仅约束当前缓存实现和对应源码版本的兼容性，不授予 PIT、Tushare strict history 或 B1 资格。缺少 manifest、源码变更导致不匹配或校验失败时，系统拒绝缓存命中，保留诊断，并回到原有 live 路径。切回 `shadow` 或 `disabled` 可停用 CURRENT 本地优先读取；`shadow` 仍遵守其既有旁路写入语义。

此配置指南不授予真实账户或生产数据库执行权限。启用后的真实账户权限与真实 API 行为仍需在相应授权范围内单独验证。
