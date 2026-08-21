"""Unit propagation for the PubGrub resolver.

When an incompatibility has all but one term satisfied, the
remaining term's negation is derived (unit rule).  This module
owns that loop plus the per-term/per-incompatibility evaluators
that classify each term as SATISFIED, CONTRADICTED, or UNDETERMINED.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#unit-propagation
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any

from .types import IncompatibilityState, SetRelation, Term

if TYPE_CHECKING:
    from .resolver import Resolver
    from .types import Incompatibility

__all__ = [
    "classify_relation",
    "evaluate_incompatibility",
    "term_relation",
    "unit_propagation",
]

# Upper bound on relation_cache size; cleared on overflow to bound memory.
RELATION_CACHE_MAX = 100_000


def unit_propagation(
    resolver: Resolver[Any, Any], changed_package: Any
) -> Incompatibility[Any, Any] | None:
    """Propagate constraints from incompatibilities.

    When an incompatibility has all but one term satisfied, the
    remaining term's negation is derived (unit rule). Returns a
    conflicting incompatibility if all terms are satisfied, or
    None if propagation completes without conflict.

    The per-term relation check is inlined here rather than calling
    ``term_relation`` per term: this loop is the resolver's inner loop
    during backtracking, and on a deep backtrack it evaluates tens of
    thousands of terms, each of which would otherwise pay two function
    calls and re-fetch the solution, the relation cache and the
    incompatibility tables from the resolver.

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#unit-propagation
    """
    solution = resolver.solution
    solution_get = solution.get
    has_positive_constraint = solution.has_positive_constraint
    derive = solution.derive
    cache = resolver.relation_cache
    incompatibilities = resolver.incompatibilities
    related_by_package = resolver.package_to_incompatibilities
    stats = resolver.stats
    observer = resolver.observer
    satisfied = SetRelation.SATISFIED
    contradicted = SetRelation.CONTRADICTED

    propagation_queue: deque[Any] = deque([changed_package])
    in_queue: set[Any] = {changed_package}

    while propagation_queue:
        package = propagation_queue.popleft()
        in_queue.discard(package)

        for incompatibility_index in related_by_package.get(package, ()):
            incompatibility = incompatibilities[incompatibility_index]

            # evaluate_incompatibility, inlined.
            undetermined_term = None
            conflict = True
            for term in incompatibility.terms:
                # term_relation, inlined.
                assignment = solution_get(term.package)
                if assignment is None:
                    relation = None
                else:
                    positive = term._positive  # noqa: SLF001
                    key = (positive, assignment, term.constraint)
                    relation = cache.get(key)
                    if relation is None:
                        range_relation = assignment.relation(term.constraint)
                        relation = classify_relation(
                            term,
                            subset=range_relation.is_subset,
                            disjoint=range_relation.is_disjoint,
                        )
                        if len(cache) >= RELATION_CACHE_MAX:
                            cache.clear()
                        cache[key] = relation
                    if (
                        (positive and relation is satisfied)
                        or (not positive and relation is contradicted)
                    ) and not has_positive_constraint(term.package):
                        relation = None
                if relation is satisfied:
                    continue
                if relation is contradicted or undetermined_term is not None:
                    conflict = False
                    undetermined_term = None
                    break
                undetermined_term = term

            if undetermined_term is None:
                if conflict:
                    return incompatibility
                continue

            # Derive the negation of the one undetermined term.
            negated_package = undetermined_term.package
            negated_positive = not undetermined_term._positive  # noqa: SLF001
            range_before = solution_get(negated_package)
            derive(
                negated_package,
                undetermined_term.constraint,
                positive=negated_positive,
                cause=incompatibility,
            )
            range_after = solution_get(negated_package)
            if range_before != range_after:
                stats.derivations += 1
                observer.on_derivation(
                    negated_package,
                    positive=negated_positive,
                    cause=incompatibility,
                )
                if negated_package not in in_queue:
                    propagation_queue.append(negated_package)
                    in_queue.add(negated_package)

    return None


def evaluate_incompatibility(
    resolver: Resolver[Any, Any], incompatibility: Incompatibility[Any, Any]
) -> IncompatibilityState | Term[Any, Any] | None:
    """Evaluate an incompatibility against the current partial solution.

    Returns:
      ``IncompatibilityState.CONFLICT``: all terms satisfied
      ``Term``: exactly one undetermined term (unit propagation candidate)
      ``None``: 0 or 2+ undetermined terms (nothing to do yet)

    ``unit_propagation`` inlines this; it stays as the reference form and
    for callers outside the hot loop.
    """
    undetermined_term: Term[Any, Any] | None = None

    for term in incompatibility.terms:
        relation = term_relation(resolver, term)
        if relation is SetRelation.SATISFIED:
            continue
        if relation is SetRelation.CONTRADICTED:
            return None
        if undetermined_term is not None:
            return None
        undetermined_term = term

    if undetermined_term is not None:
        return undetermined_term
    return IncompatibilityState.CONFLICT


def term_relation(resolver: Resolver[Any, Any], term: Term[Any, Any]) -> SetRelation:
    """Check how the partial solution relates to this term.

    A term can only be satisfied or contradicted when the package has a
    positive constraint; without one it might not participate in the
    solution (negative terms are trivially true for absent packages).

    See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#term
    """
    assignment = resolver.solution.get(term.package)
    if assignment is None:
        return SetRelation.UNDETERMINED

    positive = term._positive  # noqa: SLF001
    cache = resolver.relation_cache
    key = (positive, assignment, term.constraint)
    result = cache.get(key)
    if result is None:
        relation = assignment.relation(term.constraint)
        result = classify_relation(
            term, subset=relation.is_subset, disjoint=relation.is_disjoint
        )
        if len(cache) >= RELATION_CACHE_MAX:
            cache.clear()
        cache[key] = result

    needs_positive = (positive and result is SetRelation.SATISFIED) or (
        not positive and result is SetRelation.CONTRADICTED
    )
    if needs_positive and not resolver.solution.has_positive_constraint(term.package):
        return SetRelation.UNDETERMINED
    return result


def classify_relation(
    term: Term[Any, Any],
    *,
    subset: bool,
    disjoint: bool,
) -> SetRelation:
    """Classify a term from the assignment's relation to its constraint.

    ``subset`` and ``disjoint`` describe the assignment against
    ``term.constraint``, as returned by ``RangeProtocol.relation``.

    Positive term: satisfied when the assignment is a subset of the
    constraint; contradicted when the two are disjoint.
    Negative term: the two swap, since a negative term forbids the
    constraint's versions.
    """
    if term.is_positive():
        satisfied, contradicted = subset, disjoint
    else:
        satisfied, contradicted = disjoint, subset

    if satisfied:
        return SetRelation.SATISFIED
    if contradicted:
        return SetRelation.CONTRADICTED
    return SetRelation.UNDETERMINED
