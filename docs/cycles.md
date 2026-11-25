# 分块电池循环契约（`process_cycles`）

`process_cycles(data)` 在保留 [`process`](contract.md) 的样本、精确有理数、分段、梯形积分和 `too_short` 语义之外，处理测试台分块上传的多个电池运行。

## 顶层结构

```python
{
    "active_current_a": "1",
    "min_phase_seconds": "5",
    "runs": [
        {
            "run_id": "run-1",
            "closed": False,
            "final_ordinal": None,
            "chunks": [
                {"chunk_id": "c0", "ordinal": 0, "samples": [...]},
            ],
        }
    ],
}
```

- 顶层只能有上述三个字段；`runs` 为 1..64 项，`run_id` 必须是非空字符串且全局唯一。
- 数值仍是十进制字符串，内部全部使用 `Fraction`，输出的安时、瓦时和效率为精确分数字符串；整数输出为 `"3"` 而不是 `3.0`。
- 布尔值不能冒充整数；`ordinal`、`final_ordinal` 必须是非负整数。
- 每个块含 2..5000 个样本；单个运行去重和边界合并后最多 100000 个样本。

## 重建与原子性

块先按 `ordinal` 排序。序号必须从 0 连续；封闭运行必须有 0..`final_ordinal` 的全部块，开放运行已提供序号也必须连续。

相邻序号块必须共享同一个完整边界样本：前块末样本与后块首样本的 `id/time_s/voltage_v/current_a` 相等。合并时该样本只保留一次。相同 `chunk_id` 且原始内容完全一致的重传块只计算一次；同 ID 内容冲突、同序号指向不同块、边界不一致、非边界重复 ID、样本时间非严格递增等均为该运行的坏数据。

顶层契约错误抛出 `InputError`（`ValueError` 子类）。运行身份冲突会作为该运行的原子 rejected 结果返回；在多运行请求中，序号缺口、边界错误、非相邻重复样本、封闭运行缺末块等运行错误也会被隔离，其他运行继续处理。对应结果为：

```python
{
    "run_id": "run-1",
    "status": "rejected",
    "closed": None,
    "phases": [],
    "cycles": [],
    "orphan_phase_ids": [],
    "pending_cycle": None,
    "continuation": None,
    "error": {"code": "boundary_mismatch", "message": "..."},
    "error_code": "boundary_mismatch",
    "error_category": "boundary_mismatch",
    "error_message": "...",
}
```

错误类别代码是稳定的 snake_case 字符串。按验收约定，单运行请求中的上述整请求契约错误会直接作为 `ValueError` 抛出；多运行请求默认隔离。调用 `process_cycles(data, raise_on_run_error=False)` 可在单运行时也始终得到 rejected 结果，传 `True` 则任一运行错误立即抛出；也可以直接使用 `process_run(run, threshold, min_duration)`。

函数不会修改 `data` 或其中的嵌套对象。

## 循环资格

阶段仍按重建后的时间顺序编号为 `p1`, `p2`, ...。循环规则：

1. `charge` 开始候选循环；
2. 其后最多吸收一个 `rest`；
3. 必须遇到 `discharge` 才形成充放电配对；
4. 放电后紧邻的一个 `rest` 属于该循环；
5. 放电前再次遇到 `charge`，旧候选记为 `incomplete / missing_discharge`，从新 `charge` 重新开始；
6. 第一个 `charge` 之前尚未配对的 `rest`、`discharge` 等阶段放入 `orphan_phase_ids`；
7. 已配对循环若充电或放电阶段为 `too_short`，仍输出阶段 ID 与能量，但状态为 `ineligible`，`reason="short_active_phase"`；否则为 `eligible`。

封闭运行末尾只有充电候选时，该循环在 `cycles` 中为 `incomplete / missing_discharge`。开放运行的当前候选放在 `pending_cycle`，状态为 `pending`，不会进入已确定的 `cycles`；被后续充电取代的旧候选仍会作为 `incomplete` 保留。完整配对即使运行开放也进入 `cycles`，不会标记为 pending。

充电能量为零时 `round_trip_efficiency` 为 `null`；否则输出放电能量（正幅值）/充电能量的精确分数。

开放运行的 `continuation` 形如：

```python
{"next_ordinal": 2, "last_sample_id": "s4"}
```

封闭运行和 rejected 运行的 `continuation` 为 `null`。

顶层结果为 `{"runs": [...]}`，运行按 `run_id` 字典序排列；运行内阶段和循环按时间顺序排列。直接访问时还提供 `state`、`results`、`phases`（循环内的阶段 ID 列表）、`next_chunk_ordinal`、`error["category"]` 等语义别名，但标准字段以上述名称为准。
