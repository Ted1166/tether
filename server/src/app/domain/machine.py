from collections.abc import Mapping
from enum import StrEnum

from app.domain.errors import IllegalTransition


class StateMachine[S: StrEnum]:
    def __init__(self, name: str, transitions: Mapping[S, frozenset[S]]) -> None:
        self._name = name
        self._transitions = dict(transitions)

    @property
    def name(self) -> str:
        return self._name

    @property
    def states(self) -> frozenset[S]:
        return frozenset(self._transitions)

    def next_states(self, state: S) -> frozenset[S]:
        return self._transitions.get(state, frozenset())

    def is_terminal(self, state: S) -> bool:
        return not self.next_states(state)

    def can_advance(self, current: S, target: S) -> bool:
        return current == target or target in self.next_states(current)

    def advance(self, current: S, target: S) -> S:
        if not self.can_advance(current, target):
            raise IllegalTransition(self._name, current.value, target.value)
        return target
