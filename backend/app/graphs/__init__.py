"""LangGraph 图编排层（阶段 C：行为等价迁移）。

PlanningGraph 复刻 MultiAgentTripPlanner.plan_trip 的现有流程：
research(并行) → generate_plan(含受限修复) → finalize_plan(质量/预算/路线)
节点内部调用既有方法与提示词，产出与旧路径完全一致。
"""
