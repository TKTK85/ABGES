# -*- coding: utf-8 -*-
"""
Router
======
路由决策：依据任务类型、数据可用性与模型类型，选择 Agent / Skill / Workflow 组合。

两条职责：
  1. Agent 边界：谁能调用谁，由 agent-registry#can_call 决定，
     越界调用直接拒绝（只有 00-supervisor 有调度权）；
  2. 模型选型：按 model-registry#selection_rules 顺序匹配，命中即停。
     用户显式指定但数据不满足 requires 时，拒绝执行并给出可行方案，
     绝不静默改选（forbid_silent_switch）。

本组件只做决策，不执行任何数据处理。
"""

from _bootstrap import bootstrap

bootstrap()

import abges_core as core

AGENT_REGISTRY = "configs/agents/agent-registry.yaml"
MODEL_REGISTRY = "configs/models/model-registry.yaml"


class ModelDecision(object):
    """一次模型选型的完整结论，含依据与可追溯理由。"""

    def __init__(self, model_id, rule_id, reason, source, requires=None,
                 missing=None, alternatives=None):
        self.model_id = model_id
        self.rule_id = rule_id
        self.reason = reason
        self.source = source              # explicit | auto
        self.requires = requires or []
        self.missing = missing or []
        self.alternatives = alternatives or []

    def to_dict(self):
        return {
            "model_id": self.model_id,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "source": self.source,
            "requires": self.requires,
            "missing": self.missing,
            "alternatives": self.alternatives,
        }


