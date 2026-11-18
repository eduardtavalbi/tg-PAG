from datetime import datetime

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    String,
    Integer,
    ForeignKey,
    ForeignKeyConstraint,
    DateTime,
    Numeric,
    CheckConstraint,
    UniqueConstraint,
    Index,
    event,
)

from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    async_sessionmaker,
    create_async_engine,
)


DATABASE_URL = "sqlite+aiosqlite:///db.sqlite3"

engine = create_async_engine(
    url=DATABASE_URL,
    echo=False,
)

async_session = async_sessionmaker(
    engine,
    expire_on_commit=False,
)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)

    tg_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )



class ProcessedConfirmation(Base):
    __tablename__ = "processed_confirmations"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "confirmation_id",
            name="uq_processed_confirmation_user_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    confirmation_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )



class Category(Base):
    __tablename__ = "categories"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "name",
            "type",
            name="uq_category_user_name_type",
        ),
        UniqueConstraint(
            "id",
            "user_id",
            name="uq_category_id_user_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(50))
    type: Mapped[str] = mapped_column(String(10))

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )


class Transaction(Base):
    __tablename__ = "transactions"

    __table_args__ = (
        CheckConstraint(
            "type IN ('income', 'expense')",
            name="check_transaction_type",
        ),
        CheckConstraint(
            "amount > 0 AND amount <= 9999999999.99",
            name="check_transaction_amount",
        ),
        Index(
            "ix_transactions_user_date",
            "user_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    type: Mapped[str] = mapped_column(String(10))

    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        default="CNY",
    )

    description: Mapped[str] = mapped_column(String(100))

    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )



class Goal(Base):
    __tablename__ = "goals"

    __table_args__ = (
        CheckConstraint(
            "target_amount > 0 AND target_amount <= 9999999999.99",
            name="check_goal_target_amount",
        ),
        CheckConstraint(
            "status IN ('active', 'completed', 'deleted')",
            name="check_goal_status",
        ),
        UniqueConstraint(
            "id",
            "user_id",
            name="uq_goal_id_user_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    target_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        default="CNY",
        nullable=False,
    )

    deadline: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        nullable=False,
    )


class GoalContribution(Base):
    __tablename__ = "goal_contributions"

    __table_args__ = (
        CheckConstraint(
            "amount > 0 AND amount <= 9999999999.99",
            name="check_contribution_amount",
        ),
        ForeignKeyConstraint(
            ["goal_id", "user_id"],
            ["goals.id", "goals.user_id"],
            ondelete="CASCADE",
            name="fk_contribution_goal_user",
        ),
        Index(
            "ix_goal_contributions_goal_user",
            "goal_id",
            "user_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )

    goal_id: Mapped[int] = mapped_column(
        nullable=False,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        nullable=False,
    )

class BudgetWarningNotification(Base):
    __tablename__ = "budget_warning_notifications"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "category_name",
            "currency",
            "period_key",
            name="uq_budget_warning_user_category_period",
        ),
        CheckConstraint(
            "severity IN ('moderate', 'high')",
            name="ck_budget_warning_severity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    category_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    period_key: Mapped[str] = mapped_column(
        String(7),
        nullable=False,
    )

    severity: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    sent_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )



class Budget(Base):
    __tablename__ = "budgets"

    __table_args__ = (
        CheckConstraint(
            "amount > 0 AND amount <= 9999999999.99",
            name="check_budget_amount",
        ),
        UniqueConstraint(
            "id",
            "user_id",
            name="uq_budget_id_user_id",
        ),
        ForeignKeyConstraint(
            ["category_id", "user_id"],
            ["categories.id", "categories.user_id"],
            ondelete="CASCADE",
            name="fk_budget_category_user",
        ),
        Index(
            "ix_budgets_user",
            "user_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    category_id: Mapped[int] = mapped_column(
        nullable=False,
    )

    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        default="CNY",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )


async def async_main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def async_close():
    await engine.dispose()



class BudgetLimitNotification(Base):
    __tablename__ = "budget_limit_notifications"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "budget_id",
            "period_key",
            "threshold_percent",
            name="uq_budget_limit_notification",
        ),
        CheckConstraint(
            "threshold_percent IN (70, 90, 100)",
            name="ck_budget_limit_threshold",
        ),
        Index(
            "ix_budget_limit_notifications_user",
            "user_id",
        ),
        ForeignKeyConstraint(
            ["budget_id", "user_id"],
            ["budgets.id", "budgets.user_id"],
            ondelete="CASCADE",
            name="fk_budget_limit_notification_budget_user",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    budget_id: Mapped[int] = mapped_column(
        nullable=False,
    )

    period_key: Mapped[str] = mapped_column(
        String(7),
        nullable=False,
    )

    threshold_percent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    sent_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        nullable=False,
    )


@event.listens_for(engine.sync_engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()