# 离线计量审定与取用凭证契约（`metrology`）

本模块在既有 [`process_cycles`](cycles.md) 证据之上提供离线计量审定（metrological audit）和下游取用凭证（access credential）。所有计算仅使用 Python 3.11 标准库，数值全部为精确有理数（`fractions.Fraction`），不接受调用方自填能量，也不控制任何充放电设备或前端。

- 名义能量永远来自随审定请求一并提交的 `process_cycles` 输入，由本仓 `process_cycles` 实际算出的 `charge_wh`、`discharge_wh` 决定。
- 校准档案从空档案开始；更正（correction）以整批通道区间生成新版本，未涉及通道继承上一版。
- 原始试验输入与已签发档案均不可变；更正只改变下游此刻可取用的范围，不重写历史。

## 1. 审定请求

`audit_batch(request, intervals)` 为无状态纯函数；`MetrologyStore.audit(request, *, version=None)` 在持久化档案上按当前版（或指定历史版）审定。请求对象只允许以下字段：

```python
{
    "batch_id": "batch-2026-001",          # 非空字符串
    "threshold": "0.9",                    # 正的有限十进制字符串，效率门槛 T
    "charge_channel": "ch-voltage-a",      # 非空字符串，充电计量通道
    "discharge_channel": "ch-voltage-b",   # 非空字符串，放电计量通道（可与充电相同）
    "process_input": { ... },              # 一个完整合法的 process_cycles 输入
    "samples": [                           # 非空抽检清单
        {"sample_id": "sp-1", "run_id": "run-7", "cycle_id": "c1"},
    ],
}
```

输入规则：

- `sample_id` 在本审定内唯一；同一次审定不得以同一个 `(run_id, cycle_id)` 循环冒充多个样件，否则抛 `MetrologyInputError`。
- 样件对象只允许 `sample_id/run_id/cycle_id` 三个字段；在样件里夹带 `charge_wh` 等能量字段会被拒绝。能量只能取自仓库计算结果。
- `threshold`、校准区间的 `lo/hi` 必须是有限十进制字符串：可选正负号、数字和至多一个小数点；不接受科学计数法（`1e-3`）、百分号、分数或浮点数。
- 请求对象多出或缺少字段、空清单、空通道 ID、非正门槛均抛 `MetrologyInputError`（`InputError`/`ValueError` 子类）。
- 函数不修改 `process_input` 或其嵌套对象。

### 测量证据可用性

样件证据必须同时满足：来源可在该 `process_input` 的处理结果中定位、运行 `status == "closed"`、循环 `status == "eligible"`、且 `charge_wh` 与 `discharge_wh` 均为正。任何不满足的样件都出现在输出里，状态为 `unverifiable`，不能跳过：

| reason 代码 | 含义 |
| --- | --- |
| `run_not_found` | 处理结果中没有该 `run_id` |
| `run_not_closed` | 命中运行不是封闭运行（含 open 与 rejected） |
| `cycle_not_found` | 封闭运行内没有该 `cycle_id` |
| `cycle_ineligible` | 循环存在但状态不是 `eligible` |
| `non_positive_energy` | 循环能量不满足两项均为正 |
| `channel_not_registered` | 充/放通道之一未在该版登记（`reason_detail.channels` 给出缺失通道） |

沿用既有运行隔离语义：处理以 `raise_on_run_error=False` 进行，无关运行被隔离为 rejected，不会让引用其他有效运行的样件变成坏数据。

## 2. 校准区间与逐样件上下界

校准版为每个通道 `m` 给出增益误差闭区间 `[lo, hi]`，要求 `-1 < lo <= hi`。该版下：

```
实际 Wh = 名义 Wh × (1 + g_m)
```

同一通道的 `g_m` 是同一个未知量（充电与放电共用一个通道时，二者乘同一个 `g`）；不同通道的未知量相互独立，可各自在闭区间内取任意值。对每个样件精确计算

```
margin(g) = 实际放电 Wh − T × 实际充电 Wh
```

的最小值 `margin_min` 与最大值 `margin_max`。实现先按通道合并 `g` 的系数（共用通道自然变成单一未知量），再对每个独立未知量在闭区间端点上取极值，全程 `Fraction` 运算。

样件判定：

- `margin_min >= 0` → `pass`
- `margin_max < 0` → `fail`
- 其他（区间跨过 0）→ `uncertain`
- 证据或通道缺失 → `unverifiable`

整批 `verdict` 优先级：存在 `unverifiable` 即 `unverifiable`；否则存在 `fail` 即 `fail`；否则存在 `uncertain` 即 `uncertain`；全部 `pass` 才是 `pass`。不允许用批次平均值或强样件抵消未通过样件。

输出为 JSON 安全结构，所有有理数为精确分数字符串（整数形如 `"3"`）。可审定样件还带 `calibration_basis`，记录当时所用每个通道的 `{lo, hi}`：

