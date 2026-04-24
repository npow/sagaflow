"""Classifier subsystem — routes user prompts to specialist workflows."""

from sagaflow.missions.classifier.llm import classify_llm
from sagaflow.missions.classifier.prompts import CLASSIFIER_PROMPT
from sagaflow.missions.classifier.rules import (
    ClassifierResult,
    ClassifierVerdict,
    classify,
    classify_prefix,
    classify_rules,
)

__all__ = [
    "ClassifierVerdict",
    "ClassifierResult",
    "classify",
    "classify_prefix",
    "classify_rules",
    "classify_llm",
    "CLASSIFIER_PROMPT",
]
