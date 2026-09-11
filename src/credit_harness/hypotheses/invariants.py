from .models import HypothesisStatus


class HypothesisInvariantError(ValueError):
    pass


class HypothesisGraphInvariantValidator:
    """Reject invalid projections; never repair or propagate hypothesis states."""
    def validate(self, graph) -> None:
        definitions = {d.hypothesis_id: d for d in graph.definitions}
        states = {s.hypothesis_id: s for s in graph.hypotheses}
        if (len(definitions) != len(graph.definitions) or len(states) != len(graph.hypotheses)
                or definitions.keys() != states.keys()):
            raise HypothesisInvariantError("graph definitions/states must be unique and complete")
        for definition in definitions.values():
            parent = definition.parent_hypothesis_id
            if parent is None:
                continue
            if parent not in states:
                raise HypothesisInvariantError("parent missing from graph")
            if (states[definition.hypothesis_id].status == HypothesisStatus.CONFIRMED
                    and states[parent].status != HypothesisStatus.CONFIRMED):
                raise HypothesisInvariantError("confirmed child requires confirmed parent contract")
            seen = {definition.hypothesis_id}
            while parent is not None:
                if parent in seen or parent not in definitions:
                    raise HypothesisInvariantError("invalid parent hierarchy")
                seen.add(parent)
                parent = definitions[parent].parent_hypothesis_id
