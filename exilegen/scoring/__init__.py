"""exilegen scoring layer: keeps the optimiser from winning through PoB gaps.

Modules: adapter (schema access), conditions (registry), energy (meta-gem trigger rates), floors,
constraints (DSL), objective (Scorer, Pareto helpers). See README.md.
"""
from .adapter import BuildView
from .conditions import Registry
from .objective import Scorer, dominates, front

__all__ = ["BuildView", "Registry", "Scorer", "dominates", "front"]
