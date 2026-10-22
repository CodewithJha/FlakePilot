"""Store ciphertext only. An unconfigured sealer never calls the inner store.

The composition root does not construct this. Local files stay plaintext until
a key manager exists outside the execution plane.
"""

from flakepilot.integrations.application.ports import ArtifactRef, ArtifactStore
from flakepilot.integrations.application.sealing import ArtifactSealer

__all__ = ["SealedArtifactStore"]


class SealedArtifactStore:
    def __init__(self, inner: ArtifactStore, sealer: ArtifactSealer) -> None:
        self._inner = inner
        self._sealer = sealer

    async def put(
        self, *, owner: str, org_id: str, repo_id: str, data: bytes
    ) -> ArtifactRef:
        sealed = self._sealer.seal(data)
        return await self._inner.put(
            owner=owner, org_id=org_id, repo_id=repo_id, data=sealed
        )

    async def get(self, ref: ArtifactRef) -> bytes:
        blob = await self._inner.get(ref)
        return self._sealer.open(blob)
