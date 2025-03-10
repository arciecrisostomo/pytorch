import functools

import torch
from torch._inductor.compile_fx import fake_tensor_prop
from ..._dynamo.utils import counters

from .. import config
from ..pattern_matcher import (
    _return_true,
    inference_graph,
    init_once_fakemode,
    PatternMatcherPass,
    register_graph_pattern,
    register_replacement,
    stable_topological_sort,
)

aten = torch.ops.aten

# First pass_patterns[0] are applied, then [1], then [2]
pass_patterns = [
    PatternMatcherPass(),
    PatternMatcherPass(),
    PatternMatcherPass(),
]

binary_folding_pass = PatternMatcherPass()


def freezing_passes(gm: torch.fx.GraphModule, aot_example_inputs):
    """
    Passes that are applied to the graph to freeze pass.
    """

    from ..freezing import constant_fold

    lazy_init()
    # We need a few rounds of binary folding to get rid of all the
    # unnecessary nodes, but may need a good method to chose the rounds number.
    # works like: conv+binary+binary.
    binary_folding = counters["inductor"]["binary_folding"]
    for _ in range(4):
        constant_fold(gm)
        # Make sure meta['val'] is properly set for all nodes
        fake_tensor_prop(gm, aot_example_inputs, True)
        binary_folding_pass.apply(gm.graph)
        # If we don't have binary folding, we don't need to run the pass again.
        # TODO: remove the need to run fake_tensor_prop on the whole model.
        if counters["inductor"]["binary_folding"] == binary_folding:
            break
        binary_folding = counters["inductor"]["binary_folding"]

    constant_fold(gm)
    for pattern in pass_patterns:
        pattern.apply(gm.graph)

    # The CPU weight packing always assume the conv's weight is channels last,
    # So make sure the layout_optimization is on when doing it.
    if (
        torch._C._has_mkldnn
        and config.cpp.weight_prepack
        and config.layout_optimization
    ):
        from .mkldnn_fusion import _eliminate_duplicate_packed_nodes

        _eliminate_duplicate_packed_nodes(gm)

    stable_topological_sort(gm.graph)
    gm.recompile()
    gm.graph.lint()


@init_once_fakemode
def lazy_init():
    if torch._C._has_mkldnn and config.cpp.weight_prepack:
        from .mkldnn_fusion import _mkldnn_weight_pack_init

        _mkldnn_weight_pack_init()

    from .binary_folding import binary_folding_init

    addmm_patterns_init()
    binary_folding_init()


def register_freezing_graph_pattern(pattern, extra_check=_return_true, pass_number=0):
    return register_graph_pattern(
        pattern,
        extra_check=extra_check,
        pass_dict=pass_patterns[pass_number],
    )


def register_binary_folding_pattern(pattern, extra_check=_return_true):
    return register_graph_pattern(
        pattern,
        extra_check=extra_check,
        pass_dict=binary_folding_pass,
    )


@functools.lru_cache(None)
def addmm_patterns_init():
    if torch.cuda.is_available():
        # workaround https://github.com/pytorch/pytorch/issues/97894
        device = "cuda"
    else:
        device = "cpu"
    val = functools.partial(torch.empty, (10, 10), device=device, requires_grad=False)

    def check_concat_weights(match):
        weights = [
            match.kwargs["w1"],
            match.kwargs["w2"],
            match.kwargs["w3"],
        ]
        return all(
            w.op == "get_attr" and w.meta["val"].shape == weights[0].meta["val"].shape
            for w in weights
        )

    def matmul_fuse_pattern(inp, w1, w2, w3):
        return (inp @ w1, inp @ w2, inp @ w3)

    def matmul_replacement(inp, w1, w2, w3):
        cat_t = torch.cat((w1, w2, w3), dim=1)
        mm = inp @ cat_t
        return mm.chunk(3, dim=1)

    register_replacement(
        matmul_fuse_pattern,
        matmul_replacement,
        [val(), val(), val(), val()],
        inference_graph,
        pass_patterns[0],
        extra_check=check_concat_weights,
        exclusive_arg_names=("w1", "w2", "w3"),
    )

    def addmm_fuse_pattern_second(inp, w1, w2, w3, b1, b2, b3):
        return (
            aten.addmm(b1, inp, w1),
            aten.addmm(b2, inp, w2),
            aten.addmm(b3, inp, w3),
        )

    def addmm_fuse_replacement_second(inp, w1, w2, w3, b1, b2, b3):
        cat_w = torch.cat((w1, w2, w3), dim=1)
        cat_b = torch.cat((b1, b2, b3))
        return aten.addmm(cat_b, inp, cat_w).chunk(3, dim=1)

    register_replacement(
        addmm_fuse_pattern_second,
        addmm_fuse_replacement_second,
        [val() for _ in range(7)],
        inference_graph,
        pass_patterns[0],
        extra_check=check_concat_weights,
        exclusive_arg_names=("w1", "w2", "w3", "b1", "b2", "b3"),
    )
