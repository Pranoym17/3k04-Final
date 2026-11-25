"""Transition Table module.

Responsibility: represent a state machine as a complete table with one cell
per (state, event), as taught in Week 7 ("every cell says something").
Secret: how a table is stored and looked up. Modules declare their machine as
rows of cells and call `lookup`; a blank cell is rejected when the table is
built, so a missing transition cannot reach a running system.

Cell kinds:
    Move(next, action)  change to `next` and run the named action
    Stay(action)        stay in the state, run the named action (may be empty)
    Ignore(reason)      do nothing, record that the event was ignored (A8, FR-HIS-05)
    Refuse(reason)      do nothing, tell the caller it was refused, record it
    Illegal()           cannot happen; reaching it is a defect (fail safe)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Mapping, Tuple, Union


@dataclass(frozen=True)
class Move:
    """A table cell that changes state and runs an optional action."""
    next: Enum
    action: str = ""


@dataclass(frozen=True)
class Stay:
    """A table cell that retains state and runs an optional action."""
    action: str = ""


@dataclass(frozen=True)
class Ignore:
    """A table cell that records an ignored event without changing state."""
    reason: str = ""


@dataclass(frozen=True)
class Refuse:
    """A table cell that records and returns a refusal."""
    reason: str = ""


@dataclass(frozen=True)
class Illegal:
    """A table cell that must never be reached during valid operation."""
    pass


Cell = Union[Move, Stay, Ignore, Refuse, Illegal]
ILLEGAL = Illegal()


class TransitionTable:
    """Store a complete mapping from state and event pairs to cells."""
    def __init__(self, name: str, states: Iterable[Enum], events: Iterable[Enum],
                 rows: Mapping[Enum, Mapping[Enum, Cell]]):
        """Validate and store the complete state/event table."""
        self.name = name
        self.states: Tuple[Enum, ...] = tuple(states)
        self.events: Tuple[Enum, ...] = tuple(events)
        self._cells: Dict[Tuple[Enum, Enum], Cell] = {}
        for s in self.states:
            row = rows.get(s)
            if row is None:
                raise ValueError(f"{name}: no row for state {s}")
            for e in self.events:
                if e not in row or row[e] is None:
                    raise ValueError(f"{name}: blank cell ({s.name}, {e.name})")
                cell = row[e]
                if not isinstance(cell, (Move, Stay, Ignore, Refuse, Illegal)):
                    raise ValueError(f"{name}: invalid cell ({s.name}, {e.name})")
                self._cells[(s, e)] = ILLEGAL if isinstance(cell, Illegal) else cell
            extra = set(row) - set(self.events)
            if extra:
                raise ValueError(f"{name}: unknown events in row {s.name}: {extra}")

    def lookup(self, state: Enum, event: Enum) -> Cell:
        """Return the cell for the supplied state and event."""
        return self._cells.get((state, event), ILLEGAL)

    def cells(self):
        """All cells, in row order (used by the table-driven tests and by the
        document generator)."""
        for s in self.states:
            for e in self.events:
                yield s, e, self._cells[(s, e)]