class Router(object):
    """Agent 与模型路由。"""

    def __init__(self, audit=None, agent_registry=None, model_registry=None):
        self.audit = audit
        self.agent_config = agent_registry or core.load_yaml_config(AGENT_REGISTRY)
        self.model_config = model_registry or core.load_yaml_config(MODEL_REGISTRY)
        self.agents = {entry["id"]: entry
                       for entry in self.agent_config.get("agents", []) or []}
        self.by_key = {entry.get("key"): entry
                       for entry in self.agent_config.get("agents", []) or []}
        self.models = {entry["id"]: entry
                       for entry in self.model_config.get("models", []) or []}

    # -----------------------------------------------------------
    # 一、Agent 查询与边界
    # -----------------------------------------------------------
    def agent(self, agent_id):
        entry = self.agents.get(str(agent_id))
        if entry is None:
            raise core.AbgesError("E016", "未登记的 Agent：%s" % agent_id,
                                  {"known": sorted(self.agents.keys())})
        return entry

    def agent_name(self, agent_id):
        return self.agent(agent_id).get("name", agent_id)

    def skills_of(self, agent_id):
        return list(self.agent(agent_id).get("skills", []) or [])

    def gates_of(self, agent_id):
        return list(self.agent(agent_id).get("gates", []) or [])

    def is_optional(self, agent_id):
        return bool(self.agent(agent_id).get("optional", False))

    def assert_can_call(self, caller_id, callee_id):
        """
        校验调用边界。除 00-supervisor 外，各 Agent 的 can_call 均为空，
        任何横向调用都视为越界。
        """
        allowed = list(self.agent(caller_id).get("can_call", []) or [])
        if str(callee_id) not in allowed:
            raise core.AbgesError(
                "E016", "Agent %s 无权调用 %s" % (caller_id, callee_id),
                {"allowed": allowed})
        if self.audit is not None:
            self.audit.event("route", caller=caller_id, callee=callee_id)
        return True

    def pipeline(self):
        pipeline = list(self.agent_config.get("default_pipeline", []) or [])
        duplicates = sorted({agent_id for agent_id in pipeline if pipeline.count(agent_id) > 1})
        if duplicates:
            raise core.AbgesError("E016", "default_pipeline contains duplicate Agent IDs: %s" % duplicates,
                                  {"pipeline": pipeline, "duplicates": duplicates})
        return pipeline

    def parallel_groups(self, availability=None):
        """Return only the branch group valid for this trait source.

        Derived traits are a dependency chain (04 -> 01), not a fourth sibling
        of phenotype QC.  Pedigree/genotype remain independent branches.
        """
        derived_required = bool((availability or {}).get("traits_missing_in_phenotype"))
        selected = []
        for entry in self.agent_config.get("parallel_groups", []) or []:
            if isinstance(entry, dict):
                when = entry.get("when")
                if when == "derived_traits" and not derived_required:
                    continue
                if when == "raw_traits" and derived_required:
                    continue
                entry = entry.get("members", [])
            selected.append(list(entry))
        return selected

    def skip_conditions(self):
        return dict(self.agent_config.get("skip_conditions", {}) or {})

    def common_constraints(self):
        return dict(self.agent_config.get("common_constraints", {}) or {})

    # -----------------------------------------------------------
    # 二、跳过判定
    # -----------------------------------------------------------
    def should_skip(self, agent_id, availability):
        """
        判断某阶段是否跳过，返回 (是否跳过, 理由)。

        availability 需含：
          has_pedigree / has_genotype / model_hint / traits_missing_in_phenotype
        判定口径与 agent-registry#skip_conditions 一一对应，
        并把"目标性状是否已在表型表中"作为 04 的实际依据。
        """
        agent_id = str(agent_id)
        conditions = self.skip_conditions()

        if agent_id == "02":
            if not availability.get("has_pedigree"):
                model_hint = str(availability.get("model_hint") or "").lower()
                if model_hint == "gblup":
                    return True, conditions.get("02", "无系谱文件且模型为 gblup")
                return True, "未提供系谱文件"
            return False, None

        if agent_id == "03":
            if not availability.get("has_genotype"):
                return True, conditions.get("03", "无基因型文件")
            return False, None

        if agent_id == "04":
            pending = list(availability.get("traits_missing_in_phenotype") or [])
            if not pending:
                return True, conditions.get(
                    "04", "目标性状均为原始性状，无需派生")
            return False, None

        return False, None

    # -----------------------------------------------------------
    # 三、模型选型
    # -----------------------------------------------------------
    def model_entry(self, model_id):
        entry = self.models.get(str(model_id).lower())
        if entry is None:
            raise core.AbgesError("E012", "未登记的模型：%s" % model_id,
                                  {"known": sorted(self.models.keys())})
        return entry

    def model_requires(self, model_id):
        return list(self.model_entry(model_id).get("requires", []) or [])

    def workflow_for(self, model_id):
        return self.model_entry(model_id).get("workflow")

    def model_config_path(self, model_id):
        return self.model_entry(model_id).get("config")

    def _unmet_requirements(self, model_id, availability):
        """返回该模型缺失的数据类型清单。"""
        mapping = {
            "phenotype": availability.get("has_phenotype", False),
            "pedigree": availability.get("has_pedigree", False),
            "genotype": availability.get("has_genotype", False),
        }
        return [item for item in self.model_requires(model_id)
                if not mapping.get(item, False)]

    def feasible_models(self, availability):
        """列出当前数据条件下可执行的模型，按 priority 升序。"""
        feasible = []
        for model_id, entry in self.models.items():
            if not self._unmet_requirements(model_id, availability):
                feasible.append((int(entry.get("priority", 99)), model_id))
        return [model_id for _, model_id in sorted(feasible)]

    def select_model(self, availability, explicit=None):
        """
        选定模型。

        availability 需含 has_phenotype / has_pedigree / has_genotype，
        以及可选的 genotyped_ratio（已测型个体占比）。

        显式指定且数据满足     -> 采用，source=explicit
        显式指定但数据不满足   -> 抛错，附可行模型清单（禁止静默改选）
        未指定或 auto          -> 按 selection_rules 顺序匹配
        """
        explicit_id = str(explicit).lower() if explicit else "auto"

        if explicit_id and explicit_id not in ("auto", "none", ""):
            missing = self._unmet_requirements(explicit_id, availability)
            if missing:
                alternatives = self.feasible_models(availability)
                override = self.model_config.get("explicit_override", {}) or {}
                raise core.AbgesError(
                    "E012",
                    "显式指定的模型 %s 需要 %s，当前缺少 %s"
                    % (explicit_id, self.model_requires(explicit_id), missing),
                    {"missing": missing,
                     "feasible_models": alternatives,
                     "forbid_silent_switch": bool(
                         (override.get("on_requirement_unmet") or {})
                         .get("forbid_silent_switch", True))})
            decision = ModelDecision(
                explicit_id, "EXPLICIT", "用户显式指定，且数据满足其 requires",
                "explicit", self.model_requires(explicit_id))
            self._audit_decision(decision)
            return decision

        for rule in self.model_config.get("selection_rules", []) or []:
            if not self._rule_matches(rule.get("condition", {}) or {}, availability):
                continue
            selected = rule.get("select")
            if not selected:
                raise core.AbgesError(
                    "E012", "%s：%s" % (rule.get("rule_id", "MS"),
                                        rule.get("reason", "无法构建亲缘关系矩阵")),
                    {"action": rule.get("action"),
                     "availability": dict(availability)})
            decision = ModelDecision(
                str(selected).lower(), rule.get("rule_id"), rule.get("reason"),
                "auto", self.model_requires(selected))
            self._audit_decision(decision)
            return decision

        raise core.AbgesError(
            "E012", "选型规则未覆盖当前数据组合，拒绝臆断",
            {"availability": dict(availability)})

    def _rule_matches(self, condition, availability):
        """逐项比对选型条件。任一项不符即不匹配。"""
        for key, expected in condition.items():
            if key == "genotyped_ratio_lt":
                ratio = availability.get("genotyped_ratio")
                if ratio is None or not ratio < float(expected):
                    return False
            elif key == "genotyped_ratio_gte":
                ratio = availability.get("genotyped_ratio")
                if ratio is None or not ratio >= float(expected):
                    return False
            else:
                if bool(availability.get(key, False)) != bool(expected):
                    return False
        return True

    def _audit_decision(self, decision):
        if self.audit is not None:
            self.audit.event("model_selected", **decision.to_dict())
        return decision

    # -----------------------------------------------------------
    # 四、多性状约束
    # -----------------------------------------------------------
    def multi_trait_policy(self):
        return dict(self.model_config.get("multi_trait", {}) or {})

    def engine_entry(self, engine_name="hiblup"):
        return dict((self.model_config.get("engines", {}) or {})
                    .get(engine_name, {}) or {})
