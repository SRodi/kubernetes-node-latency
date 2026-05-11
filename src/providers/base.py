"""ClusterProvider abstraction.

Each provider knows how to create / fetch credentials for / delete a cluster
in its target cloud, and which CNIProbe pairs with it by default. The runner
and collectors are otherwise cloud-agnostic.
"""
from __future__ import annotations

import dataclasses as dc
from pathlib import Path
from typing import Protocol

from ..cni.base import CNIProbe


@dc.dataclass
class ClusterHandle:
    name: str
    region: str
    provider: str
    kubeconfig: Path
    created: bool = False  # True if this run created the cluster (so we delete it)
    extra: dict = dc.field(default_factory=dict)


class ClusterProvider(Protocol):
    name: str

    def create(self, cfg) -> ClusterHandle: ...
    def get_credentials(self, h: ClusterHandle) -> Path: ...
    def delete(self, h: ClusterHandle) -> None: ...
    def node_autoprovision_hint(self) -> dict: ...
    def cni_probe(self) -> CNIProbe: ...
