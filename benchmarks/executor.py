"""
Instantiate topologies in CUBE.

Turns a pure :class:`~benchmarks.models.Topology` into real plugin instances, deriving
``previous_id`` / ``plugininstances`` linkage from each node's parents once ids are
known. The whole DAG is created up front (CUBE accepts children before parents finish),
then the poller takes over. Concurrent feeds are built in parallel — that is what the
``feeds`` dimension stresses on the control plane.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from .chris_api import ChrisApi, ChrisApiError
from .models import FeedRef, InstanceRef, NodeSpec, PluginRole, Topology


@dataclass
class FeedBuild:
    """
    Result of building one feed's DAG (possibly partial on failure).
    """
    feed: Optional[FeedRef]
    instances: list[InstanceRef] = field(default_factory=list)
    error: Optional[str] = None


def _params_for(node: NodeSpec, key_to_id: dict[str, int]) -> dict:
    """
    Plugin params plus parent linkage for one node.
    """
    params = dict(node.params)

    if node.role is PluginRole.DS:
        params["previous_id"] = key_to_id[node.parents[0]]
    elif node.role is PluginRole.TS:
        parent_ids = [key_to_id[p] for p in node.parents]
        params["previous_id"] = parent_ids[0]                       # must be in the list
        params["plugininstances"] = ",".join(str(i) for i in parent_ids)
    return params


def build_feed(api: ChrisApi, topology: Topology,
               plugin_ids: dict[PluginRole, int]) -> FeedBuild:
    """
    Create every instance of one DAG in topological order.
    """
    key_to_id: dict[str, int] = {}
    instances: list[InstanceRef] = []
    feed_id: Optional[int] = None
    root_id: Optional[int] = None

    try:
        for node in topology.nodes:
            params = _params_for(node, key_to_id)
            inst = api.create_plugin_instance(plugin_ids[node.role], params)
            iid = int(inst["id"])

            if feed_id is None:
                feed_id = int(inst["feed_id"])
                root_id = iid

            key_to_id[node.key] = iid
            parent_ids = tuple(key_to_id[p] for p in node.parents)
            instances.append(InstanceRef(iid, feed_id, node.key, node.role, parent_ids))
    except (ChrisApiError, KeyError, ValueError) as exc:
        feed = (FeedRef(feed_id, root_id, tuple(i.instance_id for i in instances))
                if feed_id is not None else None)
        return FeedBuild(feed=feed, instances=instances, error=str(exc))

    return FeedBuild(
        feed=FeedRef(feed_id, root_id, tuple(i.instance_id for i in instances)),
        instances=instances,
    )


def build_feeds(api: ChrisApi, topology: Topology, n_feeds: int,
                plugin_ids: dict[PluginRole, int]) -> list[FeedBuild]:
    """
    Build ``n_feeds`` independent copies of the topology, concurrently when n>1.
    """
    if n_feeds <= 1:
        return [build_feed(api, topology, plugin_ids)]

    builds: list[FeedBuild] = []

    with ThreadPoolExecutor(max_workers=n_feeds) as pool:
        futures = [pool.submit(build_feed, api, topology, plugin_ids)
                   for _ in range(n_feeds)]
        
        for fut in as_completed(futures):
            builds.append(fut.result())
    return builds
