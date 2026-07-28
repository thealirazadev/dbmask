"""Plan versus schema diff and strategy compatibility, as a list of findings.

This module is the whole of `check` and the preflight of every run, so a clean result
here is the promise that a run will get past its own preflight on the same schema.

Findings carry identifiers, constraint names and counts only. No cell value reaches a
finding message, and neither does the secret (docs/rules.md, value-hygiene rule).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from .config import ColumnRule, MaskPlan, template_placeholders
from .errors import DbmaskError, ErrorCode
from .introspect import ColumnInfo, SchemaModel, TableInfo
from .strategies import max_output_length

SEVERITY_PII: Final = "pii-drift"
SEVERITY_DRIFT: Final = "drift"
SEVERITY_COMPAT: Final = "compat"

_SEVERITY_ORDER: Final = {SEVERITY_PII: 0, SEVERITY_DRIFT: 1, SEVERITY_COMPAT: 2}

MODE_MASK: Final = "mask"
MODE_PUMP: Final = "pump"
MODE_ALL: Final = "all"


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str
    subject: str
    message: str

    def format(self, severity_width: int = 0, subject_width: int = 0) -> str:
        prefix = f"finding[{self.severity}]".ljust(len("finding[]") + severity_width + 2)
        return f"{prefix}{self.subject.ljust(subject_width)}  {self.message}"


def format_findings(findings: Sequence[Finding]) -> list[str]:
    """Aligned, greppable finding lines: `finding[SEVERITY]  subject  explanation`."""
    severity_width = max((len(f.severity) for f in findings), default=0)
    subject_width = max((len(f.subject) for f in findings), default=0)
    return [finding.format(severity_width, subject_width) for finding in findings]


def pii_count(findings: Sequence[Finding]) -> int:
    return sum(1 for finding in findings if finding.severity == SEVERITY_PII)


def check_database_name(plan: MaskPlan, database: str) -> None:
    """Refuse a database whose name fails the safety pattern, before anything is read."""
    if plan.database_name_pattern is None:
        raise DbmaskError(
            ErrorCode.E_CONFIG,
            "safety.database_name_pattern is required before masking; add it to "
            f"{plan.path.name}",
        )
    if re.search(plan.database_name_pattern, database) is None:
        raise DbmaskError(
            ErrorCode.E_SAFETY,
            f'database "{database}" does not match safety.database_name_pattern '
            f'"{plan.database_name_pattern}"',
        )


def find_findings(plan: MaskPlan, schema: SchemaModel, mode: str = MODE_ALL) -> list[Finding]:
    """Every drift and compatibility problem, PII findings first, then drift, then compat."""
    findings = [*_drift(plan, schema), *_compatibility(plan, schema, mode)]
    return sorted(findings, key=lambda f: (_SEVERITY_ORDER[f.severity], f.subject))


def _drift(plan: MaskPlan, schema: SchemaModel) -> Iterator[Finding]:
    for name, table in schema.tables.items():
        configured = plan.tables.get(name)
        if configured is None:
            yield Finding(
                SEVERITY_DRIFT,
                name,
                f"table not in config; {len(table.columns)} columns need a strategy "
                "(dbmask init drafts them)",
            )
            # PII columns stay individually visible even when the whole table is missing:
            # deny-by-default must not hide behind one table-level line.
            for column in table.columns.values():
                if column.pii is not None:
                    yield _unconfigured(column)
            continue
        for column_name, column in table.columns.items():
            if column_name not in configured:
                yield _unconfigured(column)

    for name, columns in plan.tables.items():
        table_info = schema.tables.get(name)
        if table_info is None:
            yield Finding(SEVERITY_DRIFT, name, "configured but not in schema; remove it")
            continue
        for column_name in columns:
            if column_name not in table_info.columns:
                yield Finding(
                    SEVERITY_DRIFT,
                    f"{name}.{column_name}",
                    "configured but not in schema; remove it",
                )


def _unconfigured(column: ColumnInfo) -> Finding:
    if column.pii is not None:
        return Finding(
            SEVERITY_PII,
            column.ref,
            f'not in config; name matches pattern "{column.pii}"; strategy required',
        )
    return Finding(SEVERITY_DRIFT, column.ref, "not in config; add a strategy or explicit keep")


def _compatibility(plan: MaskPlan, schema: SchemaModel, mode: str) -> Iterator[Finding]:
    for name, columns in plan.tables.items():
        table = schema.tables.get(name)
        if table is None:
            continue
        masked = [
            column
            for column, rule in columns.items()
            if rule.strategy != "keep" and column in table.columns
        ]
        if mode in (MODE_MASK, MODE_ALL) and masked and not table.primary_key:
            yield Finding(
                SEVERITY_COMPAT,
                name,
                f"{len(masked)} masked columns but the table has no primary key; "
                "in-place mask batches by primary key",
            )
        for column_name, rule in columns.items():
            column = table.columns.get(column_name)
            if column is not None:
                yield from _column_findings(rule, column, table)
        yield from _template_findings(columns, table)
    if mode in (MODE_PUMP, MODE_ALL):
        yield from _cycle_findings(schema)


def _column_findings(rule: ColumnRule, column: ColumnInfo, table: TableInfo) -> Iterator[Finding]:
    key = rule.strategy
    if key != "keep":
        # Key stability is an invariant, not a preference: masked keys would break
        # referential integrity, and key translation is backlogged for v2.
        if column.name in table.primary_key:
            yield _compat(column, f'primary key column must use "keep" in v1, not "{key}"')
            return
        if column.name in table.foreign_key_columns:
            yield _compat(column, f'foreign key column must use "keep" in v1, not "{key}"')
            return
    if rule.strategy == "keep":
        return
    if rule.strategy == "null":
        if not column.nullable:
            yield _compat(column, 'strategy "null" but the column is NOT NULL')
        return

    if not column.is_text:
        yield _compat(
            column,
            f'strategy "{rule.strategy}" generates text but the column type is '
            f"{column.type_name}",
        )
    budget = max_output_length(rule)
    if column.length is not None and budget is not None and budget > column.length:
        yield _compat(
            column,
            f'strategy "{rule.strategy}" may generate up to {budget} characters but the '
            f"column holds {column.length}",
        )
    for index in table.unique_indexes_covering(column.name):
        if not rule.unique:
            yield _compat(
                column,
                f"unique index {index.name} covers this masked column but unique = true "
                "is not set",
            )


def _template_findings(columns: dict[str, ColumnRule], table: TableInfo) -> Iterator[Finding]:
    """A template may only read primary key or `keep` columns, so it cannot re-embed PII."""
    for name, rule in columns.items():
        if rule.strategy != "template" or rule.template is None:
            continue
        subject = f"{table.name}.{name}"
        for placeholder in template_placeholders(rule.template):
            if placeholder in table.primary_key:
                continue
            referenced = columns.get(placeholder)
            if placeholder not in table.columns:
                yield Finding(
                    SEVERITY_COMPAT,
                    subject,
                    f'template references "{placeholder}", which is not in the schema',
                )
            elif referenced is None or referenced.strategy != "keep":
                yield Finding(
                    SEVERITY_COMPAT,
                    subject,
                    f'template references "{placeholder}", which is not a primary key or '
                    "keep column",
                )


def _cycle_findings(schema: SchemaModel) -> Iterator[Finding]:
    graph = {
        name: sorted(
            {
                key.referred_table
                for key in table.foreign_keys
                if key.referred_table in schema.tables
            }
        )
        for name, table in schema.tables.items()
    }
    cycle = _find_cycle(graph)
    if cycle is not None:
        yield Finding(
            SEVERITY_COMPAT,
            cycle[0],
            "foreign key cycle " + " -> ".join(cycle) + "; pump cannot order these tables",
        )


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """One cycle in the FK graph, as a table path that returns to its start."""
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for neighbour in graph.get(node, []):
            if state.get(neighbour) == 1:
                return [*path[path.index(neighbour) :], neighbour]
            if state.get(neighbour, 0) == 0:
                found = visit(neighbour)
                if found is not None:
                    return found
        path.pop()
        state[node] = 2
        return None

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            found = visit(node)
            if found is not None:
                return found
    return None


def _compat(column: ColumnInfo, message: str) -> Finding:
    return Finding(SEVERITY_COMPAT, column.ref, message)
