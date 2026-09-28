# 有效因子库融合指南

## 一、改造总结

### 原始项目
- 项目：quant-research-lab（本地 Python 量化研究工作台）
- 核心能力：因子库治理、研究流水线、批量参数搜索、回测验证

### 融合方式
将 quant-research-lab 的核心能力抽象为 Nerya skill，使用 Nerya 原生工具链执行，不依赖外部数据库和代码。

### 产出
- `quant_factor_library` skill：因子库治理规则、生命周期管理、验证流水线
- 完全兼容 Nerya skill 规范，通过 `catalog_parent: quant_research` 挂载到现有量化研究体系下

---

## 二、有效因子库在 Nerya 里的体现

### 2.1 目录结构

```
strategies/research/
├── factors/
│   ├── candidates/          # 候选因子
│   │   └── ema_slope_20.py
│   ├── validated/           # 已验证因子
│   │   └── ema_slope_20.py
│   └── production/          # 生产因子（人工批准）
│       └── ema_slope_20.py
├── experiments/
│   └── runs/
│       └── <run-id>/
│           ├── manifest.json    # 实验清单
│           ├── metrics.json     # 因子指标
│           └── report.md        # 验证报告
└── library.json               # 因子库索引（agent 维护）
```

### 2.2 因子库索引格式

`library.json` 由 agent 自动维护，记录每个因子的当前状态：

```json
{
  "factors": [
    {
      "factor_id": "trend.ema_slope.20",
      "name": "EMA 20 slope",
      "family": "moving_average_trend",
      "category": "trend",
      "status": "validated",
      "version": 1,
      "formula_path": "strategies/research/factors/validated/ema_slope_20.py",
      "lookback": 20,
      "direction": "higher_is_bullish",
      "normalization": "rolling_zscore",
      "validation": {
        "out_of_sample": {"return": 0.08, "pf": 1.4, "trades": 156},
        "cost_stress": {"return": 0.05, "pf": 1.2},
        "correlation_with_existing": 0.12
      },
      "created_at": "2026-09-28T10:00:00Z",
      "updated_at": "2026-09-28T15:30:00Z"
    }
  ]
}
```

### 2.3 状态流转

```
candidate ──验证通过──> validated ──人工批准──> production
    │                       │                      │
    │                       │                      │
    ▼                       ▼                      ▼
rejected               degraded ──重新验证──> production
                           │
                           ▼
                        retired
```

---

## 三、因子库怎么累计

### 3.1 累计流程

```
用户提出因子想法
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│ Step 1: 注册候选因子                                      │
│ - Agent 在 strategies/research/factors/candidates/ 创建因子文件 │
│ - 更新 library.json 状态为 candidate                     │
└─────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│ Step 2: 快速验证（fast_screen）                           │
│ - 加载 quant_research skill 做统计分析                   │
│ - 加载 analysis skill 做数据 profiling                   │
│ - 检查：无未来数据、IC 值、换手率、衰减速度               │
│ - 通过 → 进入下一步；失败 → 标记 rejected               │
└─────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│ Step 3: 样本外验证（full_validation）                     │
│ - 加载 backtest skill 做真实成本回测                     │
│ - 多窗口样本外测试                                       │
│ - 成本压力测试                                           │
│ - 与现有生产因子做相关性去重                             │
│ - 通过 → 标记 validated；失败 → 标记 rejected           │
└─────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│ Step 4: 人工审批                                         │
│ - Agent 展示验证报告                                     │
│ - 用户确认后 → 移入 production/ 目录                     │
│ - 更新 library.json 状态为 production                   │
└─────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│ Step 5: 持续监控                                         │
│ - 定期用最新数据重新验证 production 因子                 │
│ - 发现衰减 → 标记 degraded                               │
│ - 重新验证通过 → 恢复 production                         │
│ - 持续衰减 → 标记 retired                                │
└─────────────────────────────────────────────────────────┘
```

### 3.2 关键原则

1. **无未来数据**：因子计算只能使用当前及历史数据
2. **多窗口验证**：至少 3 个独立时间窗口的样本外测试
3. **成本敏感**：扣除真实交易成本后仍有增量价值
4. **相关性去重**：与现有生产因子相关性 < 0.7
5. **人工审批**：production 状态必须人工批准，agent 无权自动晋升

### 3.3 使用 Nerya 工具

| 步骤 | 使用的 Nerya Skill | 作用 |
|------|-------------------|------|
| 统计分析 | `quant_research` | IC 分析、泄漏检查、信号验证 |
| 数据 profiling | `analysis` | 数据质量检查、图表展示 |
| 回测验证 | `backtest` | 真实成本回测、覆盖率检查 |
| 策略迭代 | `quant-strategy-loop` | 有界训练/校准循环 |

---

## 四、使用示例

### 4.1 注册新因子

```
用户：帮我研究一个 EMA 20 斜率因子

Agent 执行：
1. 创建 strategies/research/factors/candidates/ema_slope_20.py
2. 更新 library.json 添加 candidate 记录
3. 加载 quant_research 做初步统计分析
4. 报告：IC=0.03, 换手率=12%, 无泄漏
```

### 4.2 验证因子

```
用户：验证一下这个因子

Agent 执行：
1. 加载 backtest 做样本外回测
2. 多窗口测试（3 个独立时间段）
3. 成本压力测试
4. 与现有生产因子做相关性检查
5. 报告：OOS return=8%, PF=1.4, 相关性=0.12
6. 更新 library.json 状态为 validated
```

### 4.3 晋升生产

```
用户：把这个因子加入生产

Agent 执行：
1. 展示完整验证报告
2. 用户确认
3. 移入 strategies/research/factors/production/
4. 更新 library.json 状态为 production
```

---

## 五、注意事项

1. **Agent 不会自动晋升因子**：production 状态必须人工批准
2. **保留失败记录**：rejected 因子保留在 candidates/ 目录，标记失败原因
3. **定期复核**：production 因子至少每月用最新数据重新验证
4. **相关性检查**：新因子必须与现有生产因子做相关性去重
5. **数据版本**：记录每次验证使用的数据版本，确保可复现