```python
{
    "sample_id": "sp-1",
    "run_id": "run-7",
    "cycle_id": "c1",
    "status": "pass",
    "reason": None,
    "reason_detail": None,
    "nominal_charge_wh": "10",
    "nominal_discharge_wh": "9",
    "margin_min": "0",
    "margin_max": "0",
    "calibration_basis": {"m-shared": {"lo": "-1/10", "hi": "1/10"}},
}
```

## 3. 校准档案与更正（`MetrologyStore`）

```python
from battery_cycles import MetrologyStore

store = MetrologyStore("/var/lib/battery/metrology.json")
```

- 路径不存在时初始化空档案并立即落盘；存在时整体读入。空档案 `current_version == 0`，没有任何已登记通道。
- `apply_correction({"ch-a": {"lo": "-0.01", "hi": "0.01"}, ...})` 成批登记或替换通道区间。
  - 更正必须是非空通道映射；整批全部合法才生成下一版，任何一条非法（空映射、`lo <= -1`、`lo > hi`、非十进制、结构错误、空通道 ID 等）都抛 `CalibrationError`，**不产生新版本、不改动任何凭证**。
  - 未涉及的通道从上一版继承；版本号从 1 开始单调递增。
  - 返回 `{"version", "intervals", "revoked_credentials"}`。
- `list_versions()` / `get_version(n)` 查询版本与该版完整通道区间；`get_version` 对不存在的版号抛 `VersionNotFoundError`。
- `audit(request, version=n)` 可按历史版区间重放审定，不影响当前版。

### 凭证重判的原子性

每次更正生成新整版时，所有 `active` 凭证都按新版全通道重新判定：任一样件 `margin_min < 0`（即不再 `pass`，包含 `fail` 与 `uncertain`）凭证即从该版起永久不可用，记录 `first_failed_version` 与首个导致失效的 `first_failed_sample`。重判与新版本在同一次原子写入中完成，因此取用检查不可能观察到“新校准已生效、旧取用许可却还未撤下”的中间状态。

## 4. 取用凭证

### 签发 `issue_credential(credential_id, request)`

- `credential_id` 由调用方指定且必须全局唯一；重复使用曾签发过的号抛 `CredentialError`，即使旧凭证已失效也不复用。
- 仅当请求在**当前校准版**下整批 `verdict == "pass"` 才能签发；否则抛 `CredentialError`。
- 同一批次同时最多一张 `active` 凭证；已存在有效凭证时再签同批次抛 `CredentialError`。
- 失败的签发不落任何部分状态。
- 签发档案保存：批次 ID、样件与原试验引用（`run_id/cycle_id`）、签发时名义能量、门槛、当时区间、逐样件上下界、校准版号，以及生命周期字段。

### 查询与取用检查

- `get_credential(credential_id)`：始终可读的签发档案加生命周期元数据。历史档案的签发快照（名义能量、门槛、区间、上下界、版号）永不随更正改变；`status/first_failed_version/first_failed_sample` 是附加的生命周期记录。未知号抛 `CredentialNotFoundError`。
- `check_access(credential_id)`：当前能否用于下游，返回 `usable`、`status`（`active`/`revoked`）、`issued_at_version`、`current_version` 及失效信息。
- `list_credentials(batch_id=None)`：列出全部或指定批次凭证。
- 区间即使恢复到安全范围也**不能复活**旧凭证；同批次可按当前证据另行签发新号。
- 失效在更正当时即重判并持久化，因此重开本地存储后，即使一张凭证从未被查询过，也不会漏判其失效。

### 持久化契约

- 整个档案为单个 JSON 文件，顶层 `{"format": "battery-cycles-metrology-1", "versions": [...], "credentials": {...}}`；所有有理数以精确分数字符串存储，无 NaN/Infinity。
- 每次变更先写同目录临时文件、`flush` + `fsync`，再 `os.replace` 原子替换，并对目录 `fsync`；异常时清理临时文件，旧文件保持可用。
- 加载时校验 `format` 与结构，损坏档案抛 `MetrologyInputError`。
- 档案面向单进程离线使用；`close()` 或上下文管理器退出后拒绝继续操作。

## 5. 错误类型

| 异常 | code | 触发场景 |
| --- | --- | --- |
| `MetrologyInputError` | `invalid_metrology_input` | 审定/档案请求结构或数值不合法 |
| `CalibrationError` | `invalid_calibration` | 更正批不合法，不产生新版本 |
| `CredentialError` | `credential_rejected` | 签发失败（未通过、重复号、同批次已有有效凭证） |
| `CredentialNotFoundError` | `credential_not_found` | 查询/取用检查未知凭证号 |
| `VersionNotFoundError` | `version_not_found` | 引用不存在的校准版 |

均继承自既有 `InputError`（`ValueError` 子类）。纯函数 `build_evidence_index(process_input)` 也单独导出，便于离线复核证据索引。

## 6. 边界

本模块不估计校准漂移之外的任何测量不确定度，不引入概率分布或蒙特卡洛方法，不做设备控制、告警或前端展示；通道区间与门槛的法定适用性由计量方在更正数据中负责。
