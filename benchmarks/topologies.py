"""
Pure topology builders.

Each builder returns a :class:`~benchmarks.models.Topology` — an ordered tuple of
:class:`~benchmarks.models.NodeSpec` (parents always precede children) plus the shape
params that produced it. No API calls happen here; the executor turns a Topology into
real plugin instances.
"""

from __future__ import annotations

from .models import NodeSpec, PluginRole, Topology
from .units import to_dbg_params


def _root(file_count: int, file_size: str, key: str = "root") -> NodeSpec:
    return NodeSpec(key=key, role=PluginRole.FS,
                    params=to_dbg_params(file_count, file_size), parents=())


def _ds(key: str, parent: str, sleep_length: float) -> NodeSpec:
    params: dict = {"prefix": f"{key}_"}
    if sleep_length:
        # simpledsapp does time.sleep(int(value)): "60.0" crashes it, so emit an
        # integer string for integral values
        value = int(sleep_length) if float(sleep_length).is_integer() else sleep_length
        params["sleepLength"] = str(value)
    return NodeSpec(key=key, role=PluginRole.DS, params=params, parents=(parent,))


def _ts(key: str, parents: tuple[str, ...]) -> NodeSpec:
    # groupByInstance gives each parent its own subdir in the merge output — the usual
    # way the plugin is run, and it keeps same-named files from sibling branches from
    # colliding. CUBE zips 'filter' regexes positionally with 'plugininstances' and
    # copies everything for parents beyond the regex list, so one '.*' covers all
    # parents; the previous instance must be among the listed parents.
    return NodeSpec(key=key, role=PluginRole.TS,
                    params={"filter": ".*", "groupByInstance": True}, parents=parents)


def build_linear(depth: int, file_count: int, file_size: str,
                 sleep_length: float = 0) -> Topology:
    """
    root -> ds -> ds -> ... (``depth`` downstream ds nodes).
    """
    if depth < 1:
        raise ValueError("linear depth must be >= 1")
    
    nodes = [_root(file_count, file_size)]
    parent = "root"

    for i in range(depth):
        key = f"ds{i}"
        nodes.append(_ds(key, parent, sleep_length))
        parent = key

    return Topology(kind="linear", nodes=tuple(nodes),
                    params={"depth": depth, "file_count": file_count,
                            "file_size": file_size, "sleep_length": sleep_length})


def build_fanout_fanin(branches: int, file_count: int, file_size: str,
                       sleep_length: float = 0, merges: int = 1) -> Topology:
    """
    root -> [ds x branches] -> [ts x merges] (each ts merges all branches).
    """
    if branches < 1:
        raise ValueError("branches must be >= 1")
    
    if merges < 1:
        raise ValueError("merges must be >= 1")
    
    nodes = [_root(file_count, file_size)]
    branch_keys = []

    for b in range(branches):
        key = f"b{b}"
        nodes.append(_ds(key, "root", sleep_length))
        branch_keys.append(key)

    for m in range(merges):
        nodes.append(_ts(f"merge{m}", tuple(branch_keys)))

    return Topology(kind="fanout_fanin", nodes=tuple(nodes),
                    params={"branches": branches, "merges": merges,
                            "file_count": file_count, "file_size": file_size,
                            "sleep_length": sleep_length})


def build_layered_diamond(branches: int, layers: int, file_count: int, file_size: str,
                          sleep_length: float = 0) -> Topology:
    """
    root -> (fan-out ``branches`` -> merge) repeated ``layers`` times.
    """
    if branches < 1:
        raise ValueError("branches must be >= 1")
    
    if layers < 1:
        raise ValueError("layers must be >= 1")
    
    nodes = [_root(file_count, file_size)]
    layer_parent = "root"

    for layer in range(layers):
        branch_keys = []

        for b in range(branches):
            key = f"l{layer}b{b}"
            nodes.append(_ds(key, layer_parent, sleep_length))
            branch_keys.append(key)

        merge_key = f"merge{layer}"
        nodes.append(_ts(merge_key, tuple(branch_keys)))
        layer_parent = merge_key

    return Topology(kind="diamond", nodes=tuple(nodes),
                    params={"branches": branches, "layers": layers,
                            "file_count": file_count, "file_size": file_size,
                            "sleep_length": sleep_length})


def max_width(topology: Topology) -> int:
    """
    The most nodes of one topology that can run at once: the largest number at one depth
    (the longest path from the root). Every node of a depth here shares the parents of the
    others, so no two depths overlap and this is the exact bound.
    """
    depth: dict[str, int] = {}

    for node in topology.nodes:      # parents precede children
        depth[node.key] = 1 + max((depth[p] for p in node.parents), default=-1)

    widths: dict[int, int] = {}

    for d in depth.values():
        widths[d] = widths.get(d, 0) + 1
    return max(widths.values(), default=0)


def build(kind: str, params) -> Topology:
    """
    Dispatch to a builder by topology kind using a ``ScenarioParams``-like object.
    """
    if kind == "linear":
        return build_linear(params.depth, params.file_count, params.file_size,
                             params.sleep_length)
    if kind == "fanout_fanin":
        return build_fanout_fanin(params.branches, params.file_count, params.file_size,
                                  params.sleep_length, params.merges)
    if kind == "diamond":
        return build_layered_diamond(params.branches, params.layers, params.file_count,
                                     params.file_size, params.sleep_length)
    
    raise ValueError(f"unknown topology kind: {kind!r}")
