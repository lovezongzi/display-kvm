"""display-kvm 核心领域层。

把原 agent.py 的"上帝类"按职责拆成多个 mixin（transport / pairing / sync /
discovery / switch / control / probe），由 agent.py 里的 Agent 组合它们。

拆分原则
--------
- 每个 mixin 只读写自己的那一类职责，**不直接改别的职责的字段**，需要别的
  模块干活时一律走 `self.xxx()`（方法调用），由 Agent 的 MRO 把方法解析到
  对应 mixin。这样模块之间只有"方法调用"一种耦合，没有跨文件的状态纠缠。
- Agent 自身只保留：__init__（建 socket + 集中放共享字段）、run（启线程）、
  对外 facade（api_*/update_cfg/_save_cfg）、以及 main/load_config 入口。
- 所有对外的命令、UDP 协议、TCP 控制口协议、配置文件结构**一律不变**，
  外部（app.py / kvmctl.py / 已存的测试用例）完全无感。
"""
