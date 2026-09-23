"""Turns a schema graph plus the builder's answers into an access model.

Readme.md sections 10 and 11, producing contract 7.2. Runs in the build job,
after `introspect` and before policies are written.

Two rules shape everything here:

  - **Deterministic.** No LLM decides anything (section 3 rule 7). Same graph
    and same answers in, same JSON out, including the order of every list.
  - **Never guess silently** (section 3 rule 5). Anything ambiguous comes back
    as a `Question` and blocks the deploy until it is answered; anything we do
    not support comes back as a `Refusal` and blocks it outright. A derivation
    with either is not a model, and `model` stays `None`.

Two places where the Readme is ambiguous and the choice made here is the
cautious one, both flagged in the code below: sensitive apps default unlinked
tables to `admin_only` rather than asking, and a nullable owner column is stated
in the confirmation text instead of being a yes/no question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# Section 11 step 1. Order matters only for the message, not the decision.
PRINCIPAL_NAMES = ("users", "user", "accounts", "profiles", "members")

# Section 11 step 4 and section 9.3: an EXISTS chain longer than this is both
# unreadable in a confirmation screen and too slow to run per row.
MAX_PATH_LENGTH = 4

# kernel/render.py accepts exactly these as a principal key type.
SUPPORTED_KEY_TYPES = ("uuid", "text", "bigint", "integer")

AUDIENCES = ("me", "team", "customers")
SIZES = ("small", "medium", "large")
VISIBILITIES = ("everyone", "own_data", "per_table")

UNLINKED_OPTIONS = ("shared", "read_only_shared", "admin_only")

MODEL_VERSION = 1


@dataclass(frozen=True)
class Question:
    """Something the builder must answer. No answer blocks launch."""

    id: str
    kind: str
    table: str | None
    text: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class Refusal:
    """Something we will not deploy, whatever the builder answers."""

    table: str | None
    reason: str


@dataclass(frozen=True)
class Answers:
    """Section 10. The four always-asked questions plus any follow-ups.

    `follow_ups` is keyed by `Question.id` so answers can be reused across runs
    and only new or changed tables produce new questions.
    """

    audience: str
    size: str
    visibility: str
    sensitive: bool
    follow_ups: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, allowed, name in (
            (self.audience, AUDIENCES, "audience"),
            (self.size, SIZES, "size"),
            (self.visibility, VISIBILITIES, "visibility"),
        ):
            if value not in allowed:
                raise ValueError(f"{name} must be one of {allowed}, got {value!r}")

    def as_json(self) -> dict[str, Any]:
        return {
            "audience": self.audience,
            "size": self.size,
            "visibility": self.visibility,
            "sensitive": self.sensitive,
            **dict(sorted(self.follow_ups.items())),
        }


@dataclass(frozen=True)
class Derivation:
    questions: tuple[Question, ...]
    refusals: tuple[Refusal, ...]
    explanation: tuple[str, ...]
    model: dict[str, Any] | None

    @property
    def is_complete(self) -> bool:
        return self.model is not None


# --------------------------------------------------------------------------
# The foreign key graph
# --------------------------------------------------------------------------


def _hop(table: str, fk: Mapping[str, Any]) -> dict[str, Any]:
    """One child -> parent step.

    Contract 7.2 spells a hop `column` / `to_column`. That only describes a
    single-column key, so a composite hop is spelled `columns` / `to_columns`
    instead of, not as well as, the singular form: a reader that only knows the
    contract shape then fails loudly on a composite key rather than quietly
    joining on the first column and handing out other people's rows.
    """
    hop = {
        "from": table,
        "to": fk["references"],
    }
    if len(fk["columns"]) == 1:
        hop["column"] = fk["columns"][0]
        hop["to_column"] = fk["referenced_columns"][0]
    else:
        hop["columns"] = list(fk["columns"])
        hop["to_columns"] = list(fk["referenced_columns"])
    return hop


def _hop_columns(hop: Mapping[str, Any]) -> list[str]:
    return [hop["column"]] if "column" in hop else list(hop["columns"])


def _path_key(path: Sequence[Mapping[str, Any]]) -> str:
    """Stable, readable name for a path, used as a follow-up answer value."""
    return ".".join("+".join(_hop_columns(hop)) for hop in path)


def paths_to_principal(
    graph: Mapping[str, Any], table: str, principal: str
) -> list[list[dict[str, Any]]]:
    """Every simple child -> parent path from `table` to `principal`.

    Self-references are ignored (section 11 step 2) and a path stops the moment
    it reaches the principal, so the principal never appears mid-path.
    """
    tables = graph["tables"]
    found: list[list[dict[str, Any]]] = []

    def walk(current: str, visited: frozenset[str], path: list[dict[str, Any]]) -> None:
        if len(path) >= MAX_PATH_LENGTH:
            return
        for fk in tables[current]["foreign_keys"]:
            parent = fk["references"]
            if parent == current or parent not in tables or parent in visited:
                continue
            step = path + [_hop(current, fk)]
            if parent == principal:
                found.append(step)
            else:
                walk(parent, visited | {parent}, step)

    walk(table, frozenset({table}), [])
    found.sort(key=lambda p: (len(p), _path_key(p)))
    return found


def is_membership_table(
    graph: Mapping[str, Any], table: str, principal: str
) -> bool:
    """A junction whose primary key is exactly its foreign keys.

    `project_members(project_id, account_id)` is the shape: rows in it decide
    who may reach a project. Section 11 step 4 refuses these outright.
    """
    entry = graph["tables"][table]
    primary_key = entry["primary_key"]
    if len(primary_key) < 2 or len(entry["foreign_keys"]) < 2:
        return False
    covered: set[str] = set()
    for fk in entry["foreign_keys"]:
        covered.update(fk["columns"])
    if covered != set(primary_key):
        return False
    return any(fk["references"] == principal for fk in entry["foreign_keys"])


# --------------------------------------------------------------------------
# Plain English (section 11 step 5: one sentence per table)
# --------------------------------------------------------------------------


def _render_hop(hop: Mapping[str, Any]) -> tuple[str, str]:
    columns = _hop_columns(hop)
    if len(columns) == 1:
        return f"{hop['from']}.{columns[0]}", f"{hop['to']}.{hop['to_column']}"
    return (
        f"{hop['from']}.({', '.join(columns)})",
        f"{hop['to']}.({', '.join(hop['to_columns'])})",
    )


def _explain_chain(table: str, path: Sequence[Mapping[str, Any]]) -> str:
    steps = []
    for index, hop in enumerate(path):
        child, parent = _render_hop(hop)
        last = index == len(path) - 1
        steps.append(f"`{child}` is them" if last else f"`{child}` leads to `{parent}`")
    if len(steps) == 1:
        body = steps[0]
    else:
        body = ", ".join(steps[:-1]) + ", and " + steps[-1]
    return f"Someone can see and change a row in `{table}` when {body}."


def explain(table: str, entry: Mapping[str, Any], principal: str) -> str:
    template = entry["template"]
    if template == "owner_column":
        if table == principal:
            line = f"Someone can see and change only their own row in `{table}`."
        else:
            line = (
                f"Someone can see and change a row in `{table}` when "
                f"`{table}.{entry['column']}` is them."
            )
    elif template == "fk_chain":
        line = _explain_chain(table, entry["path"])
    elif template == "shared":
        line = f"Everyone logged in can see and change every row in `{table}`."
    elif template == "read_only_shared":
        line = (
            f"Everyone logged in can read `{table}`. Only admins can change it."
        )
    else:
        line = f"Only admins can see or change `{table}`."

    if entry.get("admin_only_rows"):
        line += (
            f" Rows where `{table}.{entry['admin_only_rows']}` is empty are"
            " admin-only."
        )
    return line


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------


def _principal_candidates(graph: Mapping[str, Any]) -> list[str]:
    return [
        name
        for name in PRINCIPAL_NAMES
        if name in graph["tables"] and len(graph["tables"][name]["primary_key"]) == 1
    ]


def _single_key_tables(graph: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        sorted(
            name
            for name, entry in graph["tables"].items()
            if len(entry["primary_key"]) == 1
        )
    )


def _principal_question(options: Sequence[str]) -> Question:
    return Question(
        id="principal_table",
        kind="principal_table",
        table=None,
        text="Which table holds the people who log in?",
        options=tuple(options),
    )


def _owner_question(table: str, paths: Sequence[Sequence[Mapping[str, Any]]]) -> Question:
    options = tuple(_path_key(path) for path in paths)
    if all(len(path) == 1 for path in paths):
        choices = " or ".join(f"`{option}`" for option in options)
        text = f"Who owns a row in `{table}`: {choices}?"
    else:
        text = f"Which link decides who can see a row in `{table}`?"
    return Question(
        id=f"owner.{table}", kind="owner_path", table=table, text=text, options=options
    )


def _unlinked_question(table: str) -> Question:
    return Question(
        id=f"unlinked.{table}",
        kind="unlinked_table",
        table=table,
        text=(
            f"`{table}` isn't linked to any user. Everyone logged in, everyone"
            " reads but admins change, or admins only?"
        ),
        options=UNLINKED_OPTIONS,
    )


def _key_type(graph: Mapping[str, Any], table: str, column: str) -> str | None:
    for entry in graph["tables"][table]["columns"]:
        if entry["name"] == column:
            return entry["type"]
    return None


def _nullable(graph: Mapping[str, Any], table: str, column: str) -> bool:
    for entry in graph["tables"][table]["columns"]:
        if entry["name"] == column:
            return entry["nullable"]
    return False


def _first_nullable_column(
    graph: Mapping[str, Any], path: Sequence[Mapping[str, Any]]
) -> str | None:
    """Section 11 step 4: a nullable link means those rows are admin-only.

    Only the first hop can leave rows of *this* table unreachable, so that is
    the one named in the confirmation text.
    """
    hop = path[0]
    for column in _hop_columns(hop):
        if _nullable(graph, hop["from"], column):
            return column
    return None


def _failure(
    questions: Sequence[Question],
    refusals: Sequence[Refusal],
    explanation: Sequence[str],
) -> Derivation:
    return Derivation(
        questions=tuple(questions),
        refusals=tuple(refusals),
        explanation=tuple(explanation),
        model=None,
    )


def derive(
    graph: Mapping[str, Any],
    answers: Answers,
    *,
    app_id: str,
    schema_hash: str,
) -> Derivation:
    """Section 11, in order. Returns a model only when nothing is unresolved."""
    table_names = sorted(graph["tables"])
    if not table_names:
        return _failure(
            (),
            (Refusal(None, "The app has no tables, so there is nothing to protect."),),
            (),
        )

    principal = _resolve_principal(graph, answers)
    if isinstance(principal, (Question, Refusal)):
        explanation = tuple(
            f"We still need an answer about `{name}` before anything can be"
            " deployed."
            for name in table_names
        )
        if isinstance(principal, Question):
            return _failure((principal,), (), explanation)
        return _failure((), (principal,), explanation)

    key = graph["tables"][principal]["primary_key"][0]
    key_type = _key_type(graph, principal, key)
    if key_type not in SUPPORTED_KEY_TYPES:
        return _failure(
            (),
            (
                Refusal(
                    principal,
                    f"`{principal}.{key}` is `{key_type}`, which cannot be used"
                    " as a login key.",
                ),
            ),
            (),
        )

    questions: list[Question] = []
    refusals: list[Refusal] = []
    tables: dict[str, dict[str, Any]] = {}

    for table in table_names:
        entry, problem = _classify(graph, table, principal, key, answers)
        if entry is not None:
            tables[table] = entry
            continue
        if isinstance(problem, Question):
            questions.append(problem)
        else:
            refusals.append(problem)

    # Principal first, then alphabetical: the confirmation screen reads from
    # "who the people are" outwards.
    ordered = [principal] + [n for n in table_names if n != principal]
    refused = {refusal.table: refusal.reason for refusal in refusals}
    lines = []
    for name in ordered:
        if name in tables:
            lines.append(explain(name, tables[name], principal))
        elif name in refused:
            lines.append(refused[name])
        else:
            lines.append(
                f"We still need an answer about `{name}` before anything can"
                " be deployed."
            )
    explanation = tuple(lines)

    if questions or refusals:
        return _failure(questions, refusals, explanation)

    admin_enabled = any(
        entry["template"] in ("admin_only", "read_only_shared")
        or entry.get("admin_only_rows")
        for entry in tables.values()
    )

    model = {
        "version": MODEL_VERSION,
        "app_id": app_id,
        "principal": {"table": principal, "key": key, "key_type": key_type},
        "answers": answers.as_json(),
        "tables": tables,
        "admin_enabled": admin_enabled,
        "explanation": list(explanation),
        "confirmed_by": None,
        "confirmed_at": None,
        "schema_hash": schema_hash,
    }
    return Derivation(questions=(), refusals=(), explanation=explanation, model=model)


def _resolve_principal(
    graph: Mapping[str, Any], answers: Answers
) -> str | Question | Refusal:
    """Section 11 step 1. One candidate: propose. Otherwise ask, or refuse."""
    options = _single_key_tables(graph)
    if not options:
        return Refusal(
            None,
            "No table has a single-column primary key, so there is nothing to"
            " attach a login to.",
        )

    answered = answers.follow_ups.get("principal_table")
    if answered is not None:
        if answered not in options:
            return Refusal(
                None, f"`{answered}` cannot be the table of people who log in."
            )
        return answered

    candidates = _principal_candidates(graph)
    if len(candidates) == 1:
        return candidates[0]
    return _principal_question(options)


def _classify(
    graph: Mapping[str, Any],
    table: str,
    principal: str,
    key: str,
    answers: Answers,
) -> tuple[dict[str, Any] | None, Question | Refusal | None]:
    # Section 11 step 3: "everyone sees everything" short-circuits the rest.
    if answers.visibility == "everyone":
        return {"template": "shared"}, None

    if table == principal:
        return {"template": "owner_column", "column": key}, None

    if is_membership_table(graph, table, principal):
        return None, Refusal(
            table,
            f"`{table}` decides access through membership, which is not"
            " supported yet.",
        )

    paths = paths_to_principal(graph, table, principal)

    if not paths:
        return _unlinked(table, answers)

    if len(paths) > 1:
        chosen = answers.follow_ups.get(f"owner.{table}")
        by_key = {_path_key(path): path for path in paths}
        if chosen is None:
            return None, _owner_question(table, paths)
        if chosen not in by_key:
            return None, Refusal(
                table, f"`{chosen}` is not a way to reach `{principal}` from `{table}`."
            )
        path = by_key[chosen]
    else:
        path = paths[0]

    entry: dict[str, Any]
    if len(path) == 1 and "column" in path[0]:
        entry = {"template": "owner_column", "column": path[0]["column"]}
    else:
        entry = {"template": "fk_chain", "path": path}

    nullable = _first_nullable_column(graph, path)
    if nullable is not None:
        # Stated in the confirmation rather than asked: the policy expression
        # already yields nothing for a NULL owner, so there is no alternative
        # answer to offer. Section 11 step 4, section 25 "NULL owners".
        entry["admin_only_rows"] = nullable
    return entry, None


def _unlinked(
    table: str, answers: Answers
) -> tuple[dict[str, Any] | None, Question | Refusal | None]:
    answered = answers.follow_ups.get(f"unlinked.{table}")
    if answered is not None:
        if answered not in UNLINKED_OPTIONS:
            return None, Refusal(
                table, f"`{answered}` is not one of {', '.join(UNLINKED_OPTIONS)}."
            )
        return {"template": answered}, None
    if answers.sensitive:
        # Section 10: "If Q4 is Yes, unclear tables default to admin_only."
        # Taken as "decide, closed" rather than "ask with admin_only
        # preselected": admin_only is the one choice that cannot leak, so
        # picking it is not the silent guess rule 5 forbids.
        return {"template": "admin_only"}, None
    return None, _unlinked_question(table)
