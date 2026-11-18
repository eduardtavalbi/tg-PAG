
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def validate_positive_amount(value: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise ValueError("Сумма должна быть положительным числом")
    return value


def validate_currency(value: str) -> str:
    value = value.strip().upper()

    if len(value) != 3 or not value.isalpha():
        raise ValueError("Валюта должна состоять из трёх букв")

    return value


class Transaction(BaseModel):
    type: Literal["income", "expense"]
    amount: Decimal
    description: str
    category: str
    currency: str = "CNY"

    @field_validator("amount")
    @classmethod
    def check_amount(cls, value):
        return validate_positive_amount(value)

    @field_validator("currency")
    @classmethod
    def check_currency(cls, value):
        return validate_currency(value)


class GoalCreation(BaseModel):
    name: str
    target_amount: Decimal
    deadline: str | None = None
    currency: str = "CNY"

    @field_validator("target_amount")
    @classmethod
    def check_target_amount(cls, value):
        return validate_positive_amount(value)

    @field_validator("currency")
    @classmethod
    def check_currency(cls, value):
        return validate_currency(value)


class GoalContribution(BaseModel):
    goal_name: str
    amount: Decimal

    @field_validator("amount")
    @classmethod
    def check_amount(cls, value):
        return validate_positive_amount(value)


class GoalPercentageAllocation(BaseModel):
    goal_name: str
    percentage: Decimal

    @field_validator("percentage")
    @classmethod
    def check_percentage(cls, value):
        if not value.is_finite() or value <= 0 or value > 100:
            raise ValueError("Процент должен быть от 0 до 100")
        return value


class TransactionResult(BaseModel):
    transactions: list[Transaction] = Field(default_factory=list)
    goal_creation: GoalCreation | None = None
    goal_contribution: GoalContribution | None = None
    goal_allocation: GoalPercentageAllocation | None = None

    needs_clarification: bool = False
    clarification_question: str | None = None

    intent: Literal[
        "transactions",
        "create_goal",
        "contribute_goal",
        "none",
    ] = "none"

    goal_name: str | None = None
    target_amount: Decimal | None = None
    deadline: str | None = None
    contribution_amount: Decimal | None = None
