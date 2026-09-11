"""Final model boundary validation, independent of assembler selection paths."""
from pydantic import ValidationError

from .models import ReasoningContextSnapshot


class ContextEnvelopeInvariantError(ValueError):
    pass


class ContextEnvelopeInvariantValidator:
    def validate(self, snapshot):
        if type(snapshot) is not ReasoningContextSnapshot:
            raise ContextEnvelopeInvariantError("expected a ReasoningContextSnapshot")
        try:
            # Reconstruct nested models from typed fields. This also rejects values
            # inserted with model_copy/model_construct, which bypass validation.
            # No serialized-text regex, blacklist, sanitization or rewriting.
            ReasoningContextSnapshot.model_validate(snapshot.model_dump())
        except ValidationError:
            raise ContextEnvelopeInvariantError("model-visible envelope violates typed contracts") from None
