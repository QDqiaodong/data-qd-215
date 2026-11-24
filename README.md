# data-qd-215 · 电池循环分段与能量核算

把测试台电压/电流序列切成充电、静置和放电阶段，按时间积分安时与瓦时，标记过短活动阶段，并计算有定义时的往返效率。

本仓交付 Python 数据 API；不控制充放电设备，也不推断电池健康或安全。完整约定见 docs/contract.md。

## 安装与验证

Python 3.11+；运行依赖仅标准库，构建固定 setuptools 80.9.0、wheel 0.45.1。

    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python --offline -r requirements-build.lock
    uv pip install --python .venv/bin/python --offline --no-index --no-build-isolation -e .
    .venv/bin/python -m unittest discover -s tests -v
    .venv/bin/python examples/run_sample.py

样例形成 charge/rest/discharge 三段并输出可复核积分和 round_trip_efficiency。

## 当前边界

每个区间采用左端样本决定状态，电流和电压用梯形积分。不处理不规则设备状态码、温度补偿、循环寿命模型或主动保护逻辑。
