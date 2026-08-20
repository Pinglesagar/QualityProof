"""Runtime-neutral metadata convention for source-audited tests."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypeVar

from qualityproof.models import Provenance, TestMetadata

DecoratedT = TypeVar("DecoratedT", bound=Callable[..., object] | type[object])


def qualityproof(
    *,
    requirements: Iterable[str] = (),
    provenance: Iterable[Provenance | Mapping[str, Any]] = (),
) -> Callable[[DecoratedT], DecoratedT]:
    """Attach traceability metadata without changing test behavior.

    The same keyword convention can be used with
    ``@pytest.mark.qualityproof(requirements=[...], provenance=[...])``.
    """

    metadata = TestMetadata(
        requirement_ids=tuple(requirements),
        provenance=tuple(Provenance.model_validate(item) for item in provenance),
    )

    def decorate(target: DecoratedT) -> DecoratedT:
        setattr(target, "__qualityproof__", metadata)  # noqa: B010
        return target

    return decorate
