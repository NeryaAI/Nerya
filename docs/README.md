# Developer guide

Start with the [repository guide](../AGENTS.md) for the source layout and local commands. Use the domain references below when changing a subsystem.

| Changing | Reference |
| --- | --- |
| Conversation ownership, delegation or result delivery | [Assistant conversations](assistant-conversations.md) |
| Scheduling, durable runs or financial actions | [Task runs and financial actions](task-runs-and-financial-actions.md) |
| Approval requests and reusable tool grants | [Tool authorization](tool-permission-authorization.md) |
| Historical data storage and replay | [Backtest data pipeline](backtest-data-pipeline.md) |
| Adding a trading transport or component | [Trading components](trading-components.md) |

For strategy creation, the [authoring Skill](../nerya/skills/builtin/strategy_author/SKILL.md) defines what saving a draft delivers. A [historical replay](../nerya/skills/builtin/backtest/SKILL.md) is a separate operation. Neither action activates live trading.

Keep reusable development references here. Put one-off test results, screenshots and release evidence in the pull request or CI artifacts, not in this directory.
