"""The four questions every app is asked. Readme.md section 10.

They are asked before anything is built, because they are the only input the
derivation has that cannot be read off the schema. The wording is the builder's
wording; the values are the ones `buildjob.derive` accepts, and the two are
kept side by side here so a rename in either is impossible to do by halves.
"""

from __future__ import annotations

from dataclasses import dataclass

from buildjob.derive import AUDIENCES, SIZES, VISIBILITIES


@dataclass(frozen=True)
class BaseQuestion:
    id: str
    text: str
    options: tuple[tuple[str, str], ...]


BASE_QUESTIONS: tuple[BaseQuestion, ...] = (
    BaseQuestion(
        "audience",
        "Who will use this app?",
        (("me", "Just me"), ("team", "My team"), ("customers", "My customers")),
    ),
    BaseQuestion(
        "size",
        "About how many people?",
        (("small", "1-20"), ("medium", "20-200"), ("large", "200+")),
    ),
    BaseQuestion(
        "visibility",
        "Same data for everyone, or each person only their own?",
        (
            ("everyone", "Everyone"),
            ("own_data", "Own only"),
            ("per_table", "Choose per table"),
        ),
    ),
    BaseQuestion(
        "sensitive",
        "Is any of this data private or sensitive?",
        (("yes", "Yes"), ("no", "No")),
    ),
)

# If these ever drift apart, a builder could pick something the derivation will
# reject, and they would find out only after a build. Fail at import instead.
_EXPECTED = {"audience": AUDIENCES, "size": SIZES, "visibility": VISIBILITIES}
for _question in BASE_QUESTIONS:
    _allowed = _EXPECTED.get(_question.id)
    if _allowed is not None:
        _offered = tuple(value for value, _ in _question.options)
        if set(_offered) != set(_allowed):
            raise RuntimeError(
                f"the {_question.id} question offers {_offered} but"
                f" buildjob.derive accepts {_allowed}"
            )
